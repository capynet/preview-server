"""Cloud-resource billing records + CI-gating helpers."""

from datetime import datetime, timezone

from app.database._pool import get_pool, _now, _row_to_dict


async def log_cloud_resource(
    project_id: int, preview_name: str,
    resource_type: str, resource_id: int, resource_name: str,
    spec: str = "{}", price_hourly: float = 0, price_monthly: float = 0,
) -> int:
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO cloud_resources
           (project_id, preview_name, resource_type, resource_id, resource_name,
            spec, price_hourly, price_monthly, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
           RETURNING id""",
        project_id, preview_name, resource_type, resource_id, resource_name,
        spec, price_hourly, price_monthly, _now(),
    )
    return row["id"]


async def get_effective_require_ci(org_id: int, project_id: int) -> bool:
    """Resolve effective require_ci_success: project overrides org if not null."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT p.require_ci_success AS project_ci,
                  o.require_ci_success AS org_ci
           FROM projects p
           JOIN organizations o ON p.organization_id = o.id
           WHERE p.id = $1 AND o.id = $2""",
        project_id, org_id,
    )
    if not row:
        return False
    if row["project_ci"] is not None:
        return bool(row["project_ci"])
    return bool(row["org_ci"])


async def get_previews_waiting_for_ci(project_id: int, branch: str) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT * FROM previews WHERE project_id = $1 AND branch = $2
           AND ci_status = 'waiting' AND deleted_at IS NULL""",
        project_id, branch,
    )
    return [_row_to_dict(r) for r in rows]


async def update_preview_ci_status(
    project_id: int, preview_name: str, ci_status: str,
    status: str | None = None, error: str | None = None,
):
    pool = await get_pool()
    sets = ["ci_status = $3"]
    vals: list = [project_id, preview_name, ci_status]
    idx = 4
    if status:
        sets.append(f"status = ${idx}")
        vals.append(status)
        idx += 1
    if error:
        sets.append(f"last_deployment_error = ${idx}")
        vals.append(error)
        idx += 1
    await pool.execute(
        f"UPDATE previews SET {', '.join(sets)} WHERE project_id = $1 AND preview_name = $2",
        *vals,
    )


async def update_org_require_ci(org_id: int, enabled: bool):
    pool = await get_pool()
    await pool.execute(
        "UPDATE organizations SET require_ci_success = $2 WHERE id = $1",
        org_id, 1 if enabled else 0,
    )


async def update_project_require_ci(org_id: int, project_slug: str, value):
    """value: None (inherit), True, or False"""
    pool = await get_pool()
    db_val = None if value is None else (1 if value else 0)
    await pool.execute(
        "UPDATE projects SET require_ci_success = $3 WHERE organization_id = $1 AND slug = $2",
        org_id, project_slug, db_val,
    )


async def finish_cloud_resource(resource_type: str, resource_id: int) -> None:
    pool = await get_pool()
    now = _now()
    row = await pool.fetchrow(
        """SELECT id, created_at FROM cloud_resources
           WHERE resource_type = $1 AND resource_id = $2 AND destroyed_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        resource_type, resource_id,
    )
    if row:
        created = datetime.fromisoformat(row["created_at"])
        duration = int((datetime.now(timezone.utc) - created).total_seconds())
        await pool.execute(
            """UPDATE cloud_resources
               SET destroyed_at = $1, duration_seconds = $2
               WHERE id = $3""",
            now, duration, row["id"],
        )
