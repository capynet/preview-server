"""Preview CRUD and action endpoints"""

import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from config.settings import settings
from app.models import PreviewInfo
from app.state import PreviewStateManager
from app.auth.dependencies import require_role
from app.auth.models import Role, UserWithRole

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/previews/{project}/mr-{mr_id}", response_model=PreviewInfo)
async def get_preview(project: str, mr_id: int, user: UserWithRole = Depends(require_role(Role.viewer))):
    """Get preview information"""
    state = PreviewStateManager.load_state(project, mr_id)

    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"Preview {project}/mr-{mr_id} not found"
        )

    return PreviewInfo(
        preview_name=f"mr-{mr_id}",
        project=state.project,
        mr_id=state.mr_id,
        status=state.status,
        url=state.url,
        path=state.path,
        branch=state.branch,
        commit_sha=state.commit_sha,
        created_at=state.created_at,
        last_deployed_at=state.last_deployed_at,
        last_deployment=state.last_deployment
    )


async def get_preview_list_base(include_ddev_status: bool = True) -> dict:
    """
    Core logic to list all previews (scan filesystem + optionally DDEV status).

    Args:
        include_ddev_status: If True, run ddev describe for each preview (slow ~1s).
                            If False, return previews with status "unknown" (fast ~0.03s).

    Returns:
        dict with "previews" list and "total" count
    """
    t_total = time.monotonic()
    previews = []
    base_path = Path(settings.previews_base_path)

    if not base_path.exists():
        return {"previews": [], "total": 0}

    # Helper function to get real DDEV status asynchronously
    async def get_ddev_status(mr_dir: Path) -> str:
        """
        Get actual DDEV container status by running ddev describe
        Returns: "running", "stopped", or "unknown"
        """
        try:
            process = await asyncio.create_subprocess_exec(
                "ddev", "describe", "-j",
                cwd=str(mr_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                logger.warning(f"Timeout checking DDEV status for {mr_dir}")
                return "unknown"

            if process.returncode == 0:
                try:
                    import json
                    data = json.loads(stdout.decode())
                    raw_status = data.get("raw", {}).get("status", "unknown")

                    if raw_status in ["running", "healthy"]:
                        return "running"
                    elif raw_status in ["stopped", "exited"]:
                        return "stopped"
                    elif raw_status == "paused":
                        return "paused"
                    else:
                        return "unknown"
                except:
                    stdout_str = stdout.decode().lower()
                    if "running" in stdout_str:
                        return "running"
                    elif "stopped" in stdout_str or "exited" in stdout_str:
                        return "stopped"
                    else:
                        return "unknown"
            else:
                return "stopped"
        except Exception as e:
            logger.warning(f"Error checking DDEV status for {mr_dir}: {e}")
            return "unknown"

    # Helper function to detect project name from DDEV config
    def detect_project_from_ddev(mr_dir: Path) -> str:
        """Try to detect project name from .ddev/config.yaml"""
        try:
            import yaml
            ddev_config = mr_dir / ".ddev" / "config.yaml"
            if ddev_config.exists():
                with open(ddev_config, 'r') as f:
                    config = yaml.safe_load(f)
                    if config and 'name' in config:
                        ddev_name = config['name']
                        if '-mr-' in ddev_name:
                            return ddev_name.split('-mr-')[0]
                        return ddev_name
        except Exception as e:
            logger.warning(f"Error reading DDEV config from {mr_dir}: {e}")
        return None

    # Helper function to detect branch from preview info file
    def detect_branch_from_preview_info(mr_dir: Path) -> str:
        """Try to detect branch name from .preview-info file"""
        try:
            preview_info = mr_dir / ".preview-info"
            if preview_info.exists():
                with open(preview_info, 'r') as f:
                    for line in f:
                        if line.startswith('BRANCH='):
                            return line.split('=', 1)[1].strip()
        except Exception as e:
            logger.warning(f"Error reading preview info from {mr_dir}: {e}")

        try:
            git_head = mr_dir / ".git" / "HEAD"
            if git_head.exists():
                with open(git_head, 'r') as f:
                    content = f.read().strip()
                    if content.startswith('ref: refs/heads/'):
                        return content.replace('ref: refs/heads/', '')
                    elif len(content) == 40:
                        return content[:8]
        except Exception as e:
            logger.warning(f"Error reading git HEAD from {mr_dir}: {e}")

        return "unknown"

    # Helper function to detect Basic Auth credentials from preview info file
    def detect_basic_auth_from_preview_info(mr_dir: Path) -> tuple:
        """Try to detect Basic Auth credentials from .preview-info file"""
        try:
            preview_info = mr_dir / ".preview-info"
            if preview_info.exists():
                username = None
                password = None
                with open(preview_info, 'r') as f:
                    for line in f:
                        if line.startswith('BASIC_AUTH_USER='):
                            username = line.split('=', 1)[1].strip()
                        elif line.startswith('BASIC_AUTH_PASS='):
                            password = line.split('=', 1)[1].strip()

                if username and password:
                    return (username, password)
        except Exception as e:
            logger.warning(f"Error reading basic auth from {mr_dir}: {e}")

        return (None, None)

    # Helper function to add preview from directory
    def add_preview_from_dir(mr_dir: Path, project_name: str = "default"):
        if not mr_dir.is_dir() or not mr_dir.name.startswith("mr-"):
            return

        deployment_complete = mr_dir / ".deployment-complete"
        if not deployment_complete.exists():
            logger.debug(f"Skipping {mr_dir} - no successful deployment yet")
            return

        try:
            mr_id = int(mr_dir.name.replace("mr-", ""))
            state_file = mr_dir / ".preview-state.json"

            detected_project = detect_project_from_ddev(mr_dir)
            if detected_project:
                project_name = detected_project

            if state_file.exists():
                try:
                    with open(state_file, 'r') as f:
                        state_data = json.load(f)
                        project = state_data.get("project", project_name)

                        branch = state_data.get("branch", "unknown")
                        if branch == "unknown":
                            branch = detect_branch_from_preview_info(mr_dir)

                        basic_auth_user, basic_auth_pass = detect_basic_auth_from_preview_info(mr_dir)

                        previews.append({
                            "name": f"mr-{mr_id}",
                            "project": project,
                            "mr_id": mr_id,
                            "status": "unknown",
                            "url": state_data.get("url", f"https://mr-{mr_id}-{project}.mr.preview-mr.com"),
                            "branch": branch,
                            "commit_sha": state_data.get("commit_sha", ""),
                            "last_deployed_at": state_data.get("last_deployed_at"),
                            "basic_auth_user": basic_auth_user,
                            "basic_auth_pass": basic_auth_pass,
                            "_path": str(mr_dir)
                        })
                        return
                except Exception as e:
                    logger.warning(f"Error reading state file {state_file}: {e}")

            ddev_dir = mr_dir / ".ddev"
            if ddev_dir.exists():
                branch = detect_branch_from_preview_info(mr_dir)
                basic_auth_user, basic_auth_pass = detect_basic_auth_from_preview_info(mr_dir)

                previews.append({
                    "name": f"mr-{mr_id}",
                    "project": project_name,
                    "mr_id": mr_id,
                    "status": "unknown",
                    "url": f"https://mr-{mr_id}-{project_name}.mr.preview-mr.com",
                    "branch": branch,
                    "commit_sha": "",
                    "last_deployed_at": None,
                    "basic_auth_user": basic_auth_user,
                    "basic_auth_pass": basic_auth_pass,
                    "_path": str(mr_dir)
                })
        except Exception as e:
            logger.warning(f"Error reading preview {mr_dir}: {e}")

    # First, check for mr-* directories directly in base_path
    t_scan = time.monotonic()
    for item in base_path.iterdir():
        if item.is_dir() and item.name.startswith("mr-"):
            add_preview_from_dir(item, "previews")

    # Then, scan project subdirectories for mr-* folders
    for project_dir in base_path.iterdir():
        if not project_dir.is_dir() or project_dir.name.startswith("mr-"):
            continue

        project_name = project_dir.name

        for mr_dir in project_dir.iterdir():
            add_preview_from_dir(mr_dir, project_name)

    t_scan_done = time.monotonic()
    logger.info(f"[TIMING] Filesystem scan: {t_scan_done - t_scan:.3f}s ({len(previews)} previews found)")

    # Get DDEV status for all previews in parallel
    async def update_preview_status(preview):
        """Update a single preview's status"""
        if "_path" in preview:
            preview_path = Path(preview["_path"])
            t_ddev = time.monotonic()
            status = await get_ddev_status(preview_path)
            logger.info(f"[TIMING] DDEV status {preview['name']}: {time.monotonic() - t_ddev:.3f}s → {status}")
            preview["status"] = status

    if include_ddev_status and previews:
        t_ddev_all = time.monotonic()
        await asyncio.gather(*[update_preview_status(p) for p in previews])
        logger.info(f"[TIMING] DDEV status (all {len(previews)} parallel): {time.monotonic() - t_ddev_all:.3f}s")

    # Strip _path for external consumers
    for preview in previews:
        preview.pop("_path", None)

    logger.info(f"[TIMING] get_preview_list_base TOTAL: {time.monotonic() - t_total:.3f}s")

    return {
        "previews": previews,
        "total": len(previews)
    }



@router.delete("/api/previews/{project}/mr-{mr_id}")
async def delete_preview(project: str, mr_id: int, user: UserWithRole = Depends(require_role(Role.member))):
    """Delete a preview (DANGEROUS - removes directory)"""
    preview_path = PreviewStateManager.get_preview_path(project, mr_id)

    if not preview_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Preview {project}/mr-{mr_id} not found"
        )

    try:
        # Stop DDEV if running
        try:
            result = subprocess.run(
                ["ddev", "stop"],
                cwd=str(preview_path),
                capture_output=True,
                text=True,
                timeout=60
            )
            logger.info(f"DDEV stopped for {project}/mr-{mr_id}")
        except Exception as e:
            logger.warning(f"Error stopping DDEV: {e}")

        # Delete state file
        PreviewStateManager.delete_state(project, mr_id)

        # Delete directory (DANGEROUS!)
        # TODO: For safety, maybe just mark as deleted instead?
        import shutil
        shutil.rmtree(preview_path)
        logger.info(f"Preview directory deleted: {preview_path}")

        return {
            "success": True,
            "message": f"Preview {project}/mr-{mr_id} deleted successfully"
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
        status: If true (default), include DDEV status (slower).
    """
    result = await get_preview_list_base(include_ddev_status=status)
    return result


def _get_preview_dir(project: str, mr_id: int) -> Path:
    """Resolve preview directory or raise 404."""
    preview_path = PreviewStateManager.get_preview_path(project, mr_id)
    if not preview_path.exists():
        raise HTTPException(status_code=404, detail=f"Preview {project}/mr-{mr_id} not found")
    return preview_path


async def _run_ddev_command(
    command: list[str],
    cwd: Path,
    timeout: int = 120,
) -> dict:
    """Run a ddev command and return {success, output, error}."""
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


@router.post("/api/previews/{project}/mr-{mr_id}/stop")
async def stop_preview(project: str, mr_id: int, user: UserWithRole = Depends(require_role(Role.member))):
    """Stop a preview (ddev stop)."""
    preview_path = _get_preview_dir(project, mr_id)
    return await _run_ddev_command(["ddev", "stop"], preview_path, timeout=60)


@router.post("/api/previews/{project}/mr-{mr_id}/start")
async def start_preview(project: str, mr_id: int, user: UserWithRole = Depends(require_role(Role.member))):
    """Start a preview (ddev start)."""
    preview_path = _get_preview_dir(project, mr_id)
    return await _run_ddev_command(["ddev", "start"], preview_path, timeout=120)


@router.post("/api/previews/{project}/mr-{mr_id}/restart")
async def restart_preview(project: str, mr_id: int, user: UserWithRole = Depends(require_role(Role.member))):
    """Restart a preview (ddev restart)."""
    preview_path = _get_preview_dir(project, mr_id)
    return await _run_ddev_command(["ddev", "restart"], preview_path, timeout=120)


@router.post("/api/previews/{project}/mr-{mr_id}/drush-uli")
async def drush_uli(project: str, mr_id: int, user: UserWithRole = Depends(require_role(Role.member))):
    """Get a one-time login link (ddev drush uli)."""
    preview_path = _get_preview_dir(project, mr_id)
    preview_url = f"https://mr-{mr_id}-{project}.mr.preview-mr.com"
    return await _run_ddev_command(
        ["ddev", "drush", "uli", f"--uri={preview_url}"],
        preview_path,
        timeout=30,
    )


@router.post("/api/previews/{project}/mr-{mr_id}/drush")
async def drush_command(project: str, mr_id: int, request: Request, user: UserWithRole = Depends(require_role(Role.member))):
    """
    Run an arbitrary drush command.

    Body: {"args": "cr"} or {"args": "status"}
    """
    body = await request.json()
    args_str = body.get("args", "")
    if not args_str:
        raise HTTPException(status_code=400, detail="Missing 'args' in request body")

    preview_path = _get_preview_dir(project, mr_id)
    command = ["ddev", "drush"] + args_str.split()
    return await _run_ddev_command(command, preview_path, timeout=120)


@router.post("/api/previews/{project}/mr-{mr_id}/rebuild")
async def rebuild_preview(project: str, mr_id: int, user: UserWithRole = Depends(require_role(Role.member))):
    """Trigger a GitLab pipeline to rebuild this preview."""
    # Verify preview exists
    _get_preview_dir(project, mr_id)

    if not settings.gitlab_api_token:
        raise HTTPException(status_code=400, detail="GitLab API token not configured")

    # URL-encode the project path for GitLab API
    gitlab_project_path = f"{settings.gitlab_group_name}/{project}"
    encoded_path = gitlab_project_path.replace("/", "%2F")

    # Find the branch for this MR from state
    state = PreviewStateManager.load_state(project, mr_id)
    if not state or not state.branch:
        raise HTTPException(status_code=400, detail="Cannot determine branch for this preview")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.gitlab_url}/api/v4/projects/{encoded_path}/pipeline",
                headers={"PRIVATE-TOKEN": settings.gitlab_api_token},
                json={"ref": state.branch},
                timeout=30,
            )

        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "success": True,
                "output": f"Pipeline #{data.get('id')} created for branch {state.branch}",
                "error": "",
                "pipeline_id": data.get("id"),
                "pipeline_url": data.get("web_url", ""),
            }
        else:
            return {
                "success": False,
                "output": "",
                "error": f"GitLab API returned {resp.status_code}: {resp.text}",
            }
    except Exception as e:
        logger.error(f"Error triggering pipeline: {e}", exc_info=True)
        return {"success": False, "output": "", "error": str(e)}


@router.get("/api/previews/{project}/mr-{mr_id}/db/download")
async def download_db(project: str, mr_id: int, user: UserWithRole = Depends(require_role(Role.member))):
    """Stream a gzipped SQL dump of the preview database."""
    preview_path = _get_preview_dir(project, mr_id)

    async def generate():
        process = await asyncio.create_subprocess_exec(
            "ddev", "drush", "sql-dump",
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

    filename = f"{project}-mr-{mr_id}.sql.gz"
    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/previews/{project}/mr-{mr_id}/files/download")
async def download_files(project: str, mr_id: int, user: UserWithRole = Depends(require_role(Role.member))):
    """Stream a tar.gz of the preview's Drupal files directory."""
    preview_path = _get_preview_dir(project, mr_id)

    # Drupal public files are typically in web/sites/default/files
    files_dir = preview_path / "web" / "sites" / "default" / "files"
    if not files_dir.exists():
        raise HTTPException(status_code=404, detail="Files directory not found")

    async def generate():
        process = await asyncio.create_subprocess_exec(
            "tar", "czf", "-", "-C", str(files_dir), ".",
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

    filename = f"{project}-mr-{mr_id}-files.tar.gz"
    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
