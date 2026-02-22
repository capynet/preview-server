"""FastAPI auth dependencies"""

import logging
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Request

from app.auth import database as db
from app.auth.models import Role, UserWithRole, has_min_role

logger = logging.getLogger(__name__)

SESSION_COOKIE = "pm_session"


async def get_current_user(
    request: Request,
    pm_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> UserWithRole:
    """Resolve the current user from session cookie or Bearer token."""
    user_id: Optional[int] = None

    # 1. Try session cookie
    if pm_session:
        session = await db.get_session(pm_session)
        if session:
            user_id = session["user_id"]

    # 2. Try Bearer token
    if user_id is None and authorization and authorization.startswith("Bearer "):
        raw_token = authorization[7:]
        token = await db.validate_api_token(raw_token)
        if token:
            user_id = token["user_id"]

    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    role_str = await db.get_role(user_id)
    role = Role(role_str) if role_str else None

    return UserWithRole(
        id=user["id"],
        email=user["email"],
        name=user["name"],
        avatar_url=user.get("avatar_url"),
        created_at=user["created_at"],
        updated_at=user["updated_at"],
        role=role,
    )


def require_role(min_role: Role):
    """Return a dependency that enforces a minimum role."""
    async def _check(
        user: UserWithRole = __import__("fastapi").Depends(get_current_user),
    ) -> UserWithRole:
        if not has_min_role(user.role, min_role):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return _check
