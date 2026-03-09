"""Auth-specific CRUD functions.

Shared DB infrastructure (get_db, _now, init_db) lives in app.database.
This module contains user, session, OAuth, and token queries.
"""

import hashlib
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

import bcrypt

from config.settings import settings
from app.database import get_db, _now

logger = logging.getLogger(__name__)


# ---- Users ----

async def get_user_by_id(user_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_user_by_email(email: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def create_user(email: str, name: str, avatar_url: Optional[str] = None, is_superadmin: bool = False) -> dict:
    now = _now()
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO users (email, name, avatar_url, is_superadmin, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (email, name, avatar_url, int(is_superadmin), now, now),
        )
        await db.commit()
        user_id = cur.lastrowid
        return {
            "id": user_id, "email": email, "name": name,
            "avatar_url": avatar_url, "is_superadmin": int(is_superadmin),
            "created_at": now, "updated_at": now,
        }
    finally:
        await db.close()


async def user_count() -> int:
    db = await get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) as cnt FROM users")
        row = await cur.fetchone()
        return row["cnt"]
    finally:
        await db.close()


async def list_users() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM users ORDER BY id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def delete_user(user_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()
    finally:
        await db.close()


# ---- Setup / Password ----

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


async def create_user_with_password(
    email: str, name: str, password: str, is_superadmin: bool = False
) -> dict:
    now = _now()
    pw_hash = hash_password(password)
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO users (email, name, password_hash, is_superadmin, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (email, name, pw_hash, int(is_superadmin), now, now),
        )
        await db.commit()
        user_id = cur.lastrowid
        return {
            "id": user_id, "email": email, "name": name,
            "avatar_url": None, "is_superadmin": int(is_superadmin),
            "created_at": now, "updated_at": now,
        }
    finally:
        await db.close()


async def get_user_by_email_and_password(email: str, password: str) -> Optional[dict]:
    user = await get_user_by_email(email)
    if not user or not user.get("password_hash"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


async def is_setup_complete() -> bool:
    return (await user_count()) > 0


# ---- OAuth Accounts ----

async def get_oauth_account(provider: str, provider_user_id: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM oauth_accounts WHERE provider = ? AND provider_user_id = ?",
            (provider, provider_user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def create_oauth_account(user_id: int, provider: str, provider_user_id: str, provider_username: Optional[str] = None):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO oauth_accounts (user_id, provider, provider_user_id, provider_username, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, provider, provider_user_id, provider_username, _now()),
        )
        await db.commit()
    finally:
        await db.close()


# ---- Sessions ----

async def create_session(user_id: int) -> str:
    session_id = secrets.token_urlsafe(32)
    now = _now()
    expires = datetime.fromtimestamp(
        time.time() + settings.session_max_age_seconds, tz=timezone.utc
    ).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, now, expires),
        )
        await db.commit()
        return session_id
    finally:
        await db.close()


async def get_session(session_id: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        if not row:
            return None
        session = dict(row)
        if session["expires_at"] < _now():
            await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await db.commit()
            return None
        return session
    finally:
        await db.close()


async def delete_session(session_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db.commit()
    finally:
        await db.close()


# ---- API Tokens (org-scoped) ----

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_api_token(user_id: int, org_id: int, name: str) -> tuple[int, str]:
    """Returns (token_id, raw_token). The raw token is only returned once."""
    raw_token = secrets.token_urlsafe(48)
    token_hash = _hash_token(raw_token)
    token_prefix = raw_token[:8]
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO api_tokens (user_id, organization_id, name, token_hash, token_prefix, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, org_id, name, token_hash, token_prefix, _now()),
        )
        await db.commit()
        return cur.lastrowid, raw_token
    finally:
        await db.close()


async def validate_api_token(raw_token: str) -> Optional[dict]:
    token_hash = _hash_token(raw_token)
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,))
        row = await cur.fetchone()
        if not row:
            return None
        token = dict(row)
        await db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (_now(), token["id"]))
        await db.commit()
        return token
    finally:
        await db.close()


async def list_api_tokens(user_id: int, org_id: int) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, user_id, organization_id, name, token_prefix, created_at, last_used_at FROM api_tokens WHERE user_id = ? AND organization_id = ? ORDER BY id",
            (user_id, org_id),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def delete_api_token(token_id: int, user_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user_id))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# ---- CLI Auth Requests ----

async def create_cli_auth_request(code: str, org_id: Optional[int] = None):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO cli_auth_requests (code, organization_id, status, created_at) VALUES (?, ?, 'pending', ?)",
            (code, org_id, _now()),
        )
        await db.commit()
    finally:
        await db.close()


async def get_cli_auth_request(code: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM cli_auth_requests WHERE code = ?", (code,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def approve_cli_auth_request(code: str, user_id: int, token: str):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE cli_auth_requests SET status = 'approved', user_id = ?, token = ? WHERE code = ? AND status = 'pending'",
            (user_id, token, code),
        )
        await db.commit()
    finally:
        await db.close()
