"""Shared SQLite database for Preview Manager (auth + previews)."""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from config.settings import settings

logger = logging.getLogger(__name__)

_db_path: str = ""

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    avatar_url TEXT,
    password_hash TEXT,
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

CREATE TABLE IF NOT EXISTS roles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    token_hash TEXT UNIQUE NOT NULL,
    token_prefix TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS cli_auth_requests (
    code TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    user_id INTEGER,
    token TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    invited_by INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

PROJECT_MEMBERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_slug TEXT NOT NULL,
    added_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE(user_id, project_slug)
);
"""

CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

PREVIEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS previews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    mr_id INTEGER,
    preview_name TEXT NOT NULL,
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
    UNIQUE(project, preview_name)
);
"""

PROJECTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    slug TEXT PRIMARY KEY,
    auto_stop_enabled INTEGER,
    auto_stop_minutes INTEGER,
    env_vars TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

DEPLOYMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preview_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    log_output TEXT DEFAULT '',
    error TEXT,
    triggered_by TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration INTEGER,
    FOREIGN KEY (preview_id) REFERENCES previews(id) ON DELETE CASCADE
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        await db.executescript(AUTH_SCHEMA)
        await db.executescript(PREVIEWS_SCHEMA)
        await db.executescript(DEPLOYMENTS_SCHEMA)
        await db.executescript(PROJECT_MEMBERS_SCHEMA)
        await db.executescript(CONFIG_SCHEMA)
        await db.executescript(PROJECTS_SCHEMA)

        # Migration: rebuild previews table to make mr_id nullable and add preview_name
        cur = await db.execute("PRAGMA table_info(previews)")
        columns = {row[1]: row[3] for row in await cur.fetchall()}  # name -> notnull
        needs_migration = columns.get("mr_id") == 1  # mr_id is NOT NULL in old schema

        if needs_migration:
            logger.info("Migrating previews table: making mr_id nullable, adding preview_name")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS previews_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    mr_id INTEGER,
                    preview_name TEXT NOT NULL,
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
                    UNIQUE(project, preview_name)
                );

                INSERT INTO previews_new
                    SELECT id, project, mr_id, 'mr-' || mr_id, branch, commit_sha,
                           status, url, path, created_at, last_deployed_at,
                           last_deployment_status, last_deployment_error,
                           last_deployment_duration, last_deployment_completed_at,
                           last_accessed_at
                    FROM previews;

                DROP TABLE previews;
                ALTER TABLE previews_new RENAME TO previews;
            """)
            logger.info("Migration complete: previews table rebuilt")

        # Migration: add auto_update column if missing
        cur2 = await db.execute("PRAGMA table_info(previews)")
        col_names = {row[1] for row in await cur2.fetchall()}
        if "auto_update" not in col_names:
            logger.info("Migrating previews table: adding auto_update column")
            await db.execute("ALTER TABLE previews ADD COLUMN auto_update INTEGER NOT NULL DEFAULT 1")

        if "pinned" not in col_names:
            logger.info("Migrating previews table: adding pinned column")
            await db.execute("ALTER TABLE previews ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")

        if "env_vars" not in col_names:
            logger.info("Migrating previews table: adding env_vars column")
            await db.execute("ALTER TABLE previews ADD COLUMN env_vars TEXT DEFAULT '{}'")

        # Migration: add project_slug column to invitations if missing
        cur3 = await db.execute("PRAGMA table_info(invitations)")
        inv_cols = {row[1] for row in await cur3.fetchall()}
        if "project_slug" not in inv_cols:
            logger.info("Migrating invitations table: adding project_slug column")
            await db.execute("ALTER TABLE invitations ADD COLUMN project_slug TEXT")

        # Migration: populate projects table from app_config keys
        cur_proj = await db.execute("SELECT COUNT(*) FROM projects")
        proj_count = (await cur_proj.fetchone())[0]
        if proj_count == 0:
            # Collect unique slugs from previews and project_members
            cur_slugs = await db.execute(
                "SELECT DISTINCT project AS slug FROM previews "
                "UNION SELECT DISTINCT project_slug AS slug FROM project_members"
            )
            slugs = [row[0] for row in await cur_slugs.fetchall() if row[0]]
            now = _now()
            for slug in slugs:
                cur_as_e = await db.execute(
                    "SELECT value FROM app_config WHERE key = ?",
                    (f"auto_stop_{slug}_enabled",),
                )
                row_as_e = await cur_as_e.fetchone()
                as_enabled = None
                if row_as_e and row_as_e[0] is not None:
                    as_enabled = 1 if row_as_e[0] == "true" else 0

                cur_as_m = await db.execute(
                    "SELECT value FROM app_config WHERE key = ?",
                    (f"auto_stop_{slug}_minutes",),
                )
                row_as_m = await cur_as_m.fetchone()
                as_minutes = int(row_as_m[0]) if row_as_m and row_as_m[0] else None

                cur_ev = await db.execute(
                    "SELECT value FROM app_config WHERE key = ?",
                    (f"env_vars_{slug}",),
                )
                row_ev = await cur_ev.fetchone()
                env_vars = row_ev[0] if row_ev and row_ev[0] else "{}"

                await db.execute(
                    """INSERT OR IGNORE INTO projects
                       (slug, auto_stop_enabled, auto_stop_minutes, env_vars, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (slug, as_enabled, as_minutes, env_vars, now, now),
                )
            if slugs:
                logger.info(f"Migrated {len(slugs)} project(s) to projects table")

        await db.commit()
        logger.info(f"Database initialized at {_db_path}")
    finally:
        await db.close()


