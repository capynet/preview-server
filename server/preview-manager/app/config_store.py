"""Key-value config store backed by the app_config SQLite table."""

import json
import logging
from typing import Optional

from app.database import get_db
from config.settings import settings

logger = logging.getLogger(__name__)


# ---- Generic CRUD ----

async def get_config(key: str) -> Optional[str]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT value FROM app_config WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None
    finally:
        await db.close()


async def set_config(key: str, value: str):
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()
    finally:
        await db.close()


async def delete_config(key: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM app_config WHERE key = ?", (key,))
        await db.commit()
    finally:
        await db.close()


async def get_all_config() -> dict[str, str]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT key, value FROM app_config")
        rows = await cur.fetchall()
        return {row["key"]: row["value"] for row in rows}
    finally:
        await db.close()


# ---- Startup: load DB → settings ----

async def load_config_to_settings():
    """Load all config from DB into the settings singleton."""
    cfg = await get_all_config()
    if "gitlab_url" in cfg:
        settings.gitlab_url = cfg["gitlab_url"]
    if "gitlab_oauth_access_token" in cfg:
        settings.gitlab_oauth_access_token = cfg["gitlab_oauth_access_token"] or None

    logger.info("App config loaded from database")


# ---- GitLab token helpers ----

async def save_gitlab_token(token: str):
    settings.gitlab_oauth_access_token = token
    await set_config("gitlab_oauth_access_token", token)
    logger.info("GitLab token saved to database")


async def remove_gitlab_token():
    settings.gitlab_oauth_access_token = None
    await delete_config("gitlab_oauth_access_token")


# ---- Enabled project IDs helpers ----

async def load_enabled_project_ids() -> set[int]:
    val = await get_config("gitlab_enabled_project_ids")
    if not val:
        return set()
    try:
        return set(json.loads(val))
    except (json.JSONDecodeError, TypeError):
        return set()


async def save_enabled_project_id(project_id: int):
    ids = await load_enabled_project_ids()
    ids.add(project_id)
    await set_config("gitlab_enabled_project_ids", json.dumps(sorted(ids)))


async def clear_enabled_project_ids():
    await delete_config("gitlab_enabled_project_ids")


# ---- Project path helpers (gitlab_id -> path_with_namespace) ----

async def load_project_paths() -> dict[int, str]:
    val = await get_config("gitlab_project_paths")
    if not val:
        return {}
    try:
        raw = json.loads(val)
        return {int(k): v for k, v in raw.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


async def save_project_path(project_id: int, path: str):
    paths = await load_project_paths()
    paths[project_id] = path
    await set_config("gitlab_project_paths", json.dumps({str(k): v for k, v in paths.items()}))


async def get_project_path_by_slug(slug: str) -> str | None:
    paths = await load_project_paths()
    for path in paths.values():
        if path.rsplit("/", 1)[-1] == slug:
            return path
    return None


async def clear_project_paths():
    await delete_config("gitlab_project_paths")


# ---- Allowed email domains helpers ----

async def load_allowed_domains() -> list[dict]:
    """Load allowed email domains from config. Returns list of {domain, role}."""
    val = await get_config("allowed_domains")
    if not val:
        return []
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []


async def save_allowed_domains(domains: list[dict]):
    """Save allowed email domains to config."""
    await set_config("allowed_domains", json.dumps(domains))


async def match_allowed_domain(email: str) -> Optional[str]:
    """Check if email domain matches an allowed domain. Returns role or None."""
    parts = email.rsplit("@", 1)
    if len(parts) != 2:
        return None
    email_domain = parts[1].lower()
    domains = await load_allowed_domains()
    for entry in domains:
        if entry.get("domain", "").lower() == email_domain:
            return entry.get("role")
    return None
