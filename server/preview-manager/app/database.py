"""Shared PostgreSQL database for Preview Manager — multi-tenant with organizations."""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from config.settings import settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_url_hash(org_slug: str, project_slug: str, preview_name: str) -> str:
    """Compute a deterministic 8-char hash for preview URLs."""
    raw = f"{org_slug}-{project_slug}-{preview_name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


async def init_pool():
    """Create the asyncpg connection pool."""
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=10,
    )
    logger.info("PostgreSQL connection pool initialized")


async def close_pool():
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL connection pool closed")


async def get_pool() -> asyncpg.Pool:
    """Get the connection pool."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


def _row_to_dict(row: asyncpg.Record) -> dict:
    return dict(row)


# ---- Organization CRUD ----

async def create_organization(slug: str, name: str, **fields) -> dict:
    now = _now()
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO organizations
           (slug, name, avatar_url, gitlab_url, gitlab_access_token,
            auto_erase_enabled, auto_erase_days,
            color, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
           RETURNING *""",
        slug, name,
        fields.get("avatar_url"),
        fields.get("gitlab_url"),
        fields.get("gitlab_access_token"),
        fields.get("auto_erase_enabled", 0),
        fields.get("auto_erase_days", 10),
        fields.get("color", "#6366f1"),
        now, now,
    )
    return _row_to_dict(row)


async def get_organization_by_id(org_id: int, **_kwargs) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM organizations WHERE id = $1", org_id)
    return _row_to_dict(row) if row else None


async def get_organization_by_slug(slug: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM organizations WHERE slug = $1", slug)
    return _row_to_dict(row) if row else None


async def list_organizations() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM organizations ORDER BY name")
    return [_row_to_dict(r) for r in rows]


async def list_user_organizations(user_id: int) -> list[dict]:
    """List orgs the user is a member of, with their role."""
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT o.*, om.role
           FROM organizations o
           JOIN org_members om ON o.id = om.organization_id
           WHERE om.user_id = $1
           ORDER BY o.name""",
        user_id,
    )
    return [_row_to_dict(r) for r in rows]


async def update_organization(org_id: int, **fields) -> dict:
    pool = await get_pool()
    sets = ["updated_at = $1"]
    vals = [_now()]
    idx = 2
    for k, v in fields.items():
        sets.append(f"{k} = ${idx}")
        vals.append(v)
        idx += 1
    vals.append(org_id)
    await pool.execute(
        f"UPDATE organizations SET {', '.join(sets)} WHERE id = ${idx}", *vals
    )
    return await get_organization_by_id(org_id)


async def delete_organization(org_id: int):
    pool = await get_pool()
    await pool.execute("DELETE FROM organizations WHERE id = $1", org_id)


# ---- Org Members ----

async def add_org_member(user_id: int, org_id: int, role: str) -> dict:
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO org_members (user_id, organization_id, role, created_at)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT(user_id, organization_id) DO UPDATE SET role = EXCLUDED.role""",
        user_id, org_id, role, _now(),
    )
    row = await pool.fetchrow(
        "SELECT * FROM org_members WHERE user_id = $1 AND organization_id = $2",
        user_id, org_id,
    )
    return _row_to_dict(row)


async def get_org_member(user_id: int, org_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM org_members WHERE user_id = $1 AND organization_id = $2",
        user_id, org_id,
    )
    return _row_to_dict(row) if row else None


async def list_org_members(org_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT u.id, u.email, u.name, u.avatar_url, om.role, om.created_at
           FROM org_members om
           JOIN users u ON om.user_id = u.id
           WHERE om.organization_id = $1
           ORDER BY u.name""",
        org_id,
    )
    return [_row_to_dict(r) for r in rows]


async def update_org_member_role(user_id: int, org_id: int, role: str):
    pool = await get_pool()
    await pool.execute(
        "UPDATE org_members SET role = $1 WHERE user_id = $2 AND organization_id = $3",
        role, user_id, org_id,
    )


async def remove_org_member(user_id: int, org_id: int):
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM org_members WHERE user_id = $1 AND organization_id = $2",
        user_id, org_id,
    )


# ---- Org Email Domains ----

async def add_email_domain(org_id: int, domain: str, default_role: str = "member") -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO org_email_domains (organization_id, domain, default_role, created_at)
           VALUES ($1, $2, $3, $4)
           RETURNING *""",
        org_id, domain.lower(), default_role, _now(),
    )
    return _row_to_dict(row)


