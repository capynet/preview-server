"""GitLab connection (PAT) and project management endpoints"""

import asyncio
import logging
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from config.settings import settings
from app.auth import database as db
from app.auth.dependencies import SESSION_COOKIE, get_current_user, require_role
from app.auth.models import Role, UserWithRole
from app.auth.oauth import GitLabOAuth
from app import config_store
from app.config_store import load_project_details

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gitlab", tags=["gitlab"])

# ---- In-memory cache for GitLab project listing ----
_projects_cache: list[dict] | None = None
_projects_cache_ts: float = 0.0
_PROJECTS_CACHE_TTL = 3600  # 1 hour


def _invalidate_projects_cache():
    global _projects_cache, _projects_cache_ts
    _projects_cache = None
    _projects_cache_ts = 0.0


async def _fetch_all_gitlab_projects(token: str) -> list[dict]:
    """Fetch all GitLab projects, using in-memory cache (1h TTL)."""
    global _projects_cache, _projects_cache_ts
    if _projects_cache is not None and (time.monotonic() - _projects_cache_ts) < _PROJECTS_CACHE_TTL:
        return _projects_cache

    all_projects = []
    page = 1
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            resp = await client.get(
                f"{settings.gitlab_url}/api/v4/projects",
                headers={"PRIVATE-TOKEN": token},
                params={
                    "membership": "true",
                    "archived": "false",
                    "per_page": 100,
                    "page": page,
                    "order_by": "name",
                    "sort": "asc",
                },
            )
            resp.raise_for_status()
            projects_page = resp.json()
            if not projects_page:
                break
            all_projects.extend(projects_page)
            if len(projects_page) < 100:
                break
            page += 1

    _projects_cache = all_projects
    _projects_cache_ts = time.monotonic()
    return all_projects


async def _get_gitlab_token() -> str:
    """Get the stored GitLab Personal Access Token."""
    token = settings.gitlab_oauth_access_token
    if not token:
        raise HTTPException(status_code=400, detail="GitLab not connected")
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
            count = await db.user_count()

            # Only allow signup if first user, has invitation, or matches allowed domain
            domain_role = await config_store.match_allowed_domain(email) if count > 0 else None
            if count > 0 and not invitation and not domain_role:
                logger.warning(f"OAuth signup rejected for {email}: no invitation")
                return RedirectResponse(f"{settings.frontend_url}/auth/login?error=not_invited")

            user = await db.create_user(email, name, avatar_url)
            await db.create_oauth_account(user["id"], "gitlab", provider_user_id, provider_username)
            logger.info(f"Created new user {email} via gitlab")

            if count == 0:
                await db.set_role(user["id"], Role.admin.value)
                logger.info(f"First user {email} assigned admin role")
            elif invitation:
                await db.set_role(user["id"], invitation["role"])
                await db.mark_invitation_accepted(invitation["id"])
                logger.info(f"User {email} accepted invitation with role {invitation['role']}")
            else:
                await db.set_role(user["id"], domain_role)
                logger.info(f"User {email} auto-registered with role {domain_role} via allowed domain")

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
    """Check if GitLab is connected by validating the stored token."""
    if not settings.gitlab_oauth_access_token:
        return {"connected": False, "gitlab_url": settings.gitlab_url}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.gitlab_url}/api/v4/personal_access_tokens/self",
                headers={"PRIVATE-TOKEN": settings.gitlab_oauth_access_token},
                timeout=10,
            )
        if resp.status_code == 200:
            pat_info = resp.json()
            if not pat_info.get("active", False):
                await config_store.remove_gitlab_token()
                return {"connected": False, "gitlab_url": settings.gitlab_url}
            return {"connected": True, "gitlab_url": settings.gitlab_url}
        elif resp.status_code == 401:
            logger.info("GitLab token invalid (HTTP 401), removing stored token")
            await config_store.remove_gitlab_token()
            return {"connected": False, "gitlab_url": settings.gitlab_url}
        else:
            logger.warning(f"GitLab API returned HTTP {resp.status_code}, treating as connected (transient error)")
            return {"connected": True, "gitlab_url": settings.gitlab_url}
    except Exception as e:
        logger.warning(f"Could not verify GitLab token: {e}")
        return {"connected": True, "gitlab_url": settings.gitlab_url}


class GitLabConnectRequest(BaseModel):
    gitlab_url: str
    token: str


