"""GitLab connection and project management — org-scoped."""

import asyncio
import logging
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from config.settings import settings
from app.auth import database as auth_db
from app.auth.dependencies import SESSION_COOKIE, get_org_context, require_org_role
from app.auth.models import (
    EnableProjectRequest,
    GitLabConnectRequest,
    OrgRole,
    UserWithContext,
)
from app.auth.oauth import GitLabOAuth
from app.database import (
    add_org_member,
    get_organization_by_slug,
    get_project_by_gitlab_id,
    get_invitation_by_email,
    mark_invitation_accepted,
    match_email_domain,
    upsert_project,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["gitlab"])

# ---- In-memory cache for GitLab project listing (per org, per page) ----
# Structure: {org_id: {page_number: (projects_list, timestamp)}}
_pages_cache: dict[int, dict[int, tuple[list[dict], float]]] = {}
_PROJECTS_CACHE_TTL = 3600  # 1 hour
_GITLAB_PAGE_SIZE = 20


def _invalidate_projects_cache(org_id: int):
    _pages_cache.pop(org_id, None)


async def _fetch_all_gitlab_projects(gitlab_url: str, token: str, org_id: int) -> list[dict]:
    org_cache = _pages_cache.setdefault(org_id, {})
    now = time.monotonic()

    all_projects = []
    page = 1
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            # Use cached page if still valid
            cached_page = org_cache.get(page)
            if cached_page and (now - cached_page[1]) < _PROJECTS_CACHE_TTL:
                projects_page = cached_page[0]
            else:
                resp = await client.get(
                    f"{gitlab_url}/api/v4/projects",
                    headers={"PRIVATE-TOKEN": token},
                    params={
                        "membership": "true",
                        "archived": "false",
                        "per_page": _GITLAB_PAGE_SIZE,
                        "page": page,
                        "order_by": "name",
                        "sort": "asc",
                    },
                )
                resp.raise_for_status()
                projects_page = resp.json()
                org_cache[page] = (projects_page, time.monotonic())

            if not projects_page:
                break
            all_projects.extend(projects_page)
            if len(projects_page) < _GITLAB_PAGE_SIZE:
                break
            page += 1

    return all_projects


def _get_org_gitlab(user: UserWithContext) -> tuple[str, str]:
    """Get GitLab URL and token from the org context."""
    if not user.org or not user.org.gitlab_url:
        raise HTTPException(status_code=400, detail="GitLab not connected for this organization")
    from app.database import get_db
    # We need the raw token which is not in the Organization model
    # Read it directly
    return user.org.gitlab_url, ""


async def _get_org_gitlab_token(org_id: int) -> tuple[str, str]:
    """Get gitlab_url and gitlab_access_token from the org."""
    from app.database import get_organization_by_id
    org = await get_organization_by_id(org_id)
    if not org or not org.get("gitlab_access_token"):
        raise HTTPException(status_code=400, detail="GitLab not connected for this organization")
    return org["gitlab_url"], org["gitlab_access_token"]


# ---- GitLab user login (not org-scoped) ----

@router.get("/gitlab/auth/login")
async def gitlab_auth_login():
    oauth = GitLabOAuth()
    state = secrets.token_urlsafe(24)
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


@router.get("/gitlab/auth/callback")
async def gitlab_auth_callback(code: str, state: str = ""):
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

    # Lookup by provider account
    oauth_account = await auth_db.get_oauth_account("gitlab", provider_user_id)

    if oauth_account:
        user = await auth_db.get_user_by_id(oauth_account["user_id"])
    else:
        user_dict = await auth_db.get_user_by_email(email)
        if user_dict:
            user = user_dict
            await auth_db.create_oauth_account(user["id"], "gitlab", provider_user_id, provider_username)
        else:
            count = await auth_db.user_count()
            invitation = await get_invitation_by_email(email)
            domain_match = await match_email_domain(email) if count > 0 else None

            if count > 0 and not invitation and not domain_match:
                return RedirectResponse(f"{settings.frontend_url}/auth/login?error=not_invited")

            is_superadmin = count == 0
            user = await auth_db.create_user(email, name, avatar_url, is_superadmin=is_superadmin)
            await auth_db.create_oauth_account(user["id"], "gitlab", provider_user_id, provider_username)

            if is_superadmin:
                logger.info(f"First user {email} assigned superadmin via GitLab")
            elif invitation:
                await mark_invitation_accepted(invitation["id"])
                await add_org_member(user["id"], invitation["organization_id"], invitation["role"])
                logger.info(f"User {email} accepted invitation via GitLab")
            elif domain_match:
                await add_org_member(user["id"], domain_match["organization_id"], domain_match["default_role"])
                logger.info(f"User {email} auto-joined org via domain match")

    if not user:
        raise HTTPException(status_code=500, detail="Failed to resolve user")

    session_id = await auth_db.create_session(user["id"])
    response = RedirectResponse(settings.frontend_url)
    from app.routes.auth import _set_session_cookie
    _set_session_cookie(response, session_id)
    return response


