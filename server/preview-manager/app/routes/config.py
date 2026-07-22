"""Configuration and health check endpoints — multi-tenant org system."""

import json
import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request

from config.settings import settings
from app.auth.dependencies import require_org_role, require_project_role, get_org_context, require_superadmin
from app.auth.models import OrgRole, UserWithContext
from app.database import (
    update_organization,
    get_project_by_slug,
    upsert_project,
    get_effective_require_ci,
    update_org_require_ci,
    update_project_require_ci,
)

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
# Org-level composer proxy
# ---------------------------------------------------------------------------


@router.get("/api/orgs/{org}/settings/composer-proxy")
async def get_org_composer_proxy(
    user: UserWithContext = Depends(require_org_role(OrgRole.owner)),
):
    """Get org-level composer proxy configuration."""
    return {
        "enabled": user.org.composer_proxy_enabled,
    }


@router.put("/api/orgs/{org}/settings/composer-proxy")
async def save_org_composer_proxy(
    request: Request,
    user: UserWithContext = Depends(require_org_role(OrgRole.owner)),
):
    """Save org-level composer proxy configuration."""
    body = await request.json()
    enabled = 1 if body.get("enabled") else 0
    await update_organization(user.org.id, composer_proxy_enabled=enabled)
    return {"success": True}


# ---------------------------------------------------------------------------
# Org-level require CI success
# ---------------------------------------------------------------------------


@router.get("/api/orgs/{org}/settings/require-ci")
async def get_org_require_ci(
    user: UserWithContext = Depends(require_org_role(OrgRole.owner)),
):
    """Get org-level require-ci-success configuration."""
    from app.database import get_organization_by_id
    org_data = await get_organization_by_id(user.org.id)
    return {
        "enabled": bool(org_data.get("require_ci_success", 0)) if org_data else False,
    }


@router.put("/api/orgs/{org}/settings/require-ci")
async def save_org_require_ci(
    request: Request,
    user: UserWithContext = Depends(require_org_role(OrgRole.owner)),
):
    """Save org-level require-ci-success configuration."""
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    await update_org_require_ci(user.org.id, enabled)
    return {"success": True}


