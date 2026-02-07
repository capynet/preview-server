"""GitLab OAuth connection and project management endpoints"""

import asyncio
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from config.settings import settings
from app.auth import database as db
from app.auth.dependencies import SESSION_COOKIE, get_current_user, require_role
from app.auth.models import Role, UserWithRole
from app.auth.oauth import GitLabOAuth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gitlab", tags=["gitlab"])

# In-memory CSRF state store for OAuth connect flow (state -> expiry timestamp)
_pending_oauth_states: dict[str, float] = {}
_TOKEN_REFRESH_LOCK = asyncio.Lock()


def _cleanup_expired_states():
    """Remove expired CSRF states."""
    now = time.time()
    expired = [s for s, exp in _pending_oauth_states.items() if now > exp]
    for s in expired:
        del _pending_oauth_states[s]


def _save_oauth_tokens_to_config(access_token: str, refresh_token: str | None, expires_at: int | None):
    """Persist OAuth tokens to app-config.json and in-memory settings."""
    import json
    from app.routes.config import CONFIG_FILE

    settings.gitlab_oauth_access_token = access_token
    settings.gitlab_oauth_refresh_token = refresh_token
    settings.gitlab_oauth_token_expires_at = expires_at

    # Read existing config, merge tokens
    config = {}
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
    except Exception:
        pass

    config["gitlab_oauth_access_token"] = access_token
    config["gitlab_oauth_refresh_token"] = refresh_token
    config["gitlab_oauth_token_expires_at"] = expires_at

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    logger.info("GitLab OAuth tokens saved to config")


def _remove_oauth_tokens_from_config():
    """Remove OAuth tokens from app-config.json and in-memory settings."""
    import json
    from app.routes.config import CONFIG_FILE

    settings.gitlab_oauth_access_token = None
    settings.gitlab_oauth_refresh_token = None
    settings.gitlab_oauth_token_expires_at = None

    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
            config.pop("gitlab_oauth_access_token", None)
            config.pop("gitlab_oauth_refresh_token", None)
            config.pop("gitlab_oauth_token_expires_at", None)
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
    except Exception as e:
        logger.warning(f"Error removing OAuth tokens from config: {e}")


async def _get_gitlab_token() -> str:
    """Get a valid GitLab access token, refreshing if needed."""
    async with _TOKEN_REFRESH_LOCK:
        token = settings.gitlab_oauth_access_token
        if not token:
            raise HTTPException(status_code=400, detail="GitLab not connected")

        expires_at = settings.gitlab_oauth_token_expires_at
        refresh_token = settings.gitlab_oauth_refresh_token

        # Refresh if token expires within 5 minutes
        if expires_at and refresh_token and time.time() > (expires_at - 300):
            logger.info("Refreshing GitLab OAuth token")
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{settings.gitlab_url}/oauth/token",
                        data={
                            "client_id": settings.gitlab_connect_client_id,
                            "client_secret": settings.gitlab_connect_client_secret,
                            "refresh_token": refresh_token,
                            "grant_type": "refresh_token",
                        },
                        timeout=30,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                new_access = data["access_token"]
                new_refresh = data.get("refresh_token", refresh_token)
                new_expires = int(time.time()) + data.get("expires_in", 7200)

                _save_oauth_tokens_to_config(new_access, new_refresh, new_expires)
                return new_access
            except Exception as e:
                logger.error(f"Failed to refresh GitLab token: {e}")
                # Return existing token, it might still work
                return token

        return token


# ---- Auth endpoints (login via GitLab) ----

@router.get("/auth/login")
async def gitlab_auth_login():
    """Redirect to GitLab OAuth for user login (scope: read_user)."""
    oauth = GitLabOAuth()
    state = secrets.token_urlsafe(24)
    # Build URL with callback pointing to /api/gitlab/auth/callback
    redirect_uri = f"{settings.oauth_redirect_uri_base.rsplit('/api/', 1)[0]}/api/gitlab/auth/callback"
    url = (
        f"{settings.gitlab_url}/oauth/authorize"
        f"?client_id={settings.gitlab_oauth_client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope=read_user"
        f"&state={state}"
    )
    return RedirectResponse(url)


