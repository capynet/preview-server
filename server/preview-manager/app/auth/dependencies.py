"""FastAPI auth dependencies — multi-tenant with org-scoped roles."""

import logging
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, Request

from app.auth import database as auth_db
from app.auth.models import OrgRole, UserWithContext, has_min_role, ORG_ROLE_HIERARCHY
from app.database import get_organization_by_slug, get_org_member, get_project_by_slug, get_project_member

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
            auto_erase_enabled=bool(org.get("auto_erase_enabled", 0)),
            auto_erase_days=org.get("auto_erase_days", 10),
            composer_proxy_enabled=bool(org.get("composer_proxy_enabled", 0)),
            created_at=org["created_at"], updated_at=org["updated_at"],
        )
        user.org_role = OrgRole.owner
        return user

    # Check org membership
    membership = await get_org_member(user.id, org["id"])
    if membership:
        user.org_role = OrgRole(membership["role"])
    else:
        # Check if user has access via project membership
        from app.database import get_pool
        pool = await get_pool()
        has_project = await pool.fetchrow(
            """SELECT 1 FROM project_members pm
               JOIN projects p ON pm.project_id = p.id
               WHERE pm.user_id = $1 AND p.organization_id = $2
               LIMIT 1""",
            user.id, org["id"],
        )
        if not has_project:
            raise HTTPException(status_code=403, detail="Not a member of this organization")
        # Project-only member gets implicit viewer at org level
        user.org_role = OrgRole.viewer

    from app.auth.models import Organization
    user.org = Organization(
        id=org["id"], slug=org["slug"], name=org["name"],
        avatar_url=org.get("avatar_url"),
        gitlab_url=org.get("gitlab_url"),
        auto_erase_enabled=bool(org.get("auto_erase_enabled", 0)),
        auto_erase_days=org.get("auto_erase_days", 10),
        composer_proxy_enabled=bool(org.get("composer_proxy_enabled", 0)),
        created_at=org["created_at"], updated_at=org["updated_at"],
    )
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


async def get_project_context(
    request: Request,
    user: UserWithContext = Depends(get_current_user),
) -> UserWithContext:
    """Resolve org + project context. Sets effective_role = max(org_role, project_role).

    Unlike get_org_context, this allows access for project-only members who
    are not org members.
    """
    from app.auth.models import Organization

    # Resolve org
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

    # Build org model
    org_model = Organization(
        id=org["id"], slug=org["slug"], name=org["name"],
        avatar_url=org.get("avatar_url"),
        gitlab_url=org.get("gitlab_url"),
        auto_erase_enabled=bool(org.get("auto_erase_enabled", 0)),
        auto_erase_days=org.get("auto_erase_days", 10),
        composer_proxy_enabled=bool(org.get("composer_proxy_enabled", 0)),
        created_at=org["created_at"], updated_at=org["updated_at"],
    )
    user.org = org_model

    # Superadmin has implicit owner access
    if user.is_superadmin:
        user.org_role = OrgRole.owner
        user.effective_role = OrgRole.owner
        return user

    # Check org membership
    membership = await get_org_member(user.id, org["id"])
    if membership:
        user.org_role = OrgRole(membership["role"])

    # Resolve project context if available
    project_slug = request.path_params.get("project") or request.path_params.get("slug")
    if project_slug:
        project = await get_project_by_slug(org["id"], project_slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_slug}' not found")

        # Store project in request state for downstream use
        request.state.project = project

        # Check project-level membership
        pm = await get_project_member(user.id, project["id"])
        if pm:
            user.project_role = OrgRole(pm["role"])

    # effective_role = max(org_role, project_role)
    org_level = ORG_ROLE_HIERARCHY.get(user.org_role, 0)
    proj_level = ORG_ROLE_HIERARCHY.get(user.project_role, 0)
    if proj_level > org_level:
        user.effective_role = user.project_role
    else:
        user.effective_role = user.org_role

    # User must have at least some role (org or project)
    if user.effective_role is None:
        raise HTTPException(status_code=403, detail="Not a member of this organization or project")

    return user


def require_project_role(min_role: OrgRole):
    """Return a dependency that enforces a minimum effective role (org + project combined)."""
    async def _check(
        user: UserWithContext = Depends(get_project_context),
    ) -> UserWithContext:
        if not has_min_role(user.effective_role, min_role):
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
