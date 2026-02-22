"""Auth routes: OAuth login, sessions, tokens, CLI device flow, user management"""

import logging
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from config.settings import settings
from app.auth import database as db
from app.auth.dependencies import SESSION_COOKIE, get_current_user, require_role
from app.database import update_last_accessed
from app.auth.models import (
    AcceptInviteBody,
    AddProjectMemberBody,
    CLIApproveBody,
    CLIRequestBody,
    CreateTokenRequest,
    InviteBody,
    LoginBody,
    Role,
    SetupBody,

    UpdateRoleBody,
    UserWithRole,
    has_min_role,
)
from app.auth.email import send_invitation_email
from app.auth.oauth import get_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

def _set_session_cookie(response: Response, session_id: str):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="none",
        secure=True,
        domain=".preview-mr.com",
    )


def _delete_session_cookie(response: Response):
    response.delete_cookie(SESSION_COOKIE, domain=".preview-mr.com")


# ---- Setup & Password Login ----

@router.get("/setup/status")
async def setup_status():
    """Check if initial setup has been completed (i.e. at least one user exists)."""
    done = await db.is_setup_complete()
    return {"setup_complete": done}


@router.post("/setup")
async def initial_setup(body: SetupBody):
    """Create the first admin user with email+password. Only works when no users exist."""
    if await db.is_setup_complete():
        raise HTTPException(status_code=400, detail="Setup already completed")

    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    user = await db.create_user_with_password(body.email, body.name or "Admin", body.password)
    await db.set_role(user["id"], Role.admin.value)
    logger.info(f"Initial setup: admin user {body.email} created")

    session_id = await db.create_session(user["id"])

    response = Response(
        content='{"success": true}',
        media_type="application/json",
    )
    _set_session_cookie(response, session_id)
    return response


