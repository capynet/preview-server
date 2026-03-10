"""Shared SQLite database for Preview Manager — multi-tenant with organizations."""

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from config.settings import settings

logger = logging.getLogger(__name__)

_db_path: str = ""

# ---- Schema ----

SCHEMA = """
-- Users (instance-level)
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    avatar_url TEXT,
    password_hash TEXT,
    is_superadmin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    provider_username TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(provider, provider_user_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Organizations
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    avatar_url TEXT,
    gitlab_url TEXT,
    gitlab_access_token TEXT,
    auto_stop_enabled INTEGER NOT NULL DEFAULT 1,
    auto_stop_minutes INTEGER NOT NULL DEFAULT 15,
    auto_erase_enabled INTEGER NOT NULL DEFAULT 0,
    auto_erase_days INTEGER NOT NULL DEFAULT 10,
    color TEXT NOT NULL DEFAULT '#6366f1',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS org_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, organization_id)
);

CREATE TABLE IF NOT EXISTS org_email_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    domain TEXT UNIQUE NOT NULL,
    default_role TEXT NOT NULL DEFAULT 'member',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS org_invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    invited_by INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- API tokens (per-org)
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    token_hash TEXT UNIQUE NOT NULL,
    token_prefix TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS cli_auth_requests (
    code TEXT PRIMARY KEY,
    organization_id INTEGER REFERENCES organizations(id),
    status TEXT NOT NULL DEFAULT 'pending',
    user_id INTEGER,
    token TEXT,
    created_at TEXT NOT NULL
);

-- Projects (belong to org)
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    name TEXT,
    gitlab_project_id INTEGER,
    gitlab_project_path TEXT,
    gitlab_web_url TEXT,
    gitlab_default_branch TEXT DEFAULT 'main',
    env_vars TEXT DEFAULT '{}',
    auto_stop_enabled INTEGER,
    auto_stop_minutes INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, slug)
);

CREATE TABLE IF NOT EXISTS project_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    added_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE(user_id, project_id)
);

-- Previews (belong to project)
CREATE TABLE IF NOT EXISTS previews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    preview_name TEXT NOT NULL,
    url_hash TEXT NOT NULL,
    mr_id INTEGER,
    branch TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'creating',
    url TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_deployed_at TEXT,
    last_deployment_status TEXT,
    last_deployment_error TEXT,
    last_deployment_duration INTEGER,
    last_deployment_completed_at TEXT,
    last_accessed_at TEXT,
    auto_update INTEGER NOT NULL DEFAULT 1,
    pinned INTEGER NOT NULL DEFAULT 0,
    env_vars TEXT DEFAULT '{}',
    vm_id INTEGER,
    vm_ip TEXT,
    volume_id INTEGER,
    UNIQUE(project_id, preview_name)
);

CREATE INDEX IF NOT EXISTS idx_previews_url_hash ON previews(url_hash);

CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preview_id INTEGER NOT NULL REFERENCES previews(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running',
    log_output TEXT DEFAULT '',
    error TEXT,
    triggered_by TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration INTEGER
);

CREATE TABLE IF NOT EXISTS cloud_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    preview_name TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id INTEGER NOT NULL,
    resource_name TEXT NOT NULL,
    spec TEXT DEFAULT '{}',
    price_hourly REAL DEFAULT 0,
    price_monthly REAL DEFAULT 0,
    created_at TEXT NOT NULL,
    destroyed_at TEXT,
    duration_seconds INTEGER
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_url_hash(org_slug: str, project_slug: str, preview_name: str) -> str:
    """Compute a deterministic 8-char hash for preview URLs.

    Format: SHA256("{org_id}-{project_slug}-{preview_name}")[:8]
    """
    raw = f"{org_slug}-{project_slug}-{preview_name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(_db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    global _db_path
    _db_path = settings.db_path

    db_file = Path(_db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        await db.commit()
        logger.info(f"Database initialized at {_db_path}")
    finally:
        await db.close()


# ---- Organization CRUD ----

async def create_organization(slug: str, name: str, **fields) -> dict:
    now = _now()
    db = await get_db()
    try:
        cur = await db.execute(
            """INSERT INTO organizations
               (slug, name, avatar_url, gitlab_url, gitlab_access_token,
                auto_stop_enabled, auto_stop_minutes, auto_erase_enabled, auto_erase_days,
                color, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                slug, name,
                fields.get("avatar_url"),
                fields.get("gitlab_url"),
                fields.get("gitlab_access_token"),
                fields.get("auto_stop_enabled", 1),
                fields.get("auto_stop_minutes", 15),
                fields.get("auto_erase_enabled", 0),
                fields.get("auto_erase_days", 10),
                fields.get("color", "#6366f1"),
                now, now,
            ),
        )
        await db.commit()
        org_id = cur.lastrowid
        return await get_organization_by_id(org_id, db=db)
    finally:
        await db.close()