@router.post("/connect")
async def gitlab_connect(body: GitLabConnectRequest, user: UserWithRole = Depends(require_role(Role.admin))):
    """Validate a GitLab Personal Access Token and save it."""
    gitlab_url = body.gitlab_url.rstrip("/")
    if not gitlab_url:
        raise HTTPException(status_code=400, detail="GitLab URL is required")

    # Validate the token against GitLab API
    headers = {"PRIVATE-TOKEN": body.token}
    token_name = ""
    try:
        async with httpx.AsyncClient() as client:
            # Try /personal_access_tokens/self first (works even if /user is restricted)
            resp = await client.get(
                f"{gitlab_url}/api/v4/personal_access_tokens/self",
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                pat_info = resp.json()
                token_name = pat_info.get("name", "")
                if not pat_info.get("active", False):
                    raise HTTPException(status_code=401, detail="Token is revoked or inactive")
                if "api" not in pat_info.get("scopes", []):
                    raise HTTPException(status_code=401, detail="Token needs 'api' scope")
            elif resp.status_code in (401, 403):
                raise HTTPException(status_code=401, detail="Invalid token: authentication failed")
            else:
                raise HTTPException(status_code=502, detail=f"GitLab API error: HTTP {resp.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach GitLab at {gitlab_url}: {e}")

    # Save URL and token
    await config_store.set_config("gitlab_url", gitlab_url)
    settings.gitlab_url = gitlab_url
    await config_store.save_gitlab_token(body.token)

    return {
        "success": True,
        "gitlab_url": gitlab_url,
        "token_name": token_name,
    }


@router.get("/projects")
async def gitlab_projects(user: UserWithRole = Depends(require_role(Role.viewer))):
    """List GitLab projects accessible to the connected account (cached 1h)."""
    token = await _get_gitlab_token()

    try:
        all_projects = await _fetch_all_gitlab_projects(token)

        # Load enabled project IDs from config
        enabled_ids = await config_store.load_enabled_project_ids()
        webhook_url = f"{settings.oauth_redirect_uri_base.rsplit('/api/', 1)[0]}/api/webhooks/gitlab"

        # For enabled projects, check if webhook still exists in GitLab (in parallel)
        webhook_status: dict[int, bool] = {}

        async def check_webhook(project_id: int):
            try:
                async with httpx.AsyncClient() as c:
                    resp = await c.get(
                        f"{settings.gitlab_url}/api/v4/projects/{project_id}/hooks",
                        headers={"PRIVATE-TOKEN": token},
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        hooks = resp.json()
                        webhook_status[project_id] = any(
                            h.get("url") == webhook_url and h.get("merge_requests_events")
                            for h in hooks
                        )
                    else:
                        webhook_status[project_id] = False
            except Exception:
                # Network error — assume OK to avoid false alarms
                webhook_status[project_id] = True

        enabled_in_list = [p["id"] for p in all_projects if p["id"] in enabled_ids]
        if enabled_in_list:
            await asyncio.gather(*[check_webhook(pid) for pid in enabled_in_list])

        return {
            "projects": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "path_with_namespace": p["path_with_namespace"],
                    "description": p.get("description") or "",
                    "web_url": p["web_url"],
                    "default_branch": p.get("default_branch", "main"),
                    "previews_enabled": p["id"] in enabled_ids,
                    "webhook_active": webhook_status.get(p["id"], True) if p["id"] in enabled_ids else None,
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


class EnableProjectRequest(BaseModel):
    path_with_namespace: str = ""
    name: str = ""
    web_url: str = ""
    default_branch: str = "main"


@router.get("/projects/enabled")
async def gitlab_enabled_projects(user: UserWithRole = Depends(require_role(Role.viewer))):
    """List only enabled projects using local data (no GitLab API calls for listing)."""
    token = settings.gitlab_oauth_access_token
    if not token:
        raise HTTPException(status_code=400, detail="GitLab not connected")

    enabled_ids = await config_store.load_enabled_project_ids()
    if not enabled_ids:
        return {"projects": []}

    project_details = await load_project_details()
    project_paths = await config_store.load_project_paths()
    webhook_url = f"{settings.oauth_redirect_uri_base.rsplit('/api/', 1)[0]}/api/webhooks/gitlab"

    # Check webhooks in parallel for enabled projects
    webhook_status: dict[int, bool] = {}

    async def check_webhook(project_id: int):
        try:
            async with httpx.AsyncClient() as c:
                resp = await c.get(
                    f"{settings.gitlab_url}/api/v4/projects/{project_id}/hooks",
                    headers={"PRIVATE-TOKEN": token},
                    timeout=10,
                )
                if resp.status_code == 200:
                    hooks = resp.json()
                    webhook_status[project_id] = any(
                        h.get("url") == webhook_url and h.get("merge_requests_events")
                        for h in hooks
                    )
                else:
                    webhook_status[project_id] = False
        except Exception:
            webhook_status[project_id] = True

    await asyncio.gather(*[check_webhook(pid) for pid in enabled_ids])

    projects = []
    for pid in sorted(enabled_ids):
        details = project_details.get(pid)
        path = project_paths.get(pid, "")
        if details:
            projects.append({
                "id": pid,
                "name": details.get("name", path.rsplit("/", 1)[-1] if path else f"Project {pid}"),
                "path_with_namespace": details.get("path_with_namespace", path),
                "description": "",
                "web_url": details.get("web_url", ""),
                "default_branch": details.get("default_branch", "main"),
                "previews_enabled": True,
                "webhook_active": webhook_status.get(pid, True),
            })
        else:
            # Legacy fallback: project was enabled before details were stored
            name = path.rsplit("/", 1)[-1] if path else f"Project {pid}"
            projects.append({
                "id": pid,
                "name": name,
                "path_with_namespace": path,
                "description": "",
                "web_url": f"{settings.gitlab_url}/{path}" if path else "",
                "default_branch": "main",
                "previews_enabled": True,
                "webhook_active": webhook_status.get(pid, True),
            })

    return {"projects": projects}


@router.post("/projects/{project_id}/enable")
async def enable_project_previews(project_id: int, body: EnableProjectRequest = EnableProjectRequest(), user: UserWithRole = Depends(require_role(Role.admin))):
    """Create or update a webhook in the GitLab project for MR and push events."""
    token = await _get_gitlab_token()
    webhook_url = f"{settings.oauth_redirect_uri_base.rsplit('/api/', 1)[0]}/api/webhooks/gitlab"

    try:
        async with httpx.AsyncClient() as client:
            # Check if a webhook with our URL already exists
            existing_hook_id = None
            resp = await client.get(
                f"{settings.gitlab_url}/api/v4/projects/{project_id}/hooks",
                headers={"PRIVATE-TOKEN": token},
                timeout=15,
            )
            if resp.status_code == 200:
                for hook in resp.json():
                    if hook.get("url") == webhook_url:
                        existing_hook_id = hook["id"]
                        break

            hook_payload = {
                "url": webhook_url,
                "merge_requests_events": True,
                "push_events": True,
                "enable_ssl_verification": True,
            }
            if settings.gitlab_webhook_secret:
                hook_payload["token"] = settings.gitlab_webhook_secret

            if existing_hook_id:
                # Update existing webhook
                resp = await client.put(
                    f"{settings.gitlab_url}/api/v4/projects/{project_id}/hooks/{existing_hook_id}",
                    headers={"PRIVATE-TOKEN": token},
                    json=hook_payload,
                    timeout=30,
                )
                resp.raise_for_status()
                hook = resp.json()
                message = f"Webhook updated for project {project_id}"
            else:
                # Create new webhook
                resp = await client.post(
                    f"{settings.gitlab_url}/api/v4/projects/{project_id}/hooks",
                    headers={"PRIVATE-TOKEN": token},
                    json=hook_payload,
                    timeout=30,
                )
                resp.raise_for_status()
                hook = resp.json()
                message = f"Webhook created for project {project_id}"

        await config_store.save_enabled_project_id(project_id)
        if body.path_with_namespace:
            await config_store.save_project_path(project_id, body.path_with_namespace)
        # Save full project details for the enabled projects endpoint
        if body.name or body.path_with_namespace:
            await config_store.save_project_details(project_id, {
                "name": body.name or body.path_with_namespace.rsplit("/", 1)[-1],
                "path_with_namespace": body.path_with_namespace,
                "web_url": body.web_url,
                "default_branch": body.default_branch or "main",
            })

        return {
            "success": True,
            "hook_id": hook["id"],
            "message": message,
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


@router.get("/projects/{project_id}/branches")
async def list_project_branches(project_id: int, user: UserWithRole = Depends(require_role(Role.viewer))):
    """List branches for a GitLab project."""
    token = await _get_gitlab_token()

    try:
        all_branches = []
        page = 1
        async with httpx.AsyncClient() as client:
            while True:
                resp = await client.get(
                    f"{settings.gitlab_url}/api/v4/projects/{project_id}/repository/branches",
                    headers={"PRIVATE-TOKEN": token},
                    params={
                        "per_page": 100,
                        "page": page,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                branches_page = resp.json()
                if not branches_page:
                    break
                all_branches.extend(branches_page)
                if len(branches_page) < 100:
                    break
                page += 1

        return {
            "branches": [
                {
                    "name": b["name"],
                    "commit_sha": b["commit"]["id"],
                    "commit_message": b["commit"].get("message", ""),
                    "default": b.get("default", False),
                }
                for b in all_branches
            ]
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(status_code=401, detail="GitLab token expired or revoked")
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Error listing branches: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e}")


@router.get("/projects/by-slug/{project_slug}/branches")
async def list_project_branches_by_slug(project_slug: str, user: UserWithRole = Depends(require_role(Role.viewer))):
    """List branches for a GitLab project using the project slug (no numeric ID needed)."""
    token = await _get_gitlab_token()
    project_path = await config_store.get_project_path_by_slug(project_slug)
    if not project_path:
        raise HTTPException(status_code=404, detail=f"Project '{project_slug}' not found in enabled projects")
    encoded_path = project_path.replace("/", "%2F")

    try:
        all_branches = []
        page = 1
        async with httpx.AsyncClient() as client:
            while True:
                resp = await client.get(
                    f"{settings.gitlab_url}/api/v4/projects/{encoded_path}/repository/branches",
                    headers={"PRIVATE-TOKEN": token},
                    params={"per_page": 100, "page": page},
                    timeout=15,
                )
                resp.raise_for_status()
                branches_page = resp.json()
                if not branches_page:
                    break
                all_branches.extend(branches_page)
                if len(branches_page) < 100:
                    break
                page += 1

        return {
            "branches": [
                {
                    "name": b["name"],
                    "commit_sha": b["commit"]["id"],
                    "commit_message": b["commit"].get("message", ""),
                    "default": b.get("default", False),
                }
                for b in all_branches
            ]
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(status_code=401, detail="GitLab token expired or revoked")
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Project '{project_slug}' not found in GitLab")
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Error listing branches by slug: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e}")


@router.post("/disconnect")
async def gitlab_disconnect(user: UserWithRole = Depends(require_role(Role.admin))):
    """Remove webhooks from enabled projects, clear config, and remove OAuth tokens."""
    webhooks_deleted = 0
    errors: list[str] = []

    # Try to clean up webhooks before removing tokens
    token = settings.gitlab_oauth_access_token
    if token:
        enabled_ids = await config_store.load_enabled_project_ids()
        if enabled_ids:
            webhook_url = f"{settings.oauth_redirect_uri_base.rsplit('/api/', 1)[0]}/api/webhooks/gitlab"

            async def delete_project_webhooks(project_id: int) -> tuple[int, str | None]:
                deleted = 0
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(
                            f"{settings.gitlab_url}/api/v4/projects/{project_id}/hooks",
                            headers={"PRIVATE-TOKEN": token},
                            timeout=10,
                        )
                        if resp.status_code != 200:
                            return 0, f"Project {project_id}: failed to list hooks (HTTP {resp.status_code})"
                        hooks = resp.json()
                        for hook in hooks:
                            if hook.get("url") == webhook_url:
                                del_resp = await client.delete(
                                    f"{settings.gitlab_url}/api/v4/projects/{project_id}/hooks/{hook['id']}",
                                    headers={"PRIVATE-TOKEN": token},
                                    timeout=10,
                                )
                                if del_resp.status_code in (200, 204):
                                    deleted += 1
                                else:
                                    return deleted, f"Project {project_id}: failed to delete hook {hook['id']} (HTTP {del_resp.status_code})"
                except Exception as e:
                    return deleted, f"Project {project_id}: {e}"
                return deleted, None

            results = await asyncio.gather(*[delete_project_webhooks(pid) for pid in enabled_ids])
            for count, error in results:
                webhooks_deleted += count
                if error:
                    errors.append(error)

    # Delete all previews (since only GitLab is a provider currently)
    from app.database import get_all_previews
    from app.routes.previews import delete_preview_internal

    previews_deleted = 0
    all_previews = await get_all_previews()
    for p in all_previews:
        try:
            await delete_preview_internal(p["project"], p["preview_name"])
            previews_deleted += 1
        except Exception as e:
            errors.append(f"Preview {p['project']}/{p['preview_name']}: {e}")

    await config_store.clear_enabled_project_ids()
    await config_store.clear_project_paths()
    await config_store.clear_project_details()
    await config_store.remove_gitlab_token()
    _invalidate_projects_cache()

    return {
        "success": True,
        "message": "GitLab disconnected",
        "webhooks_deleted": webhooks_deleted,
        "previews_deleted": previews_deleted,
        "errors": errors,
    }
