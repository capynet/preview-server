"""GitLab webhook receiver for merge request events (multi-tenant)."""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request

from config.settings import settings
from app.database import (
    get_organization_by_slug,
    get_project_by_gitlab_id,
    get_preview,
    get_preview_by_branch,
    compute_url_hash,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

async def clear_deploy_lock(project_slug: str, preview_name: str):
    """Remove the deploy lock for a preview (e.g. after deletion).

    This prevents stale locks from blocking new deploys when a preview
    is deleted and recreated with the same name.
    """
    try:
        from app.valkey import release_deploy_lock
        await release_deploy_lock(f"{project_slug}/{preview_name}")
    except Exception:
        pass


async def _clone_and_deploy(
    org_id: int,
    org_slug: str,
    project_id: int,
    project_slug: str,
    project_path: str,  # gitlab path_with_namespace
    preview_name: str,
    source_branch: str,
    commit_sha: str,
    triggered_by: str = "webhook",
    mr_iid: int | None = None,
    force_new: bool = False,
    mr_title: str | None = None,
):
    """Clone repo on VM and run deployment.

    Called from arq worker (task_deploy_preview) or in-process background task.
    Uses Valkey distributed lock to prevent duplicate concurrent deploys.
    """
    from app.deployment import PreviewDeployer
    from app.state import PreviewStateManager
    from app.routes.gitlab import _get_org_gitlab_token
    from app.valkey import acquire_deploy_lock, release_deploy_lock, is_deploy_locked

    deploy_key = f"{project_slug}/{preview_name}"

    # Distributed lock via Valkey (TTL 30min to prevent stale locks)
    if await is_deploy_locked(deploy_key):
        logger.info(f"Skipping duplicate deploy for {deploy_key} — already in progress")
        return

    if not await acquire_deploy_lock(deploy_key):
        logger.info(f"Could not acquire lock for {deploy_key} — deploy already in progress")
        return

    try:
        logger.info(f"Starting deploy for {deploy_key} (branch={source_branch}, commit={commit_sha[:8]})")

        # Create deployment record early so UI can show progress immediately
        # If a running deployment already exists (e.g. worker restart re-enqueued the job),
        # reuse it instead of creating a duplicate.
        from app.database import create_deployment, get_running_deployment
        from app.websockets import deployment_log_broadcaster, preview_list_manager
        preview = await get_preview(project_id, preview_name)
        early_deployment_id = None
        if preview:
            existing = await get_running_deployment(preview["id"])
            if existing:
                early_deployment_id = existing["id"]
                logger.info(f"Reusing existing running deployment #{early_deployment_id} for {deploy_key}")
            else:
                early_deployment_id = await create_deployment(preview["id"], triggered_by)
            await deployment_log_broadcaster.register(early_deployment_id)
            await preview_list_manager.force_broadcast()

        # Ensure local preview directory exists (for compose generation)
        preview_path = PreviewStateManager.get_preview_path(org_slug, project_slug, preview_name)
        preview_path.mkdir(parents=True, exist_ok=True)

        # Get org's GitLab token for cloning
        try:
            gitlab_url, gitlab_token = await _get_org_gitlab_token(org_id)
        except Exception as e:
            logger.error(f"Failed to get GitLab token for org {org_slug}: {e}")
            from app.database import finish_deployment
            if early_deployment_id:
                await finish_deployment(
                    early_deployment_id, "failed",
                    f"Failed to get GitLab token: {e}"
                )
            await PreviewStateManager.save_state(
                project_id, preview_name,
                status="failed",
                last_deployment_error="Failed to get GitLab token",
            )
            await preview_list_manager.force_broadcast()
            return

        # Clone locally (shallow, for preview.yml parsing only)
        ok = await _local_shallow_clone(
            gitlab_url, gitlab_token, project_path,
            org_slug, project_slug, preview_name,
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
                project_id, preview_name,
                status="failed",
                last_deployment_error="Clone failed",
            )
            await preview_list_manager.force_broadcast()
            return

        deployer = PreviewDeployer(
            org_slug=org_slug,
            project_slug=project_slug,
            project_id=project_id,
            project_name=project_slug,
            preview_name=preview_name,
            branch=source_branch,
            commit_sha=commit_sha,
            triggered_by=triggered_by,
            mr_iid=mr_iid,
            mr_title=mr_title,
            deployment_id=early_deployment_id,
        )
        deployer.force_new = force_new
        success = await deployer.deploy()
        if not success:
            logger.error(f"Deploy failed for {deploy_key}")
        else:
            logger.info(f"Deploy completed successfully for {deploy_key}")
    finally:
        await release_deploy_lock(deploy_key)


async def _local_shallow_clone(
    gitlab_url: str,
    gitlab_token: str,
    project_path: str,  # gitlab path_with_namespace
    org_slug: str,
    project_slug: str,
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
        parsed = urlparse(gitlab_url)
        clone_url = f"https://oauth2:{gitlab_token}@{parsed.hostname}/{project_path}.git"

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


@router.post("/{org_slug}/gitlab")
async def gitlab_webhook(
    request: Request,
    org_slug: str,
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

    # Resolve organization from URL path
    org = await get_organization_by_slug(org_slug)
    if not org:
        logger.warning(f"Webhook for unknown organization: {org_slug}")
        raise HTTPException(status_code=404, detail="Organization not found")

    org_id = org["id"]

    payload = await request.json()
    object_kind = payload.get("object_kind")

    # Look up the project in our DB by GitLab project ID
    gitlab_project_id = payload.get("project", {}).get("id")
    project = await get_project_by_gitlab_id(org_id, gitlab_project_id)
    if not project:
        logger.warning(f"Webhook for non-enabled project {gitlab_project_id} in org {org_slug}")
        return {"status": "ignored", "reason": "project not enabled"}

    project_id = project["id"]
    project_slug = project["slug"]
    project_path = project.get("gitlab_project_path", "")

    if object_kind == "push":
        return await _handle_push_event(
            payload, request,
            org_id=org_id, org_slug=org_slug,
            project_id=project_id, project_slug=project_slug,
            project_path=project_path,
        )
    elif object_kind == "merge_request":
        return await _handle_mr_event(
            payload, request,
            org_id=org_id, org_slug=org_slug,
            project_id=project_id, project_slug=project_slug,
            project_path=project_path,
        )
    else:
        logger.debug(f"Ignoring webhook event: {object_kind}")
        return {"status": "ignored", "reason": f"unhandled event: {object_kind}"}


async def _handle_push_event(
    payload: dict,
    request: Request,
    *,
    org_id: int,
    org_slug: str,
    project_id: int,
    project_slug: str,
    project_path: str,
):
    """Handle push events: auto-update branch previews."""
    ref = payload.get("ref", "")
    if not ref.startswith("refs/heads/"):
        return {"status": "ignored", "reason": "not a branch push"}

    branch = ref.removeprefix("refs/heads/")
    commit_sha = payload.get("after", "")

    if commit_sha == "0" * 40:
        return {"status": "ignored", "reason": "branch deleted"}

    existing = await get_preview_by_branch(project_id, branch)
    if not existing:
        return {"status": "ignored", "reason": "no branch preview exists"}

    if not existing.get("auto_update", 1):
        return {"status": "ignored", "reason": "auto_update disabled"}

    preview_name = existing["preview_name"]
    logger.info(f"Push event: updating {project_slug}/{preview_name}")
    await request.app.state.arq.enqueue_job(
        "task_deploy_preview",
        org_id, org_slug, project_id, project_slug, project_path,
        preview_name, branch, commit_sha, "webhook-push",
    )
    return {"status": "ok", "action": "push_update", "project": project_path, "preview_name": preview_name}


async def _handle_mr_event(
    payload: dict,
    request: Request,
    *,
    org_id: int,
    org_slug: str,
    project_id: int,
    project_slug: str,
    project_path: str,
):
    """Handle merge request events."""
    attrs = payload.get("object_attributes", {})
    mr_iid = attrs.get("iid")
    action = attrs.get("action")
    source_branch = attrs.get("source_branch")
    commit_sha = attrs.get("last_commit", {}).get("id")
    mr_title = attrs.get("title")

    preview_name = f"mr-{mr_iid}"
    state = attrs.get("state")

    if action in ("close", "merge") or state in ("closed", "merged"):
        await request.app.state.arq.enqueue_job(
            "task_delete_preview",
            org_slug, project_slug, project_id, preview_name,
        )
    elif action in ("open", "reopen", "update"):
        if action == "update":
            existing = await get_preview(project_id, preview_name)
            if existing and not existing.get("auto_update", 1):
                return {"status": "ignored", "reason": "auto_update disabled"}

        await request.app.state.arq.enqueue_job(
            "task_deploy_preview",
            org_id, org_slug, project_id, project_slug, project_path,
            preview_name, source_branch, commit_sha, "webhook", mr_iid,
            False, mr_title,
        )
    else:
        return {"status": "ignored", "reason": f"unhandled action: {action}"}

    return {"status": "ok", "action": action, "project": project_path, "mr_iid": mr_iid}