@router.get("/auth/callback")
async def gitlab_auth_callback(code: str, state: str = ""):
    """GitLab login callback - exchange code, upsert user, create session."""
    redirect_uri = f"{settings.oauth_redirect_uri_base.rsplit('/api/', 1)[0]}/api/gitlab/auth/callback"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.gitlab_url}/oauth/token",
                data={
                    "client_id": settings.gitlab_oauth_client_id,
                    "client_secret": settings.gitlab_oauth_client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                timeout=30,
            )
            resp.raise_for_status()
            access_token = resp.json()["access_token"]
    except Exception as e:
        logger.error(f"GitLab OAuth token exchange error: {e}", exc_info=True)
        return RedirectResponse(f"{settings.frontend_url}/auth/login?error=oauth_error")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.gitlab_url}/api/v4/user",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error(f"GitLab user info error: {e}", exc_info=True)
        return RedirectResponse(f"{settings.frontend_url}/auth/login?error=oauth_error")

    email = data["email"]
    name = data.get("name", data.get("username", ""))
    avatar_url = data.get("avatar_url")
    provider_user_id = str(data["id"])
    provider_username = data.get("username")

    # Lookup by provider account first
    oauth_account = await db.get_oauth_account("gitlab", provider_user_id)

    if oauth_account:
        user = await db.get_user_by_id(oauth_account["user_id"])
    else:
        user_dict = await db.get_user_by_email(email)
        if user_dict:
            user = user_dict
            await db.create_oauth_account(user["id"], "gitlab", provider_user_id, provider_username)
            logger.info(f"Linked gitlab account to existing user {email}")
        else:
            invitation = await db.get_invitation_by_email(email)

            if not invitation:
                allowed = settings.allowed_email_domains.strip()
                if allowed:
                    domain = email.split("@")[1] if "@" in email else ""
                    allowed_list = [d.strip() for d in allowed.split(",") if d.strip()]
                    if domain not in allowed_list:
                        return RedirectResponse(f"{settings.frontend_url}/auth/login?error=domain_not_allowed")

            user = await db.create_user(email, name, avatar_url)
            await db.create_oauth_account(user["id"], "gitlab", provider_user_id, provider_username)
            logger.info(f"Created new user {email} via gitlab")

            count = await db.user_count()
            if count == 1:
                await db.set_role(user["id"], Role.admin.value)
                logger.info(f"First user {email} assigned admin role")
            elif invitation:
                await db.set_role(user["id"], invitation["role"])
                await db.mark_invitation_accepted(invitation["id"])
                logger.info(f"User {email} accepted invitation with role {invitation['role']}")
            else:
                await db.set_role(user["id"], Role.member.value)

    if not user:
        raise HTTPException(status_code=500, detail="Failed to resolve user")

    role = await db.get_role(user["id"])
    if role is None:
        return RedirectResponse(f"{settings.frontend_url}/auth/login?error=no_role")

    session_id = await db.create_session(user["id"])

    response = RedirectResponse(settings.frontend_url)
    from app.routes.auth import _set_session_cookie
    _set_session_cookie(response, session_id)
    return response


# ---- Connect endpoints (GitLab API access for previews) ----

@router.get("/status")
async def gitlab_status(user: UserWithRole = Depends(require_role(Role.viewer))):
    """Check if GitLab is connected (has OAuth access token)."""
    connected = bool(settings.gitlab_oauth_access_token)
    return {
        "connected": connected,
        "gitlab_url": settings.gitlab_url,
    }


