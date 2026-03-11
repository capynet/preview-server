"""arq worker definitions — background task processing via Valkey.

Handles deploys, deletes, and cron jobs (auto-erase, VM health checks).
All tasks use Valkey-backed log broadcasting for cross-process streaming.
"""

import logging
from arq import cron
from arq.connections import RedisSettings

from config.settings import settings

logger = logging.getLogger(__name__)


async def startup(ctx):
    """Called when the worker starts."""
    from app.database import init_pool
    from app.valkey import init_valkey
    await init_pool()
    await init_valkey()
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
):
    """Deploy a preview. Runs in arq worker with Valkey-backed log streaming."""
    from app.routes.webhooks import _clone_and_deploy
    await _clone_and_deploy(
        org_id, org_slug, project_id, project_slug, project_path,
        preview_name, source_branch, commit_sha, triggered_by,
        mr_iid, force_new, mr_title,
    )


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


# ---- Worker settings ----

class WorkerSettings:
    functions = [task_deploy_preview, task_delete_preview, task_auto_erase, task_check_vms, task_cleanup_orphan_vms]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.valkey_url)
    max_jobs = 5
    job_timeout = 3600  # 1 hour max per job
    health_check_interval = 30
    cron_jobs = [
        cron(task_auto_erase, hour=None, minute=0),  # Every hour
        cron(task_check_vms, hour=None, minute={0, 15, 30, 45}),  # Every 15 min
        cron(task_cleanup_orphan_vms, hour=None, minute={10, 40}),  # Every 30 min
    ]
