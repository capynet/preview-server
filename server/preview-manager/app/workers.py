"""arq worker definitions — background task processing via Valkey.

Handles deploys, deletes, and cron jobs (auto-erase, VM health checks).
All tasks use Valkey-backed log broadcasting for cross-process streaming.

Recovery features:
- Startup recovery: detects deployments left 'running' from a previous worker and re-enqueues them.
- Graceful shutdown: on SIGTERM, catches CancelledError in deploy tasks and re-enqueues them
  so the new worker picks them up immediately.
"""

import asyncio
import logging
from arq import cron
from arq.connections import RedisSettings

from config.settings import settings

logger = logging.getLogger(__name__)


# ---- Recovery helpers ----

async def _handle_interrupted_deploy(
    org_id: int, org_slug: str,
    project_id: int, project_slug: str, project_path: str,
    preview_name: str, source_branch: str, commit_sha: str,
    mr_iid: int | None, force_new: bool, mr_title: str | None,
    reason: str = "Interrupted by worker shutdown",
):
    """Mark current deployment as failed, release lock, and re-enqueue for recovery."""
    from app.database import get_preview, fail_running_deployments
    from app.state import PreviewStateManager
    from app.valkey import release_deploy_lock

    deploy_key = f"{project_slug}/{preview_name}"

    # Close any running deployment row BY ITS OWN ID — this must not depend on
    # the preview still existing (a racing delete may have removed it), otherwise
    # the deployment is left stuck in 'running' forever (a "zombie").
    try:
        closed = await fail_running_deployments(
            project_id, preview_name, error=f"{reason} — will retry",
        )
        if closed:
            logger.info(f"Closed {closed} running deployment(s) for {deploy_key} ({reason})")

        # Best-effort: reset preview status only if it still exists.
        if await get_preview(project_id, preview_name):
            await PreviewStateManager.save_state(
                project_id, preview_name,
                status="failed",
                last_deployment_status="failed",
                last_deployment_error=f"{reason} — re-deploying",
            )
    except Exception as e:
        logger.error(f"Error marking deployment failed for {deploy_key}: {e}")

    # Release deploy lock so the re-enqueued job can acquire it
    try:
        await release_deploy_lock(deploy_key)
    except Exception:
        pass

    # Note: arq automatically retries cancelled tasks, so no need to re-enqueue here.
    # The startup recovery (recover_interrupted_deployments) handles the case where
    # arq's retry doesn't happen (e.g., kill -9, OOM).


async def recover_interrupted_deployments():
    """Startup recovery: find deployments left 'running' from a previous worker and mark them as failed."""
    from app.database import get_all_running_deployments, finish_deployment
    from app.state import PreviewStateManager
    from app.valkey import release_deploy_lock

    running = await get_all_running_deployments()
    if not running:
        logger.info("No interrupted deployments to recover")
        return

    logger.info(f"Found {len(running)} interrupted deployment(s) — recovering")

    for dep in running:
        dep_id = dep["deployment_id"]
        preview_name = dep["preview_name"]
        project_slug = dep["project_slug"]
        deploy_key = f"{project_slug}/{preview_name}"

        # Mark deployment as failed
        await finish_deployment(dep_id, "failed", error="Interrupted by worker restart")
        logger.info(f"Marked deployment #{dep_id} as failed ({deploy_key})")

        # Reset preview status so it's not stuck in 'creating'
        await PreviewStateManager.save_state(
            dep["project_id"], preview_name,
            status="failed",
            last_deployment_status="failed",
            last_deployment_error="Interrupted by worker restart",
        )

        # Release stale deploy lock
        try:
            await release_deploy_lock(deploy_key)
        except Exception:
            pass

    logger.info(f"Recovery complete: {len(running)} deployment(s) marked as failed")


# ---- Worker lifecycle ----

