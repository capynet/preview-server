"""Warm pool — keep pre-created VMs ready for instant assignment.

Called as an arq cron job (every 5 minutes). Ensures warm_pool_size VMs
are always available. After a deploy claims a pool VM, this task
replenishes it automatically.

Uses a Valkey lock to prevent concurrent executions (cron + deploy trigger).
"""

import logging

from config.settings import settings

logger = logging.getLogger(__name__)

LOCK_KEY = "lock:replenish_warm_pool"
LOCK_TTL = 300  # 5 minutes — matches cron interval


async def replenish_warm_pool():
    """Ensure the warm pool has enough VMs ready."""
    if settings.warm_pool_size <= 0:
        return

    from app.valkey import get_valkey
    valkey = await get_valkey()

    # Acquire lock — skip if another instance is already running
    acquired = await valkey.set(LOCK_KEY, "1", nx=True, ex=LOCK_TTL)
    if not acquired:
        logger.debug("Warm pool replenishment already in progress, skipping")
        return

    try:
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
    finally:
        await valkey.delete(LOCK_KEY)