# ---- Org-scoped GitLab endpoints ----

@router.get("/orgs/{org}/gitlab/status")
async def gitlab_status(user: UserWithContext = Depends(get_org_context)):
    org = user.org
    if not org.gitlab_url:
        return {"connected": False, "gitlab_url": None}

    from app.database import get_organization_by_id
    org_data = await get_organization_by_id(org.id)
    token = org_data.get("gitlab_access_token") if org_data else None
    if not token:
        return {"connected": False, "gitlab_url": org.gitlab_url}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{org.gitlab_url}/api/v4/personal_access_tokens/self",
                headers={"PRIVATE-TOKEN": token},
                timeout=10,
            )
        if resp.status_code == 200:
            pat_info = resp.json()
            if not pat_info.get("active", False):
                return {"connected": False, "gitlab_url": org.gitlab_url}
            return {"connected": True, "gitlab_url": org.gitlab_url}
        elif resp.status_code == 401:
            return {"connected": False, "gitlab_url": org.gitlab_url}
        else:
            return {"connected": True, "gitlab_url": org.gitlab_url}
    except Exception:
        return {"connected": True, "gitlab_url": org.gitlab_url}


@router.post("/orgs/{org}/gitlab/connect")
async def gitlab_connect(body: GitLabConnectRequest, user: UserWithContext = Depends(require_org_role(OrgRole.owner))):
    gitlab_url = body.gitlab_url.rstrip("/")
    if not gitlab_url:
        raise HTTPException(status_code=400, detail="GitLab URL is required")

    headers = {"PRIVATE-TOKEN": body.token}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{gitlab_url}/api/v4/personal_access_tokens/self",
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                pat_info = resp.json()
                if not pat_info.get("active", False):
                    raise HTTPException(status_code=401, detail="Token is revoked or inactive")
                if "api" not in pat_info.get("scopes", []):
                    raise HTTPException(status_code=401, detail="Token needs 'api' scope")
            elif resp.status_code in (401, 403):
                raise HTTPException(status_code=401, detail="Invalid token")
            else:
                raise HTTPException(status_code=502, detail=f"GitLab API error: HTTP {resp.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach GitLab at {gitlab_url}: {e}")

    from app.database import update_organization
    await update_organization(user.org.id, gitlab_url=gitlab_url, gitlab_access_token=body.token)

    return {"success": True, "gitlab_url": gitlab_url}


@router.post("/orgs/{org}/gitlab/disconnect")
async def gitlab_disconnect(user: UserWithContext = Depends(require_org_role(OrgRole.owner))):
    from app.database import update_organization
    await update_organization(user.org.id, gitlab_url=None, gitlab_access_token=None)
    _invalidate_projects_cache(user.org.id)
    return {"success": True}


@router.get("/orgs/{org}/gitlab/projects/enabled")
async def gitlab_enabled_projects(user: UserWithContext = Depends(require_org_role(OrgRole.viewer))):
    """Return only projects that have previews enabled (from local DB, no GitLab call)."""
    from app.database import list_projects
    enabled_projects = await list_projects(user.org.id)
    return {
        "projects": [
            {
                "id": p.get("gitlab_project_id"),
                "name": p.get("name") or p["slug"],
                "path_with_namespace": p.get("gitlab_project_path", ""),
                "description": "",
                "web_url": p.get("gitlab_web_url", ""),
                "default_branch": p.get("gitlab_default_branch", "main"),
                "previews_enabled": True,
            }
            for p in enabled_projects
            if p.get("gitlab_project_id")
        ]
    }


