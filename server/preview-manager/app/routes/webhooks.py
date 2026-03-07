"""GitLab webhook receiver for merge request events."""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from config.settings import settings
from app.routes.gitlab import _get_gitlab_token
from app import config_store
from app.database import get_preview, get_preview_by_branch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

# Track in-flight deploys to deduplicate concurrent webhook calls
_deploy_locks: dict[str, asyncio.Lock] = {}


async def _clone_and_deploy(
    project_path: str,
    project_name: str,
    preview_name: str,
    source_branch: str,
    commit_sha: str,
    triggered_by: str = "webhook",
    mr_iid: int | None = None,
    force_new: bool = False,
):
    """Clone repo on VM and run deployment (runs in background).

    For cloud VMs, the clone happens inside the deployer on the VM via SSH.
    We still need the local preview_path for generating compose locally.
    """
    from app.deployment import PreviewDeployer
    from app.state import PreviewStateManager

    deploy_key = f"{project_name}/{preview_name}"

    if deploy_key not in _deploy_locks:
        _deploy_locks[deploy_key] = asyncio.Lock()
    lock = _deploy_locks[deploy_key]

    if lock.locked():
        logger.info(f"Skipping duplicate webhook for {deploy_key} — deploy already in progress")
        return

    async with lock:
        logger.info(f"Starting deploy for {deploy_key} (branch={source_branch}, commit={commit_sha[:8]})")

        # Create deployment record early so UI can show progress immediately
        from app.database import create_deployment
        from app.websockets import deployment_log_broadcaster, preview_list_manager
        preview = await get_preview(project_name, preview_name)
        early_deployment_id = None
        if preview:
            early_deployment_id = await create_deployment(preview["id"], triggered_by)
            deployment_log_broadcaster.register(early_deployment_id)
            await preview_list_manager.force_broadcast()

        # Ensure local preview directory exists (for compose generation)
        preview_path = PreviewStateManager.get_preview_path(project_name, preview_name)
        preview_path.mkdir(parents=True, exist_ok=True)

        # Clone locally (shallow, for preview.yml parsing only)
        ok = await _local_shallow_clone(
            project_path, project_name, preview_name,
            source_branch, commit_sha, preview_path,
        )
        if not ok:
            logger.error(f"Local clone failed for {deploy_key}")
            from app.database import finish_deployment
            if early_deployment_id:
                await finish_deployment(
                    early_deployment_id, "failed",
                    "Clone failed (git clone error, check logs)"
                )
            await PreviewStateManager.save_state(
                project_name, preview_name,
                status="failed",
                last_deployment_error="Clone failed",
            )
            await preview_list_manager.force_broadcast()
            return

        deployer = PreviewDeployer(
            project_name=project_name,
            preview_name=preview_name,
            branch=source_branch,
            commit_sha=commit_sha,
            triggered_by=triggered_by,
            mr_iid=mr_iid,
            deployment_id=early_deployment_id,
        )
        deployer.force_new = force_new
        success = await deployer.deploy()
        if not success:
            logger.error(f"Deploy failed for {deploy_key}")
        else:
            logger.info(f"Deploy completed successfully for {deploy_key}")


