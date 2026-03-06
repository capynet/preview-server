"""Preview CRUD and action endpoints — cloud VM version."""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from typing import Optional
from pydantic import BaseModel

from config.settings import settings
from app.models import PreviewInfo
from app.state import PreviewStateManager
from app.database import (
    get_all_previews, get_preview, delete_preview_from_db,
    list_deployments as db_list_deployments,
    get_deployment as db_get_deployment,
    update_preview_vm, update_preview_volume,
)
from app.auth.dependencies import require_role
from app.auth.models import Role, UserWithRole, has_min_role
from app.auth import database as auth_db
from app import config_store
from app.cloud import cloud_manager
from app.caddy_api import caddy_manager
from app.remote import RemoteExecutor

logger = logging.getLogger(__name__)

router = APIRouter()


def _sanitize_branch_name(branch: str) -> str:
    """Sanitize a branch name for use in preview_name."""
    sanitized = branch.replace("/", "--")
    sanitized = re.sub(r"[^a-zA-Z0-9\-]", "", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    return sanitized


def _build_preview_info(state: dict) -> PreviewInfo:
    """Build a PreviewInfo response from a DB row dict."""
    last_deployment = None
    if state.get("last_deployment_status"):
        last_deployment = {
            "commit_sha": state["commit_sha"],
            "status": state["last_deployment_status"],
            "completed_at": state.get("last_deployment_completed_at"),
        }
        if state.get("last_deployment_error"):
            last_deployment["error"] = state["last_deployment_error"]
        if state.get("last_deployment_duration") is not None:
            last_deployment["duration_seconds"] = state["last_deployment_duration"]

    env_vars = state.get("env_vars", "{}")
    if isinstance(env_vars, str):
        try:
            env_vars = json.loads(env_vars) if env_vars else {}
        except (json.JSONDecodeError, TypeError):
            env_vars = {}

    # Extract stack info from docker-compose.yml (local copy)
    exposed_services: dict[str, str] = {}
    stack: dict[str, str] = {}
    preview_path = state.get("path", "")
    if preview_path:
        compose_file = Path(preview_path) / "docker-compose.yml"
        if compose_file.exists():
            try:
                import yaml
                compose = yaml.safe_load(compose_file.read_text()) or {}
                services = compose.get("services", {})
                preview_domain = state["url"].replace("https://", "").replace("http://", "")

                # Exposed services (port mappings on non-php services)
                for svc_name, svc in services.items():
                    if svc_name == "php":
                        continue
                    if svc.get("ports"):
                        for port_map in svc["ports"]:
                            port = str(port_map).split(":")[0]
                            exposed_services[svc_name] = f"https://{svc_name}--{preview_domain}"

                # Stack: PHP version
                php_image = (services.get("php") or {}).get("image", "")
                if ":php" in php_image:
                    stack["PHP"] = php_image.split(":php")[-1]
                    stack["Webserver"] = "OpenLiteSpeed"

                # Stack: Database
                db_image = (services.get("db") or {}).get("image", "")
                if db_image:
                    stack["Database"] = db_image

                # Stack: Redis/Valkey
                redis_image = (services.get("redis") or {}).get("image", "")
                if redis_image:
                    if "valkey" in redis_image:
                        ver = redis_image.split(":")[-1].replace("-alpine", "") if ":" in redis_image else ""
                        stack["Valkey"] = ver or redis_image
                    else:
                        ver = redis_image.split(":")[-1].replace("-alpine", "") if ":" in redis_image else ""
                        stack["Redis"] = ver or redis_image

                # Stack: Solr
                solr_image = (services.get("solr") or {}).get("image", "")
                if solr_image and ":" in solr_image:
                    stack["Solr"] = solr_image.split(":")[-1]

                # Stack: LiteSpeed Cache
                php_env = (services.get("php") or {}).get("environment", {})
                if php_env.get("PREV_LITESPEED_CACHE") == "1":
                    stack["LSCache"] = "Enabled"
            except Exception:
                pass

    return PreviewInfo(
        preview_name=state["preview_name"],
        project=state["project"],
        mr_id=state.get("mr_id"),
        status=state["status"],
        url=state["url"],
        path=state["path"],
        branch=state["branch"],
        commit_sha=state["commit_sha"],
        created_at=state["created_at"],
        last_deployed_at=state.get("last_deployed_at"),
        last_deployment=last_deployment,
        auto_update=bool(state.get("auto_update", 1)),
        pinned=bool(state.get("pinned", 0)),
        env_vars=env_vars,
        exposed_services=exposed_services,
        stack=stack,
    )


# ---------------------------------------------------------------------------
# Branch preview creation
# ---------------------------------------------------------------------------


class CreateBranchPreviewRequest(BaseModel):
    branch: str


@router.post("/api/previews/{project}/branch")
async def create_branch_preview(
    project: str,
    body: CreateBranchPreviewRequest,
    background_tasks: BackgroundTasks,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    """Create a preview from a branch (not tied to a MR)."""
    import httpx
    from app.routes.gitlab import _get_gitlab_token

    enabled_ids = await config_store.load_enabled_project_ids()
    if not enabled_ids:
        raise HTTPException(status_code=400, detail="No projects are enabled")

    sanitized = _sanitize_branch_name(body.branch)
    if not sanitized:
        raise HTTPException(status_code=400, detail="Invalid branch name")

    preview_name = f"branch-{sanitized}"

    existing = await get_preview(project, preview_name)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Preview {preview_name} already exists for project {project}"
        )

    token = await _get_gitlab_token()
    project_path = await config_store.get_project_path_by_slug(project)
    if not project_path:
        raise HTTPException(status_code=404, detail=f"Project '{project}' not found in enabled projects")
    encoded_path = project_path.replace("/", "%2F")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.gitlab_url}/api/v4/projects/{encoded_path}/repository/branches/{body.branch}",
                headers={"PRIVATE-TOKEN": token},
                timeout=15,
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Branch '{body.branch}' not found")
            resp.raise_for_status()
            branch_data = resp.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching branch info: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e}")

    commit_sha = branch_data["commit"]["id"]

    await PreviewStateManager.save_state(
        project, preview_name,
        branch=body.branch,
        commit_sha=commit_sha,
        status="pending",
        url=f"https://{preview_name}-{project}.mr.preview-mr.com",
        path=str(Path(settings.previews_base_path) / project / preview_name),
        auto_update=0,
    )

    from app.routes.webhooks import _clone_and_deploy
    background_tasks.add_task(
        _clone_and_deploy,
        project_path,
        project,
        preview_name,
        body.branch,
        commit_sha,
        user.email,
    )

    return {
        "success": True,
        "preview_name": preview_name,
        "branch": body.branch,
        "commit_sha": commit_sha,
        "message": f"Creating preview {preview_name} from branch {body.branch}",
    }


