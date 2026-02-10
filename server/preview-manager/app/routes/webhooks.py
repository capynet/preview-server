"""GitLab webhook receiver for merge request events."""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from config.settings import settings
from app.routes.gitlab import _get_gitlab_token, _load_enabled_project_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


async def _clone_preview(
    project_path: str,
    project_name: str,
    mr_iid: int,
    source_branch: str,
    commit_sha: str,
):
    """Clone the MR branch into the previews directory (runs in background)."""
    dest = Path(settings.previews_base_path) / project_name / f"mr-{mr_iid}"
    try:
        token = await _get_gitlab_token()
        clone_url = f"https://oauth2:{token}@gitlab.com/{project_path}.git"

        # Ensure project directory exists
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Clone into a temp dir next to the destination
        tmpdir = tempfile.mkdtemp(dir=str(dest.parent), prefix=f".mr-{mr_iid}-tmp-")
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
                    f"git clone failed for {project_path} MR-{mr_iid}: "
                    f"{stderr.decode().strip()}"
                )
                shutil.rmtree(tmpdir, ignore_errors=True)
                return

            # Remove .git directory — we don't need history
            git_dir = Path(tmpdir) / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir)

            # Sync to destination (rsync for atomic-ish updates, no downtime window)
            dest.mkdir(parents=True, exist_ok=True)
            proc_sync = await asyncio.create_subprocess_exec(
                "rsync", "-a", "--delete", f"{tmpdir}/", f"{dest}/",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_sync, stderr_sync = await proc_sync.communicate()

            if proc_sync.returncode != 0:
                logger.error(
                    f"rsync failed for {project_path} MR-{mr_iid}: "
                    f"{stderr_sync.decode().strip()}"
                )
                return

            logger.info(
                f"Cloned {project_path} MR-{mr_iid} ({commit_sha[:8]}) → {dest}"
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        logger.error(f"Failed to clone preview {project_path} MR-{mr_iid}: {e}")


async def _delete_preview(project_name: str, mr_iid: int):
    """Delete a preview by delegating to the existing delete logic in previews.py."""
    from app.routes.previews import delete_preview_internal
    try:
        await delete_preview_internal(project_name, mr_iid)
    except Exception as e:
        logger.error(f"Failed to delete preview {project_name}/mr-{mr_iid}: {e}")


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
    enabled_ids = _load_enabled_project_ids()
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

    # Determine what to do based on action
    if action in ("open", "reopen", "update"):
        logger.info(
            f"{'Create' if action != 'update' else 'Update'} preview for "
            f"{project_path} MR-{mr_iid} (branch: {source_branch}, commit: {commit_sha})"
        )
        background_tasks.add_task(
            _clone_preview, project_path, project_name, mr_iid, source_branch, commit_sha
        )
    elif action in ("close", "merge"):
        logger.info(f"Delete preview for {project_path} MR-{mr_iid}")
        background_tasks.add_task(_delete_preview, project_name, mr_iid)
    else:
        logger.debug(f"Ignoring MR action '{action}' for {project_path} MR-{mr_iid}")
        return {"status": "ignored", "reason": f"unhandled action: {action}"}

    return {"status": "ok", "action": action, "project": project_path, "mr_iid": mr_iid}
