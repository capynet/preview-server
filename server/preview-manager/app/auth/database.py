"""Auth-specific CRUD functions.

Shared DB infrastructure (get_pool, _now) lives in app.database.
This module contains user, session, OAuth, and token queries.
"""

import hashlib
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from app.database import get_pool, _now

logger = logging.getLogger(__name__)


def _row_to_dict(row) -> dict:
    return dict(row)


# ---- Users ----

async def get_user_by_id(user_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    return _row_to_dict(row) if row else None


async def get_user_by_email(email: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM users WHERE email = $1", email)
    return _row_to_dict(row) if row else None


async def create_user(email: str, name: str, avatar_url: Optional[str] = None, is_superadmin: bool = False, system_role: Optional[str] = None) -> dict:
    now = _now()
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO users (email, name, avatar_url, is_superadmin, system_role, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
        email, name, avatar_url, int(is_superadmin), system_role, now, now,
    )
    return _row_to_dict(row)


async def user_count() -> int:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT COUNT(*) as cnt FROM users")
    return row["cnt"]


async def list_users() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM users ORDER BY id")
    return [_row_to_dict(r) for r in rows]


async def delete_user(user_id: int):
    pool = await get_pool()
    await pool.execute("DELETE FROM users WHERE id = $1", user_id)


async def is_setup_complete() -> bool:
    return (await user_count()) > 0


# ---- OAuth Accounts ----

async def get_oauth_account(provider: str, provider_user_id: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM oauth_accounts WHERE provider = $1 AND provider_user_id = $2",
        provider, provider_user_id,
    )
    return _row_to_dict(row) if row else None


async def create_oauth_account(user_id: int, provider: str, provider_user_id: str, provider_username: Optional[str] = None):
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO oauth_accounts (user_id, provider, provider_user_id, provider_username, created_at)
           VALUES ($1, $2, $3, $4, $5)""",
        user_id, provider, provider_user_id, provider_username, _now(),
    )


# ---- Sessions ----

async def create_session(user_id: int) -> str:
    session_id = secrets.token_urlsafe(32)
    now = _now()
    expires = datetime.fromtimestamp(
        time.time() + settings.session_max_age_seconds, tz=timezone.utc
    ).isoformat()
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES ($1, $2, $3, $4)",
        session_id, user_id, now, expires,
    )
    return session_id


async def get_session(session_id: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
    if not row:
        return None
    session = _row_to_dict(row)
    if session["expires_at"] < _now():
        await pool.execute("DELETE FROM sessions WHERE id = $1", session_id)
        return None
    # Renew session on each access (sliding window)
    new_expires = datetime.fromtimestamp(
        time.time() + settings.session_max_age_seconds, tz=timezone.utc
    ).isoformat()
    await pool.execute(
        "UPDATE sessions SET expires_at = $1 WHERE id = $2", new_expires, session_id,
    )
    return session


async def delete_session(session_id: str):
    pool = await get_pool()
    await pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


# ---- API Tokens ----

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_api_token(user_id: int, org_id: Optional[int], name: str) -> tuple[int, str]:
    """Returns (token_id, raw_token). The raw token is only returned once."""
    raw_token = secrets.token_urlsafe(48)
    token_hash = _hash_token(raw_token)
    token_prefix = raw_token[:8]
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO api_tokens (user_id, organization_id, name, token_hash, token_prefix, created_at)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
        user_id, org_id, name, token_hash, token_prefix, _now(),
    )
    return row["id"], raw_token


async def validate_api_token(raw_token: str) -> Optional[dict]:
    token_hash = _hash_token(raw_token)
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM api_tokens WHERE token_hash = $1", token_hash)
    if not row:
        return None
    token = _row_to_dict(row)
    await pool.execute("UPDATE api_tokens SET last_used_at = $1 WHERE id = $2", _now(), token["id"])
    return token


async def list_api_tokens(user_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, user_id, organization_id, name, token_prefix, created_at, last_used_at
           FROM api_tokens WHERE user_id = $1 ORDER BY id""",
        user_id,
    )
    return [_row_to_dict(r) for r in rows]


async def delete_api_token(token_id: int, user_id: int) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM api_tokens WHERE id = $1 AND user_id = $2", token_id, user_id
    )
    # asyncpg returns "DELETE N" string
    return result != "DELETE 0"


# ---- CLI Auth Requests ----

async def create_cli_auth_request(code: str, org_id: Optional[int] = None):
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO cli_auth_requests (code, organization_id, status, created_at) VALUES ($1, $2, 'pending', $3)",
        code, org_id, _now(),
    )


async def get_cli_auth_request(code: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM cli_auth_requests WHERE code = $1", code)
    return _row_to_dict(row) if row else None


async def approve_cli_auth_request(code: str, user_id: int, token: str):
    pool = await get_pool()
    await pool.execute(
        "UPDATE cli_auth_requests SET status = 'approved', user_id = $1, token = $2 WHERE code = $3 AND status = 'pending'",
        user_id, token, code,
    )


# ---- SSH Keys ----

async def add_ssh_key(user_id: int, name: str, public_key: str, fingerprint: str) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO ssh_keys (user_id, name, public_key, fingerprint, created_at) VALUES ($1, $2, $3, $4, $5) RETURNING id, name, fingerprint, created_at",
        user_id, name, public_key, fingerprint, _now(),
    )
    return dict(row)


async def list_ssh_keys(user_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id, name, fingerprint, created_at FROM ssh_keys WHERE user_id = $1 ORDER BY id",
        user_id,
    )
    return [dict(r) for r in rows]


async def delete_ssh_key(key_id: int, user_id: int) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM ssh_keys WHERE id = $1 AND user_id = $2", key_id, user_id
    )
    return result != "DELETE 0"


async def get_all_ssh_keys() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT public_key FROM ssh_keys ORDER BY id")
    return [dict(r) for r in rows]


# ---- Magic Link Tokens ----

async def create_magic_link_token(user_id: int, expires_minutes: int = 15) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = datetime.fromtimestamp(
        time.time() + expires_minutes * 60, tz=timezone.utc
    ).isoformat()
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO magic_link_tokens (user_id, token, created_at, expires_at) VALUES ($1, $2, $3, $4)",
        user_id, token, now, expires,
    )
    return token


async def validate_and_consume_magic_link_token(token: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """UPDATE magic_link_tokens
           SET used_at = $1
           WHERE token = $2 AND used_at IS NULL AND expires_at > $1
           RETURNING user_id""",
        _now(), token,
    )
    return _row_to_dict(row) if row else None
