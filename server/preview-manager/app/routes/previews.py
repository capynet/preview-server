"""Preview CRUD and action endpoints — multi-tenant org-scoped, cloud VM version."""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

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
    update_preview_vm,
    get_project_by_slug,
    compute_url_hash,
    update_last_accessed,
    list_user_organizations,
)
from app.auth.dependencies import require_org_role, get_current_user
from app.auth.models import OrgRole, UserWithContext, has_min_role
from app.auth import database as auth_db
from app.cloud import cloud_manager
from app.remote import RemoteExecutor
from app.deployment import VM_PREVIEW_DIR

logger = logging.getLogger(__name__)

# Org-scoped router for /api/orgs/{org}/projects/{project}/previews/...
router = APIRouter(prefix="/api/orgs/{org}/projects/{project}")

# Separate router for cross-org preview listing (used by websocket, superadmin)
global_router = APIRouter()


def _sanitize_branch_name(branch: str) -> str:
    """Sanitize a branch name for use in preview_name."""
    sanitized = branch.replace("/", "--")
    sanitized = re.sub(r"[^a-zA-Z0-9\-]", "", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    return sanitized


async def _resolve_project(user: UserWithContext, project_slug: str) -> dict:
    """Resolve a project from org context. Raises 404 if not found."""
    project = await get_project_by_slug(user.org.id, project_slug)
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{project_slug}' not found in organization '{user.org.slug}'",
        )
    return project


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

    org_slug = state.get("org_slug", "")
    project_slug = state.get("project_slug", state.get("project", ""))
    url_hash = state.get("url_hash", "")

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
                preview_domain = f"{url_hash}.mr.preview-mr.com"

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

                # Stack: Database — strip registry prefix (e.g. "91.99.157.66:5000/mysql:5.7" -> "mysql:5.7")
                db_image = (services.get("db") or {}).get("image", "")
                if db_image:
                    stack["Database"] = db_image.split("/")[-1]

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
        project_slug=project_slug,
        org_slug=org_slug,
        url_hash=url_hash,
        mr_id=state.get("mr_id"),
        mr_title=state.get("mr_title"),
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


@router.post("/previews/branch")
async def create_branch_preview(
    project: str,
    body: CreateBranchPreviewRequest,
    background_tasks: BackgroundTasks,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    """Create a preview from a branch (not tied to a MR)."""
    import httpx
    from app.routes.gitlab import _get_org_gitlab_token

    proj = await _resolve_project(user, project)
    project_id = proj["id"]
    project_slug = proj["slug"]
    org_slug = user.org.slug

    sanitized = _sanitize_branch_name(body.branch)
    if not sanitized:
        raise HTTPException(status_code=400, detail="Invalid branch name")

    preview_name = f"branch-{sanitized}"

    existing = await get_preview(project_id, preview_name)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Preview {preview_name} already exists for project {project_slug}"
        )

    gitlab_url, token = await _get_org_gitlab_token(user.org.id)
    project_path = proj.get("gitlab_project_path")
    if not project_path:
        raise HTTPException(status_code=404, detail=f"Project '{project_slug}' has no GitLab project path configured")
    encoded_path = project_path.replace("/", "%2F")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{gitlab_url}/api/v4/projects/{encoded_path}/repository/branches/{quote(body.branch, safe='')}",
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
    url_hash = compute_url_hash(org_slug, project_slug, preview_name)
    preview_url = f"https://{url_hash}.mr.preview-mr.com"
    preview_path = str(PreviewStateManager.get_preview_path(org_slug, project_slug, preview_name))

    await PreviewStateManager.save_state(
        project_id, preview_name,
        branch=body.branch,
        commit_sha=commit_sha,
        status="pending",
        url=preview_url,
        url_hash=url_hash,
        path=preview_path,
        auto_update=0,
    )

    from app.routes.webhooks import _clone_and_deploy
    background_tasks.add_task(
        _clone_and_deploy,
        user.org.id, org_slug, project_id, project_slug, project_path,
        preview_name, body.branch, commit_sha, user.email,
    )

    return {
        "success": True,
        "preview_name": preview_name,
        "branch": body.branch,
        "commit_sha": commit_sha,
        "url_hash": url_hash,
        "message": f"Creating preview {preview_name} from branch {body.branch}",
    }


# ---------------------------------------------------------------------------
# Preview CRUD
# ---------------------------------------------------------------------------


