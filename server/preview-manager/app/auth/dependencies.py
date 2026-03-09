"""FastAPI auth dependencies — multi-tenant with org-scoped roles."""

import logging
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, Request

from app.auth import database as auth_db
from app.auth.models import OrgRole, UserWithContext, has_min_role
from app.database import get_organization_by_slug, get_org_member

logger = logging.getLogger(__name__)

SESSION_COOKIE = "pm_session"


async def get_current_user(
    request: Request,
    pm_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> UserWithContext:
    """Resolve the current user from session cookie or Bearer token.

    Returns a UserWithContext WITHOUT org context — use get_org_context()
    or require_org_role() for org-scoped checks.
    """
    user_id: Optional[int] = None
    org_id_from_token: Optional[int] = None

    # 1. Try session cookie
    if pm_session:
        session = await auth_db.get_session(pm_session)
        if session:
            user_id = session["user_id"]

    # 2. Try Bearer token (org-scoped)
    if user_id is None and authorization and authorization.startswith("Bearer "):
        raw_token = authorization[7:]
        token = await auth_db.validate_api_token(raw_token)
        if token:
            user_id = token["user_id"]
            org_id_from_token = token["organization_id"]

    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await auth_db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    ctx = UserWithContext(
        id=user["id"],
        email=user["email"],
        name=user["name"],
        avatar_url=user.get("avatar_url"),
        is_superadmin=bool(user.get("is_superadmin", 0)),
        created_at=user["created_at"],
        updated_at=user["updated_at"],
    )

    # Store token org_id for later resolution
    if org_id_from_token:
        request.state.token_org_id = org_id_from_token

    return ctx


async def get_org_context(
    request: Request,
    user: UserWithContext = Depends(get_current_user),
) -> UserWithContext:
    """Resolve the organization from the URL path parameter `org` and set org context.

    Expects the route to have a path parameter named `org` (the org slug).
    """
    org_slug = request.path_params.get("org")
    if not org_slug:
        raise HTTPException(status_code=400, detail="Organization slug required")

    org = await get_organization_by_slug(org_slug)
    if not org:
        raise HTTPException(status_code=404, detail=f"Organization '{org_slug}' not found")

    # If using an API token, verify it belongs to this org
    token_org_id = getattr(request.state, "token_org_id", None)
    if token_org_id and token_org_id != org["id"]:
        raise HTTPException(status_code=403, detail="Token does not belong to this organization")

    # Superadmin has implicit owner access
    if user.is_superadmin:
        from app.auth.models import Organization
        user.org = Organization(
            id=org["id"], slug=org["slug"], name=org["name"],
            avatar_url=org.get("avatar_url"),
            gitlab_url=org.get("gitlab_url"),
            auto_stop_enabled=bool(org.get("auto_stop_enabled", 1)),
            auto_stop_minutes=org.get("auto_stop_minutes", 15),
            auto_erase_enabled=bool(org.get("auto_erase_enabled", 0)),
            auto_erase_days=org.get("auto_erase_days", 30),
            created_at=org["created_at"], updated_at=org["updated_at"],
        )
        user.org_role = OrgRole.owner
        return user

    # Check org membership
    membership = await get_org_member(user.id, org["id"])
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this organization")

    from app.auth.models import Organization
    user.org = Organization(
        id=org["id"], slug=org["slug"], name=org["name"],
        avatar_url=org.get("avatar_url"),
        gitlab_url=org.get("gitlab_url"),
        auto_stop_enabled=bool(org.get("auto_stop_enabled", 1)),
        auto_stop_minutes=org.get("auto_stop_minutes", 15),
        auto_erase_enabled=bool(org.get("auto_erase_enabled", 0)),
        auto_erase_days=org.get("auto_erase_days", 30),
        created_at=org["created_at"], updated_at=org["updated_at"],
    )
    user.org_role = OrgRole(membership["role"])
    return user


def require_org_role(min_role: OrgRole):
    """Return a dependency that enforces a minimum org role."""
    async def _check(
        user: UserWithContext = Depends(get_org_context),
    ) -> UserWithContext:
        if not has_min_role(user.org_role, min_role):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return _check


def require_superadmin():
    """Return a dependency that enforces superadmin access."""
    async def _check(
        user: UserWithContext = Depends(get_current_user),
    ) -> UserWithContext:
        if not user.is_superadmin:
            raise HTTPException(status_code=403, detail="Superadmin access required")
        return user
    return _check
