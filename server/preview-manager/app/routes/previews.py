"""Preview CRUD and action endpoints"""

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
)
from app.auth.dependencies import require_role
from app.auth.models import Role, UserWithRole, has_min_role
from app.auth import database as auth_db
from app import config_store

logger = logging.getLogger(__name__)

router = APIRouter()


def _sanitize_branch_name(branch: str) -> str:
    """Sanitize a branch name for use in preview_name.

    Replaces / with --, removes non-alphanumeric chars except -.
    """
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
    )


# ---------------------------------------------------------------------------
# Branch preview creation (must be before {preview_name} routes)
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

    # Verify project is enabled
    enabled_ids = await config_store.load_enabled_project_ids()
    if not enabled_ids:
        raise HTTPException(status_code=400, detail="No projects are enabled")

    # Sanitize branch name
    sanitized = _sanitize_branch_name(body.branch)
    if not sanitized:
        raise HTTPException(status_code=400, detail="Invalid branch name")

    preview_name = f"branch-{sanitized}"

    # Check if preview already exists
    existing = await get_preview(project, preview_name)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Preview {preview_name} already exists for project {project}"
        )

    # Get the latest commit from the branch via GitLab API
    token = await _get_gitlab_token()
    project_path = f"{settings.gitlab_group_name}/{project}"
    encoded_path = project_path.replace("/", "%2F")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.gitlab_url}/api/v4/projects/{encoded_path}/repository/branches/{body.branch}",
                headers={"Authorization": f"Bearer {token}"},
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

    # Pre-create the preview record with auto_update=0 for branch previews
    await PreviewStateManager.save_state(
        project, preview_name,
        branch=body.branch,
        commit_sha=commit_sha,
        status="creating",
        url=f"https://{preview_name}-{project}.mr.preview-mr.com",
        path=str(Path(settings.previews_base_path) / project / preview_name),
        auto_update=0,
    )

    # Launch clone + deploy in background
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
# Preview CRUD (uses {preview_name} path param)
# ---------------------------------------------------------------------------


@router.get("/api/previews/{project}/{preview_name}", response_model=PreviewInfo)
async def get_preview_endpoint(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.viewer))):
    """Get preview information"""
    state = await PreviewStateManager.load_state(project, preview_name)

    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"Preview {project}/{preview_name} not found"
        )

    return _build_preview_info(state)


class UpdatePreviewRequest(BaseModel):
    auto_update: Optional[bool] = None