@router.get("/previews/{preview_name}", response_model=PreviewInfo)
async def get_preview_endpoint(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_org_role(OrgRole.viewer)),
):
    proj = await _resolve_project(user, project)
    state = await PreviewStateManager.load_state(proj["id"], preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")
    # Enrich state with org/project slugs
    state["org_slug"] = user.org.slug
    state["project_slug"] = proj["slug"]
    return _build_preview_info(state)


class UpdatePreviewRequest(BaseModel):
    auto_update: Optional[bool] = None
    pinned: Optional[bool] = None
    env_vars: Optional[dict[str, str]] = None


@router.patch("/previews/{preview_name}")
async def update_preview_endpoint(
    project: str,
    preview_name: str,
    body: UpdatePreviewRequest,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    proj = await _resolve_project(user, project)
    project_id = proj["id"]

    state = await PreviewStateManager.load_state(project_id, preview_name)
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
        await PreviewStateManager.save_state(project_id, preview_name, **updates)

    updated = await PreviewStateManager.load_state(project_id, preview_name)
    updated["org_slug"] = user.org.slug
    updated["project_slug"] = proj["slug"]
    result = _build_preview_info(updated)

    if body.env_vars is not None:
        return {**result.model_dump(), "needs_rebuild": True}

    return result


def _get_preview_status(preview: dict) -> str:
    """Determine preview status based on VM state and active deployments."""
    # If there's an active deployment running, the preview is building
    latest_status = preview.get("latest_deployment_status")
    if latest_status and latest_status == "running":
        return "building"

    if preview.get("vm_id"):
        return "running"
    elif preview.get("status") in ("creating", "pending"):
        return preview["status"]
    else:
        return preview.get("status", "unknown")


async def get_preview_list_base(
    include_docker_status: bool = True,
    org_id: Optional[int] = None,
) -> dict:
    """Core logic to list all previews with cloud VM status.

    This is an internal helper, not a route endpoint.
    """
    t_total = time.monotonic()

    rows = await get_all_previews(org_id=org_id)
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

        url_hash = row.get("url_hash", "")

        previews.append({
            "name": row["preview_name"],
            "project": row.get("project_slug", ""),
            "project_slug": row.get("project_slug", ""),
            "org_slug": row.get("org_slug", ""),
            "url_hash": url_hash,
            "mr_id": row.get("mr_id"),
            "mr_title": row.get("mr_title"),
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


async def delete_preview_internal(
    org_slug: str, project_slug: str, project_id: int, preview_name: str,
):
    """Core delete logic: destroy VM, remove from DB."""
    # Clear any in-flight deploy lock so a recreated preview won't be blocked
    from app.routes.webhooks import clear_deploy_lock
    clear_deploy_lock(project_slug, preview_name)

    preview = await get_preview(project_id, preview_name)

    # Destroy VM if exists
    if preview and preview.get("vm_id"):
        try:
            await cloud_manager.destroy_vm(preview["vm_id"])
            logger.info(f"Destroyed VM for {org_slug}/{project_slug}/{preview_name}")
        except Exception as e:
            logger.warning(f"Error destroying VM for {org_slug}/{project_slug}/{preview_name}: {e}")

    # Remove Caddy direct route
    from app.caddy_api import caddy_manager
    url_hash = preview.get("url_hash", "") if preview else compute_url_hash(org_slug, project_slug, preview_name)
    domain = f"{url_hash}.mr.preview-mr.com"
    await caddy_manager.remove_preview_route(domain)

    # Delete from DB
    await PreviewStateManager.delete_state(project_id, preview_name)

    # Clean up local preview directory (compose files etc.)
    preview_path = PreviewStateManager.get_preview_path(org_slug, project_slug, preview_name)
    if preview_path.exists():
        import shutil
        shutil.rmtree(preview_path, ignore_errors=True)
        logger.info(f"Cleaned up local directory: {preview_path}")

    logger.info(f"Preview {org_slug}/{project_slug}/{preview_name} fully deleted")


@router.delete("/previews/{preview_name}")
async def delete_preview(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    proj = await _resolve_project(user, project)
    project_id = proj["id"]

    state = await PreviewStateManager.load_state(project_id, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")

    try:
        await delete_preview_internal(user.org.slug, proj["slug"], project_id, preview_name)
        return {
            "success": True,
            "message": f"Preview {project}/{preview_name} deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting preview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Global preview list (cross-org, for websocket / superadmin)
# ---------------------------------------------------------------------------


@global_router.get("/api/previews")
async def list_previews(
    status: bool = True,
    user: UserWithContext = Depends(get_current_user),
):
    """List all previews across orgs.

    Superadmin sees all previews. Regular users see previews for their orgs only.
    """
    if user.is_superadmin:
        result = await get_preview_list_base(include_docker_status=status)
    else:
        # Gather previews across all user's orgs
        user_orgs = await list_user_organizations(user.id)
        all_previews = []
        for org in user_orgs:
            org_result = await get_preview_list_base(include_docker_status=status, org_id=org["id"])
            all_previews.extend(org_result["previews"])
        result = {"previews": all_previews, "total": len(all_previews)}

    return result


# ---------------------------------------------------------------------------
# Org-scoped preview list
# ---------------------------------------------------------------------------


@router.get("/previews")
async def list_org_previews(
    project: str,
    status: bool = True,
    user: UserWithContext = Depends(require_org_role(OrgRole.viewer)),
):
    """List previews for a specific project in the org."""
    proj = await _resolve_project(user, project)
    project_id = proj["id"]

    # Get all previews for this org, then filter by project
    result = await get_preview_list_base(include_docker_status=status, org_id=user.org.id)
    result["previews"] = [p for p in result["previews"] if p["project"] == proj["slug"]]
    result["total"] = len(result["previews"])
    return result


# ---------------------------------------------------------------------------
# REST endpoints for CLI / actions
# ---------------------------------------------------------------------------


async def _get_executor(project_id: int, preview_name: str, project_slug: str) -> tuple[RemoteExecutor, dict]:
    """Get a RemoteExecutor for the preview's VM. Raises 503 if no VM."""
    preview = await get_preview(project_id, preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail=f"Preview {project_slug}/{preview_name} not found")
    if not preview.get("vm_id") or not preview.get("vm_ip"):
        raise HTTPException(status_code=503, detail="Preview VM is not running. Visit the preview URL to wake it up.")
    return RemoteExecutor(preview["vm_ip"]), preview


@router.post("/previews/{preview_name}/stop")
async def stop_preview(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    """Stop a preview (shutdown VM, keep disk intact)."""
    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found")

    if not preview.get("vm_id"):
        return {"success": True, "output": "Preview already stopped", "error": ""}

    try:
        await cloud_manager.shutdown_vm(preview["vm_id"])
        return {"success": True, "output": "VM shutdown", "error": ""}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)}


@router.post("/previews/{preview_name}/start")
async def start_preview(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    """Start a preview (power on shutdown VM)."""
    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found")

    if not preview.get("vm_id"):
        return {"success": False, "output": "", "error": "No VM found -- use rebuild to create a new one"}

    try:
        vm_ip = await cloud_manager.power_on_vm(preview["vm_id"])
        await cloud_manager.wait_for_vm_ready(preview["vm_id"], timeout=120)

        # Start containers
        executor = RemoteExecutor(vm_ip)
        proc = await executor.run_shell(
            f"cd {VM_PREVIEW_DIR}/code && docker compose up -d"
        )
        await proc.communicate()

        await update_preview_vm(preview["id"], preview["vm_id"], vm_ip)
        return {"success": True, "output": f"VM powered on (IP: {vm_ip})", "error": ""}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)}


@router.post("/previews/{preview_name}/restart")
async def restart_preview(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    """Restart containers on the VM."""
    proj = await _resolve_project(user, project)
    executor, preview = await _get_executor(proj["id"], preview_name, proj["slug"])
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


@router.post("/previews/{preview_name}/drush-uli")
async def drush_uli(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_org_role(OrgRole.viewer)),
):
    """Get a one-time login link (drush uli) via SSH."""
    proj = await _resolve_project(user, project)
    executor, preview = await _get_executor(proj["id"], preview_name, proj["slug"])

    url_hash = preview.get("url_hash", compute_url_hash(user.org.slug, proj["slug"], preview_name))
    preview_url = f"https://{url_hash}.mr.preview-mr.com"
    php_container = f"{preview_name}-{proj['slug']}-php"

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


@router.post("/previews/{preview_name}/drush")
async def drush_command(
    project: str,
    preview_name: str,
    request: Request,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    """Run an arbitrary drush command via SSH."""
    body = await request.json()
    args_str = body.get("args", "")
    if not args_str:
        raise HTTPException(status_code=400, detail="Missing 'args' in request body")

    proj = await _resolve_project(user, project)
    executor, preview = await _get_executor(proj["id"], preview_name, proj["slug"])
    php_container = f"{preview_name}-{proj['slug']}-php"

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


@router.post("/previews/{preview_name}/rebuild")
async def rebuild_preview(
    project: str,
    preview_name: str,
    background_tasks: BackgroundTasks,
    force_new: bool = False,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    """Re-clone the preview from GitLab (internal rebuild)."""
    proj = await _resolve_project(user, project)
    project_id = proj["id"]
    project_slug = proj["slug"]

    state = await PreviewStateManager.load_state(project_id, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail="Preview not found")
    if not state.get("branch"):
        raise HTTPException(status_code=400, detail="Cannot determine branch for this preview")

    project_path = proj.get("gitlab_project_path")
    if not project_path:
        raise HTTPException(status_code=400, detail=f"Project '{project_slug}' has no GitLab project path configured")

    from app.routes.webhooks import _clone_and_deploy

    background_tasks.add_task(
        _clone_and_deploy,
        user.org.id, user.org.slug, project_id, project_slug, project_path,
        preview_name, state["branch"], state.get("commit_sha", ""),
        "rebuild" if force_new else "update",
        state.get("mr_id"),
        force_new,
    )

    return {
        "success": True,
        "output": f"Rebuild started for {project_slug}/{preview_name} (branch: {state['branch']}, force_new={force_new})",
        "error": "",
    }


@router.get("/previews/{preview_name}/deployments")
async def list_preview_deployments(
    project: str,
    preview_name: str,
    limit: int = 50,
    user: UserWithContext = Depends(require_org_role(OrgRole.viewer)),
):
    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")
    deployments = await db_list_deployments(preview["id"], limit=limit)
    return {"deployments": deployments, "total": len(deployments)}


@router.get("/previews/{preview_name}/deployments/{deployment_id}")
async def get_preview_deployment(
    project: str,
    preview_name: str,
    deployment_id: int,
    user: UserWithContext = Depends(require_org_role(OrgRole.viewer)),
):
    deployment = await db_get_deployment(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")
    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview or deployment["preview_id"] != preview["id"]:
        raise HTTPException(status_code=404, detail="Deployment not found for this preview")
    return deployment


@router.get("/previews/{preview_name}/deployments/{deployment_id}/live-logs")
async def get_deployment_live_logs(
    project: str,
    preview_name: str,
    deployment_id: int,
    offset: int = 0,
    user: UserWithContext = Depends(require_org_role(OrgRole.viewer)),
):
    from app.websockets import deployment_log_broadcaster

    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
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


@router.get("/previews/{preview_name}/db/download")
async def download_db(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    """Stream a gzipped SQL dump from the preview VM."""
    proj = await _resolve_project(user, project)
    executor, preview = await _get_executor(proj["id"], preview_name, proj["slug"])
    php_container = f"{preview_name}-{proj['slug']}-php"

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

    filename = f"{proj['slug']}-{preview_name}.sql.gz"
    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/previews/{preview_name}/files/download")
async def download_files(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    """Stream a tar.gz of the preview's files directory from the VM."""
    proj = await _resolve_project(user, project)
    executor, preview = await _get_executor(proj["id"], preview_name, proj["slug"])

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

    filename = f"{proj['slug']}-{preview_name}-files.tar.gz"
    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/previews/{preview_name}/stats")
async def preview_vm_stats(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_org_role(OrgRole.viewer)),
):
    """Get CPU, RAM and disk stats from the preview's VM."""
    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found")
    if not preview.get("vm_id") or not preview.get("vm_ip"):
        return {"status": "unavailable", "reason": "VM not ready"}
    executor = RemoteExecutor(preview["vm_ip"])

    script = (
        "import json, os, time\n"
        "def read_cpu():\n"
        "    lines = open('/proc/stat').readlines()\n"
        "    c = lines[0].split()[1:]\n"
        "    return [int(x) for x in c]\n"
        "c1 = read_cpu()\n"
        "time.sleep(0.5)\n"
        "c2 = read_cpu()\n"
        "d = [b - a for a, b in zip(c1, c2)]\n"
        "total = sum(d)\n"
        "idle = d[3] + d[4]\n"
        "cpu_pct = round((total - idle) / total * 100, 1) if total > 0 else 0.0\n"
        "mem = open('/proc/meminfo').read()\n"
        "mt = int([l for l in mem.splitlines() if l.startswith('MemTotal')][0].split()[1]) * 1024\n"
        "ma = int([l for l in mem.splitlines() if l.startswith('MemAvailable')][0].split()[1]) * 1024\n"
        "st = os.statvfs('/')\n"
        "dt = st.f_blocks * st.f_frsize\n"
        "du = (st.f_blocks - st.f_bfree) * st.f_frsize\n"
        "ncpu = os.cpu_count()\n"
        "print(json.dumps({\n"
        "    'memory_total_gb': round(mt/1073741824, 2),\n"
        "    'memory_available_gb': round(ma/1073741824, 2),\n"
        "    'memory_percent': round((mt - ma) / mt * 100, 1),\n"
        "    'disk_total_gb': round(dt/1073741824, 2),\n"
        "    'disk_used_gb': round(du/1073741824, 2),\n"
        "    'disk_percent': round(du / dt * 100, 1),\n"
        "    'cpu_percent': cpu_pct,\n"
        "    'cpu_count': ncpu\n"
        "}))\n"
    )
    import base64
    encoded = base64.b64encode(script.encode()).decode()
    cmd = f"echo {encoded} | base64 -d | python3"
    try:
        proc = await executor.run_shell(cmd)
        stdout, stderr = await proc.communicate()
    except Exception:
        return {"status": "unavailable", "reason": "VM not reachable"}
    if proc.returncode != 0:
        return {"status": "unavailable", "reason": "VM not reachable"}

    try:
        data = json.loads(stdout.decode().strip())
        data["status"] = "ok"
        return data
    except Exception:
        return {"status": "unavailable", "reason": "Failed to parse stats"}
