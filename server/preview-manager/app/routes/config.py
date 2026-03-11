"""Configuration and health check endpoints — multi-tenant org system."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import require_org_role, get_org_context
from app.auth.models import OrgRole, UserWithContext, CreateTokenRequest
from app.database import (
    update_organization,
    get_project_by_slug,
    upsert_project,
)
from app.auth import database as auth_db

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Org-level auto-erase
# ---------------------------------------------------------------------------


@router.get("/api/orgs/{org}/settings/auto-erase")
async def get_org_auto_erase(
    user: UserWithContext = Depends(require_org_role(OrgRole.owner)),
):
    """Get org-level auto-erase configuration."""
    org = user.org
    return {
        "enabled": org.auto_erase_enabled,
        "days": org.auto_erase_days,
    }


@router.put("/api/orgs/{org}/settings/auto-erase")
async def save_org_auto_erase(
    request: Request,
    user: UserWithContext = Depends(require_org_role(OrgRole.owner)),
):
    """Save org-level auto-erase configuration."""
    body = await request.json()
    updates = {}
    if "enabled" in body:
        updates["auto_erase_enabled"] = 1 if body["enabled"] else 0
    if "days" in body:
        updates["auto_erase_days"] = int(body["days"])
    if updates:
        await update_organization(user.org.id, **updates)
    return {"success": True}


# ---------------------------------------------------------------------------
# Project environment variables
# ---------------------------------------------------------------------------


@router.get("/api/orgs/{org}/projects/{project}/env-vars")
async def get_project_env_vars(
    project: str,
    user: UserWithContext = Depends(require_org_role(OrgRole.member)),
):
    """Get environment variables for a project."""
    proj = await get_project_by_slug(user.org.id, project)
    if not proj or not proj.get("env_vars"):
        return {"env_vars": {}}
    try:
        return {"env_vars": json.loads(proj["env_vars"])}
    except (json.JSONDecodeError, TypeError):
        return {"env_vars": {}}


@router.put("/api/orgs/{org}/projects/{project}/env-vars")
async def save_project_env_vars(
    project: str,
    request: Request,
    user: UserWithContext = Depends(require_org_role(OrgRole.admin)),
):
    """Save environment variables for a project."""
    body = await request.json()
    env_vars = body.get("env_vars", {})
    if not isinstance(env_vars, dict):
        raise HTTPException(status_code=400, detail="env_vars must be an object")
    for k, v in env_vars.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise HTTPException(
                status_code=400, detail="All keys and values must be strings"
            )
    await upsert_project(user.org.id, project, env_vars=json.dumps(env_vars))

    # Check if there are active previews that would need a rebuild
    from app.database import get_all_previews

    all_previews = await get_all_previews(org_id=user.org.id)
    active_previews = [
        p["preview_name"]
        for p in all_previews
        if p.get("project_slug") == project
        and p["status"] in ("active", "failed")
    ]

    return {
        "success": True,
        "env_vars": env_vars,
        "needs_rebuild": len(active_previews) > 0,
        "affected_previews": active_previews,
    }


# ---------------------------------------------------------------------------
# API tokens (per user, scoped to org)
# ---------------------------------------------------------------------------


@router.get("/api/orgs/{org}/tokens")
async def list_tokens(
    user: UserWithContext = Depends(require_org_role(OrgRole.owner)),
):
    """List API tokens for the current user in this org."""
    tokens = await auth_db.list_api_tokens(user.id, user.org.id)
    return {"tokens": tokens}


@router.post("/api/orgs/{org}/tokens")
async def create_token(
    body: CreateTokenRequest,
    user: UserWithContext = Depends(require_org_role(OrgRole.owner)),
):
    """Create an API token for the current user in this org."""
    token_id, raw_token = await auth_db.create_api_token(
        user.id, user.org.id, body.name
    )
    return {"token_id": token_id, "token": raw_token}


@router.delete("/api/orgs/{org}/tokens/{token_id}")
async def delete_token(
    token_id: int,
    user: UserWithContext = Depends(require_org_role(OrgRole.owner)),
):
    """Delete an API token."""
    deleted = await auth_db.delete_api_token(token_id, user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"success": True}


# ---------------------------------------------------------------------------
# Health & root
# ---------------------------------------------------------------------------


@router.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@router.get("/")
async def root():
    """Root endpoint."""
    return {"status": "ok"}
