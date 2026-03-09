"""Background task: auto-stop previews after inactivity.

For cloud previews: shutdown the VM (keeps disk intact).
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.database import (
    get_all_previews, get_project, has_running_deployment,
    list_organizations, compute_url_hash,
)
from app.cloud import cloud_manager

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60  # 1 minute


async def auto_stop_loop():
    """Run every CHECK_INTERVAL_SECONDS, stopping idle previews."""
    await asyncio.sleep(30)
    logger.info("Auto-stop background task started")

    while True:
        try:
            await _check_and_stop()
        except Exception as e:
            logger.error(f"Auto-stop loop error: {e}", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _check_and_stop():
    """Check all previews and stop those that exceed their inactivity threshold."""
    # Iterate over all organizations
    orgs = await list_organizations()
    for org in orgs:
        if not org.get("auto_stop_enabled"):
            continue

        org_minutes = org.get("auto_stop_minutes") or 60

        previews = await get_all_previews(org_id=org["id"])
        if not previews:
            continue

        now = datetime.now(timezone.utc)
        stopped_count = 0

        for p in previews:
            preview_name = p["preview_name"]
            project_slug = p.get("project_slug", "")
            org_slug = p.get("org_slug", org["slug"])

            # Only stop previews that have an active VM
            if not p.get("vm_id"):
                continue

            # Check per-project override
            project_id = p.get("project_id")
            if project_id:
                proj = await get_project(project_id)
                if proj and proj.get("auto_stop_enabled") is not None:
                    if not proj["auto_stop_enabled"]:
                        continue
                    threshold_minutes = proj.get("auto_stop_minutes") or org_minutes
                else:
                    threshold_minutes = org_minutes
            else:
                threshold_minutes = org_minutes

            # Determine last activity
            last_accessed = p.get("last_accessed_at")
            last_deployed = p.get("last_deployed_at")

            last_activity = None
            for ts in (last_accessed, last_deployed):
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts)
                        if last_activity is None or dt > last_activity:
                            last_activity = dt
                    except ValueError:
                        pass

            if not last_activity:
                continue

            idle_seconds = (now - last_activity).total_seconds()
            if idle_seconds < threshold_minutes * 60:
                continue

            # Skip if there's an active deployment
            if await has_running_deployment(p["id"]):
                continue

            # Shutdown VM (keeps disk intact)
            logger.info(
                f"Auto-stopping {org_slug}/{project_slug}/{preview_name}: "
                f"idle for {int(idle_seconds / 60)} min (threshold: {threshold_minutes} min)"
            )
            try:
                # Remove Caddy direct route so wildcard handles wake-up
                from app.caddy_api import caddy_manager
                url_hash = compute_url_hash(org_slug, project_slug, preview_name)
                domain = f"{url_hash}.mr.preview-mr.com"
                await caddy_manager.remove_preview_route(domain)

                await cloud_manager.shutdown_vm(p["vm_id"])
                stopped_count += 1
                logger.info(f"Auto-stopped {org_slug}/{project_slug}/{preview_name} (VM shutdown)")
            except Exception as e:
                logger.error(f"Failed to auto-stop {project_slug}/{preview_name}: {e}")

        if stopped_count:
            logger.info(f"Auto-stop: stopped {stopped_count} preview(s) in org '{org['slug']}'")
