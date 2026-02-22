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
    if "gitlab_group_name" in cfg:
        settings.gitlab_group_name = cfg["gitlab_group_name"]
    if "gitlab_oauth_access_token" in cfg:
        settings.gitlab_oauth_access_token = cfg["gitlab_oauth_access_token"] or None
    if "gitlab_oauth_refresh_token" in cfg:
        settings.gitlab_oauth_refresh_token = cfg["gitlab_oauth_refresh_token"] or None
    if "gitlab_oauth_token_expires_at" in cfg:
        val = cfg["gitlab_oauth_token_expires_at"]
        settings.gitlab_oauth_token_expires_at = int(val) if val else None

    logger.info("App config loaded from database")


# ---- OAuth token helpers ----

async def save_oauth_tokens(access_token: str, refresh_token: Optional[str], expires_at: Optional[int]):
    settings.gitlab_oauth_access_token = access_token
    settings.gitlab_oauth_refresh_token = refresh_token
    settings.gitlab_oauth_token_expires_at = expires_at

    await set_config("gitlab_oauth_access_token", access_token)
    await set_config("gitlab_oauth_refresh_token", refresh_token or "")
    await set_config("gitlab_oauth_token_expires_at", str(expires_at) if expires_at else "")

    logger.info("GitLab OAuth tokens saved to database")


async def remove_oauth_tokens():
    settings.gitlab_oauth_access_token = None
    settings.gitlab_oauth_refresh_token = None
    settings.gitlab_oauth_token_expires_at = None

    await delete_config("gitlab_oauth_access_token")
    await delete_config("gitlab_oauth_refresh_token")
    await delete_config("gitlab_oauth_token_expires_at")


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