async def list_email_domains(org_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM org_email_domains WHERE organization_id = $1 ORDER BY domain",
        org_id,
    )
    return [_row_to_dict(r) for r in rows]


async def remove_email_domain(domain_id: int):
    pool = await get_pool()
    await pool.execute("DELETE FROM org_email_domains WHERE id = $1", domain_id)


async def match_email_domain(email: str) -> Optional[dict]:
    """Check if email domain matches any org's allowed domain."""
    parts = email.rsplit("@", 1)
    if len(parts) != 2:
        return None
    email_domain = parts[1].lower()
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM org_email_domains WHERE domain = $1",
        email_domain,
    )
    return _row_to_dict(row) if row else None


# ---- Org Invitations ----

async def create_org_invitation(
    org_id: int, email: str, role: str, invited_by: int,
    project_id: Optional[int] = None,
) -> dict:
    import secrets
    import time

    token = secrets.token_urlsafe(32)
    now = _now()
    expires = datetime.fromtimestamp(
        time.time() + 7 * 24 * 3600, tz=timezone.utc
    ).isoformat()
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO org_invitations
           (organization_id, email, role, token, project_id, invited_by, status, created_at, expires_at)
           VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8)
           RETURNING *""",
        org_id, email, role, token, project_id, invited_by, now, expires,
    )
    return _row_to_dict(row)


async def get_invitation_by_token(token: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM org_invitations WHERE token = $1 AND status = 'pending'",
        token,
    )
    if not row:
        return None
    inv = _row_to_dict(row)
    if inv["expires_at"] < _now():
        return None
    return inv


async def get_invitation_by_email(email: str, org_id: Optional[int] = None) -> Optional[dict]:
    pool = await get_pool()
    if org_id:
        row = await pool.fetchrow(
            "SELECT * FROM org_invitations WHERE email = $1 AND organization_id = $2 AND status = 'pending'",
            email, org_id,
        )
    else:
        row = await pool.fetchrow(
            "SELECT * FROM org_invitations WHERE email = $1 AND status = 'pending'",
            email,
        )
    if not row:
        return None
    inv = _row_to_dict(row)
    if inv["expires_at"] < _now():
        return None
    return inv


async def list_org_invitations(org_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT i.*, u.name as invited_by_name
           FROM org_invitations i
           JOIN users u ON i.invited_by = u.id
           WHERE i.organization_id = $1 AND i.status = 'pending'
           ORDER BY i.id DESC""",
        org_id,
    )
    return [_row_to_dict(r) for r in rows]


async def delete_invitation(invitation_id: int):
    pool = await get_pool()
    await pool.execute("DELETE FROM org_invitations WHERE id = $1", invitation_id)


async def mark_invitation_accepted(invitation_id: int):
    pool = await get_pool()
    await pool.execute(
        "UPDATE org_invitations SET status = 'accepted' WHERE id = $1",
        invitation_id,
    )


# ---- Preview CRUD ----

async def get_preview(project_id: int, preview_name: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM previews WHERE project_id = $1 AND preview_name = $2",
        project_id, preview_name,
    )
    return _row_to_dict(row) if row else None


async def get_preview_by_id(preview_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM previews WHERE id = $1", preview_id)
    return _row_to_dict(row) if row else None


async def get_preview_by_hash(url_hash: str) -> Optional[dict]:
    """Find a preview by its URL hash (for domain resolution)."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT p.*, proj.slug as project_slug, proj.organization_id,
                  o.slug as org_slug
           FROM previews p
           JOIN projects proj ON p.project_id = proj.id
           JOIN organizations o ON proj.organization_id = o.id
           WHERE p.url_hash = $1""",
        url_hash,
    )
    return _row_to_dict(row) if row else None


async def get_preview_by_domain(domain: str) -> Optional[dict]:
    """Find a preview by its domain (e.g. 'a3f8b2c1.mr.preview-mr.com')."""
    import re
    match = re.match(r"^(.+?)\.mr\.preview-mr\.com$", domain)
    if not match:
        return None
    subdomain = match.group(1)
    if "--" in subdomain:
        subdomain = subdomain.split("--")[-1]
    return await get_preview_by_hash(subdomain)