@router.get("/orgs/{org}/gitlab/projects")
async def gitlab_projects(user: UserWithContext = Depends(require_org_role(OrgRole.viewer))):
    gitlab_url, token = await _get_org_gitlab_token(user.org.id)

    try:
        all_projects = await _fetch_all_gitlab_projects(gitlab_url, token, user.org.id)

        # Determine which are enabled by checking projects table
        from app.database import list_projects
        enabled_projects = await list_projects(user.org.id)
        enabled_gitlab_ids = {p["gitlab_project_id"] for p in enabled_projects if p.get("gitlab_project_id")}

        return {
            "projects": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "path_with_namespace": p["path_with_namespace"],
                    "description": p.get("description") or "",
                    "web_url": p["web_url"],
                    "default_branch": p.get("default_branch", "main"),
                    "previews_enabled": p["id"] in enabled_gitlab_ids,
                }
                for p in all_projects
            ]
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(status_code=401, detail="GitLab token expired or revoked")
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e.response.status_code}")


@router.post("/orgs/{org}/gitlab/projects/{project_id}/enable")
async def enable_project_previews(
    project_id: int,
    body: EnableProjectRequest = EnableProjectRequest(),
    user: UserWithContext = Depends(require_org_role(OrgRole.admin)),
):
    gitlab_url, token = await _get_org_gitlab_token(user.org.id)
    webhook_url = f"{settings.oauth_redirect_uri_base.rsplit('/api/', 1)[0]}/api/webhooks/{user.org.slug}/gitlab"

    try:
        async with httpx.AsyncClient() as client:
            existing_hook_id = None
            resp = await client.get(
                f"{gitlab_url}/api/v4/projects/{project_id}/hooks",
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
                resp = await client.put(
                    f"{gitlab_url}/api/v4/projects/{project_id}/hooks/{existing_hook_id}",
                    headers={"PRIVATE-TOKEN": token},
                    json=hook_payload,
                    timeout=30,
                )
                resp.raise_for_status()
                hook = resp.json()
            else:
                resp = await client.post(
                    f"{gitlab_url}/api/v4/projects/{project_id}/hooks",
                    headers={"PRIVATE-TOKEN": token},
                    json=hook_payload,
                    timeout=30,
                )
                resp.raise_for_status()
                hook = resp.json()

        # Create/update project in DB
        slug = body.path_with_namespace.rsplit("/", 1)[-1] if body.path_with_namespace else f"project-{project_id}"
        await upsert_project(
            user.org.id, slug,
            name=body.name or slug,
            gitlab_project_id=project_id,
            gitlab_project_path=body.path_with_namespace,
            gitlab_web_url=body.web_url,
            gitlab_default_branch=body.default_branch or "main",
        )

        return {"success": True, "hook_id": hook["id"]}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(status_code=401, detail="GitLab token expired or revoked")
        if e.response.status_code == 403:
            raise HTTPException(status_code=403, detail="Insufficient permissions to create webhooks")
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e.response.status_code}")


@router.get("/orgs/{org}/gitlab/projects/{project_id}/branches")
async def list_project_branches(project_id: int, user: UserWithContext = Depends(require_org_role(OrgRole.viewer))):
    gitlab_url, token = await _get_org_gitlab_token(user.org.id)

    try:
        all_branches = []
        page = 1
        async with httpx.AsyncClient() as client:
            while True:
                resp = await client.get(
                    f"{gitlab_url}/api/v4/projects/{project_id}/repository/branches",
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
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e.response.status_code}")


@router.get("/orgs/{org}/gitlab/projects/by-slug/{project_slug}/branches")
async def list_project_branches_by_slug(project_slug: str, user: UserWithContext = Depends(require_org_role(OrgRole.viewer))):
    gitlab_url, token = await _get_org_gitlab_token(user.org.id)
    from app.database import get_project_by_slug
    project = await get_project_by_slug(user.org.id, project_slug)
    if not project or not project.get("gitlab_project_path"):
        raise HTTPException(status_code=404, detail=f"Project '{project_slug}' not found")
    encoded_path = project["gitlab_project_path"].replace("/", "%2F")

    try:
        all_branches = []
        page = 1
        async with httpx.AsyncClient() as client:
            while True:
                resp = await client.get(
                    f"{gitlab_url}/api/v4/projects/{encoded_path}/repository/branches",
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
