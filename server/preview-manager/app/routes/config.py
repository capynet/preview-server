"""Configuration and health check endpoints"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from config.settings import settings
from app.auth.dependencies import require_role
from app.auth.models import Role, UserWithRole

logger = logging.getLogger(__name__)

router = APIRouter()

CONFIG_FILE = Path("/var/www/preview-manager/app-config.json")


def load_config_from_file():
    """Load configuration from file on startup"""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                settings.gitlab_url = config.get("gitlab_url", settings.gitlab_url)
                settings.gitlab_api_token = config.get("gitlab_api_token") or None
                settings.gitlab_group_name = config.get("gitlab_group_name", settings.gitlab_group_name)
                # Load OAuth tokens
                settings.gitlab_oauth_access_token = config.get("gitlab_oauth_access_token") or None
                settings.gitlab_oauth_refresh_token = config.get("gitlab_oauth_refresh_token") or None
                settings.gitlab_oauth_token_expires_at = config.get("gitlab_oauth_token_expires_at") or None
                logger.info(f"Loaded configuration from {CONFIG_FILE}")
    except Exception as e:
        logger.warning(f"Could not load config from file: {e}")


# Load config on module import
load_config_from_file()


@router.get("/api/config")
async def get_app_config(user: UserWithRole = Depends(require_role(Role.admin))):
    """Get application configuration"""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                return {
                    "gitlab_url": config.get("gitlab_url", settings.gitlab_url),
                    "gitlab_api_token": config.get("gitlab_api_token", ""),
                    "gitlab_group_name": config.get("gitlab_group_name", settings.gitlab_group_name)
                }
        else:
            return {
                "gitlab_url": settings.gitlab_url,
                "gitlab_api_token": "",
                "gitlab_group_name": settings.gitlab_group_name
            }
    except Exception as e:
        logger.error(f"Error reading config: {e}")
        raise HTTPException(status_code=500, detail=f"Error reading configuration: {str(e)}")


@router.post("/api/config")
async def save_app_config(request: Request, user: UserWithRole = Depends(require_role(Role.admin))):
    """Save application configuration"""
    try:
        body = await request.json()

        gitlab_url = body.get("gitlab_url", settings.gitlab_url)
        gitlab_api_token = body.get("gitlab_api_token", "")
        gitlab_group_name = body.get("gitlab_group_name", settings.gitlab_group_name)

        # Read existing config to preserve OAuth tokens
        existing_config = {}
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, 'r') as f:
                    existing_config = json.load(f)
        except Exception:
            pass

        existing_config["gitlab_url"] = gitlab_url
        existing_config["gitlab_api_token"] = gitlab_api_token
        existing_config["gitlab_group_name"] = gitlab_group_name

        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

        with open(CONFIG_FILE, 'w') as f:
            json.dump(existing_config, f, indent=2)

        settings.gitlab_url = gitlab_url
        settings.gitlab_api_token = gitlab_api_token if gitlab_api_token else None
        settings.gitlab_group_name = gitlab_group_name

        logger.info(f"App configuration saved to {CONFIG_FILE}")

        return {
            "success": True,
            "message": "Configuration saved successfully"
        }
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        raise HTTPException(status_code=500, detail=f"Error saving configuration: {str(e)}")


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
            "deploy": "POST /api/deploy",
            "get_preview": "GET /api/previews/{project}/mr-{mr_id}",
            "list_previews": "WS /ws/previews",
            "delete_preview": "DELETE /api/previews/{project}/mr-{mr_id}",
            "stop": "POST /api/previews/{preview_name}/stop",
            "start": "POST /api/previews/{preview_name}/start",
            "restart": "POST /api/previews/{preview_name}/restart",
            "drush_uli": "GET /api/previews/{preview_name}/drush-uli",
            "health": "GET /api/health"
        },
        "docs": "/docs"
    }