# ---- Preview CRUD ----

async def get_preview(project: str, preview_name: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM previews WHERE project = ? AND preview_name = ?",
            (project, preview_name),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_preview_by_branch(project: str, branch: str) -> Optional[dict]:
    """Find a branch preview by project and branch name."""
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM previews WHERE project = ? AND branch = ? AND preview_name LIKE 'branch-%'",
            (project, branch),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_all_previews() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT p.*,
                      d.id AS latest_deployment_id,
                      d.status AS latest_deployment_status,
                      d.completed_at AS latest_deployment_completed_at
               FROM previews p
               LEFT JOIN deployments d ON d.id = (
                   SELECT d2.id FROM deployments d2 WHERE d2.preview_id = p.id ORDER BY d2.id DESC LIMIT 1
               )
               ORDER BY p.last_deployed_at DESC NULLS LAST"""
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def upsert_preview(project: str, preview_name: str, **fields) -> dict:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM previews WHERE project = ? AND preview_name = ?",
            (project, preview_name),
        )
        existing = await cur.fetchone()

        if existing:
            # Update only provided fields
            sets = []
            vals = []
            for k, v in fields.items():
                sets.append(f"{k} = ?")
                vals.append(v)
            if sets:
                vals.extend([project, preview_name])
                await db.execute(
                    f"UPDATE previews SET {', '.join(sets)} WHERE project = ? AND preview_name = ?",
                    vals,
                )
                await db.commit()
            cur2 = await db.execute(
                "SELECT * FROM previews WHERE project = ? AND preview_name = ?",
                (project, preview_name),
            )
            return dict(await cur2.fetchone())
        else:
            # Insert — require essential fields
            now = _now()
            await db.execute(
                """INSERT INTO previews
                   (project, preview_name, mr_id, branch, commit_sha, status, url, path,
                    created_at, last_deployed_at,
                    last_deployment_status, last_deployment_error,
                    last_deployment_duration, last_deployment_completed_at, auto_update, pinned)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project,
                    preview_name,
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
                ),
            )
            await db.commit()
            cur2 = await db.execute(
                "SELECT * FROM previews WHERE project = ? AND preview_name = ?",
                (project, preview_name),
            )
            return dict(await cur2.fetchone())
    finally:
        await db.close()


async def delete_preview_from_db(project: str, preview_name: str):
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM previews WHERE project = ? AND preview_name = ?",
            (project, preview_name),
        )
        await db.commit()
    finally:
        await db.close()


async def get_preview_by_domain(domain: str) -> Optional[dict]:
    """Find a preview by its domain (e.g. 'branch-main-drupal-test.mr.preview-mr.com').

    Also handles domain aliases: if the domain is '{prefix}--{base-domain}',
    strips the alias prefix and looks up by the base domain.
    """
    url = f"https://{domain}"
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM previews WHERE url = ?",
            (url,),
        )
        row = await cur.fetchone()
        if row:
            return dict(row)

        # Try stripping alias prefix: {prefix}--{rest} → {rest}
        if "--" in domain:
            base_domain = domain.split("--", 1)[1]
            cur = await db.execute(
                "SELECT * FROM previews WHERE url = ?",
                (f"https://{base_domain}",),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

        return None
    finally:
        await db.close()


async def update_last_accessed(project: str, preview_name: str):
    """Update the last_accessed_at timestamp for a preview."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE previews SET last_accessed_at = ? WHERE project = ? AND preview_name = ?",
            (_now(), project, preview_name),
        )
        await db.commit()
    finally:
        await db.close()


async def has_running_deployment(preview_id: int) -> bool:
    """Check if a preview has any deployment with status 'running'."""
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
    """Create a new deployment record. Returns the deployment id."""
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
    deployment_id: int,
    status: str,
    log_output: str = "",
    error: str | None = None,
    duration: int | None = None,
):
    """Mark a deployment as completed/failed with its log output."""
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
    """Get a single deployment by id (includes log_output)."""
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_deployments(preview_id: int, limit: int = 50) -> list[dict]:
    """List deployments for a preview (without log_output for performance)."""
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

async def get_project(slug: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM projects WHERE slug = ?", (slug,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def upsert_project(slug: str, **fields) -> dict:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM projects WHERE slug = ?", (slug,))
        existing = await cur.fetchone()
        now = _now()

        if existing:
            sets = ["updated_at = ?"]
            vals = [now]
            for k, v in fields.items():
                sets.append(f"{k} = ?")
                vals.append(v)
            vals.append(slug)
            await db.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE slug = ?",
                vals,
            )
        else:
            await db.execute(
                """INSERT INTO projects
                   (slug, auto_stop_enabled, auto_stop_minutes, env_vars, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    slug,
                    fields.get("auto_stop_enabled"),
                    fields.get("auto_stop_minutes"),
                    fields.get("env_vars", "{}"),
                    now,
                    now,
                ),
            )
        await db.commit()
        cur2 = await db.execute("SELECT * FROM projects WHERE slug = ?", (slug,))
        return dict(await cur2.fetchone())
    finally:
        await db.close()


async def get_all_projects() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM projects ORDER BY slug")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()
