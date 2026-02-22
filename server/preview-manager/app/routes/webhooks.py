"""GitLab webhook receiver for merge request events."""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from config.settings import settings
from app.routes.gitlab import _get_gitlab_token
from app import config_store
from app.database import get_preview

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

# Track in-flight deploys to deduplicate concurrent webhook calls
_deploy_locks: dict[str, asyncio.Lock] = {}

# Files/dirs to preserve during rsync updates
RSYNC_EXCLUDES = [
    "--exclude=docker-compose.yml",
]


async def _clone_preview(
    project_path: str,
    project_name: str,
    preview_name: str,
    source_branch: str,
    commit_sha: str,
) -> bool:
    """Clone a branch into the previews directory.

    Returns True on success, False on failure.
    For updates (dest already exists with active deploy), rsync preserves Docker Compose config.
    """
    dest = Path(settings.previews_base_path) / project_name / preview_name

    # Check DB for existing active/failed preview to determine if this is an update
    existing = await get_preview(project_name, preview_name)
    is_update = existing is not None and existing["status"] in ("active", "failed")

    try:
        token = await _get_gitlab_token()
        clone_url = f"https://oauth2:{token}@gitlab.com/{project_path}.git"

        dest.parent.mkdir(parents=True, exist_ok=True)

        tmpdir = tempfile.mkdtemp(dir=str(dest.parent), prefix=f".{preview_name}-tmp-")
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", "--branch", source_branch,
                clone_url, tmpdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(
                    f"git clone failed for {project_path} {preview_name}: "
                    f"{stderr.decode().strip()}"
                )
                return False

            # Remove .git directory — we don't need history
            git_dir = Path(tmpdir) / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir)

            # Sync to destination
            dest.mkdir(parents=True, exist_ok=True)

            # Fix ownership before rsync: Docker containers create files as
            # root, which prevents rsync --delete from removing them.
            # Use a lightweight Docker container to chown everything to the
            # current user so rsync can overwrite/delete freely.
            if is_update:
                import os
                uid = os.getuid()
                gid = os.getgid()
                chown_proc = await asyncio.create_subprocess_exec(
                    "docker", "run", "--rm",
                    "-v", f"{dest}:/data",
                    "alpine", "chown", "-R", f"{uid}:{gid}", "/data",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(chown_proc.communicate(), timeout=60)

            rsync_cmd = ["rsync", "-a", "--delete"]
            if is_update:
                rsync_cmd.extend(RSYNC_EXCLUDES)
            rsync_cmd.extend([f"{tmpdir}/", f"{dest}/"])

            proc_sync = await asyncio.create_subprocess_exec(
                *rsync_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_sync, stderr_sync = await proc_sync.communicate()

            if proc_sync.returncode != 0:
                logger.error(
                    f"rsync failed for {project_path} {preview_name}: "
                    f"{stderr_sync.decode().strip()}"
                )
                return False

            # Ensure the preview directory is world-readable so Apache
            # inside the container can serve files (rsync -a preserves the
            # restrictive 0700 permissions from mkdtemp).
            dest.chmod(0o755)

            logger.info(
                f"Cloned {project_path} {preview_name} ({commit_sha[:8]}) -> {dest}"
                f"{' (update, excludes applied)' if is_update else ''}"
            )
            return True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        logger.error(f"Failed to clone preview {project_path} {preview_name}: {e}")
        return False


async def _clone_and_deploy(
    project_path: str,
    project_name: str,
    preview_name: str,
    source_branch: str,
    commit_sha: str,
    triggered_by: str = "webhook",
    mr_iid: int | None = None,
):
    """Clone repo then run deployment (runs in background)."""
    from app.deployment import PreviewDeployer

    deploy_key = f"{project_name}/{preview_name}"

    # Get or create a lock for this preview to deduplicate concurrent webhooks
    if deploy_key not in _deploy_locks:
        _deploy_locks[deploy_key] = asyncio.Lock()
    lock = _deploy_locks[deploy_key]

    if lock.locked():
        logger.info(f"Skipping duplicate webhook for {deploy_key} — deploy already in progress")
        return

    async with lock:
        logger.info(f"Starting clone+deploy for {deploy_key} (branch={source_branch}, commit={commit_sha[:8]})")

        ok = await _clone_preview(project_path, project_name, preview_name, source_branch, commit_sha)
        if not ok:
            logger.error(f"Clone failed, skipping deploy for {deploy_key}")
            # Update preview status so it doesn't stay stuck in pending/creating
            from app.state import PreviewStateManager
            await PreviewStateManager.save_state(
                project_name, preview_name,
                status="failed",
                last_deployment_error="Clone failed (git clone or rsync error, check logs)",
            )
            return

        deployer = PreviewDeployer(
            project_name=project_name,
            preview_name=preview_name,
            branch=source_branch,
            commit_sha=commit_sha,
            triggered_by=triggered_by,
            mr_iid=mr_iid,
        )
        success = await deployer.deploy()
        if not success:
            logger.error(f"Deploy failed for {deploy_key}")
        else:
            logger.info(f"Deploy completed successfully for {deploy_key}")


async def _delete_preview(project_name: str, preview_name: str):
    """Delete a preview by delegating to the existing delete logic in previews.py."""
    from app.routes.previews import delete_preview_internal
    try:
        await delete_preview_internal(project_name, preview_name)
    except Exception as e:
        logger.error(f"Failed to delete preview {project_name}/{preview_name}: {e}")


@router.post("/gitlab")
async def gitlab_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_token: str = Header(None),
    x_gitlab_event: str = Header(None),
):
    """Receive GitLab webhook events for merge requests."""
    # Validate webhook secret
    if not settings.gitlab_webhook_secret:
        logger.error("Webhook received but GITLAB_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    if x_gitlab_token != settings.gitlab_webhook_secret:
        logger.warning("Webhook received with invalid token")
        raise HTTPException(status_code=403, detail="Invalid webhook token")

    payload = await request.json()

    # Only handle merge request events
    if payload.get("object_kind") != "merge_request":
        logger.debug(f"Ignoring webhook event: {payload.get('object_kind')}")
        return {"status": "ignored", "reason": "not a merge_request event"}

    # Verify project is enabled
    project_id = payload.get("project", {}).get("id")
    enabled_ids = await config_store.load_enabled_project_ids()
    if project_id not in enabled_ids:
        logger.warning(f"Webhook for non-enabled project {project_id}")
        return {"status": "ignored", "reason": "project not enabled"}

    # Extract MR data
    attrs = payload.get("object_attributes", {})
    project_path = payload.get("project", {}).get("path_with_namespace", "unknown")
    project_name = project_path.split("/")[-1]
    mr_iid = attrs.get("iid")
    action = attrs.get("action")
    source_branch = attrs.get("source_branch")
    commit_sha = attrs.get("last_commit", {}).get("id")

    preview_name = f"mr-{mr_iid}"

    # Determine what to do based on action
    if action in ("open", "reopen", "update"):
        # For updates, check if auto_update is disabled
        if action == "update":
            existing = await get_preview(project_name, preview_name)
            if existing and not existing.get("auto_update", 1):
                logger.info(f"Skipping update for {project_path} {preview_name}: auto_update disabled")
                return {"status": "ignored", "reason": "auto_update disabled"}

        logger.info(
            f"{'Create' if action != 'update' else 'Update'} preview for "
            f"{project_path} {preview_name} (branch: {source_branch}, commit: {commit_sha})"
        )
        background_tasks.add_task(
            _clone_and_deploy, project_path, project_name, preview_name,
            source_branch, commit_sha, "webhook", mr_iid
        )
    elif action in ("close", "merge"):
        logger.info(f"Delete preview for {project_path} {preview_name}")
        background_tasks.add_task(_delete_preview, project_name, preview_name)
    else:
        logger.debug(f"Ignoring MR action '{action}' for {project_path} {preview_name}")
        return {"status": "ignored", "reason": f"unhandled action: {action}"}

    return {"status": "ok", "action": action, "project": project_path, "mr_iid": mr_iid}
