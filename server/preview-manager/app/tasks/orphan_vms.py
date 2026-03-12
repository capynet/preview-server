"""Clean up orphaned Hetzner VMs that have no matching preview in the database.

Called as an arq cron job (every 30 minutes).
"""

import logging
from datetime import datetime, timezone, timedelta

from app.database import get_previews_with_active_vms

logger = logging.getLogger(__name__)

# Ignore VMs created less than this many minutes ago to avoid race conditions
# with deploys that are still in progress.
GRACE_PERIOD_MINUTES = 10


async def cleanup_orphan_vms():
    """Find VMs in Hetzner with prev- prefix that don't match any preview in the DB, and destroy them."""
    from app.cloud import cloud_manager

    # Get all VMs from Hetzner
    try:
        hetzner_vms = await cloud_manager.get_active_vms()
    except Exception as e:
        logger.error(f"Failed to list Hetzner VMs: {e}")
        return

    if not hetzner_vms:
        return

    # Get all VM IDs tracked in the database
    db_previews = await get_previews_with_active_vms()
    db_vm_ids = {p["vm_id"] for p in db_previews if p.get("vm_id")}

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=GRACE_PERIOD_MINUTES)

    orphans = [
        vm for vm in hetzner_vms
        if vm.data_model.id not in db_vm_ids
        and vm.data_model.created < cutoff
    ]

    if not orphans:
        logger.debug("No orphan VMs found")
        return

    logger.info(f"Found {len(orphans)} orphan VM(s) to clean up")

    for vm in orphans:
        vm_id = vm.data_model.id
        vm_name = vm.data_model.name
        vm_ip = vm.data_model.public_net.ipv4.ip
        try:
            await cloud_manager.destroy_vm(vm_id)
            logger.info(f"Destroyed orphan VM: {vm_name} (id={vm_id}, ip={vm_ip})")
        except Exception as e:
            logger.error(f"Failed to destroy orphan VM {vm_name} (id={vm_id}): {e}")
