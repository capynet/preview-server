"""Organization, member, email-domain and invitation CRUD."""

from datetime import datetime, timezone
from typing import Optional

from app.database._pool import get_pool, _now, _row_to_dict


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
    """List orgs the user has access to (via org membership or project membership)."""
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT DISTINCT o.*, COALESCE(om.role, 'viewer') as role
           FROM organizations o
           LEFT JOIN org_members om ON o.id = om.organization_id AND om.user_id = $1
           LEFT JOIN projects p ON p.organization_id = o.id
           LEFT JOIN project_members pm ON pm.project_id = p.id AND pm.user_id = $1
           WHERE om.user_id = $1 OR pm.user_id = $1
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


async def list_project_invitations(org_id: int, project_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT i.*, u.name as invited_by_name
           FROM org_invitations i
           JOIN users u ON i.invited_by = u.id
           WHERE i.organization_id = $1 AND i.project_id = $2 AND i.status = 'pending'
           ORDER BY i.id DESC""",
        org_id, project_id,
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
