"""Warm pool — keep pre-created VMs ready for instant assignment.

Called as an arq cron job (every 5 minutes). Ensures warm_pool_size VMs
are always available. After a deploy claims a pool VM, this task
replenishes it automatically.
"""

import logging

from config.settings import settings

logger = logging.getLogger(__name__)


async def replenish_warm_pool():
    """Ensure the warm pool has enough VMs ready."""
    if settings.warm_pool_size <= 0:
        return

    from app.cloud import cloud_manager

    try:
        pool_vms = await cloud_manager.get_pool_vms()
    except Exception as e:
        logger.error(f"Failed to list pool VMs: {e}")
        return

    current = len(pool_vms)
    needed = settings.warm_pool_size - current

    if needed <= 0:
        logger.info("Warm pool OK: %d/%d VMs ready", current, settings.warm_pool_size)
        return

    logger.info("Warm pool: %d/%d — creating %d VM(s)", current, settings.warm_pool_size, needed)

    for i in range(needed):
        try:
            await cloud_manager.create_pool_vm()
        except Exception as e:
            logger.error(f"Failed to create pool VM ({i+1}/{needed}): {e}")
            break