async def startup(ctx):
    """Called when the worker starts."""
    from app.database import init_pool
    from app.valkey import init_valkey
    await init_pool()
    await init_valkey()
    await recover_interrupted_deployments()
    logger.info("arq worker started")


async def shutdown(ctx):
    """Called when the worker shuts down."""
    from app.database import close_pool
    from app.valkey import close_valkey
    await close_pool()
    await close_valkey()
    logger.info("arq worker stopped")


# ---- Deploy / delete task functions ----

async def task_deploy_preview(
    ctx,
    org_id: int,
    org_slug: str,
    project_id: int,
    project_slug: str,
    project_path: str,
    preview_name: str,
    source_branch: str,
    commit_sha: str,
    triggered_by: str = "webhook",
    mr_iid: int | None = None,
    force_new: bool = False,
    mr_title: str | None = None,
    target_branch: str | None = None,
):
    """Deploy a preview. Runs in arq worker with Valkey-backed log streaming."""
    from app.database import get_preview
    from app.routes.webhooks import _clone_and_deploy

    # Log job pickup with traceability info
    job_try = ctx.get("job_try", 1)
    job_id = ctx.get("job_id", "?")
    enqueue_time = ctx.get("enqueue_time")
    enqueue_info = f" enqueued_at={enqueue_time.isoformat()}" if enqueue_time else ""
    retry_info = f" RETRY try={job_try}" if job_try >= 2 else ""
    logger.info(
        f"Job picked up: {project_slug}/{preview_name} job_id={job_id}{retry_info} "
        f"triggered_by={triggered_by} branch={source_branch} commit={commit_sha[:8]}{enqueue_info}"
    )

    # Maintenance drain: park NEW deploys so the worker can be restarted with a
    # quiet queue. In-flight deploys (they hold the lock) and recovery retries
    # (job_try>=2) are allowed through; a fresh deploy is re-enqueued with a delay
    # and resumes automatically once maintenance clears.
    from app.valkey import is_maintenance_active, is_deploy_locked
    deploy_key = f"{project_slug}/{preview_name}"
    if job_try < 2 and await is_maintenance_active() and not await is_deploy_locked(deploy_key):
        from datetime import timedelta
        logger.info(f"Maintenance active — parking deploy {deploy_key} (defer 30s)")
        await ctx["redis"].enqueue_job(
            "task_deploy_preview",
            org_id, org_slug, project_id, project_slug, project_path,
            preview_name, source_branch, commit_sha, triggered_by,
            mr_iid, force_new, mr_title, target_branch,
            _defer_by=timedelta(seconds=30), _expires=timedelta(hours=6),
        )
        return

    # On retry (worker restart), skip if the preview was deleted while queued
    if job_try >= 2:
        preview = await get_preview(project_id, preview_name)
        if not preview:
            logger.info(f"Skipping retry for deleted preview {project_slug}/{preview_name} (try={job_try})")
            return

    import time
    t0 = time.monotonic()
    try:
        await _clone_and_deploy(
            org_id, org_slug, project_id, project_slug, project_path,
            preview_name, source_branch, commit_sha, triggered_by,
            mr_iid, force_new, mr_title, target_branch,
        )
    except asyncio.CancelledError:
        # CancelledError can mean a graceful SIGTERM shutdown OR arq killing the
        # job at job_timeout (a hung poll). Distinguish by elapsed time so the
        # log points at the real cause instead of always blaming "shutdown".
        elapsed = int(time.monotonic() - t0)
        if elapsed >= WorkerSettings.job_timeout - 30:
            reason = "Interrupted by job_timeout"
            logger.error(
                f"Deploy {project_slug}/{preview_name} hit job_timeout after "
                f"{elapsed}s (~{elapsed // 3600}h) — likely a hung poll, NOT a worker shutdown"
            )
        else:
            reason = "Interrupted by worker shutdown"
            logger.warning(
                f"Deploy {project_slug}/{preview_name} cancelled after {elapsed}s "
                f"(worker shutdown or cooperative cancel)"
            )
        try:
            await asyncio.shield(_handle_interrupted_deploy(
                org_id, org_slug, project_id, project_slug, project_path,
                preview_name, source_branch, commit_sha,
                mr_iid, force_new, mr_title,
                reason=reason,
            ))
        except asyncio.CancelledError:
            pass
        # Do NOT re-raise — prevents arq from re-enqueueing zombie jobs.
        # The user can manually rebuild if needed.


