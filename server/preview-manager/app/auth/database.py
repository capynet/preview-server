"""SQLite database for auth (users, sessions, tokens, roles).

Shared DB infrastructure (get_db, _now, init_db) lives in app.database.
This module contains only auth-specific CRUD functions.
"""

import hashlib
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

import bcrypt

from config.settings import settings
from app.auth.models import Role
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


async def create_user(email: str, name: str, avatar_url: Optional[str] = None) -> dict:
    now = _now()
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO users (email, name, avatar_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (email, name, avatar_url, now, now),
        )
        await db.commit()
        user_id = cur.lastrowid
        return {"id": user_id, "email": email, "name": name, "avatar_url": avatar_url, "created_at": now, "updated_at": now}
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
        cur = await db.execute(
            "SELECT u.*, r.role FROM users u LEFT JOIN roles r ON u.id = r.user_id ORDER BY u.id"
        )
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


async def create_user_with_password(email: str, name: str, password: str) -> dict:
    """Create a user with email+password (used for initial setup)."""
    now = _now()
    pw_hash = hash_password(password)
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO users (email, name, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (email, name, pw_hash, now, now),
        )
        await db.commit()
        user_id = cur.lastrowid
        return {"id": user_id, "email": email, "name": name, "avatar_url": None, "created_at": now, "updated_at": now}
    finally:
        await db.close()


async def get_user_by_email_and_password(email: str, password: str) -> Optional[dict]:
    """Validate email+password login. Returns user dict or None."""
    user = await get_user_by_email(email)
    if not user:
        return None
    if not user.get("password_hash"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


async def is_setup_complete() -> bool:
    """Returns True if at least one user exists."""
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


# ---- Roles ----

async def get_role(user_id: int) -> Optional[str]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT role FROM roles WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row["role"] if row else None
    finally:
        await db.close()


async def set_role(user_id: int, role: str):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO roles (user_id, role) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET role = excluded.role",
            (user_id, role),
        )
        await db.commit()
    finally:
        await db.close()


async def delete_role(user_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM roles WHERE user_id = ?", (user_id,))
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


# ---- API Tokens ----

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_api_token(user_id: int, name: str) -> tuple[int, str]:
    """Returns (token_id, raw_token). The raw token is only returned once."""
    raw_token = secrets.token_urlsafe(48)
    token_hash = _hash_token(raw_token)
    token_prefix = raw_token[:8]
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO api_tokens (user_id, name, token_hash, token_prefix, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, token_hash, token_prefix, _now()),
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


async def list_api_tokens(user_id: int) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, user_id, name, token_prefix, created_at, last_used_at FROM api_tokens WHERE user_id = ? ORDER BY id",
            (user_id,),
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

async def create_cli_auth_request(code: str):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO cli_auth_requests (code, status, created_at) VALUES (?, 'pending', ?)",
            (code, _now()),
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


# ---- Invitations ----

async def create_invitation(email: str, role: str, invited_by: int, project_slug: Optional[str] = None) -> dict:
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = datetime.fromtimestamp(
        time.time() + 7 * 24 * 3600, tz=timezone.utc
    ).isoformat()
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO invitations (email, role, token, invited_by, status, created_at, expires_at, project_slug) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (email, role, token, invited_by, now, expires, project_slug),
        )
        await db.commit()
        return {
            "id": cur.lastrowid, "email": email, "role": role, "token": token,
            "invited_by": invited_by, "status": "pending", "created_at": now, "expires_at": expires,
            "project_slug": project_slug,
        }
    finally:
        await db.close()


async def get_invitation_by_token(token: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM invitations WHERE token = ? AND status = 'pending'", (token,))
        row = await cur.fetchone()
        if not row:
            return None
        inv = dict(row)
        if inv["expires_at"] < _now():
            return None
        return inv
    finally:
        await db.close()


async def get_invitation_by_email(email: str) -> Optional[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM invitations WHERE email = ? AND status = 'pending'", (email,))
        row = await cur.fetchone()
        if not row:
            return None
        inv = dict(row)
        if inv["expires_at"] < _now():
            return None
        return inv
    finally:
        await db.close()


async def list_invitations(project_slug: Optional[str] = None) -> list[dict]:
    db = await get_db()
    try:
        if project_slug:
            cur = await db.execute(
                "SELECT i.*, u.name as invited_by_name FROM invitations i JOIN users u ON i.invited_by = u.id WHERE i.status = 'pending' AND i.project_slug = ? ORDER BY i.id DESC",
                (project_slug,),
            )
        else:
            cur = await db.execute(
                "SELECT i.*, u.name as invited_by_name FROM invitations i JOIN users u ON i.invited_by = u.id WHERE i.status = 'pending' ORDER BY i.id DESC"
            )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def delete_invitation(invitation_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM invitations WHERE id = ?", (invitation_id,))
        await db.commit()
    finally:
        await db.close()


async def mark_invitation_accepted(invitation_id: int):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE invitations SET status = 'accepted' WHERE id = ?", (invitation_id,)
        )
        await db.commit()
    finally:
        await db.close()


# ---- Project Members ----

async def list_project_members(project_slug: str) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT u.id, u.email, u.name, u.avatar_url, r.role "
            "FROM project_members pm "
            "JOIN users u ON pm.user_id = u.id "
            "LEFT JOIN roles r ON u.id = r.user_id "
            "WHERE pm.project_slug = ? ORDER BY u.name",
            (project_slug,),
        )
        return [dict(row) for row in await cur.fetchall()]
    finally:
        await db.close()


async def add_project_member(user_id: int, project_slug: str, added_by: int):
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO project_members (user_id, project_slug, added_by, created_at) VALUES (?, ?, ?, ?)",
            (user_id, project_slug, added_by, _now()),
        )
        await db.commit()
    finally:
        await db.close()


async def remove_project_member(user_id: int, project_slug: str):
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM project_members WHERE user_id = ? AND project_slug = ?",
            (user_id, project_slug),
        )
        await db.commit()
    finally:
        await db.close()


async def get_user_project_slugs(user_id: int) -> list[str]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT project_slug FROM project_members WHERE user_id = ?",
            (user_id,),
        )
        return [row["project_slug"] for row in await cur.fetchall()]
    finally:
        await db.close()
