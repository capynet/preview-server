"""Permanently remove soft-deleted previews after the retention window.

Runs once a day via arq cron. Rows with ``deleted_at`` older than
``settings.soft_delete_retention_days`` get hard-deleted. Associated
``deployments`` rows cascade-delete via the FK constraint.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.database import get_pool
from config.settings import settings

logger = logging.getLogger(__name__)


async def purge_soft_deleted():
    retention_days = settings.soft_delete_retention_days
    if retention_days <= 0:
        logger.info("Soft-delete purge disabled (retention_days <= 0)")
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    pool = await get_pool()

    rows = await pool.fetch(
        """SELECT id, preview_name, project_id, deleted_at
           FROM previews
           WHERE deleted_at IS NOT NULL AND deleted_at < $1""",
        cutoff,
    )

    if not rows:
        logger.info(f"Soft-delete purge: nothing to remove (cutoff={cutoff})")
        return

    result = await pool.execute(
        """DELETE FROM previews
           WHERE deleted_at IS NOT NULL AND deleted_at < $1""",
        cutoff,
    )
    deleted_count = int(result.split()[-1]) if result.startswith("DELETE") else len(rows)

    logger.info(
        f"Soft-delete purge: permanently removed {deleted_count} preview(s) "
        f"older than {retention_days} days (cutoff={cutoff})"
    )
    for r in rows:
        logger.info(
            f"  - hard-deleted preview_id={r['id']} name={r['preview_name']} "
            f"project_id={r['project_id']} deleted_at={r['deleted_at']}"
        )