async def task_run_post_deploy(
    ctx,
    org_slug: str,
    project_id: int,
    project_slug: str,
    preview_name: str,
    triggered_by: str = "manual",
):
    """Run post-deploy script for an existing preview. Runs in arq worker."""
    from app.deployment import PreviewDeployer
    from app.database import get_preview
    from app.docker_compose import parse_preview_yml
    from app.state import PreviewStateManager
    from pathlib import Path

    preview = await get_preview(project_id, preview_name)
    if not preview:
        logger.error(f"Post-deploy: preview {project_slug}/{preview_name} not found")
        return

    vm_ip = preview.get("vm_ip")
    if not vm_ip:
        logger.error(f"Post-deploy: no VM for {project_slug}/{preview_name}")
        return

    # Parse druploy.yml to get post_deploy config
    preview_path = PreviewStateManager.get_preview_path(org_slug, project_slug, preview_name)
    config = parse_preview_yml(preview_path)

    # Determine phase based on deployment count
    from app.database import list_deployments
    deploys = await list_deployments(preview["id"], limit=100)
    deploy_count = sum(1 for d in deploys if d.get("type", "deploy") == "deploy")
    phase = "new" if deploy_count <= 1 else "update"

    deploy_path = config["post_deploy"].get(phase)
    if not deploy_path:
        logger.info(f"Post-deploy: no script for phase '{phase}' in {project_slug}/{preview_name}")
        return

    # Create a deployer instance just for post-deploy
    deployer = PreviewDeployer(
        project_name=project_slug,
        preview_name=preview_name,
        branch=preview.get("branch", ""),
        commit_sha=preview.get("commit_sha", ""),
        triggered_by=triggered_by,
        mr_iid=preview.get("mr_id"),
        org_slug=org_slug,
        project_slug=project_slug,
        project_id=project_id,
    )
    deployer._vm_ip = vm_ip
    deployer._vm_id = preview.get("vm_id")
    deployer._preview_config = config

    from app.deployment import RemoteExecutor
    deployer._executor = RemoteExecutor(vm_ip)

    await deployer._run_post_deploy_with_record(phase, deploy_path)


async def task_delete_preview(
    ctx,
    org_slug: str,
    project_slug: str,
    project_id: int,
    preview_name: str,
):
    """Delete a preview. Runs in arq worker.

    This task is the automatic deletion path (enqueued by the MR close/merge
    webhook), so it honours the 'prevent auto erase' (pinned) guard as a
    safety net even if the enqueuer didn't check.
    """
    from app.routes.previews import delete_preview_internal
    from app.database import get_preview
    from app.preview_rules import is_protected_from_auto_delete

    # Maintenance drain: park deletes too, so no infra mutation happens mid-deploy.
    from app.valkey import is_maintenance_active
    if await is_maintenance_active():
        from datetime import timedelta
        logger.info(f"Maintenance active — parking delete {project_slug}/{preview_name} (defer 30s)")
        await ctx["redis"].enqueue_job(
            "task_delete_preview", org_slug, project_slug, project_id, preview_name,
            _defer_by=timedelta(seconds=30), _expires=timedelta(hours=6),
        )
        return

    try:
        preview = await get_preview(project_id, preview_name)
        if is_protected_from_auto_delete(preview):
            logger.info(
                f"Skipping auto-delete for {org_slug}/{project_slug}/{preview_name}: "
                f"preview is pinned (prevent auto erase)"
            )
            return
        await delete_preview_internal(org_slug, project_slug, project_id, preview_name)
    except Exception as e:
        logger.error(f"Failed to delete preview {org_slug}/{project_slug}/{preview_name}: {e}")