async def get_organization_by_id(org_id: int, db=None) -> Optional[dict]:
    should_close = db is None
    if db is None:
        db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM organizations WHERE id = ?", (org_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        if should_close:
            await db.close()


async def get_organization_by_slug(slug: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM organizations WHERE slug = ?", (slug,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_organizations() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM organizations ORDER BY name")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def list_user_organizations(user_id: int) -> list[dict]:
    """List orgs the user is a member of, with their role."""
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT o.*, om.role
               FROM organizations o
               JOIN org_members om ON o.id = om.organization_id
               WHERE om.user_id = ?
               ORDER BY o.name""",
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def update_organization(org_id: int, **fields) -> dict:
    db = await get_db()
    try:
        sets = ["updated_at = ?"]
        vals = [_now()]
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(org_id)
        await db.execute(
            f"UPDATE organizations SET {', '.join(sets)} WHERE id = ?", vals
        )
        await db.commit()
        return await get_organization_by_id(org_id, db=db)
    finally:
        await db.close()


async def delete_organization(org_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM organizations WHERE id = ?", (org_id,))
        await db.commit()
    finally:
        await db.close()


# ---- Org Members ----

async def add_org_member(user_id: int, org_id: int, role: str) -> dict:
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO org_members (user_id, organization_id, role, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, organization_id) DO UPDATE SET role = excluded.role""",
            (user_id, org_id, role, _now()),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT * FROM org_members WHERE user_id = ? AND organization_id = ?",
            (user_id, org_id),
        )
        return dict(await cur.fetchone())
    finally:
        await db.close()


