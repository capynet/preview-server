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
    from app.database import get_preview, get_running_deployment, finish_deployment
    from app.state import PreviewStateManager
    from app.valkey import release_deploy_lock

    deploy_key = f"{project_slug}/{preview_name}"

    # Mark running deployment as failed and reset preview status
    try:
        preview = await get_preview(project_id, preview_name)
        if preview:
            running = await get_running_deployment(preview["id"])
            if running:
                await finish_deployment(
                    running["id"], "failed",
                    error=f"{reason} — will retry",
                )
                logger.info(f"Marked deployment #{running['id']} as failed for {deploy_key}")

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
    """Startup recovery: find deployments left 'running' from a previous worker and re-enqueue them."""
    from app.database import get_all_running_deployments, finish_deployment
    from app.state import PreviewStateManager
    from app.valkey import release_deploy_lock
    from arq import create_pool as create_arq_pool

    running = await get_all_running_deployments()
    if not running:
        logger.info("No interrupted deployments to recover")
        return

    logger.info(f"Found {len(running)} interrupted deployment(s) — recovering")
    arq_pool = await create_arq_pool(RedisSettings.from_dsn(settings.valkey_url))

    for dep in running:
        dep_id = dep["deployment_id"]
        preview_name = dep["preview_name"]
        project_slug = dep["project_slug"]
        deploy_key = f"{project_slug}/{preview_name}"
        dep_type = dep.get("type", "deploy")

        # Mark deployment as failed
        await finish_deployment(dep_id, "failed", error="Interrupted by worker restart")
        logger.info(f"Marked deployment #{dep_id} as failed ({deploy_key})")

        # Reset preview status so it's not stuck in 'creating'
        await PreviewStateManager.save_state(
            dep["project_id"], preview_name,
            status="failed",
            last_deployment_status="failed",
            last_deployment_error="Interrupted by worker restart — re-deploying",
        )

        # Only re-enqueue deploy-type tasks; others just mark as failed
        if dep_type != "deploy":
            continue

        if not dep.get("gitlab_project_path"):
            logger.warning(f"Cannot re-enqueue {deploy_key}: no gitlab_project_path")
            continue

        # Release stale deploy lock
        try:
            await release_deploy_lock(deploy_key)
        except Exception:
            pass

        # Re-enqueue
        await arq_pool.enqueue_job(
            "task_deploy_preview",
            dep["org_id"], dep["org_slug"],
            dep["project_id"], dep["project_slug"],
            dep["gitlab_project_path"],
            preview_name, dep["branch"], dep["commit_sha"],
            "recovery",
            dep["mr_id"],
            False,  # force_new
            dep.get("mr_title"),
        )
        logger.info(f"Re-enqueued {deploy_key} for recovery")

    await arq_pool.aclose()
    logger.info(f"Recovery complete: {len(running)} deployment(s) processed")


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
    from app.routes.webhooks import _clone_and_deploy
    try:
        await _clone_and_deploy(
            org_id, org_slug, project_id, project_slug, project_path,
            preview_name, source_branch, commit_sha, triggered_by,
            mr_iid, force_new, mr_title, target_branch,
        )
    except asyncio.CancelledError:
        logger.warning(f"Deploy {project_slug}/{preview_name} cancelled (worker shutdown) — re-enqueueing")
        try:
            await asyncio.shield(_handle_interrupted_deploy(
                org_id, org_slug, project_id, project_slug, project_path,
                preview_name, source_branch, commit_sha,
                mr_iid, force_new, mr_title,
            ))
        except asyncio.CancelledError:
            pass  # shield was cancelled but inner task continues
        raise


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

    # Parse preview.yml to get post_deploy config
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
    """Delete a preview. Runs in arq worker."""
    from app.routes.previews import delete_preview_internal
    try:
        await delete_preview_internal(org_slug, project_slug, project_id, preview_name)
    except Exception as e:
        logger.error(f"Failed to delete preview {org_slug}/{project_slug}/{preview_name}: {e}")


# ---- Cron task functions ----

async def task_auto_erase(ctx):
    """Auto-erase previews after prolonged inactivity. Runs as cron job."""
    from app.tasks.auto_erase import check_and_erase
    await check_and_erase()


async def task_check_vms(ctx):
    """Check cloud VM status and clean up stale entries. Runs as cron job."""
    from app.tasks.docker_events import check_vm_status
    await check_vm_status()


async def task_cleanup_orphan_vms(ctx):
    """Destroy Hetzner VMs that have no matching preview in the DB. Runs as cron job."""
    from app.tasks.orphan_vms import cleanup_orphan_vms
    await cleanup_orphan_vms()


async def task_replenish_warm_pool(ctx):
    """Ensure warm pool has enough pre-created VMs ready. Runs as cron job."""
    from app.tasks.warm_pool import replenish_warm_pool
    await replenish_warm_pool()


async def task_docker_prune(ctx):
    """Remove unused Docker images and build cache. Runs as cron job."""
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
    functions = [task_deploy_preview, task_run_post_deploy, task_delete_preview, task_auto_erase, task_check_vms, task_cleanup_orphan_vms, task_replenish_warm_pool, task_docker_prune]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.valkey_url)
    max_jobs = 50
    job_timeout = 36000  # 10 hours — match TIMEOUT_DEPLOY_SCRIPT
    health_check_interval = 30
    cron_jobs = [
        cron(task_auto_erase, hour=None, minute=0),  # Every hour
        cron(task_check_vms, hour=None, minute={0, 15, 30, 45}),  # Every 15 min
        cron(task_cleanup_orphan_vms, hour=None, minute={10, 40}),  # Every 30 min
        cron(task_replenish_warm_pool, hour=None, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),  # Every 5 min
        cron(task_docker_prune, hour={3}, minute=0),  # Daily at 3 AM
    ]