# ---- Webhook inbox processing ----

async def task_process_webhook(ctx, inbox_id: int):
    """Route a single stored webhook (see webhooks.dispatch_webhook).

    Never re-raises: on failure the row is reset to 'pending' (or 'failed' after
    repeated attempts) and the safety-net cron re-enqueues it. Deploy/delete jobs
    it enqueues are idempotent (deploy lock + dedup), so at-least-once is safe.
    """
    from app.routes.webhooks import dispatch_webhook
    from app.database import (
        get_webhook, mark_webhook_processing, mark_webhook_done, mark_webhook_pending,
    )

    row = await get_webhook(inbox_id)
    if not row:
        return
    if row["status"] in ("done", "ignored"):
        return  # already handled (duplicate enqueue)

    await mark_webhook_processing(inbox_id)
    try:
        result = await dispatch_webhook(inbox_id, ctx["redis"])
        status = "ignored" if (result or {}).get("status") == "ignored" else "done"
        await mark_webhook_done(inbox_id, status)
    except Exception as e:
        logger.error(f"Webhook #{inbox_id} processing failed: {e}", exc_info=True)
        cur = await get_webhook(inbox_id)
        if cur and cur.get("attempts", 0) >= 5:
            await mark_webhook_done(inbox_id, "failed", error=str(e))
        else:
            await mark_webhook_pending(inbox_id)  # cron will retry


async def task_dispatch_webhook_inbox(ctx):
    """Safety net: re-enqueue webhooks left pending, or stuck 'processing' (a
    worker died mid-flight). Guarantees no webhook is lost even if the direct
    enqueue in the HTTP handler failed. Runs as cron job.
    """
    from datetime import timedelta
    from app.database import get_stuck_webhooks

    stuck = await get_stuck_webhooks(stuck_seconds=300, limit=200)
    if not stuck:
        return
    logger.info(f"Re-enqueuing {len(stuck)} stuck webhook(s)")
    for row in stuck:
        await ctx["redis"].enqueue_job(
            "task_process_webhook", row["id"], _expires=timedelta(hours=3),
        )


# ---- Cron task functions ----

async def _skip_for_maintenance(name: str) -> bool:
    """Return True (and log) if a mutating cron should be skipped during maintenance."""
    from app.valkey import is_maintenance_active
    if await is_maintenance_active():
        logger.info(f"Maintenance active — skipping cron {name}")
        return True
    return False


async def task_auto_erase(ctx):
    """Auto-erase previews after prolonged inactivity. Runs as cron job."""
    if await _skip_for_maintenance("task_auto_erase"):
        return
    from app.tasks.auto_erase import check_and_erase
    await check_and_erase()


async def task_check_vms(ctx):
    """Check cloud VM status and clean up stale entries. Runs as cron job."""
    if await _skip_for_maintenance("task_check_vms"):
        return
    from app.tasks.docker_events import check_vm_status
    await check_vm_status()


async def task_cleanup_orphan_vms(ctx):
    """Destroy Hetzner VMs that have no matching preview in the DB. Runs as cron job."""
    if await _skip_for_maintenance("task_cleanup_orphan_vms"):
        return
    from app.tasks.orphan_vms import cleanup_orphan_vms
    await cleanup_orphan_vms()


async def task_replenish_warm_pool(ctx):
    """Ensure warm pool has enough pre-created VMs ready. Runs as cron job."""
    if await _skip_for_maintenance("task_replenish_warm_pool"):
        return
    from app.tasks.warm_pool import replenish_warm_pool
    await replenish_warm_pool()