@router.get("/connect")
async def gitlab_connect(user: UserWithRole = Depends(require_role(Role.admin))):
    """Return the GitLab OAuth authorize URL for the Connect flow (scope: api)."""
    if not settings.gitlab_connect_client_id:
        raise HTTPException(status_code=400, detail="GitLab Connect OAuth not configured (missing client_id)")

    _cleanup_expired_states()

    state = secrets.token_urlsafe(32)
    _pending_oauth_states[state] = time.time() + 600  # 10 min expiry

    redirect_uri = f"{settings.oauth_redirect_uri_base.rsplit('/api/', 1)[0]}/api/gitlab/connect/callback"
    params = {
        "client_id": settings.gitlab_connect_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "api",
        "state": state,
    }
    authorize_url = f"{settings.gitlab_url}/oauth/authorize?{urlencode(params)}"
    return {"authorize_url": authorize_url}


@router.get("/connect/callback")
async def gitlab_connect_callback(code: str, state: str = ""):
    """Exchange OAuth code for tokens and save them. Redirects to frontend."""
    _cleanup_expired_states()

    if state not in _pending_oauth_states:
        return RedirectResponse(f"{settings.frontend_url}?gitlab_error=invalid_state")

    del _pending_oauth_states[state]

    redirect_uri = f"{settings.oauth_redirect_uri_base.rsplit('/api/', 1)[0]}/api/gitlab/connect/callback"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.gitlab_url}/oauth/token",
                data={
                    "client_id": settings.gitlab_connect_client_id,
                    "client_secret": settings.gitlab_connect_client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error(f"GitLab connect token exchange error: {e}", exc_info=True)
        return RedirectResponse(f"{settings.frontend_url}?gitlab_error=token_exchange_failed")

    access_token = data["access_token"]
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in", 7200)
    expires_at = int(time.time()) + expires_in

    _save_oauth_tokens_to_config(access_token, refresh_token, expires_at)

    return RedirectResponse(f"{settings.frontend_url}?gitlab_connected=true")


@router.get("/projects")
async def gitlab_projects(user: UserWithRole = Depends(require_role(Role.viewer))):
    """List GitLab projects accessible to the connected account."""
    token = await _get_gitlab_token()

    try:
        all_projects = []
        page = 1
        async with httpx.AsyncClient() as client:
            while True:
                resp = await client.get(
                    f"{settings.gitlab_url}/api/v4/projects",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "membership": "true",
                        "per_page": 100,
                        "page": page,
                        "order_by": "name",
                        "sort": "asc",
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                projects_page = resp.json()
                if not projects_page:
                    break
                all_projects.extend(projects_page)
                # Check if there are more pages
                if len(projects_page) < 100:
                    break
                page += 1

        return {
            "projects": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "path_with_namespace": p["path_with_namespace"],
                    "description": p.get("description") or "",
                    "web_url": p["web_url"],
                    "default_branch": p.get("default_branch", "main"),
                }
                for p in all_projects
            ]
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(status_code=401, detail="GitLab token expired or revoked")
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Error listing GitLab projects: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e}")


@router.post("/projects/{project_id}/enable")
async def enable_project_previews(project_id: int, user: UserWithRole = Depends(require_role(Role.admin))):
    """Create a webhook in the GitLab project for merge request events."""
    token = await _get_gitlab_token()
    webhook_url = f"{settings.oauth_redirect_uri_base.rsplit('/api/', 1)[0]}/api/deploy"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.gitlab_url}/api/v4/projects/{project_id}/hooks",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "url": webhook_url,
                    "merge_requests_events": True,
                    "push_events": False,
                    "enable_ssl_verification": True,
                },
                timeout=30,
            )
            resp.raise_for_status()
            hook = resp.json()

        return {
            "success": True,
            "hook_id": hook["id"],
            "message": f"Webhook created for project {project_id}",
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(status_code=401, detail="GitLab token expired or revoked")
        if e.response.status_code == 403:
            raise HTTPException(status_code=403, detail="Insufficient permissions to create webhooks in this project")
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        logger.error(f"Error creating webhook: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e}")


@router.post("/disconnect")
async def gitlab_disconnect(user: UserWithRole = Depends(require_role(Role.admin))):
    """Remove GitLab OAuth tokens (disconnect)."""
    _remove_oauth_tokens_from_config()
    return {"success": True, "message": "GitLab disconnected"}