@router.patch("/api/previews/{project}/{preview_name}")
async def update_preview_endpoint(
    project: str,
    preview_name: str,
    body: UpdatePreviewRequest,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    """Update preview settings (e.g. auto_update)."""
    state = await PreviewStateManager.load_state(project, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")

    updates = {}
    if body.auto_update is not None:
        updates["auto_update"] = int(body.auto_update)

    if updates:
        await PreviewStateManager.save_state(project, preview_name, **updates)

    updated = await PreviewStateManager.load_state(project, preview_name)
    return _build_preview_info(updated)


async def get_docker_status(preview_path: Path) -> str:
    """Get container status via docker compose ps."""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "compose", "ps", "--format", "json",
            cwd=str(preview_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            logger.warning(f"Timeout checking Docker status for {preview_path}")
            return "unknown"

        if process.returncode != 0:
            return "stopped"

        stdout_str = stdout.decode().strip()
        if not stdout_str:
            return "stopped"

        # docker compose ps --format json outputs one JSON object per line
        all_running = True
        has_containers = False
        for line in stdout_str.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                container = json.loads(line)
                has_containers = True
                state = container.get("State", "").lower()
                if state != "running":
                    all_running = False
            except json.JSONDecodeError:
                continue

        if not has_containers:
            return "stopped"
        return "running" if all_running else "stopped"

    except Exception as e:
        logger.warning(f"Error checking Docker status for {preview_path}: {e}")
        return "unknown"


async def get_preview_list_base(include_docker_status: bool = True) -> dict:
    """
    Core logic to list all previews (query DB + optionally Docker status).

    Args:
        include_docker_status: If True, run docker compose ps for each preview.
                               If False, return previews with status from DB (fast).

    Returns:
        dict with "previews" list and "total" count
    """
    t_total = time.monotonic()

    rows = await get_all_previews()
    t_db = time.monotonic()
    logger.info(f"[TIMING] DB query: {t_db - t_total:.3f}s ({len(rows)} previews found)")

    previews = []
    for row in rows:
        last_deployment = None
        if row.get("last_deployment_status"):
            last_deployment = {
                "status": row["last_deployment_status"],
                "completed_at": row.get("last_deployment_completed_at"),
            }
            if row.get("last_deployment_error"):
                last_deployment["error"] = row["last_deployment_error"]
            if row.get("last_deployment_duration") is not None:
                last_deployment["duration_seconds"] = row["last_deployment_duration"]

        previews.append({
            "name": row["preview_name"],
            "project": row["project"],
            "mr_id": row.get("mr_id"),
            "status": row["status"] if not include_docker_status else "unknown",
            "url": row["url"],
            "branch": row["branch"],
            "commit_sha": row["commit_sha"],
            "last_deployed_at": row.get("last_deployed_at"),
            "last_deployment": last_deployment,
            "auto_update": bool(row.get("auto_update", 1)),
            "_path": row["path"],
        })

    async def update_preview_status(preview):
        if "_path" in preview:
            preview_path = Path(preview["_path"])
            if preview_path.exists() and (preview_path / "docker-compose.yml").exists():
                t_docker = time.monotonic()
                status = await get_docker_status(preview_path)
                logger.info(f"[TIMING] Docker status {preview['name']}: {time.monotonic() - t_docker:.3f}s -> {status}")
                preview["status"] = status
            elif not preview_path.exists():
                preview["status"] = "missing"
            else:
                preview["status"] = "stopped"

    if include_docker_status and previews:
        t_docker_all = time.monotonic()
        await asyncio.gather(*[update_preview_status(p) for p in previews])
        logger.info(f"[TIMING] Docker status (all {len(previews)} parallel): {time.monotonic() - t_docker_all:.3f}s")

    # Strip _path for external consumers
    for preview in previews:
        preview.pop("_path", None)

    logger.info(f"[TIMING] get_preview_list_base TOTAL: {time.monotonic() - t_total:.3f}s")

    return {
        "previews": previews,
        "total": len(previews)
    }


async def delete_preview_internal(project: str, preview_name: str):
    """Core delete logic: stop containers, remove state from DB, remove directory.

    Raises on failure. Used by the REST endpoint and the webhook handler.
    """
    preview_path = PreviewStateManager.get_preview_path(project, preview_name)

    # Stop and remove Docker containers
    if preview_path.exists() and (preview_path / "docker-compose.yml").exists():
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "compose", "down", "-v",
                cwd=str(preview_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=60)
            logger.info(f"Docker containers stopped for {project}/{preview_name}")
        except Exception as e:
            logger.warning(f"Error stopping Docker containers: {e}")

    # Delete from DB
    await PreviewStateManager.delete_state(project, preview_name)

    # Delete directory — files created by Docker (root-owned) can't be removed
    # by preview-user directly, so we use a throwaway container to rm -rf.
    if preview_path.exists():
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "run", "--rm",
                "-v", f"{preview_path}:/target",
                "alpine:3.20", "rm", "-rf", "/target",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=120)
        except Exception as e:
            logger.warning(f"Docker rm failed, falling back to shutil: {e}")
        # Clean up any remaining files or the empty mount point
        if preview_path.exists():
            import shutil
            shutil.rmtree(preview_path, ignore_errors=True)
        logger.info(f"Preview directory deleted: {preview_path}")
    else:
        logger.info(f"Preview {project}/{preview_name} directory already absent")


@router.delete("/api/previews/{project}/{preview_name}")
async def delete_preview(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Delete a preview (DANGEROUS - removes directory)"""
    preview_path = PreviewStateManager.get_preview_path(project, preview_name)
    state = await PreviewStateManager.load_state(project, preview_name)

    if not preview_path.exists() and not state:
        raise HTTPException(
            status_code=404,
            detail=f"Preview {project}/{preview_name} not found"
        )

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
    """
    List all previews (REST endpoint).

    Query params:
        status: If true (default), include Docker container status (slower).
    """
    result = await get_preview_list_base(include_docker_status=status)

    # Non-admin users only see previews for projects they are assigned to
    if not has_min_role(user.role, Role.admin):
        allowed_slugs = set(await auth_db.get_user_project_slugs(user.id))
        result["previews"] = [p for p in result["previews"] if p["project"] in allowed_slugs]
        result["total"] = len(result["previews"])

    return result


def _get_preview_dir(project: str, preview_name: str) -> Path:
    """Resolve preview directory or raise 404."""
    preview_path = PreviewStateManager.get_preview_path(project, preview_name)
    if not preview_path.exists():
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")
    return preview_path


async def _run_docker_command(
    command: list[str],
    cwd: Path,
    timeout: int = 120,
) -> dict:
    """Run a docker compose command and return {success, output, error}."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            return {"success": False, "output": "", "error": f"Timeout after {timeout}s"}

        stdout_str = stdout.decode()
        stderr_str = stderr.decode()
        success = process.returncode == 0

        return {
            "success": success,
            "output": stdout_str,
            "error": stderr_str if not success else "",
        }
    except Exception as e:
        logger.error(f"Error running command {command}: {e}", exc_info=True)
        return {"success": False, "output": "", "error": str(e)}


@router.post("/api/previews/{project}/{preview_name}/stop")
async def stop_preview(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Stop a preview (docker compose stop)."""
    preview_path = _get_preview_dir(project, preview_name)
    return await _run_docker_command(["docker", "compose", "stop"], preview_path, timeout=60)


@router.post("/api/previews/{project}/{preview_name}/start")
async def start_preview(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Start a preview (docker compose up -d)."""
    preview_path = _get_preview_dir(project, preview_name)
    return await _run_docker_command(["docker", "compose", "up", "-d"], preview_path, timeout=120)


@router.post("/api/previews/{project}/{preview_name}/restart")
async def restart_preview(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Restart a preview (docker compose restart)."""
    preview_path = _get_preview_dir(project, preview_name)
    return await _run_docker_command(["docker", "compose", "restart"], preview_path, timeout=120)


@router.post("/api/previews/{project}/{preview_name}/drush-uli")
async def drush_uli(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.viewer))):
    """Get a one-time login link (drush uli)."""
    preview_path = _get_preview_dir(project, preview_name)
    preview_url = f"https://{preview_name}-{project}.mr.preview-mr.com"
    php_container = f"{preview_name}-{project}-php"
    return await _run_docker_command(
        ["docker", "exec", php_container, "vendor/bin/drush", "uli", f"--uri={preview_url}"],
        preview_path,
        timeout=30,
    )


@router.post("/api/previews/{project}/{preview_name}/drush")
async def drush_command(project: str, preview_name: str, request: Request, user: UserWithRole = Depends(require_role(Role.manager))):
    """
    Run an arbitrary drush command.

    Body: {"args": "cr"} or {"args": "status"}
    """
    body = await request.json()
    args_str = body.get("args", "")
    if not args_str:
        raise HTTPException(status_code=400, detail="Missing 'args' in request body")

    preview_path = _get_preview_dir(project, preview_name)
    php_container = f"{preview_name}-{project}-php"
    command = ["docker", "exec", php_container, "vendor/bin/drush"] + args_str.split()
    return await _run_docker_command(command, preview_path, timeout=120)


@router.post("/api/previews/{project}/{preview_name}/rebuild")
async def rebuild_preview(
    project: str,
    preview_name: str,
    background_tasks: BackgroundTasks,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    """Re-clone the preview from GitLab (internal rebuild, no pipeline)."""
    _get_preview_dir(project, preview_name)

    state = await PreviewStateManager.load_state(project, preview_name)
    if not state or not state.get("branch"):
        raise HTTPException(status_code=400, detail="Cannot determine branch for this preview")

    project_path = f"{settings.gitlab_group_name}/{project}"

    from app.routes.webhooks import _clone_and_deploy

    background_tasks.add_task(
        _clone_and_deploy,
        project_path,
        project,
        preview_name,
        state["branch"],
        state.get("commit_sha", ""),
        "rebuild",
        state.get("mr_id"),
    )

    return {
        "success": True,
        "output": f"Rebuild started for {project}/{preview_name} (branch: {state['branch']})",
        "error": "",
    }


@router.get("/api/previews/{project}/{preview_name}/deployments")
async def list_preview_deployments(
    project: str, preview_name: str,
    limit: int = 50,
    user: UserWithRole = Depends(require_role(Role.viewer)),
):
    """List deployments for a preview (without log_output)."""
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
    """Get a single deployment with full log output."""
    deployment = await db_get_deployment(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")
    # Verify it belongs to the right preview
    preview = await get_preview(project, preview_name)
    if not preview or deployment["preview_id"] != preview["id"]:
        raise HTTPException(status_code=404, detail="Deployment not found for this preview")
    return deployment


@router.get("/api/previews/{project}/{preview_name}/db/download")
async def download_db(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Stream a gzipped SQL dump of the preview database."""
    preview_path = _get_preview_dir(project, preview_name)
    php_container = f"{preview_name}-{project}-php"

    async def generate():
        process = await asyncio.create_subprocess_exec(
            "docker", "exec", php_container, "vendor/bin/drush", "sql-dump",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(preview_path),
        )
        import zlib
        compress = zlib.compressobj(9, zlib.DEFLATED, 31)

        while True:
            chunk = await process.stdout.read(64 * 1024)
            if not chunk:
                break
            yield compress.compress(chunk)

        yield compress.flush(zlib.Z_FINISH)
        await process.wait()

    filename = f"{project}-{preview_name}.sql.gz"
    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/previews/{project}/{preview_name}/files/download")
async def download_files(project: str, preview_name: str, user: UserWithRole = Depends(require_role(Role.manager))):
    """Stream a tar.gz of the preview's Drupal files directory."""
    preview_path = _get_preview_dir(project, preview_name)

    # Drupal public files are typically in web/sites/default/files
    files_dir = preview_path / "web" / "sites" / "default" / "files"
    if not files_dir.exists():
        raise HTTPException(status_code=404, detail="Files directory not found")

    # Exclude Drupal cache directories (regenerable on cache rebuild)
    tar_excludes = ["--exclude=./css", "--exclude=./js", "--exclude=./php"]

    async def generate():
        process = await asyncio.create_subprocess_exec(
            "tar", "czf", "-", *tar_excludes, "-C", str(files_dir), ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(preview_path),
        )
        while True:
            chunk = await process.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
        await process.wait()

    filename = f"{project}-{preview_name}-files.tar.gz"
    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