async def task_purge_soft_deleted(ctx):
    """Permanently delete soft-deleted previews past the retention window.
    Also purges old processed webhook_inbox rows."""
    if await _skip_for_maintenance("task_purge_soft_deleted"):
        return
    from app.tasks.purge_soft_deleted import purge_soft_deleted
    await purge_soft_deleted()
    try:
        from app.database import purge_processed_webhooks
        n = await purge_processed_webhooks(older_than_days=7)
        if n:
            logger.info(f"Purged {n} processed webhook_inbox row(s)")
    except Exception as e:
        logger.warning(f"Webhook inbox purge failed: {e}")


async def task_rotate_gitlab_tokens(ctx):
    """Rotate org GitLab access tokens nearing expiry. Runs as cron job."""
    if await _skip_for_maintenance("task_rotate_gitlab_tokens"):
        return
    from app.tasks.rotate_gitlab_tokens import rotate_gitlab_tokens
    await rotate_gitlab_tokens()


async def task_reap_stale_deployments(ctx):
    """Safety net: close any deployment stuck in 'running' beyond the job
    timeout (plus margin). Catches zombies left by kill -9 / OOM / a finalize
    that never ran, without waiting for a worker restart. Runs as cron job.
    """
    from app.database import reap_stale_running_deployments, get_long_running_deployments

    # Early warning: surface deploys running unusually long BEFORE they become
    # zombies (normal deploys take ~2 min). This is the canary the incident lacked.
    try:
        for d in await get_long_running_deployments(1800):  # > 30 min
            logger.warning(
                f"dep#{d['id']} ({d.get('preview_name')}) has been running for "
                f"{d['minutes']} min — investigate (normal deploys take ~2 min)"
            )
    except Exception as e:
        logger.warning(f"Long-running deployment check failed: {e}")

    n = await reap_stale_running_deployments(WorkerSettings.job_timeout + 1800)
    if n:
        logger.warning(f"Reaper closed {n} stale 'running' deployment(s)")


async def task_docker_prune(ctx):
    """Remove unused Docker images and build cache. Runs as cron job."""
    if await _skip_for_maintenance("task_docker_prune"):
        return
    import asyncio

    logger.info("Running docker system prune...")
    proc = await asyncio.create_subprocess_exec(
        "docker", "system", "prune", "-f", "--filter", "until=24h",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        output = stdout.decode().strip()
        logger.info(f"Docker prune completed: {output}")
    else:
        logger.warning(f"Docker prune failed: {stderr.decode().strip()}")


# ---- Worker settings ----

class WorkerSettings:
    functions = [task_deploy_preview, task_run_post_deploy, task_delete_preview, task_process_webhook, task_dispatch_webhook_inbox, task_auto_erase, task_check_vms, task_cleanup_orphan_vms, task_replenish_warm_pool, task_purge_soft_deleted, task_docker_prune, task_rotate_gitlab_tokens, task_reap_stale_deployments]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.valkey_url)
    max_jobs = 50
    max_tries = 3
    job_timeout = 36000  # 10 hours — match TIMEOUT_DEPLOY_SCRIPT
    health_check_interval = 30
    cron_jobs = [
        cron(task_auto_erase, hour=None, minute=0),  # Every hour
        cron(task_check_vms, hour=None, minute={0, 15, 30, 45}),  # Every 15 min
        cron(task_cleanup_orphan_vms, hour=None, minute={10, 40}),  # Every 30 min
        cron(task_reap_stale_deployments, hour=None, minute={5, 20, 35, 50}),  # Every 15 min
        cron(task_dispatch_webhook_inbox, second=0),  # Every minute — webhook inbox safety net
        cron(task_replenish_warm_pool, hour=None, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),  # Every 5 min
        cron(task_docker_prune, hour={3}, minute=0),  # Daily at 3 AM
        cron(task_purge_soft_deleted, hour={3}, minute=30),  # Daily at 3:30 AM
        cron(task_rotate_gitlab_tokens, hour={4}, minute=0),  # Daily at 4 AM
    ]