async def get_org_member(user_id: int, org_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM org_members WHERE user_id = ? AND organization_id = ?",
            (user_id, org_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_org_members(org_id: int) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT u.id, u.email, u.name, u.avatar_url, om.role, om.created_at
               FROM org_members om
               JOIN users u ON om.user_id = u.id
               WHERE om.organization_id = ?
               ORDER BY u.name""",
            (org_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def update_org_member_role(user_id: int, org_id: int, role: str):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE org_members SET role = ? WHERE user_id = ? AND organization_id = ?",
            (role, user_id, org_id),
        )
        await db.commit()
    finally:
        await db.close()


async def remove_org_member(user_id: int, org_id: int):
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM org_members WHERE user_id = ? AND organization_id = ?",
            (user_id, org_id),
        )
        await db.commit()
    finally:
        await db.close()


# ---- Org Email Domains ----

async def add_email_domain(org_id: int, domain: str, default_role: str = "member") -> dict:
    db = await get_db()
    try:
        cur = await db.execute(
            """INSERT INTO org_email_domains (organization_id, domain, default_role, created_at)
               VALUES (?, ?, ?, ?)""",
            (org_id, domain.lower(), default_role, _now()),
        )
        await db.commit()
        return {"id": cur.lastrowid, "organization_id": org_id, "domain": domain.lower(), "default_role": default_role}
    finally:
        await db.close()


async def list_email_domains(org_id: int) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM org_email_domains WHERE organization_id = ? ORDER BY domain",
            (org_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def remove_email_domain(domain_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM org_email_domains WHERE id = ?", (domain_id,))
        await db.commit()
    finally:
        await db.close()


async def match_email_domain(email: str) -> Optional[dict]:
    """Check if email domain matches any org's allowed domain.

    Returns {organization_id, default_role} or None.
    """
    parts = email.rsplit("@", 1)
    if len(parts) != 2:
        return None
    email_domain = parts[1].lower()
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM org_email_domains WHERE domain = ?",
            (email_domain,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


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
    db = await get_db()
    try:
        cur = await db.execute(
            """INSERT INTO org_invitations
               (organization_id, email, role, token, project_id, invited_by, status, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (org_id, email, role, token, project_id, invited_by, now, expires),
        )
        await db.commit()
        return {
            "id": cur.lastrowid, "organization_id": org_id,
            "email": email, "role": role, "token": token,
            "project_id": project_id, "invited_by": invited_by,
            "status": "pending", "created_at": now, "expires_at": expires,
        }
    finally:
        await db.close()


async def get_invitation_by_token(token: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM org_invitations WHERE token = ? AND status = 'pending'",
            (token,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        inv = dict(row)
        if inv["expires_at"] < _now():
            return None
        return inv
    finally:
        await db.close()


async def get_invitation_by_email(email: str, org_id: Optional[int] = None) -> Optional[dict]:
    db = await get_db()
    try:
        if org_id:
            cur = await db.execute(
                "SELECT * FROM org_invitations WHERE email = ? AND organization_id = ? AND status = 'pending'",
                (email, org_id),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM org_invitations WHERE email = ? AND status = 'pending'",
                (email,),
            )
        row = await cur.fetchone()
        if not row:
            return None
        inv = dict(row)
        if inv["expires_at"] < _now():
            return None
        return inv
    finally:
        await db.close()


async def list_org_invitations(org_id: int) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT i.*, u.name as invited_by_name
               FROM org_invitations i
               JOIN users u ON i.invited_by = u.id
               WHERE i.organization_id = ? AND i.status = 'pending'
               ORDER BY i.id DESC""",
            (org_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def delete_invitation(invitation_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM org_invitations WHERE id = ?", (invitation_id,))
        await db.commit()
    finally:
        await db.close()


async def mark_invitation_accepted(invitation_id: int):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE org_invitations SET status = 'accepted' WHERE id = ?",
            (invitation_id,),
        )
        await db.commit()
    finally:
        await db.close()


# ---- Preview CRUD ----

async def get_preview(project_id: int, preview_name: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM previews WHERE project_id = ? AND preview_name = ?",
            (project_id, preview_name),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_preview_by_id(preview_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM previews WHERE id = ?", (preview_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_preview_by_hash(url_hash: str) -> Optional[dict]:
    """Find a preview by its URL hash (for domain resolution)."""
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT p.*, proj.slug as project_slug, proj.organization_id,
                      o.slug as org_slug
               FROM previews p
               JOIN projects proj ON p.project_id = proj.id
               JOIN organizations o ON proj.organization_id = o.id
               WHERE p.url_hash = ?""",
            (url_hash,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_preview_by_domain(domain: str) -> Optional[dict]:
    """Find a preview by its domain (e.g. 'a3f8b2c1.mr.preview-mr.com').

    Extracts the hash from the subdomain and looks up the preview.
    Also handles domain aliases: if the domain is '{prefix}--{hash}.mr.preview-mr.com',
    strips the alias prefix.
    """
    import re
    match = re.match(r"^(.+?)\.mr\.preview-mr\.com$", domain)
    if not match:
        return None
    subdomain = match.group(1)

    # Handle alias prefix: {prefix}--{hash}
    if "--" in subdomain:
        subdomain = subdomain.split("--")[-1]

    return await get_preview_by_hash(subdomain)


async def get_preview_by_branch(project_id: int, branch: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM previews WHERE project_id = ? AND branch = ? AND preview_name LIKE 'branch-%'",
            (project_id, branch),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_all_previews(org_id: Optional[int] = None) -> list[dict]:
    db = await get_db()
    try:
        if org_id:
            cur = await db.execute(
                """SELECT p.*, proj.slug as project_slug, proj.organization_id,
                          o.slug as org_slug,
                          d.id AS latest_deployment_id,
                          d.status AS latest_deployment_status,
                          d.completed_at AS latest_deployment_completed_at
                   FROM previews p
                   JOIN projects proj ON p.project_id = proj.id
                   JOIN organizations o ON proj.organization_id = o.id
                   LEFT JOIN deployments d ON d.id = (
                       SELECT d2.id FROM deployments d2 WHERE d2.preview_id = p.id ORDER BY d2.id DESC LIMIT 1
                   )
                   WHERE proj.organization_id = ?
                   ORDER BY p.last_deployed_at DESC NULLS LAST""",
                (org_id,),
            )
        else:
            cur = await db.execute(
                """SELECT p.*, proj.slug as project_slug, proj.organization_id,
                          o.slug as org_slug,
                          d.id AS latest_deployment_id,
                          d.status AS latest_deployment_status,
                          d.completed_at AS latest_deployment_completed_at
                   FROM previews p
                   JOIN projects proj ON p.project_id = proj.id
                   JOIN organizations o ON proj.organization_id = o.id
                   LEFT JOIN deployments d ON d.id = (
                       SELECT d2.id FROM deployments d2 WHERE d2.preview_id = p.id ORDER BY d2.id DESC LIMIT 1
                   )
                   ORDER BY p.last_deployed_at DESC NULLS LAST"""
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def upsert_preview(project_id: int, preview_name: str, **fields) -> dict:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM previews WHERE project_id = ? AND preview_name = ?",
            (project_id, preview_name),
        )
        existing = await cur.fetchone()

        if existing:
            sets = []
            vals = []
            for k, v in fields.items():
                sets.append(f"{k} = ?")
                vals.append(v)
            if sets:
                vals.extend([project_id, preview_name])
                await db.execute(
                    f"UPDATE previews SET {', '.join(sets)} WHERE project_id = ? AND preview_name = ?",
                    vals,
                )
                await db.commit()
            cur2 = await db.execute(
                "SELECT * FROM previews WHERE project_id = ? AND preview_name = ?",
                (project_id, preview_name),
            )
            return dict(await cur2.fetchone())
        else:
            now = _now()
            await db.execute(
                """INSERT INTO previews
                   (project_id, preview_name, url_hash, mr_id, branch, commit_sha, status, url, path,
                    created_at, last_deployed_at,
                    last_deployment_status, last_deployment_error,
                    last_deployment_duration, last_deployment_completed_at,
                    auto_update, pinned, env_vars)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    preview_name,
                    fields.get("url_hash", ""),
                    fields.get("mr_id"),
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
                ),
            )
            await db.commit()
            cur2 = await db.execute(
                "SELECT * FROM previews WHERE project_id = ? AND preview_name = ?",
                (project_id, preview_name),
            )
            return dict(await cur2.fetchone())
    finally:
        await db.close()


async def delete_preview_from_db(project_id: int, preview_name: str):
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM previews WHERE project_id = ? AND preview_name = ?",
            (project_id, preview_name),
        )
        await db.commit()
    finally:
        await db.close()


async def update_last_accessed(preview_id: int):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE previews SET last_accessed_at = ? WHERE id = ?",
            (_now(), preview_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_all_active_previews() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT p.preview_name, p.url_hash, proj.slug as project_slug,
                      o.slug as org_slug, p.vm_id, p.vm_ip
               FROM previews p
               JOIN projects proj ON p.project_id = proj.id
               JOIN organizations o ON proj.organization_id = o.id
               WHERE p.vm_ip IS NOT NULL AND p.vm_ip != ''"""
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_previews_with_active_vms() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT p.*, proj.slug as project_slug, o.slug as org_slug
               FROM previews p
               JOIN projects proj ON p.project_id = proj.id
               JOIN organizations o ON proj.organization_id = o.id
               WHERE p.vm_id IS NOT NULL"""
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def update_preview_vm(
    preview_id: int, vm_id: int | None, vm_ip: str | None,
) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE previews SET vm_id = ?, vm_ip = ? WHERE id = ?",
            (vm_id, vm_ip, preview_id),
        )
        await db.commit()
    finally:
        await db.close()


async def has_running_deployment(preview_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT 1 FROM deployments WHERE preview_id = ? AND status = 'running' LIMIT 1",
            (preview_id,),
        )
        return await cur.fetchone() is not None
    finally:
        await db.close()


# ---- Deployment CRUD ----

async def create_deployment(preview_id: int, triggered_by: str | None = None) -> int:
    db = await get_db()
    try:
        cur = await db.execute(
            """INSERT INTO deployments (preview_id, status, triggered_by, started_at)
               VALUES (?, 'running', ?, ?)""",
            (preview_id, triggered_by, _now()),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def finish_deployment(
    deployment_id: int, status: str,
    log_output: str = "", error: str | None = None,
    duration: int | None = None,
):
    db = await get_db()
    try:
        await db.execute(
            """UPDATE deployments
               SET status = ?, log_output = ?, error = ?, duration = ?, completed_at = ?
               WHERE id = ?""",
            (status, log_output, error, duration, _now(), deployment_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_deployment(deployment_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_deployments(preview_id: int, limit: int = 50) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT id, preview_id, status, error, triggered_by,
                      started_at, completed_at, duration
               FROM deployments
               WHERE preview_id = ?
               ORDER BY started_at DESC
               LIMIT ?""",
            (preview_id, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ---- Project CRUD ----

async def get_project(project_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_project_by_slug(org_id: int, slug: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM projects WHERE organization_id = ? AND slug = ?",
            (org_id, slug),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_project_by_gitlab_id(org_id: int, gitlab_project_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM projects WHERE organization_id = ? AND gitlab_project_id = ?",
            (org_id, gitlab_project_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_projects(org_id: int) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM projects WHERE organization_id = ? ORDER BY slug",
            (org_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def upsert_project(org_id: int, slug: str, **fields) -> dict:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM projects WHERE organization_id = ? AND slug = ?",
            (org_id, slug),
        )
        existing = await cur.fetchone()
        now = _now()

        if existing:
            sets = ["updated_at = ?"]
            vals = [now]
            for k, v in fields.items():
                sets.append(f"{k} = ?")
                vals.append(v)
            vals.append(existing["id"])
            await db.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", vals
            )
        else:
            await db.execute(
                """INSERT INTO projects
                   (organization_id, slug, name, gitlab_project_id, gitlab_project_path,
                    gitlab_web_url, gitlab_default_branch, env_vars,
                    auto_stop_enabled, auto_stop_minutes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    org_id, slug,
                    fields.get("name"),
                    fields.get("gitlab_project_id"),
                    fields.get("gitlab_project_path"),
                    fields.get("gitlab_web_url"),
                    fields.get("gitlab_default_branch", "main"),
                    fields.get("env_vars", "{}"),
                    fields.get("auto_stop_enabled"),
                    fields.get("auto_stop_minutes"),
                    now, now,
                ),
            )
        await db.commit()
        cur2 = await db.execute(
            "SELECT * FROM projects WHERE organization_id = ? AND slug = ?",
            (org_id, slug),
        )
        return dict(await cur2.fetchone())
    finally:
        await db.close()


async def get_all_projects() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT p.*, o.slug as org_slug
               FROM projects p
               JOIN organizations o ON p.organization_id = o.id
               ORDER BY o.slug, p.slug"""
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


# ---- Project Members ----

async def list_project_members(project_id: int) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT u.id, u.email, u.name, u.avatar_url, om.role as org_role
               FROM project_members pm
               JOIN users u ON pm.user_id = u.id
               LEFT JOIN org_members om ON u.id = om.user_id
                   AND om.organization_id = (SELECT organization_id FROM projects WHERE id = ?)
               WHERE pm.project_id = ? ORDER BY u.name""",
            (project_id, project_id),
        )
        return [dict(row) for row in await cur.fetchall()]
    finally:
        await db.close()


async def add_project_member(user_id: int, project_id: int, added_by: int):
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO project_members (user_id, project_id, added_by, created_at) VALUES (?, ?, ?, ?)",
            (user_id, project_id, added_by, _now()),
        )
        await db.commit()
    finally:
        await db.close()


async def remove_project_member(user_id: int, project_id: int):
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM project_members WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        )
        await db.commit()
    finally:
        await db.close()


# ---- Cloud Resources ----

async def log_cloud_resource(
    project_id: int, preview_name: str,
    resource_type: str, resource_id: int, resource_name: str,
    spec: str = "{}", price_hourly: float = 0, price_monthly: float = 0,
) -> int:
    db = await get_db()
    try:
        cur = await db.execute(
            """INSERT INTO cloud_resources
               (project_id, preview_name, resource_type, resource_id, resource_name,
                spec, price_hourly, price_monthly, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, preview_name, resource_type, resource_id, resource_name,
             spec, price_hourly, price_monthly, _now()),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def finish_cloud_resource(resource_type: str, resource_id: int) -> None:
    db = await get_db()
    try:
        now = _now()
        cur = await db.execute(
            """SELECT id, created_at FROM cloud_resources
               WHERE resource_type = ? AND resource_id = ? AND destroyed_at IS NULL
               ORDER BY id DESC LIMIT 1""",
            (resource_type, resource_id),
        )
        row = await cur.fetchone()
        if row:
            created = datetime.fromisoformat(row["created_at"])
            duration = int((datetime.now(timezone.utc) - created).total_seconds())
            await db.execute(
                """UPDATE cloud_resources
                   SET destroyed_at = ?, duration_seconds = ?
                   WHERE id = ?""",
                (now, duration, row["id"]),
            )
            await db.commit()
    finally:
        await db.close()
