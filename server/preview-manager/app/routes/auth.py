"""Auth routes: OAuth login, sessions, tokens, CLI device flow, user management"""

import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse

from config.settings import settings
from app.auth import database as db
from app.auth.dependencies import SESSION_COOKIE, get_current_user, require_role
from app.auth.models import (
    AcceptInviteBody,
    CLIApproveBody,
    CLIRequestBody,
    CreateTokenRequest,
    InviteBody,
    LoginBody,
    Role,
    SetupBody,
    UpdateAllowedDomainsBody,
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

    user = await db.create_user_with_password(body.email, body.name, body.password)
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

            if not invitation:
                # No invitation — check email domain
                allowed = settings.allowed_email_domains.strip()
                if allowed:
                    domain = info.email.split("@")[1] if "@" in info.email else ""
                    allowed_list = [d.strip() for d in allowed.split(",") if d.strip()]
                    if domain not in allowed_list:
                        return RedirectResponse(f"{settings.frontend_url}/auth/login?error=domain_not_allowed")

            # Create new user
            user = await db.create_user(info.email, info.name, info.avatar_url)
            await db.create_oauth_account(user["id"], info.provider, info.provider_user_id, info.provider_username)
            logger.info(f"Created new user {info.email} via {info.provider}")

            # First user gets admin
            count = await db.user_count()
            if count == 1:
                await db.set_role(user["id"], Role.admin.value)
                logger.info(f"First user {info.email} assigned admin role")
            elif invitation:
                # Use the role from the invitation
                await db.set_role(user["id"], invitation["role"])
                await db.mark_invitation_accepted(invitation["id"])
                logger.info(f"User {info.email} accepted invitation with role {invitation['role']}")
            else:
                # Auto-assign member role if domain matches
                await db.set_role(user["id"], Role.member.value)

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
async def list_tokens(user: UserWithRole = Depends(get_current_user)):
    tokens = await db.list_api_tokens(user.id)
    return {"tokens": tokens}


@router.post("/tokens")
async def create_token(body: CreateTokenRequest, user: UserWithRole = Depends(get_current_user)):
    token_id, raw_token = await db.create_api_token(user.id, body.name)
    return {"id": token_id, "token": raw_token, "name": body.name}


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: int, user: UserWithRole = Depends(get_current_user)):
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
    user: UserWithRole = Depends(require_role(Role.manager)),
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


# ---- Auth Settings ----

@router.get("/settings")
async def get_auth_settings(user: UserWithRole = Depends(require_role(Role.admin))):
    return {"allowed_email_domains": settings.allowed_email_domains}


@router.put("/settings")
async def update_auth_settings(
    body: UpdateAllowedDomainsBody,
    user: UserWithRole = Depends(require_role(Role.admin)),
):
    settings.allowed_email_domains = body.allowed_email_domains
    return {"success": True}


# ---- Invitations ----

@router.post("/invitations")
async def create_invitation(
    body: InviteBody,
    user: UserWithRole = Depends(require_role(Role.admin)),
):
    """Create an invitation and send email."""
    existing_user = await db.get_user_by_email(body.email)
    if existing_user:
        raise HTTPException(status_code=400, detail="A user with this email already exists")

    existing_inv = await db.get_invitation_by_email(body.email)
    if existing_inv:
        raise HTTPException(status_code=400, detail="A pending invitation for this email already exists")

    invitation = await db.create_invitation(body.email, body.role.value, user.id)

    try:
        send_invitation_email(body.email, invitation["token"], body.role.value, user.name)
    except Exception:
        pass  # logged in email module, don't fail the endpoint

    return {
        "id": invitation["id"],
        "email": invitation["email"],
        "role": invitation["role"],
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

    session_id = await db.create_session(user["id"])

    response = Response(
        content='{"success": true}',
        media_type="application/json",
    )
    _set_session_cookie(response, session_id)
    return response