@router.get("/api/orgs/{org}/projects/{project}/settings/require-ci")
async def get_project_require_ci(
    project: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Get project-level require-ci-success configuration."""
    proj = await get_project_by_slug(user.org.id, project)
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project '{project}' not found")

    project_val = proj.get("require_ci_success")
    effective = await get_effective_require_ci(user.org.id, proj["id"])

    return {
        "value": None if project_val is None else bool(project_val),
        "inherited": project_val is None,
        "effective": effective,
    }


@router.put("/api/orgs/{org}/projects/{project}/settings/require-ci")
async def save_project_require_ci(
    project: str,
    request: Request,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Save project-level require-ci-success configuration.

    Body: {"value": null} to inherit from org, {"value": true/false} to override.
    """
    proj = await get_project_by_slug(user.org.id, project)
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project '{project}' not found")

    body = await request.json()
    raw_value = body.get("value")
    if raw_value is None:
        value = None
    else:
        value = bool(raw_value)

    await update_project_require_ci(user.org.id, project, value)
    return {"success": True}


# ---------------------------------------------------------------------------
# Project environment variables
# ---------------------------------------------------------------------------


@router.get("/api/orgs/{org}/projects/{project}/env-vars")
async def get_project_env_vars(
    project: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
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
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
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
# Project cron jobs
# ---------------------------------------------------------------------------


@router.get("/api/orgs/{org}/projects/{project}/cron-jobs")
async def get_project_cron_jobs(
    project: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Get cron jobs for a project."""
    from app.cron_jobs import load_cron_jobs

    proj = await get_project_by_slug(user.org.id, project)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"cron_jobs": load_cron_jobs(proj.get("cron_jobs"))}


@router.put("/api/orgs/{org}/projects/{project}/cron-jobs")
async def save_project_cron_jobs(
    project: str,
    request: Request,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Save cron jobs for a project."""
    from app.cron_jobs import CronJobValidationError, validate_cron_jobs

    body = await request.json()
    try:
        clean = validate_cron_jobs(body.get("cron_jobs"))
    except CronJobValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await upsert_project(user.org.id, project, cron_jobs=json.dumps(clean))

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
        "cron_jobs": clean,
        "needs_rebuild": len(active_previews) > 0,
        "affected_previews": active_previews,
    }


# ---------------------------------------------------------------------------
# Project auto-preview rules (webhook skip patterns)
# ---------------------------------------------------------------------------


@router.get("/api/orgs/{org}/projects/{project}/preview-rules")
async def get_project_preview_rules(
    project: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Get auto-preview skip rules for a project."""
    from app.preview_rules import load_patterns

    proj = await get_project_by_slug(user.org.id, project)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "skip_source_branches": load_patterns(proj.get("skip_source_branches")),
        "skip_target_branches": load_patterns(proj.get("skip_target_branches")),
    }


@router.put("/api/orgs/{org}/projects/{project}/preview-rules")
async def save_project_preview_rules(
    project: str,
    request: Request,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Save auto-preview skip rules for a project."""
    from app.preview_rules import PreviewRulesValidationError, validate_preview_rules

    body = await request.json()
    try:
        clean = validate_preview_rules(body)
    except PreviewRulesValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await upsert_project(
        user.org.id, project,
        skip_source_branches=json.dumps(clean["skip_source_branches"]),
        skip_target_branches=json.dumps(clean["skip_target_branches"]),
    )

    return {"success": True, **clean}


# ---------------------------------------------------------------------------
# Project public paths (bypass forward_auth)
# ---------------------------------------------------------------------------


@router.get("/api/orgs/{org}/projects/{project}/settings/public-paths")
async def get_project_public_paths(
    project: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Get public paths for a project (paths that bypass preview auth)."""
    proj = await get_project_by_slug(user.org.id, project)
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project '{project}' not found")
    try:
        paths = json.loads(proj["public_paths"]) if proj.get("public_paths") else []
    except (json.JSONDecodeError, TypeError):
        paths = []
    return {"public_paths": paths}


@router.put("/api/orgs/{org}/projects/{project}/settings/public-paths")
async def save_project_public_paths(
    project: str,
    request: Request,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Save public paths for a project.

    These paths bypass forward_auth on preview domains, allowing external
    access (e.g. for Drupal OAuth server endpoints).
    """
    body = await request.json()
    paths = body.get("public_paths", [])
    if not isinstance(paths, list):
        raise HTTPException(status_code=400, detail="public_paths must be an array")
    for p in paths:
        if not isinstance(p, str) or not p.startswith("/"):
            raise HTTPException(
                status_code=400, detail="Each path must be a string starting with /"
            )

    await upsert_project(user.org.id, project, public_paths=json.dumps(paths))

    # Rebuild Caddy routes for active previews of this project
    from app.caddy_api import caddy_manager
    await caddy_manager.refresh_project_public_paths(user.org.id, project, paths)

    return {"success": True, "public_paths": paths}


# ---------------------------------------------------------------------------
# Disk usage (superadmin only)
# ---------------------------------------------------------------------------


@router.get("/api/system/disk-usage")
async def get_disk_usage(
    user=Depends(require_superadmin()),
):
    """Get cached disk usage of key server directories."""
    from app.valkey import get_disk_usage as get_shared_disk_usage
    from app.websockets import get_disk_usage_cache
    # Prefer the snapshot shared across workers; fall back to this worker's
    # local mirror for the brief window before the first one is computed.
    shared = await get_shared_disk_usage()
    return shared or get_disk_usage_cache()


# ---------------------------------------------------------------------------
# Health & root
# ---------------------------------------------------------------------------


@router.get("/api/health")
async def health_check():
    """Health check endpoint. Reports maintenance state + in-flight deploy count
    so the Ansible drain step can poll for a quiet worker before restarting."""
    from app.valkey import get_maintenance, list_active_deploy_locks
    m = await get_maintenance()
    if m.get("active"):
        try:
            m = {**m, "active_deploys": len(await list_active_deploy_locks())}
        except Exception:
            m = {**m, "active_deploys": None}
    return {"status": "healthy", "maintenance": m}


# ---------------------------------------------------------------------------
# Maintenance mode (control-plane drain)
# ---------------------------------------------------------------------------

async def require_admin(
    pm_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None),
) -> dict:
    """Allow a superadmin (session cookie / bearer token) OR a machine caller
    presenting the shared X-Admin-Token (used by the Ansible drain playbook)."""
    if settings.admin_api_token and x_admin_token and secrets.compare_digest(
        x_admin_token, settings.admin_api_token
    ):
        return {"kind": "token", "by": "machine-token"}

    from app.auth import database as auth_db
    user_id: Optional[int] = None
    if pm_session:
        session = await auth_db.get_session(pm_session)
        if session:
            user_id = session["user_id"]
    if user_id is None and authorization and authorization.startswith("Bearer "):
        token = await auth_db.validate_api_token(authorization[7:])
        if token:
            user_id = token["user_id"]
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = await auth_db.get_user_by_id(user_id)
    if not user or not user.get("is_superadmin"):
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return {"kind": "user", "by": user.get("email", f"user:{user_id}")}


async def _maintenance_snapshot() -> dict:
    """Current maintenance state plus the in-flight deploy count/keys."""
    from app.valkey import get_maintenance, list_active_deploy_locks
    state = await get_maintenance()
    try:
        keys = await list_active_deploy_locks()
    except Exception:
        keys = []
    return {**state, "active_deploys": len(keys), "deploy_keys": keys}


@router.get("/api/admin/maintenance")
async def get_maintenance_status(admin: dict = Depends(require_admin)):
    """Read maintenance state + in-flight deploys (used by the drain poll)."""
    return await _maintenance_snapshot()


@router.post("/api/admin/maintenance")
async def set_maintenance_mode(request: Request, admin: dict = Depends(require_admin)):
    """Enable/disable maintenance. Body: {active: bool, reason?, level?}."""
    body = await request.json()
    active = bool(body.get("active"))
    reason = str(body.get("reason", ""))
    level = str(body.get("level", "drain"))

    from app.valkey import set_maintenance
    await set_maintenance(active, reason=reason, by=admin.get("by", ""), level=level)
    logger.info(f"Maintenance {'ENABLED' if active else 'disabled'} by {admin.get('by')} "
                f"(level={level}, reason={reason!r})")

    # Push the new state to every connected UI client immediately. Published via
    # Valkey so clients on ALL uvicorn workers get it, not just this process.
    try:
        from app.valkey import publish_event
        await publish_event("previews:global", {"action": "maintenance"})
    except Exception as e:
        logger.warning(f"Failed to broadcast maintenance state: {e}")

    return await _maintenance_snapshot()


@router.get("/")
async def root():
    """Root endpoint."""
    return {"status": "ok"}
