"""Deployment CRUD + zombie recovery/reaping helpers."""

from datetime import datetime, timezone
from typing import Optional

from app.database._pool import get_pool, _now, _row_to_dict


async def get_running_deployment(preview_id: int):
    """Return the most recent running deployment for a preview, or None."""
    pool = await get_pool()
    return await pool.fetchrow(
        """SELECT id, preview_id, status, triggered_by, started_at
           FROM deployments
           WHERE preview_id = $1 AND status = 'running'
           ORDER BY id DESC LIMIT 1""",
        preview_id,
    )


async def create_deployment(preview_id: int, triggered_by: str | None = None, deploy_type: str = "deploy") -> int:
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO deployments (preview_id, status, triggered_by, started_at, type)
           VALUES ($1, 'running', $2, $3, $4)
           RETURNING id""",
        preview_id, triggered_by, _now(), deploy_type,
    )
    return row["id"]


async def update_deployment_status(deployment_id: int, status: str):
    """Update only the status of a deployment (without closing it)."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE deployments SET status = $1 WHERE id = $2",
        status, deployment_id,
    )


async def finish_deployment(
    deployment_id: int, status: str,
    log_output: str = "", error: str | None = None,
    duration: int | None = None,
    phases: str | None = None,
):
    pool = await get_pool()
    await pool.execute(
        """UPDATE deployments
           SET status = $1, log_output = $2, error = $3, duration = $4, completed_at = $5, phases = $6
           WHERE id = $7""",
        status, log_output, error, duration, _now(), phases, deployment_id,
    )


async def fail_running_deployments(project_id: int, preview_name: str, error: str) -> int:
    """Mark every 'running' deployment of a preview as failed, by deployment id.

    Unlike the previous recovery path, this does NOT require the preview row to
    still exist (it may be soft-deleted): it matches deployments directly, so an
    interrupted deploy can never leave its row stuck in 'running' just because
    the preview was deleted out from under it. Preserves log_output/phases.
    Returns the number of rows closed.
    """
    pool = await get_pool()
    result = await pool.execute(
        """UPDATE deployments
           SET status = 'failed', completed_at = $4,
               error = CASE WHEN coalesce(error,'') = '' THEN $3
                            ELSE error || ' | ' || $3 END
           WHERE status = 'running'
             AND preview_id IN (
                 SELECT id FROM previews WHERE project_id = $1 AND preview_name = $2
             )""",
        project_id, preview_name, error, _now(),
    )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def reap_stale_running_deployments(max_age_seconds: int) -> int:
    """Safety-net reaper: close any deployment stuck in 'running' beyond
    ``max_age_seconds``, so a hard-killed/OOM'd job never leaves a permanent
    zombie. Casts the TEXT-stored ``started_at`` to timestamptz for a reliable
    age comparison. Preserves log_output/phases. Returns rows closed.
    """
    note = "Reaped: deployment stuck in 'running' beyond max runtime"
    pool = await get_pool()
    result = await pool.execute(
        """UPDATE deployments
           SET status = 'failed', completed_at = $2,
               error = CASE WHEN coalesce(error,'') = '' THEN $3
                            ELSE error || ' | ' || $3 END
           WHERE status = 'running'
             AND started_at::timestamptz < now() - make_interval(secs => $1)""",
        max_age_seconds, _now(), note,
    )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def get_long_running_deployments(min_age_seconds: int) -> list[dict]:
    """Return 'running' deployments older than ``min_age_seconds`` for early-warning
    logging (before the reaper hard-closes them). Read-only. Each row carries the
    age in whole minutes as ``minutes``.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT d.id, d.preview_id, pr.preview_name,
                  floor(extract(epoch FROM (now() - d.started_at::timestamptz)) / 60)::int AS minutes
           FROM deployments d
           LEFT JOIN previews pr ON pr.id = d.preview_id
           WHERE d.status = 'running'
             AND d.started_at::timestamptz < now() - make_interval(secs => $1)
           ORDER BY d.started_at""",
        min_age_seconds,
    )
    return [_row_to_dict(r) for r in rows]


async def get_all_running_deployments(min_age_seconds: int = 30) -> list[dict]:
    """Get deployments stuck in 'running' status, joined with preview/project/org data.

    Only returns deployments that have been running for at least min_age_seconds,
    to avoid interfering with arq's own retry mechanism for recently cancelled tasks.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)).isoformat()
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT d.id AS deployment_id, d.preview_id, d.triggered_by, d.type,
                  pr.preview_name, pr.branch, pr.commit_sha, pr.mr_id, pr.mr_title,
                  pr.project_id,
                  p.slug AS project_slug, p.gitlab_project_path,
                  o.id AS org_id, o.slug AS org_slug
           FROM deployments d
           JOIN previews pr ON d.preview_id = pr.id
           JOIN projects p ON pr.project_id = p.id
           JOIN organizations o ON p.organization_id = o.id
           WHERE d.status = 'running'
             AND d.started_at < $1""",
        cutoff,
    )
    return [_row_to_dict(r) for r in rows]


async def get_deployment(deployment_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM deployments WHERE id = $1", deployment_id)
    return _row_to_dict(row) if row else None


async def list_deployments(preview_id: int, limit: int = 50) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, preview_id, status, error, triggered_by,
                  started_at, completed_at, duration, type, phases
           FROM deployments
           WHERE preview_id = $1
           ORDER BY started_at DESC
           LIMIT $2""",
        preview_id, limit,
    )
    return [_row_to_dict(r) for r in rows]