async def _local_shallow_clone(
    project_path: str,
    project_name: str,
    preview_name: str,
    source_branch: str,
    commit_sha: str,
    dest: Path,
) -> bool:
    """Shallow clone locally for preview.yml parsing. Returns True on success."""
    import shutil
    import tempfile
    from urllib.parse import urlparse

    try:
        token = await _get_gitlab_token()
        parsed = urlparse(settings.gitlab_url)
        clone_url = f"https://oauth2:{token}@{parsed.hostname}/{project_path}.git"

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

            # Remove .git directory
            git_dir = Path(tmpdir) / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir)

            # Sync to destination
            dest.mkdir(parents=True, exist_ok=True)
            rsync_cmd = ["rsync", "-a", "--delete", f"{tmpdir}/", f"{dest}/"]
            proc_sync = await asyncio.create_subprocess_exec(
                *rsync_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc_sync.communicate()

            dest.chmod(0o755)
            logger.info(f"Local clone: {project_path} {preview_name} -> {dest}")
            return True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        logger.error(f"Failed to clone: {e}")
        return False


async def _delete_preview(project_name: str, preview_name: str):
    """Delete a preview by delegating to the existing delete logic."""
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
    if not settings.gitlab_webhook_secret:
        logger.error("Webhook received but GITLAB_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    if x_gitlab_token != settings.gitlab_webhook_secret:
        logger.warning("Webhook received with invalid token")
        raise HTTPException(status_code=403, detail="Invalid webhook token")

    payload = await request.json()
    object_kind = payload.get("object_kind")

    project_id = payload.get("project", {}).get("id")
    enabled_ids = await config_store.load_enabled_project_ids()
    if project_id not in enabled_ids:
        logger.warning(f"Webhook for non-enabled project {project_id}")
        return {"status": "ignored", "reason": "project not enabled"}

    if object_kind == "push":
        return await _handle_push_event(payload, background_tasks)
    elif object_kind == "merge_request":
        return await _handle_mr_event(payload, background_tasks)
    else:
        logger.debug(f"Ignoring webhook event: {object_kind}")
        return {"status": "ignored", "reason": f"unhandled event: {object_kind}"}


async def _handle_push_event(payload: dict, background_tasks: BackgroundTasks):
    """Handle push events: auto-update branch previews."""
    ref = payload.get("ref", "")
    if not ref.startswith("refs/heads/"):
        return {"status": "ignored", "reason": "not a branch push"}

    branch = ref.removeprefix("refs/heads/")
    project_path = payload.get("project", {}).get("path_with_namespace", "unknown")
    project_name = project_path.split("/")[-1]
    commit_sha = payload.get("after", "")

    if commit_sha == "0" * 40:
        return {"status": "ignored", "reason": "branch deleted"}

    existing = await get_preview_by_branch(project_name, branch)
    if not existing:
        return {"status": "ignored", "reason": "no branch preview exists"}

    if not existing.get("auto_update", 1):
        return {"status": "ignored", "reason": "auto_update disabled"}

    preview_name = existing["preview_name"]
    logger.info(f"Push event: updating {project_name}/{preview_name}")
    background_tasks.add_task(
        _clone_and_deploy, project_path, project_name, preview_name,
        branch, commit_sha, "webhook-push"
    )
    return {"status": "ok", "action": "push_update", "project": project_path, "preview_name": preview_name}


async def _handle_mr_event(payload: dict, background_tasks: BackgroundTasks):
    """Handle merge request events."""
    attrs = payload.get("object_attributes", {})
    project_path = payload.get("project", {}).get("path_with_namespace", "unknown")
    project_name = project_path.split("/")[-1]
    mr_iid = attrs.get("iid")
    action = attrs.get("action")
    source_branch = attrs.get("source_branch")
    commit_sha = attrs.get("last_commit", {}).get("id")

    preview_name = f"mr-{mr_iid}"
    state = attrs.get("state")

    if action in ("close", "merge") or state in ("closed", "merged"):
        background_tasks.add_task(_delete_preview, project_name, preview_name)
    elif action in ("open", "reopen", "update"):
        if action == "update":
            existing = await get_preview(project_name, preview_name)
            if existing and not existing.get("auto_update", 1):
                return {"status": "ignored", "reason": "auto_update disabled"}

        background_tasks.add_task(
            _clone_and_deploy, project_path, project_name, preview_name,
            source_branch, commit_sha, "webhook", mr_iid
        )
    else:
        return {"status": "ignored", "reason": f"unhandled action: {action}"}

    return {"status": "ok", "action": action, "project": project_path, "mr_iid": mr_iid}