@router.post("/login")
async def password_login(body: LoginBody):
    """Login with email+password."""
    user = await db.get_user_by_email_and_password(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    role = await db.get_role(user["id"])
    if role is None:
        raise HTTPException(status_code=403, detail="No role assigned")

    session_id = await db.create_session(user["id"])

    response = Response(
        content='{"success": true}',
        media_type="application/json",
    )
    _set_session_cookie(response, session_id)
    return response


# ---- OAuth ----

@router.get("/login/{provider}")
async def oauth_login(provider: str):
    """Redirect to OAuth provider."""
    if provider == "gitlab":
        return RedirectResponse("/api/gitlab/auth/login")
    try:
        oauth = get_provider(provider)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    state = secrets.token_urlsafe(24)
    url = oauth.get_authorize_url(state)
    return RedirectResponse(url)


@router.get("/callback/{provider}")
async def oauth_callback(provider: str, code: str, state: str = ""):
    """OAuth callback: upsert user, create session, redirect to frontend."""
    if provider == "gitlab":
        return RedirectResponse(f"/api/gitlab/auth/callback?code={code}&state={state}")
    try:
        oauth = get_provider(provider)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    try:
        access_token = await oauth.exchange_code(code)
        info = await oauth.get_user_info(access_token)
    except Exception as e:
        logger.error(f"OAuth error for {provider}: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"OAuth error: {e}")

    # Lookup by provider account first
    oauth_account = await db.get_oauth_account(info.provider, info.provider_user_id)

    if oauth_account:
        user = await db.get_user_by_id(oauth_account["user_id"])
    else:
        # Try to link by email
        user_dict = await db.get_user_by_email(info.email)
        if user_dict:
            user = user_dict
            await db.create_oauth_account(user["id"], info.provider, info.provider_user_id, info.provider_username)
            logger.info(f"Linked {info.provider} account to existing user {info.email}")
        else:
            # Check if there's a pending invitation for this email
            invitation = await db.get_invitation_by_email(info.email)
            count = await db.user_count()

            # Only allow signup if first user or has invitation
            if count > 0 and not invitation:
                logger.warning(f"OAuth signup rejected for {info.email}: no invitation")
                return RedirectResponse(f"{settings.frontend_url}/auth/login?error=not_invited")

            # Create new user
            user = await db.create_user(info.email, info.name, info.avatar_url)
            await db.create_oauth_account(user["id"], info.provider, info.provider_user_id, info.provider_username)
            logger.info(f"Created new user {info.email} via {info.provider}")

            if count == 0:
                await db.set_role(user["id"], Role.admin.value)
                logger.info(f"First user {info.email} assigned admin role")
            else:
                await db.set_role(user["id"], invitation["role"])
                await db.mark_invitation_accepted(invitation["id"])
                if invitation.get("project_slug"):
                    await db.add_project_member(user["id"], invitation["project_slug"], invitation["invited_by"])
                logger.info(f"User {info.email} accepted invitation with role {invitation['role']}")

    if not user:
        raise HTTPException(status_code=500, detail="Failed to resolve user")

    # Check user has a role
    role = await db.get_role(user["id"])
    if role is None:
        return RedirectResponse(f"{settings.frontend_url}/auth/login?error=no_role")

    session_id = await db.create_session(user["id"])

    response = RedirectResponse(settings.frontend_url)
    _set_session_cookie(response, session_id)
    return response


# ---- Forward Auth for Preview URLs ----

@router.get("/verify-preview")
async def verify_preview(request: Request):
    """Caddy forward_auth: validate session for preview URLs.

    Returns 200 if authenticated, 302 redirect to login if not.
    Also updates last_accessed_at for the preview.
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return _redirect_to_login(request)

    session = await db.get_session(session_id)
    if not session:
        return _redirect_to_login(request)

    # Update last_accessed_at for the preview
    # The subdomain format is {preview_name}-{project}.mr.preview-mr.com
    # For MR previews: mr-123-drupal-test.mr.preview-mr.com
    # For branch previews: branch-develop-drupal-test.mr.preview-mr.com
    host = request.headers.get("x-forwarded-host", "")
    match = re.match(r"(.+?)\.mr\.preview-mr\.com", host)
    if match:
        subdomain = match.group(1)  # e.g. "mr-123-drupal-test" or "branch-develop-drupal-test"
        # Try MR pattern first (unambiguous)
        mr_match = re.match(r"(mr-\d+)-(.+)", subdomain)
        if mr_match:
            preview_name = mr_match.group(1)
            project = mr_match.group(2)
        else:
            # For branch previews, find the split point by checking project dirs
            preview_name = None
            project = None
            parts = subdomain.split("-")
            # Try splitting from the end — project name is the last segment(s)
            for i in range(len(parts) - 1, 0, -1):
                candidate_project = "-".join(parts[i:])
                candidate_preview = "-".join(parts[:i])
                preview_dir = Path(settings.previews_base_path) / candidate_project / candidate_preview
                if preview_dir.exists():
                    preview_name = candidate_preview
                    project = candidate_project
                    break

        if preview_name and project:
            try:
                await update_last_accessed(project, preview_name)
            except Exception as e:
                logger.warning(f"Failed to update last_accessed_at for {project}/{preview_name}: {e}")

    return Response(status_code=200)


def _redirect_to_login(request: Request) -> RedirectResponse:
    """Build a redirect to the login page with the original URL as redirect_to."""
    original_host = request.headers.get("x-forwarded-host", "")
    original_proto = request.headers.get("x-forwarded-proto", "https")
    original_uri = request.headers.get("x-forwarded-uri", "/")
    original_url = f"{original_proto}://{original_host}{original_uri}"
    login_url = f"{settings.frontend_url}/auth/login?redirect_to={original_url}"
    return RedirectResponse(login_url, status_code=302)


# ---- Session ----

@router.post("/logout")
async def logout(response: Response, user: UserWithRole = Depends(get_current_user)):
    # We need the session id to delete it. For simplicity, just clear cookie.
    _delete_session_cookie(response)
    return {"success": True}


@router.get("/me")
async def get_me(user: UserWithRole = Depends(get_current_user)):
    return user


# ---- API Tokens ----

@router.get("/tokens")
async def list_tokens(user: UserWithRole = Depends(require_role(Role.manager))):
    tokens = await db.list_api_tokens(user.id)
    return {"tokens": tokens}


@router.post("/tokens")
async def create_token(body: CreateTokenRequest, user: UserWithRole = Depends(require_role(Role.manager))):
    token_id, raw_token = await db.create_api_token(user.id, body.name)
    return {"id": token_id, "token": raw_token, "name": body.name}


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: int, user: UserWithRole = Depends(require_role(Role.manager))):
    deleted = await db.delete_api_token(token_id, user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"success": True}


# ---- CLI Device Flow ----

@router.post("/cli/request")
async def cli_request(body: CLIRequestBody):
    """CLI posts a code to create a pending auth request."""
    existing = await db.get_cli_auth_request(body.code)
    if existing:
        raise HTTPException(status_code=409, detail="Code already exists")
    await db.create_cli_auth_request(body.code)
    return {"status": "pending"}


@router.post("/cli/approve")
async def cli_approve(body: CLIApproveBody, user: UserWithRole = Depends(get_current_user)):
    """Browser approves a CLI auth request, generating a token."""
    req = await db.get_cli_auth_request(body.code)
    if not req or req["status"] != "pending":
        raise HTTPException(status_code=404, detail="Request not found or already processed")

    token_id, raw_token = await db.create_api_token(user.id, f"CLI ({body.code[:8]})")
    await db.approve_cli_auth_request(body.code, user.id, raw_token)
    return {"success": True}


@router.get("/cli/poll/{code}")
async def cli_poll(code: str):
    """CLI polls for token."""
    req = await db.get_cli_auth_request(code)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req["status"] == "pending":
        return {"status": "pending"}
    if req["status"] == "approved":
        return {"status": "approved", "token": req["token"]}
    return {"status": req["status"]}


# ---- User Management ----

@router.get("/users")
async def list_users(user: UserWithRole = Depends(require_role(Role.manager))):
    users = await db.list_users()
    return {"users": users}


@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    body: UpdateRoleBody,
    user: UserWithRole = Depends(require_role(Role.admin)),
):
    target = await db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    await db.set_role(user_id, body.role.value)
    return {"success": True}


@router.delete("/users/{user_id}")
async def remove_user(
    user_id: int,
    user: UserWithRole = Depends(require_role(Role.admin)),
):
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    target = await db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    # Managers can't delete admins
    target_role = await db.get_role(user_id)
    if target_role == Role.admin.value and not has_min_role(user.role, Role.admin):
        raise HTTPException(status_code=403, detail="Cannot delete an admin")
    await db.delete_user(user_id)
    return {"success": True}


# ---- Invitations ----

@router.post("/invitations")
async def create_invitation(
    body: InviteBody,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    """Create an invitation and send email."""
    existing_user = await db.get_user_by_email(body.email)
    if existing_user:
        raise HTTPException(status_code=400, detail="A user with this email already exists")

    existing_inv = await db.get_invitation_by_email(body.email)
    if existing_inv:
        raise HTTPException(status_code=400, detail="A pending invitation for this email already exists")

    invitation = await db.create_invitation(body.email, body.role.value, user.id, body.project_slug)

    try:
        send_invitation_email(body.email, invitation["token"], body.role.value, user.name)
    except Exception:
        pass  # logged in email module, don't fail the endpoint

    return {
        "id": invitation["id"],
        "email": invitation["email"],
        "role": invitation["role"],
        "project_slug": invitation.get("project_slug"),
        "created_at": invitation["created_at"],
        "expires_at": invitation["expires_at"],
    }


@router.get("/invitations")
async def list_invitations(user: UserWithRole = Depends(require_role(Role.admin))):
    invitations = await db.list_invitations()
    return {"invitations": invitations}


@router.delete("/invitations/{invitation_id}")
async def cancel_invitation(
    invitation_id: int,
    user: UserWithRole = Depends(require_role(Role.admin)),
):
    await db.delete_invitation(invitation_id)
    return {"success": True}


@router.get("/invitations/validate")
async def validate_invitation(token: str):
    """Public: validate an invitation token."""
    invitation = await db.get_invitation_by_token(token)
    if not invitation:
        raise HTTPException(status_code=404, detail="Invalid or expired invitation")
    return {"email": invitation["email"], "role": invitation["role"]}


@router.post("/invitations/accept")
async def accept_invitation(body: AcceptInviteBody):
    """Public: accept an invitation with password."""
    invitation = await db.get_invitation_by_token(body.token)
    if not invitation:
        raise HTTPException(status_code=404, detail="Invalid or expired invitation")

    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    existing = await db.get_user_by_email(invitation["email"])
    if existing:
        raise HTTPException(status_code=400, detail="A user with this email already exists")

    user = await db.create_user_with_password(invitation["email"], body.name, body.password)
    await db.set_role(user["id"], invitation["role"])
    await db.mark_invitation_accepted(invitation["id"])

    # Auto-add to project if invitation was for a specific project
    if invitation.get("project_slug"):
        await db.add_project_member(user["id"], invitation["project_slug"], invitation["invited_by"])

    session_id = await db.create_session(user["id"])

    response = Response(
        content='{"success": true}',
        media_type="application/json",
    )
    _set_session_cookie(response, session_id)
    return response


# ---- Project Members ----

@router.get("/projects/{project_slug}/members")
async def list_project_members(
    project_slug: str,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    members = await db.list_project_members(project_slug)
    invitations = await db.list_invitations(project_slug=project_slug)
    return {"members": members, "invitations": invitations}


@router.post("/projects/{project_slug}/members")
async def add_project_member(
    project_slug: str,
    body: AddProjectMemberBody,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    target = await db.get_user_by_id(body.user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    await db.add_project_member(body.user_id, project_slug, user.id)
    return {"success": True}


@router.delete("/projects/{project_slug}/members/{user_id}")
async def remove_project_member(
    project_slug: str,
    user_id: int,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    await db.remove_project_member(user_id, project_slug)
    return {"success": True}


@router.get("/my-projects")
async def my_projects(user: UserWithRole = Depends(get_current_user)):
    if has_min_role(user.role, Role.admin):
        return {"all": True, "projects": []}
    slugs = await db.get_user_project_slugs(user.id)
    return {"all": False, "projects": slugs}
