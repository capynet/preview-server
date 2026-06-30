"""Preview CRUD."""

from typing import Optional

from config.settings import settings
from app.database._pool import get_pool, _now, _row_to_dict


async def get_preview(project_id: int, preview_name: str, include_deleted: bool = False) -> Optional[dict]:
    pool = await get_pool()
    deleted_filter = "" if include_deleted else " AND deleted_at IS NULL"
    row = await pool.fetchrow(
        f"SELECT * FROM previews WHERE project_id = $1 AND preview_name = $2{deleted_filter}",
        project_id, preview_name,
    )
    return _row_to_dict(row) if row else None


async def get_preview_by_id(preview_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM previews WHERE id = $1", preview_id)
    return _row_to_dict(row) if row else None


async def get_preview_by_hash(url_hash: str, include_deleted: bool = False) -> Optional[dict]:
    """Find a preview by its URL hash (for domain resolution).

    Soft-deleted previews are excluded unless ``include_deleted=True`` is passed
    — the wake-preview middleware uses that to detect resurrection opportunities.
    """
    pool = await get_pool()
    deleted_filter = "" if include_deleted else " AND p.deleted_at IS NULL"
    row = await pool.fetchrow(
        f"""SELECT p.*, proj.slug as project_slug, proj.organization_id,
                  o.slug as org_slug
           FROM previews p
           JOIN projects proj ON p.project_id = proj.id
           JOIN organizations o ON proj.organization_id = o.id
           WHERE p.url_hash = $1{deleted_filter}""",
        url_hash,
    )
    return _row_to_dict(row) if row else None


async def get_preview_by_domain(domain: str, include_deleted: bool = False) -> Optional[dict]:
    """Find a preview by its domain (e.g. 'a3f8b2c1.{preview_domain}')."""
    import re
    escaped_domain = re.escape(settings.preview_domain)
    match = re.match(rf"^(.+?)\.{escaped_domain}$", domain)
    if not match:
        return None
    subdomain = match.group(1)
    if "--" in subdomain:
        subdomain = subdomain.split("--")[-1]
    return await get_preview_by_hash(subdomain, include_deleted=include_deleted)


async def get_preview_by_branch(project_id: int, branch: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT * FROM previews WHERE project_id = $1 AND branch = $2
           AND preview_name LIKE 'branch-%' AND deleted_at IS NULL""",
        project_id, branch,
    )
    return _row_to_dict(row) if row else None


async def get_all_previews(org_id: Optional[int] = None) -> list[dict]:
    pool = await get_pool()
    if org_id:
        rows = await pool.fetch(
            """SELECT p.*, proj.slug as project_slug, proj.organization_id,
                      o.slug as org_slug,
                      d.id AS latest_deployment_id,
                      d.status AS latest_deployment_status,
                      d.completed_at AS latest_deployment_completed_at,
                      pd.id AS latest_post_deploy_id,
                      pd.status AS latest_post_deploy_status
               FROM previews p
               JOIN projects proj ON p.project_id = proj.id
               JOIN organizations o ON proj.organization_id = o.id
               LEFT JOIN LATERAL (
                   SELECT d2.id, d2.status, d2.completed_at
                   FROM deployments d2 WHERE d2.preview_id = p.id AND d2.type = 'deploy'
                   ORDER BY d2.id DESC LIMIT 1
               ) d ON true
               LEFT JOIN LATERAL (
                   SELECT d3.id, d3.status
                   FROM deployments d3 WHERE d3.preview_id = p.id AND d3.type = 'post_deploy'
                   ORDER BY d3.id DESC LIMIT 1
               ) pd ON true
               WHERE proj.organization_id = $1 AND p.deleted_at IS NULL
               ORDER BY p.created_at DESC""",
            org_id,
        )
    else:
        rows = await pool.fetch(
            """SELECT p.*, proj.slug as project_slug, proj.organization_id,
                      o.slug as org_slug,
                      d.id AS latest_deployment_id,
                      d.status AS latest_deployment_status,
                      d.completed_at AS latest_deployment_completed_at,
                      pd.id AS latest_post_deploy_id,
                      pd.status AS latest_post_deploy_status
               FROM previews p
               JOIN projects proj ON p.project_id = proj.id
               JOIN organizations o ON proj.organization_id = o.id
               LEFT JOIN LATERAL (
                   SELECT d2.id, d2.status, d2.completed_at
                   FROM deployments d2 WHERE d2.preview_id = p.id AND d2.type = 'deploy'
                   ORDER BY d2.id DESC LIMIT 1
               ) d ON true
               LEFT JOIN LATERAL (
                   SELECT d3.id, d3.status
                   FROM deployments d3 WHERE d3.preview_id = p.id AND d3.type = 'post_deploy'
                   ORDER BY d3.id DESC LIMIT 1
               ) pd ON true
               WHERE p.deleted_at IS NULL
               ORDER BY p.created_at DESC"""
        )
    return [_row_to_dict(r) for r in rows]


async def upsert_preview(project_id: int, preview_name: str, **fields) -> dict:
    pool = await get_pool()
    # Intentionally match soft-deleted rows too — upserting a previously
    # erased preview restores it in place.
    existing = await pool.fetchrow(
        "SELECT * FROM previews WHERE project_id = $1 AND preview_name = $2",
        project_id, preview_name,
    )

    if existing:
        # Any re-creation path (webhook, manual, resurrect) clears deleted_at
        # so the row becomes active again.
        fields = {**fields, "deleted_at": None}
        sets = []
        vals = []
        idx = 1
        for k, v in fields.items():
            sets.append(f"{k} = ${idx}")
            vals.append(v)
            idx += 1
        vals.extend([project_id, preview_name])
        await pool.execute(
            f"UPDATE previews SET {', '.join(sets)} WHERE project_id = ${idx} AND preview_name = ${idx + 1}",
            *vals,
        )
        row = await pool.fetchrow(
            "SELECT * FROM previews WHERE project_id = $1 AND preview_name = $2",
            project_id, preview_name,
        )
        return _row_to_dict(row)
    else:
        now = _now()
        row = await pool.fetchrow(
            """INSERT INTO previews
               (project_id, preview_name, url_hash, mr_id, mr_title, branch, commit_sha, status, url, path,
                created_at, last_deployed_at,
                last_deployment_status, last_deployment_error,
                last_deployment_duration, last_deployment_completed_at,
                auto_update, pinned, env_vars, stack_info, domain_aliases, ci_status, cron_jobs)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23)
               RETURNING *""",
            project_id,
            preview_name,
            fields.get("url_hash", ""),
            fields.get("mr_id"),
            fields.get("mr_title"),
            fields.get("branch", "unknown"),
            fields.get("commit_sha", ""),
            fields.get("status", "creating"),
            fields.get("url", ""),
            fields.get("path", ""),
            fields.get("created_at", now),
            fields.get("last_deployed_at"),
            fields.get("last_deployment_status"),
            fields.get("last_deployment_error"),
            fields.get("last_deployment_duration"),
            fields.get("last_deployment_completed_at"),
            fields.get("auto_update", 1),
            fields.get("pinned", 0),
            fields.get("env_vars", "{}"),
            fields.get("stack_info"),
            fields.get("domain_aliases"),
            fields.get("ci_status"),
            fields.get("cron_jobs", "[]"),
        )
        return _row_to_dict(row)


async def delete_preview_from_db(project_id: int, preview_name: str):
    """Soft-delete a preview.

    Flags the row with ``deleted_at`` and clears VM references so the
    resurrect-from-URL middleware can match the erased preview later and
    offer to rebuild it. Actual row removal is reserved for purge jobs.
    """
    pool = await get_pool()
    await pool.execute(
        """UPDATE previews
           SET deleted_at = $1,
               vm_id = NULL,
               vm_ip = NULL,
               status = 'deleted'
           WHERE project_id = $2 AND preview_name = $3
             AND deleted_at IS NULL""",
        _now(), project_id, preview_name,
    )


async def hard_delete_preview_from_db(project_id: int, preview_name: str):
    """Permanently remove a preview row. Reserved for purge / admin flows."""
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM previews WHERE project_id = $1 AND preview_name = $2",
        project_id, preview_name,
    )


async def update_last_accessed(preview_id: int):
    pool = await get_pool()
    await pool.execute(
        "UPDATE previews SET last_accessed_at = $1 WHERE id = $2",
        _now(), preview_id,
    )


async def get_previews_with_active_vms() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT p.*, proj.slug as project_slug, proj.public_paths as project_public_paths,
                  o.slug as org_slug
           FROM previews p
           JOIN projects proj ON p.project_id = proj.id
           JOIN organizations o ON proj.organization_id = o.id
           WHERE p.vm_id IS NOT NULL AND p.deleted_at IS NULL"""
    )
    return [_row_to_dict(r) for r in rows]


async def update_preview_vm(
    preview_id: int, vm_id: int | None, vm_ip: str | None,
) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE previews SET vm_id = $1, vm_ip = $2 WHERE id = $3",
        vm_id, vm_ip, preview_id,
    )


async def has_running_deployment(preview_id: int) -> bool:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM deployments WHERE preview_id = $1 AND status = 'running' LIMIT 1",
        preview_id,
    )
    return row is not None
