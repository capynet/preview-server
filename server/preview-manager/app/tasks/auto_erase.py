"""Background task: auto-erase previews after prolonged inactivity.

For cloud previews: destroy VM (if any) + delete volume + clean DB.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.database import get_all_previews, list_organizations

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 3600  # 1 hour


async def auto_erase_loop():
    """Run every CHECK_INTERVAL_SECONDS, deleting previews inactive for too long."""
    await asyncio.sleep(60)
    logger.info("Auto-erase background task started")

    while True:
        try:
            await _check_and_erase()
        except Exception as e:
            logger.error(f"Auto-erase loop error: {e}", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _check_and_erase():
    """Check all previews and delete those that exceed the inactivity threshold."""
    orgs = await list_organizations()
    for org in orgs:
        if not org.get("auto_erase_enabled"):
            continue

        org_days = org.get("auto_erase_days") or 7

        previews = await get_all_previews(org_id=org["id"])
        if not previews:
            continue

        now = datetime.now(timezone.utc)
        erased_count = 0

        for p in previews:
            preview_name = p["preview_name"]
            project_slug = p.get("project_slug", "")
            org_slug = p.get("org_slug", org["slug"])
            project_id = p.get("project_id")

            # Skip pinned previews
            if p.get("pinned"):
                continue

            # Use last_accessed_at, fallback to created_at for never-accessed previews
            ts = p.get("last_accessed_at") or p.get("created_at")
            if not ts:
                continue
            try:
                last_activity = datetime.fromisoformat(ts)
            except ValueError:
                continue

            idle_days = (now - last_activity).total_seconds() / 86400
            if idle_days < org_days:
                continue

            logger.info(
                f"Auto-erasing {org_slug}/{project_slug}/{preview_name}: "
                f"idle for {idle_days:.1f} days (threshold: {org_days} days)"
            )
            try:
                from app.routes.previews import delete_preview_internal
                await delete_preview_internal(org_slug, project_slug, project_id, preview_name)
                erased_count += 1
                logger.info(f"Auto-erased {org_slug}/{project_slug}/{preview_name}")
            except Exception as e:
                logger.error(f"Failed to auto-erase {project_slug}/{preview_name}: {e}")

        if erased_count:
            logger.info(f"Auto-erase: deleted {erased_count} preview(s) in org '{org['slug']}'")
