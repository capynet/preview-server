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
    return {"success": True, "env_vars": env_vars}


@router.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy"
    }


@router.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "Preview Manager",
        "version": "2.0",
        "endpoints": {
            "get_preview": "GET /api/previews/{project}/{preview_name}",
            "list_previews": "WS /ws/previews",
            "delete_preview": "DELETE /api/previews/{project}/{preview_name}",
            "rebuild": "POST /api/previews/{project}/{preview_name}/rebuild",
            "stop": "POST /api/previews/{project}/{preview_name}/stop",
            "start": "POST /api/previews/{project}/{preview_name}/start",
            "restart": "POST /api/previews/{project}/{preview_name}/restart",
            "drush_uli": "POST /api/previews/{project}/{preview_name}/drush-uli",
            "create_branch": "POST /api/previews/{project}/branch",
            "branches": "GET /api/gitlab/projects/{project_id}/branches",
            "health": "GET /api/health"
        },
        "docs": "/docs"
    }