# ---------------------------------------------------------------------------
# Preview CRUD
# ---------------------------------------------------------------------------


@router.get("/api/previews/{project}/{preview_name}", response_model=PreviewInfo)
async def get_preview_endpoint(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.viewer))):
    state = await PreviewStateManager.load_state(project, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")
    return _build_preview_info(state)


class UpdatePreviewRequest(BaseModel):
    auto_update: Optional[bool] = None
    pinned: Optional[bool] = None
    env_vars: Optional[dict[str, str]] = None


@router.patch("/api/previews/{project}/{preview_name}")
async def update_preview_endpoint(
    project: str,
    preview_name: str,
    body: UpdatePreviewRequest,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    state = await PreviewStateManager.load_state(project, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")

    updates = {}
    if body.auto_update is not None:
        updates["auto_update"] = int(body.auto_update)
    if body.pinned is not None:
        updates["pinned"] = int(body.pinned)
    if body.env_vars is not None:
        updates["env_vars"] = json.dumps(body.env_vars)

    if updates:
        await PreviewStateManager.save_state(project, preview_name, **updates)

    updated = await PreviewStateManager.load_state(project, preview_name)
    result = _build_preview_info(updated)

    if body.env_vars is not None:
        return {**result.model_dump(), "needs_rebuild": True}

    return result


def _get_preview_status(preview: dict) -> str:
    """Determine preview status based on VM state."""
    if preview.get("vm_id"):
        return "running"
    elif preview.get("volume_id"):
        return "stopped"
    elif preview.get("status") in ("creating", "pending"):
        return preview["status"]
    else:
        return preview.get("status", "unknown")


async def get_preview_list_base(include_docker_status: bool = True) -> dict:
    """Core logic to list all previews with cloud VM status."""
    t_total = time.monotonic()

    rows = await get_all_previews()
    t_db = time.monotonic()
    logger.info(f"[TIMING] DB query: {t_db - t_total:.3f}s ({len(rows)} previews found)")

    previews = []
    for row in rows:
        last_deployment = None
        latest_dep_id = row.get("latest_deployment_id")
        if latest_dep_id:
            latest_status = row.get("latest_deployment_status")
            if latest_status and row.get("latest_deployment_completed_at"):
                last_deployment = {
                    "id": latest_dep_id,
                    "status": latest_status,
                    "completed_at": row.get("latest_deployment_completed_at"),
                }
                if row.get("last_deployment_error"):
                    last_deployment["error"] = row["last_deployment_error"]
                if row.get("last_deployment_duration") is not None:
                    last_deployment["duration_seconds"] = row["last_deployment_duration"]
            else:
                last_deployment = {"id": latest_dep_id, "status": "running"}

        # Determine status from VM state
        status = _get_preview_status(row)

        previews.append({
            "name": row["preview_name"],
            "project": row["project"],
            "mr_id": row.get("mr_id"),
            "status": status,
            "url": row["url"],
            "branch": row["branch"],
            "commit_sha": row["commit_sha"],
            "last_deployed_at": row.get("last_deployed_at"),
            "last_deployment": last_deployment,
            "auto_update": bool(row.get("auto_update", 1)),
            "pinned": bool(row.get("pinned", 0)),
        })

    logger.info(f"[TIMING] get_preview_list_base TOTAL: {time.monotonic() - t_total:.3f}s")

    return {
        "previews": previews,
        "total": len(previews)
    }


async def delete_preview_internal(project: str, preview_name: str):
    """Core delete logic: destroy VM, delete volume, remove from DB."""
    preview = await get_preview(project, preview_name)

    # Destroy VM if running
    if preview and preview.get("vm_id"):
        try:
            await cloud_manager.destroy_vm(preview["vm_id"])
            logger.info(f"Destroyed VM for {project}/{preview_name}")
        except Exception as e:
            logger.warning(f"Error destroying VM for {project}/{preview_name}: {e}")

    # Remove Caddy routes
    try:
        await caddy_manager.remove_preview_routes(preview_name, project)
    except Exception as e:
        logger.warning(f"Error removing Caddy routes for {project}/{preview_name}: {e}")

    # Delete volume if exists
    if preview and preview.get("volume_id"):
        try:
            await cloud_manager.delete_volume(preview["volume_id"])
            logger.info(f"Deleted volume for {project}/{preview_name}")
        except Exception as e:
            logger.warning(f"Error deleting volume for {project}/{preview_name}: {e}")

    # Delete from DB
    await PreviewStateManager.delete_state(project, preview_name)

    # Clean up local preview directory (compose files etc.)
    preview_path = PreviewStateManager.get_preview_path(project, preview_name)
    if preview_path.exists():
        import shutil
        shutil.rmtree(preview_path, ignore_errors=True)
        logger.info(f"Cleaned up local directory: {preview_path}")

    logger.info(f"Preview {project}/{preview_name} fully deleted")


@router.delete("/api/previews/{project}/{preview_name}")
async def delete_preview(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    state = await PreviewStateManager.load_state(project, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")

    try:
        await delete_preview_internal(project, preview_name)
        return {
            "success": True,
            "message": f"Preview {project}/{preview_name} deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting preview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# REST endpoints for CLI
# ---------------------------------------------------------------------------


@router.get("/api/previews")
async def list_previews(status: bool = True, user: UserWithRole = Depends(require_role(Role.viewer))):
    result = await get_preview_list_base(include_docker_status=status)

    if not has_min_role(user.role, Role.admin):
        allowed_slugs = set(await auth_db.get_user_project_slugs(user.id))
        result["previews"] = [p for p in result["previews"] if p["project"] in allowed_slugs]
        result["total"] = len(result["previews"])

    return result


async def _get_executor(project: str, preview_name: str) -> tuple[RemoteExecutor, dict]:
    """Get a RemoteExecutor for the preview's VM. Raises 503 if no VM."""
    preview = await get_preview(project, preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")
    if not preview.get("vm_id") or not preview.get("vm_ip"):
        raise HTTPException(status_code=503, detail="Preview VM is not running. Visit the preview URL to wake it up.")
    return RemoteExecutor(preview["vm_ip"]), preview


@router.post("/api/previews/{project}/{preview_name}/stop")
async def stop_preview(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Stop a preview (destroy VM, keep volume)."""
    preview = await get_preview(project, preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found")

    if not preview.get("vm_id"):
        return {"success": True, "output": "Preview already stopped", "error": ""}

    try:
        await cloud_manager.destroy_vm(preview["vm_id"])
        await caddy_manager.remove_preview_routes(preview_name, project)
        await update_preview_vm(project, preview_name, None, None)
        return {"success": True, "output": "VM destroyed, volume kept", "error": ""}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)}


@router.post("/api/previews/{project}/{preview_name}/start")
async def start_preview(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Start a preview (create VM, attach volume)."""
    preview = await get_preview(project, preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found")

    if preview.get("vm_id"):
        return {"success": True, "output": "Preview already running", "error": ""}

    if not preview.get("volume_id"):
        return {"success": False, "output": "", "error": "No volume found"}

    try:
        vm_name = f"prev-{project}-{preview_name}"
        server = await cloud_manager.create_vm(vm_name, preview["volume_id"])
        vm_id = server.data_model.id
        vm_ip = server.data_model.public_net.ipv4.ip

        executor = RemoteExecutor(vm_ip)
        await executor.wait_for_ssh(timeout=120)

        # Mount volume and start containers
        setup_cmd = (
            "VOLDIR=$(ls -d /mnt/HC_Volume* 2>/dev/null | head -1) && "
            "ln -sfn \"$VOLDIR\" /var/www/preview && "
            "cd /var/www/preview/code && "
            "docker compose up -d"
        )
        proc = await executor.run_shell(setup_cmd)
        stdout, stderr = await proc.communicate()

        await update_preview_vm(project, preview_name, vm_id, vm_ip)
        await caddy_manager.add_preview_routes(preview_name, project, vm_ip)

        return {"success": True, "output": f"VM created (IP: {vm_ip})", "error": ""}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)}


@router.post("/api/previews/{project}/{preview_name}/restart")
async def restart_preview(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Restart containers on the VM."""
    executor, preview = await _get_executor(project, preview_name)
    try:
        proc = await executor.run_shell(
            "cd /var/www/preview/code && docker compose restart"
        )
        stdout, stderr = await proc.communicate()
        success = proc.returncode == 0
        return {
            "success": success,
            "output": stdout.decode() if isinstance(stdout, bytes) else stdout,
            "error": stderr.decode() if isinstance(stderr, bytes) else stderr if not success else "",
        }
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)}


@router.post("/api/previews/{project}/{preview_name}/drush-uli")
async def drush_uli(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.viewer))):
    """Get a one-time login link (drush uli) via SSH."""
    executor, preview = await _get_executor(project, preview_name)
    preview_url = f"https://{preview_name}-{project}.mr.preview-mr.com"
    php_container = f"{preview_name}-{project}-php"

    proc = await executor.run_shell(
        f"docker exec {php_container} vendor/bin/drush uli --uri={preview_url}"
    )
    stdout, stderr = await proc.communicate()
    success = proc.returncode == 0
    return {
        "success": success,
        "output": stdout.decode().strip() if isinstance(stdout, bytes) else stdout,
        "error": stderr.decode() if isinstance(stderr, bytes) else stderr if not success else "",
    }


@router.post("/api/previews/{project}/{preview_name}/drush")
async def drush_command(project: str, preview_name: str, request: Request, user: UserWithRole = Depends(require_role(Role.manager))):
    """Run an arbitrary drush command via SSH."""
    body = await request.json()
    args_str = body.get("args", "")
    if not args_str:
        raise HTTPException(status_code=400, detail="Missing 'args' in request body")

    executor, preview = await _get_executor(project, preview_name)
    php_container = f"{preview_name}-{project}-php"

    proc = await executor.run_shell(
        f"docker exec {php_container} vendor/bin/drush {args_str}"
    )
    stdout, stderr = await proc.communicate()
    success = proc.returncode == 0
    return {
        "success": success,
        "output": stdout.decode() if isinstance(stdout, bytes) else stdout,
        "error": stderr.decode() if isinstance(stderr, bytes) else stderr if not success else "",
    }


@router.post("/api/previews/{project}/{preview_name}/rebuild")
async def rebuild_preview(
    project: str,
    preview_name: str,
    background_tasks: BackgroundTasks,
    force_new: bool = False,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    """Re-clone the preview from GitLab (internal rebuild)."""
    state = await PreviewStateManager.load_state(project, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail="Preview not found")
    if not state.get("branch"):
        raise HTTPException(status_code=400, detail="Cannot determine branch for this preview")

    project_path = await config_store.get_project_path_by_slug(project)
    if not project_path:
        raise HTTPException(status_code=400, detail=f"Project '{project}' not found in enabled projects")

    from app.routes.webhooks import _clone_and_deploy

    background_tasks.add_task(
        _clone_and_deploy,
        project_path,
        project,
        preview_name,
        state["branch"],
        state.get("commit_sha", ""),
        "rebuild" if force_new else "update",
        state.get("mr_id"),
        force_new,
    )

    return {
        "success": True,
        "output": f"Rebuild started for {project}/{preview_name} (branch: {state['branch']}, force_new={force_new})",
        "error": "",
    }


@router.get("/api/previews/{project}/{preview_name}/deployments")
async def list_preview_deployments(
    project: str, preview_name: str,
    limit: int = 50,
    user: UserWithRole = Depends(require_role(Role.viewer)),
):
    preview = await get_preview(project, preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")
    deployments = await db_list_deployments(preview["id"], limit=limit)
    return {"deployments": deployments, "total": len(deployments)}


@router.get("/api/previews/{project}/{preview_name}/deployments/{deployment_id}")
async def get_preview_deployment(
    project: str, preview_name: str, deployment_id: int,
    user: UserWithRole = Depends(require_role(Role.viewer)),
):
    deployment = await db_get_deployment(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")
    preview = await get_preview(project, preview_name)
    if not preview or deployment["preview_id"] != preview["id"]:
        raise HTTPException(status_code=404, detail="Deployment not found for this preview")
    return deployment


@router.get("/api/previews/{project}/{preview_name}/deployments/{deployment_id}/live-logs")
async def get_deployment_live_logs(
    project: str, preview_name: str, deployment_id: int,
    offset: int = 0,
    user: UserWithRole = Depends(require_role(Role.viewer)),
):
    from app.websockets import deployment_log_broadcaster

    preview = await get_preview(project, preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found")

    entry = deployment_log_broadcaster.get(deployment_id)
    if entry:
        lines = entry["logs"][offset:]
        return {
            "lines": lines,
            "offset": offset + len(lines),
            "complete": entry["complete"],
            "status": "complete" if entry["complete"] else "running",
        }

    deployment = await db_get_deployment(deployment_id)
    if not deployment or deployment["preview_id"] != preview["id"]:
        raise HTTPException(status_code=404, detail="Deployment not found")

    log_output = deployment.get("log_output") or ""
    lines = [log_output] if offset == 0 and log_output else []
    return {
        "lines": lines,
        "offset": offset + len(lines),
        "complete": True,
        "status": deployment.get("status", "unknown"),
    }


@router.get("/api/previews/{project}/{preview_name}/db/download")
async def download_db(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Stream a gzipped SQL dump from the preview VM."""
    executor, preview = await _get_executor(project, preview_name)
    php_container = f"{preview_name}-{project}-php"

    async def generate():
        proc = await executor.run_shell(
            f"docker exec {php_container} vendor/bin/drush sql-dump | gzip"
        )
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
        await proc.wait()

    filename = f"{project}-{preview_name}.sql.gz"
    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/previews/{project}/{preview_name}/files/download")
async def download_files(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Stream a tar.gz of the preview's files directory from the VM."""
    executor, preview = await _get_executor(project, preview_name)

    tar_excludes = "--exclude=./css --exclude=./js --exclude=./php"
    files_dir = "/var/www/preview/code/web/sites/default/files"

    async def generate():
        proc = await executor.run_shell(
            f"tar czf - {tar_excludes} -C {files_dir} ."
        )
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
        await proc.wait()

    filename = f"{project}-{preview_name}-files.tar.gz"
    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
