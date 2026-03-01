"""Configuration and health check endpoints"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from config.settings import settings
from app.auth.dependencies import require_role
from app.auth.models import Role, UserWithRole
from app import config_store

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/config")
async def get_app_config(user: UserWithRole = Depends(require_role(Role.admin))):
    """Get application configuration"""
    return {
        "gitlab_url": settings.gitlab_url,
        "gitlab_group_name": settings.gitlab_group_name,
    }


@router.post("/api/config")
async def save_app_config(request: Request, user: UserWithRole = Depends(require_role(Role.admin))):
    """Save application configuration"""
    try:
        body = await request.json()

        gitlab_url = body.get("gitlab_url", settings.gitlab_url)
        gitlab_group_name = body.get("gitlab_group_name", settings.gitlab_group_name)

        await config_store.set_config("gitlab_url", gitlab_url)
        await config_store.set_config("gitlab_group_name", gitlab_group_name)

        settings.gitlab_url = gitlab_url
        settings.gitlab_group_name = gitlab_group_name

        logger.info("App configuration saved to database")

        return {
            "success": True,
            "message": "Configuration saved successfully"
        }
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        raise HTTPException(status_code=500, detail=f"Error saving configuration: {str(e)}")


# ---- Auto-stop configuration ----

@router.get("/api/config/auto-stop")
async def get_auto_stop_config(user: UserWithRole = Depends(require_role(Role.viewer))):
    """Get global auto-stop configuration."""
    enabled = await config_store.get_config("auto_stop_enabled")
    minutes = await config_store.get_config("auto_stop_minutes")
    return {
        "enabled": enabled == "true",
        "minutes": int(minutes) if minutes else 60,
    }


@router.put("/api/config/auto-stop")
async def save_auto_stop_config(request: Request, user: UserWithRole = Depends(require_role(Role.admin))):
    """Save global auto-stop configuration."""
    body = await request.json()
    await config_store.set_config("auto_stop_enabled", "true" if body.get("enabled") else "false")
    if "minutes" in body:
        await config_store.set_config("auto_stop_minutes", str(int(body["minutes"])))
    return {"success": True}


@router.get("/api/config/auto-stop/{project}")
async def get_project_auto_stop_config(project: str, user: UserWithRole = Depends(require_role(Role.viewer))):
    """Get per-project auto-stop configuration."""
    enabled = await config_store.get_config(f"auto_stop_{project}_enabled")
    minutes = await config_store.get_config(f"auto_stop_{project}_minutes")
    return {
        "override": enabled is not None,
        "enabled": enabled == "true" if enabled is not None else None,
        "minutes": int(minutes) if minutes else None,
    }


@router.put("/api/config/auto-stop/{project}")
async def save_project_auto_stop_config(project: str, request: Request, user: UserWithRole = Depends(require_role(Role.admin))):
    """Save per-project auto-stop configuration."""
    body = await request.json()
    if body.get("override") is False:
        # Remove overrides, use global
        await config_store.delete_config(f"auto_stop_{project}_enabled")
        await config_store.delete_config(f"auto_stop_{project}_minutes")
    else:
        await config_store.set_config(f"auto_stop_{project}_enabled", "true" if body.get("enabled") else "false")
        if "minutes" in body:
            await config_store.set_config(f"auto_stop_{project}_minutes", str(int(body["minutes"])))
    return {"success": True}


@router.get("/api/config/auto-erase")
async def get_auto_erase_config(user: UserWithRole = Depends(require_role(Role.viewer))):
    """Get global auto-erase configuration."""
    enabled = await config_store.get_config("auto_erase_enabled")
    days = await config_store.get_config("auto_erase_days")
    return {
        "enabled": enabled == "true",
        "days": int(days) if days else 7,
    }


@router.put("/api/config/auto-erase")
async def save_auto_erase_config(request: Request, user: UserWithRole = Depends(require_role(Role.admin))):
    """Save global auto-erase configuration."""
    body = await request.json()
    await config_store.set_config("auto_erase_enabled", "true" if body.get("enabled") else "false")
    if "days" in body:
        await config_store.set_config("auto_erase_days", str(int(body["days"])))
    return {"success": True}


# ---- Project environment variables ----

@router.get("/api/config/env-vars/{project}")
async def get_project_env_vars(project: str, user: UserWithRole = Depends(require_role(Role.viewer))):
    """Get environment variables for a project."""
    raw = await config_store.get_config(f"env_vars_{project}")
    if not raw:
        return {"env_vars": {}}
    try:
        return {"env_vars": json.loads(raw)}
    except (json.JSONDecodeError, TypeError):
        return {"env_vars": {}}


@router.put("/api/config/env-vars/{project}")
async def save_project_env_vars(project: str, request: Request, user: UserWithRole = Depends(require_role(Role.manager))):
    """Save environment variables for a project."""
    body = await request.json()
    env_vars = body.get("env_vars", {})
    if not isinstance(env_vars, dict):
        raise HTTPException(status_code=400, detail="env_vars must be an object")
    # Validate all keys and values are strings
    for k, v in env_vars.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise HTTPException(status_code=400, detail="All keys and values must be strings")
    await config_store.set_config(f"env_vars_{project}", json.dumps(env_vars))

    # Check if there are active previews that would need a rebuild
    from app.database import get_all_previews
    all_previews = await get_all_previews()
    active_previews = [p["preview_name"] for p in all_previews
                       if p["project"] == project and p["status"] in ("active", "failed")]

    return {
        "success": True,
        "env_vars": env_vars,
        "needs_rebuild": len(active_previews) > 0,
        "affected_previews": active_previews,
    }


# ---- Allowed email domains ----

@router.get("/api/config/allowed-domains")
async def get_allowed_domains(user: UserWithRole = Depends(require_role(Role.admin))):
    """Get allowed email domains for auto-registration."""
    domains = await config_store.load_allowed_domains()
    return {"domains": domains}


@router.put("/api/config/allowed-domains")
async def save_allowed_domains(request: Request, user: UserWithRole = Depends(require_role(Role.admin))):
    """Save allowed email domains for auto-registration."""
    body = await request.json()
    domains = body.get("domains", [])
    if not isinstance(domains, list):
        raise HTTPException(status_code=400, detail="domains must be a list")
    for entry in domains:
        if not isinstance(entry, dict) or "domain" not in entry or "role" not in entry:
            raise HTTPException(status_code=400, detail="Each entry must have 'domain' and 'role'")
        if entry["role"] not in ("viewer", "manager"):
            raise HTTPException(status_code=400, detail="Role must be 'viewer' or 'manager'")
        if not entry["domain"] or not isinstance(entry["domain"], str):
            raise HTTPException(status_code=400, detail="Domain must be a non-empty string")
    await config_store.save_allowed_domains(domains)
    return {"success": True, "domains": domains}


@router.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy"
    }


@router.get("/")
async def root():
    """Root endpoint"""
    return {"status": "ok"}