async def get_preview_by_branch(project_id: int, branch: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM previews WHERE project_id = $1 AND branch = $2 AND preview_name LIKE 'branch-%'",
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
               WHERE proj.organization_id = $1
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
               ORDER BY p.created_at DESC"""
        )
    return [_row_to_dict(r) for r in rows]


async def upsert_preview(project_id: int, preview_name: str, **fields) -> dict:
    pool = await get_pool()
    existing = await pool.fetchrow(
        "SELECT * FROM previews WHERE project_id = $1 AND preview_name = $2",
        project_id, preview_name,
    )

    if existing:
        if fields:
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
                auto_update, pinned, env_vars)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
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
        )
        return _row_to_dict(row)


async def delete_preview_from_db(project_id: int, preview_name: str):
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
        """SELECT p.*, proj.slug as project_slug, o.slug as org_slug
           FROM previews p
           JOIN projects proj ON p.project_id = proj.id
           JOIN organizations o ON proj.organization_id = o.id
           WHERE p.vm_id IS NOT NULL"""
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


# ---- Deployment CRUD ----

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


async def finish_deployment(
    deployment_id: int, status: str,
    log_output: str = "", error: str | None = None,
    duration: int | None = None,
):
    pool = await get_pool()
    await pool.execute(
        """UPDATE deployments
           SET status = $1, log_output = $2, error = $3, duration = $4, completed_at = $5
           WHERE id = $6""",
        status, log_output, error, duration, _now(), deployment_id,
    )


async def get_deployment(deployment_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM deployments WHERE id = $1", deployment_id)
    return _row_to_dict(row) if row else None


async def list_deployments(preview_id: int, limit: int = 50) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, preview_id, status, error, triggered_by,
                  started_at, completed_at, duration, type
           FROM deployments
           WHERE preview_id = $1
           ORDER BY started_at DESC
           LIMIT $2""",
        preview_id, limit,
    )
    return [_row_to_dict(r) for r in rows]


# ---- Project CRUD ----

async def get_project(project_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    return _row_to_dict(row) if row else None


async def get_project_by_slug(org_id: int, slug: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM projects WHERE organization_id = $1 AND slug = $2",
        org_id, slug,
    )
    return _row_to_dict(row) if row else None


async def get_project_by_gitlab_id(org_id: int, gitlab_project_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM projects WHERE organization_id = $1 AND gitlab_project_id = $2",
        org_id, gitlab_project_id,
    )
    return _row_to_dict(row) if row else None


async def list_projects(org_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM projects WHERE organization_id = $1 ORDER BY slug",
        org_id,
    )
    return [_row_to_dict(r) for r in rows]


async def upsert_project(org_id: int, slug: str, **fields) -> dict:
    pool = await get_pool()
    existing = await pool.fetchrow(
        "SELECT * FROM projects WHERE organization_id = $1 AND slug = $2",
        org_id, slug,
    )
    now = _now()

    if existing:
        sets = ["updated_at = $1"]
        vals = [now]
        idx = 2
        for k, v in fields.items():
            sets.append(f"{k} = ${idx}")
            vals.append(v)
            idx += 1
        vals.append(existing["id"])
        await pool.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ${idx}", *vals
        )
    else:
        await pool.execute(
            """INSERT INTO projects
               (organization_id, slug, name, gitlab_project_id, gitlab_project_path,
                gitlab_web_url, gitlab_default_branch, env_vars,
                created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            org_id, slug,
            fields.get("name"),
            fields.get("gitlab_project_id"),
            fields.get("gitlab_project_path"),
            fields.get("gitlab_web_url"),
            fields.get("gitlab_default_branch", "main"),
            fields.get("env_vars", "{}"),
            now, now,
        )
    row = await pool.fetchrow(
        "SELECT * FROM projects WHERE organization_id = $1 AND slug = $2",
        org_id, slug,
    )
    return _row_to_dict(row)


async def add_project_member(user_id: int, project_id: int, added_by: int):
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO project_members (user_id, project_id, added_by, created_at)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT(user_id, project_id) DO NOTHING""",
        user_id, project_id, added_by, _now(),
    )


async def remove_project_member(user_id: int, project_id: int):
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM project_members WHERE user_id = $1 AND project_id = $2",
        user_id, project_id,
    )


# ---- Cloud Resources ----

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
