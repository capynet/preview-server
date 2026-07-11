"""Webhook inbox — durable at-least-once storage for incoming GitLab webhooks.

The HTTP handler persists the raw webhook here (and returns 200) BEFORE doing any
routing work, so a control-plane restart or a Valkey blip can never drop a webhook
in the limbo between "received" and "enqueued". A worker task drains the inbox and
a safety-net cron re-enqueues rows that got stuck.

status: pending -> processing -> done | ignored | failed
"""

from app.database._pool import get_pool, _now, _row_to_dict


async def insert_webhook(
    org_slug: str,
    event: str | None,
    gitlab_id: int | None,
    delivery_id: str | None,
    payload: str,
) -> int | None:
    """Persist a raw webhook. Returns the new row id, or None if it was a
    duplicate delivery (idempotent on GitLab's X-Gitlab-Event-UUID).

    `payload` is the raw JSON body as a string (stored into a JSONB column).
    """
    pool = await get_pool()
    # The unique index on delivery_id is PARTIAL (WHERE delivery_id IS NOT NULL),
    # so the ON CONFLICT arbiter must repeat that predicate to be inferred. Rows
    # with a NULL delivery_id are not covered and always insert.
    row = await pool.fetchrow(
        """INSERT INTO webhook_inbox (org_slug, event, gitlab_id, delivery_id, payload, status, received_at)
           VALUES ($1, $2, $3, $4, $5::jsonb, 'pending', $6)
           ON CONFLICT (delivery_id) WHERE delivery_id IS NOT NULL DO NOTHING
           RETURNING id""",
        org_slug, event, gitlab_id, delivery_id, payload, _now(),
    )
    return row["id"] if row else None


async def get_webhook(inbox_id: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, org_slug, event, gitlab_id, delivery_id, payload, status, attempts FROM webhook_inbox WHERE id = $1",
        inbox_id,
    )
    return _row_to_dict(row) if row else None


async def mark_webhook_processing(inbox_id: int):
    """Move a row to 'processing' and bump its attempt counter."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE webhook_inbox SET status = 'processing', attempts = attempts + 1 WHERE id = $1",
        inbox_id,
    )


async def mark_webhook_done(inbox_id: int, status: str = "done", error: str | None = None):
    """Close a row. status ∈ {done, ignored, failed}."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE webhook_inbox SET status = $1, error = $2, processed_at = $3 WHERE id = $4",
        status, error, _now(), inbox_id,
    )


async def mark_webhook_pending(inbox_id: int):
    """Reset a row to 'pending' so the safety-net cron retries it."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE webhook_inbox SET status = 'pending' WHERE id = $1",
        inbox_id,
    )


async def get_stuck_webhooks(stuck_seconds: int, limit: int = 100) -> list[dict]:
    """Return webhooks still 'pending', or 'processing' for longer than
    stuck_seconds (a worker died mid-processing). Used by the safety-net cron.

    received_at is a TEXT ISO-8601 UTC string; the threshold is computed in
    Python and compared lexicographically (valid because the format is fixed).
    """
    from datetime import datetime, timezone, timedelta
    threshold = (datetime.now(timezone.utc) - timedelta(seconds=stuck_seconds)).isoformat()
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id FROM webhook_inbox
           WHERE status = 'pending'
              OR (status = 'processing' AND received_at < $1)
           ORDER BY id ASC
           LIMIT $2""",
        threshold, limit,
    )
    return [_row_to_dict(r) for r in rows]


async def purge_processed_webhooks(older_than_days: int) -> int:
    """Delete closed webhook rows older than the retention window."""
    from datetime import datetime, timezone, timedelta
    threshold = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    pool = await get_pool()
    result = await pool.execute(
        """DELETE FROM webhook_inbox
           WHERE status IN ('done', 'ignored')
             AND processed_at < $1""",
        threshold,
    )
    # asyncpg returns e.g. "DELETE 5"
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0
