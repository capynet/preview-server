"""GitLab webhook receiver for merge request events (multi-tenant)."""

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import httpx
import yaml

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


async def _fetch_required_ci_jobs(org_id: int, gitlab_project_path: str, ref: str) -> list[str] | None:
    """Read preview.yml from GitLab API and return ci.required_jobs list, or None if not configured."""
    try:
        from app.routes.gitlab import _get_org_gitlab_token
        gitlab_url, token = await _get_org_gitlab_token(org_id)
        encoded_path = gitlab_project_path.replace("/", "%2F")
        url = f"{gitlab_url}/api/v4/projects/{encoded_path}/repository/files/druploy.yml/raw"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"PRIVATE-TOKEN": token}, params={"ref": ref})
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning(f"Failed to fetch preview.yml from GitLab: HTTP {resp.status_code}")
                return None
            config = yaml.safe_load(resp.text)
            if not isinstance(config, dict):
                return None
            ci = config.get("ci")
            if not isinstance(ci, dict):
                return None
            jobs = ci.get("required_jobs")
            if isinstance(jobs, list) and all(isinstance(j, str) for j in jobs):
                return jobs
            return None
    except Exception as e:
        logger.warning(f"Error fetching preview.yml for CI gating: {e}")
        return None


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
    target_branch: str | None = None,
):
    """Clone repo on VM and run deployment.

    Called from arq worker (task_deploy_preview) or in-process background task.
    Uses Valkey distributed lock to prevent duplicate concurrent deploys.
    """
    from app.deployment import PreviewDeployer
    from app.state import PreviewStateManager
    from app.valkey import (
        acquire_deploy_lock, release_deploy_lock, is_deploy_locked,
        is_deploy_cancelled, clear_deploy_cancel,
    )

    deploy_key = f"{project_slug}/{preview_name}"

    # Distributed lock via Valkey (TTL 30min to prevent stale locks)
    if await is_deploy_locked(deploy_key):
        # If cancellation was requested, wait for the running deploy to finish
        if await is_deploy_cancelled(deploy_key):
            logger.info(f"Waiting for cancelled deploy {deploy_key} to release lock...")
            for _ in range(30):  # Wait up to 60s
                await asyncio.sleep(2)
                if not await is_deploy_locked(deploy_key):
                    break
            else:
                # Force-release stale lock after timeout
                logger.warning(f"Force-releasing stale lock for {deploy_key}")
                await release_deploy_lock(deploy_key)
        else:
            logger.info(f"Skipping duplicate deploy for {deploy_key} — already in progress")
            return

    # Clear any cancellation flag before starting
    await clear_deploy_cancel(deploy_key)

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

        deployer = PreviewDeployer(
            org_id=org_id,
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
            target_branch=target_branch,
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


async def _local_sync_repo(
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
    """Clone or update repo locally. Keeps .git for incremental fetches."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(gitlab_url)
        clone_url = f"https://oauth2:{gitlab_token}@{parsed.hostname}/{project_path}.git"
        dest.mkdir(parents=True, exist_ok=True)
        git_dir = dest / ".git"

        if git_dir.is_dir():
            # Incremental update: fetch + reset
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(dest),
                "remote", "set-url", "origin", clone_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(dest),
                "fetch", "--depth", "1", "origin", source_branch,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    f"git fetch failed for {project_path} {preview_name}: "
                    f"{stderr.decode().strip()} — falling back to fresh clone"
                )
                import shutil
                shutil.rmtree(dest, ignore_errors=True)
                return await _local_sync_repo(
                    gitlab_url, gitlab_token, project_path,
                    org_slug, project_slug, preview_name,
                    source_branch, commit_sha, dest,
                )

            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(dest),
                "reset", "--hard", "FETCH_HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    f"git reset failed for {project_path} {preview_name}: "
                    f"{stderr.decode().strip()}"
                )
                return False

            # Clean untracked files
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(dest), "clean", "-fdx",
                "--exclude", "druploy.yml",
                "--exclude", "docker-compose.yml",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            logger.info(f"Local fetch: {project_path} {preview_name} -> {dest}")
        else:
            # First time (or legacy dir without .git): clean and clone
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(parents=True, exist_ok=True)

            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", "--branch", source_branch,
                clone_url, str(dest),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(
                    f"git clone failed for {project_path} {preview_name}: "
                    f"{stderr.decode().strip()}"
                )
                return False

            logger.info(f"Local clone: {project_path} {preview_name} -> {dest}")

        return True
    except Exception as e:
        logger.error(f"Failed to sync repo: {e}")
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

    # Log webhook payload summary for traceability
    if object_kind == "merge_request":
        attrs = payload.get("object_attributes", {})
        logger.info(
            f"Webhook received: kind=merge_request action={attrs.get('action')} "
            f"mr_iid={attrs.get('iid')} branch={attrs.get('source_branch')} "
            f"project={project_slug} state={attrs.get('state')}"
        )
    elif object_kind == "push":
        ref = payload.get("ref", "")
        branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref
        logger.info(
            f"Webhook received: kind=push branch={branch} "
            f"commit={payload.get('after', '')[:8]} project={project_slug}"
        )
    elif object_kind == "pipeline":
        p_attrs = payload.get("object_attributes", {})
        logger.info(
            f"Webhook received: kind=pipeline status={p_attrs.get('status')} "
            f"ref={p_attrs.get('ref')} sha={p_attrs.get('sha', '')[:8]} project={project_slug}"
        )
    else:
        logger.info(f"Webhook received: kind={object_kind} project={project_slug}")

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
    elif object_kind == "pipeline":
        return await _handle_pipeline_event(
            payload, request,
            org_id=org_id, org_slug=org_slug,
            project_id=project_id, project_slug=project_slug,
            project_path=project_path,
        )
    else:
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
    logger.info(
        f"Enqueued task_deploy_preview: {project_slug}/{preview_name} "
        f"triggered_by=webhook-push branch={branch} commit={commit_sha[:8]}"
    )
    await request.app.state.arq.enqueue_job(
        "task_deploy_preview",
        org_id, org_slug, project_id, project_slug, project_path,
        preview_name, branch, commit_sha, "webhook-push",
        _expires=timedelta(hours=3),
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
    target_branch = attrs.get("target_branch")
    commit_sha = attrs.get("last_commit", {}).get("id")
    mr_title = attrs.get("title")

    preview_name = f"mr-{mr_iid}"
    state = attrs.get("state")

    if action in ("close", "merge") or state in ("closed", "merged"):
        logger.info(f"Enqueued task_delete_preview: {project_slug}/{preview_name} action={action}")
        await request.app.state.arq.enqueue_job(
            "task_delete_preview",
            org_slug, project_slug, project_id, preview_name,
        )
    elif action in ("open", "reopen", "update"):
        if action == "update":
            existing = await get_preview(project_id, preview_name)
            if existing and not existing.get("auto_update", 1):
                return {"status": "ignored", "reason": "auto_update disabled"}

        # Check if CI gating is enabled (DB setting or preview.yml)
        from app.database import get_effective_require_ci
        require_ci = await get_effective_require_ci(org_id, project_id)
        required_jobs = await _fetch_required_ci_jobs(org_id, project_path, source_branch)

        if require_ci or required_jobs:
            # Create/update the preview record so it shows in the UI as waiting
            from app.state import PreviewStateManager
            from app.websockets import preview_list_manager

            url_hash = compute_url_hash(org_slug, project_slug, preview_name)
            await PreviewStateManager.save_state(
                project_id, preview_name,
                url_hash=url_hash,
                mr_id=mr_iid,
                mr_title=mr_title,
                branch=source_branch,
                commit_sha=commit_sha,
                status="waiting_for_ci",
                ci_status="waiting",
            )
            await preview_list_manager.force_broadcast()
            logger.info(f"CI gating: {project_slug}/{preview_name} waiting for pipeline success")
        else:
            logger.info(
                f"Enqueued task_deploy_preview: {project_slug}/{preview_name} "
                f"triggered_by=webhook action={action} branch={source_branch} commit={commit_sha[:8] if commit_sha else '?'}"
            )
            await request.app.state.arq.enqueue_job(
                "task_deploy_preview",
                org_id, org_slug, project_id, project_slug, project_path,
                preview_name, source_branch, commit_sha, "webhook", mr_iid,
                False, mr_title, target_branch,
                _expires=timedelta(hours=3),
            )
    else:
        return {"status": "ignored", "reason": f"unhandled action: {action}"}

    return {"status": "ok", "action": action, "project": project_path, "mr_iid": mr_iid}


async def _handle_pipeline_event(
    payload: dict,
    request: Request,
    *,
    org_id: int,
    org_slug: str,
    project_id: int,
    project_slug: str,
    project_path: str,
):
    """Handle pipeline events: trigger deploy when CI passes for waiting previews."""
    attrs = payload.get("object_attributes", {})
    pipeline_status = attrs.get("status")
    ref = attrs.get("ref", "")
    sha = attrs.get("sha", "")

    if pipeline_status not in ("success", "failed"):
        return {"status": "ignored", "reason": f"pipeline status: {pipeline_status}"}

    from app.database import get_previews_waiting_for_ci, update_preview_ci_status
    from app.websockets import preview_list_manager

    waiting = await get_previews_waiting_for_ci(project_id, ref)
    if not waiting:
        return {"status": "ignored", "reason": "no previews waiting for CI"}

    # Check if preview.yml defines specific required jobs
    required_jobs = await _fetch_required_ci_jobs(org_id, project_path, ref)
    builds = payload.get("builds", [])

    if pipeline_status == "failed" and required_jobs:
        # Only check required jobs — ignore failures in non-required jobs
        failed_required = [
            b["name"] for b in builds
            if b["name"] in required_jobs and b.get("status") == "failed"
        ]
        if not failed_required:
            # All required jobs passed, treat as success
            logger.info(
                f"CI gating: pipeline failed but required jobs all passed "
                f"(required={required_jobs}, failed_required=none)"
            )
            pipeline_status = "success"
        else:
            logger.info(
                f"CI gating: required jobs failed: {failed_required}"
            )

    if pipeline_status == "failed":
        failed_jobs = [b["name"] for b in builds if b.get("status") == "failed"]
        error_msg = f"CI pipeline failed (jobs: {', '.join(failed_jobs)})" if failed_jobs else "CI pipeline failed"
        for preview in waiting:
            await update_preview_ci_status(
                project_id, preview["preview_name"],
                ci_status="failed", status="failed",
                error=error_msg,
            )
        await preview_list_manager.force_broadcast()
        return {"status": "ok", "action": "ci_failed", "branch": ref, "failed_jobs": failed_jobs}

    # success
    triggered = []
    for preview in waiting:
        preview_name = preview["preview_name"]
        await update_preview_ci_status(project_id, preview_name, ci_status="success", status="creating")

        logger.info(
            f"Enqueued task_deploy_preview: {project_slug}/{preview_name} "
            f"triggered_by=webhook-ci branch={preview.get('branch', ref)} commit={preview.get('commit_sha', sha)[:8]}"
        )
        await request.app.state.arq.enqueue_job(
            "task_deploy_preview",
            org_id, org_slug, project_id, project_slug, project_path,
            preview_name, preview.get("branch", ref), preview.get("commit_sha", sha),
            "webhook-ci", preview.get("mr_id"), False,
            preview.get("mr_title"), preview.get("target_branch"),
            _expires=timedelta(hours=3),
        )
        triggered.append(preview_name)

    await preview_list_manager.force_broadcast()
    return {"status": "ok", "action": "ci_passed", "branch": ref, "triggered": triggered}
