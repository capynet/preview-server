"""Auth routes: setup, login, OAuth, sessions, CLI device flow, SSH keys."""

import asyncio
import logging
import secrets
import subprocess
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from config.settings import settings
from app.auth import database as db
from app.auth.dependencies import SESSION_COOKIE, get_current_user
from app.database import (
    add_org_member, get_invitation_by_token, get_pool, get_preview_by_domain,
    list_user_organizations, mark_invitation_accepted, match_email_domain,
    resolve_project_by_slug, update_last_accessed, _now,
)
from app.auth.models import (
    AcceptInviteBody,
    CLIApproveBody,
    CLIRequestBody,
    OrgRole,
    UserWithContext,
)
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
        domain=f".{settings.base_domain}",
    )


def _delete_session_cookie(response: Response):
    response.delete_cookie(SESSION_COOKIE, domain=f".{settings.base_domain}")


# ---- OAuth ----

@router.get("/login/{provider}")
async def oauth_login(provider: str):
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

    user, is_new = await _resolve_oauth_user(info)
    if not user:
        return RedirectResponse(f"{settings.frontend_url}/auth/login?error=not_invited")

    session_id = await db.create_session(user["id"])
    response = RedirectResponse(settings.frontend_url)
    _set_session_cookie(response, session_id)
    return response


async def _resolve_oauth_user(info) -> tuple[dict | None, bool]:
    """Resolve or create a user from OAuth info. Returns (user, is_new)."""
    # Lookup by provider account first
    oauth_account = await db.get_oauth_account(info.provider, info.provider_user_id)

    if oauth_account:
        user = await db.get_user_by_id(oauth_account["user_id"])
        # Update avatar/name from provider on each login
        if user and (info.avatar_url or info.name):
            updates = {}
            if info.avatar_url and user.get("avatar_url") != info.avatar_url:
                updates["avatar_url"] = info.avatar_url
            if info.name and user.get("name") != info.name:
                updates["name"] = info.name
            if updates:
                pool = await get_pool()
                sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates))
                await pool.execute(f"UPDATE users SET {sets}, updated_at = $1 WHERE id = ${len(updates)+2}", _now(), *updates.values(), user["id"])
                user.update(updates)
        return user, False

    # Try to link by email
    user_dict = await db.get_user_by_email(info.email)
    if user_dict:
        await db.create_oauth_account(user_dict["id"], info.provider, info.provider_user_id, info.provider_username)
        # Update avatar/name from provider
        if info.avatar_url or info.name:
            updates = {}
            if info.avatar_url and user_dict.get("avatar_url") != info.avatar_url:
                updates["avatar_url"] = info.avatar_url
            if info.name and user_dict.get("name") != info.name:
                updates["name"] = info.name
            if updates:
                pool = await get_pool()
                sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates))
                await pool.execute(f"UPDATE users SET {sets}, updated_at = $1 WHERE id = ${len(updates)+2}", _now(), *updates.values(), user_dict["id"])
                user_dict.update(updates)
        return user_dict, False

    # New user
    count = await db.user_count()

    # Check if there's a pending invitation for this email
    from app.database import get_invitation_by_email
    invitation = await get_invitation_by_email(info.email)

    # Check allowed email domain
    domain_match = await match_email_domain(info.email) if count > 0 else None

    # Reject users without invitation or matching email domain
    if count > 0 and not invitation and not domain_match:
        logger.info(f"Rejected OAuth signup for {info.email}: no invitation or domain match")
        return None, False

    # Create user
    is_superadmin = count == 0
    user = await db.create_user(info.email, info.name, info.avatar_url, is_superadmin=is_superadmin)
    await db.create_oauth_account(user["id"], info.provider, info.provider_user_id, info.provider_username)

    if is_superadmin:
        logger.info(f"First user {info.email} assigned superadmin")
    elif invitation:
        await mark_invitation_accepted(invitation["id"])
        await add_org_member(user["id"], invitation["organization_id"], invitation["role"])
        if invitation.get("project_id"):
            from app.database import add_project_member
            await add_project_member(user["id"], invitation["project_id"], invitation["invited_by"], invitation["role"])
        logger.info(f"User {info.email} accepted invitation for org {invitation['organization_id']} with role {invitation['role']}")
    elif domain_match:
        await add_org_member(user["id"], domain_match["organization_id"], domain_match["default_role"])
        logger.info(f"User {info.email} auto-joined org {domain_match['organization_id']} via domain match")

    return user, True


# ---- Forward Auth for Preview URLs ----

@router.get("/verify-preview")
async def verify_preview(request: Request):
    """Caddy forward_auth: validate session for preview URLs."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return _redirect_to_login(request)

    session = await db.get_session(session_id)
    if not session:
        return _redirect_to_login(request)

    # Update last_accessed_at for the preview
    host = request.headers.get("x-forwarded-host", "")
    preview = await get_preview_by_domain(host)
    if preview:
        try:
            await update_last_accessed(preview["id"])
        except Exception as e:
            logger.warning(f"Failed to update last_accessed_at: {e}")

    return Response(status_code=200)


def _redirect_to_login(request: Request) -> RedirectResponse:
    original_host = request.headers.get("x-forwarded-host", "")
    original_proto = request.headers.get("x-forwarded-proto", "https")
    original_uri = request.headers.get("x-forwarded-uri", "/")
    original_url = f"{original_proto}://{original_host}{original_uri}"
    login_url = f"{settings.frontend_url}/auth/login?redirect_to={original_url}"
    return RedirectResponse(login_url, status_code=302)


# ---- Session ----

@router.post("/logout")
async def logout(response: Response, user: UserWithContext = Depends(get_current_user)):
    _delete_session_cookie(response)
    return {"success": True}


@router.get("/me")
async def get_me(user: UserWithContext = Depends(get_current_user)):
    orgs = await list_user_organizations(user.id)
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "is_superadmin": user.is_superadmin,
        "organizations": [
            {"org_id": o["id"], "org_slug": o["slug"], "org_name": o["name"], "role": o["role"], "color": o.get("color", "#6366f1"), "gitlab_url": o.get("gitlab_url")}
            for o in orgs
        ],
    }


@router.get("/projects/resolve")
async def resolve_project(slug: str, user: UserWithContext = Depends(get_current_user)):
    """Resolve a project slug to org+project across all user's organizations.

    Used by the CLI to auto-detect the organization from a git remote.
    """
    matches = await resolve_project_by_slug(user.id, slug)
    return {
        "matches": [
            {
                "org_slug": m["org_slug"],
                "org_name": m["org_name"],
                "project_slug": m["slug"],
                "project_id": m["id"],
            }
            for m in matches
        ],
    }


# ---- CLI Device Flow ----

@router.post("/cli/request")
async def cli_request(body: CLIRequestBody):
    existing = await db.get_cli_auth_request(body.code)
    if existing:
        raise HTTPException(status_code=409, detail="Code already exists")
    await db.create_cli_auth_request(body.code)
    return {"status": "pending"}


@router.post("/cli/approve")
async def cli_approve(body: CLIApproveBody, user: UserWithContext = Depends(get_current_user)):
    req = await db.get_cli_auth_request(body.code)
    if not req or req["status"] != "pending":
        raise HTTPException(status_code=404, detail="Request not found or already processed")

    # Org is optional — CLI tokens can be user-scoped
    org_id = None
    if body.org_slug:
        from app.database import get_organization_by_slug
        org = await get_organization_by_slug(body.org_slug)
        if not org:
            raise HTTPException(status_code=404, detail=f"Organization '{body.org_slug}' not found")
        org_id = org["id"]

    token_id, raw_token = await db.create_api_token(user.id, org_id, f"CLI ({body.code[:8]})")
    await db.approve_cli_auth_request(body.code, user.id, raw_token)
    return {"success": True}


@router.get("/cli/poll/{code}")
async def cli_poll(code: str):
    req = await db.get_cli_auth_request(code)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req["status"] == "pending":
        return {"status": "pending"}
    if req["status"] == "approved":
        return {"status": "approved", "token": req["token"]}
    return {"status": req["status"]}


# ---- Invitation Accept (public) ----

@router.get("/invitations/validate")
async def validate_invitation(token: str):
    invitation = await get_invitation_by_token(token)
    if not invitation:
        raise HTTPException(status_code=404, detail="Invalid or expired invitation")
    from app.database import get_organization_by_id
    org = await get_organization_by_id(invitation["organization_id"])
    return {
        "email": invitation["email"],
        "role": invitation["role"],
        "organization": {"slug": org["slug"], "name": org["name"]} if org else None,
    }


@router.post("/invitations/accept")
async def accept_invitation(body: AcceptInviteBody):
    invitation = await get_invitation_by_token(body.token)
    if not invitation:
        raise HTTPException(status_code=404, detail="Invalid or expired invitation")

    existing = await db.get_user_by_email(invitation["email"])
    if existing:
        # User already exists (via OAuth) — add to org/project directly
        user = existing
    else:
        # New user — create without password (OAuth only)
        user = await db.create_user(invitation["email"], body.name or invitation["email"].split("@")[0])

    await add_org_member(user["id"], invitation["organization_id"], invitation["role"])
    await mark_invitation_accepted(invitation["id"])

    # Auto-add to project if invitation was for a specific project
    if invitation.get("project_id"):
        from app.database import add_project_member
        await add_project_member(user["id"], invitation["project_id"], invitation["invited_by"], invitation["role"])

    session_id = await db.create_session(user["id"])
    response = Response(content='{"success": true}', media_type="application/json")
    _set_session_cookie(response, session_id)
    return response


# ---- Magic Link ----

class MagicLinkRequestBody(BaseModel):
    email: str


@router.post("/magic/request")
async def magic_link_request(body: MagicLinkRequestBody):
    """Send a magic link email. Always returns success to prevent email enumeration."""
    user = await db.get_user_by_email(body.email.strip().lower())
    if user:
        token = await db.create_magic_link_token(user["id"])
        from app.auth.email import send_magic_link_email
        send_magic_link_email(user["email"], token)
    return {"success": True}


@router.get("/magic/verify")
async def magic_link_verify(token: str):
    """Verify a magic link token, set session cookie, and redirect to app."""
    frontend = settings.frontend_url

    result = await db.validate_and_consume_magic_link_token(token)
    if not result:
        return RedirectResponse(f"{frontend}/auth/login?error=magic_expired")

    user = await db.get_user_by_id(result["user_id"])
    if not user:
        return RedirectResponse(f"{frontend}/auth/login?error=magic_expired")

    session_id = await db.create_session(user["id"])
    response = RedirectResponse(frontend, status_code=302)
    _set_session_cookie(response, session_id)
    return response


# ---- SSH Keys ----

AUTHORIZED_KEYS_PATH = "/home/preview-manager/.ssh/authorized_keys"
COMMAND_PREFIX = 'command="/usr/local/bin/preview-ssh-proxy"'
MANAGED_MARKER = 'command="/usr/local/bin/preview-ssh-proxy"'

VALID_KEY_PREFIXES = ("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "sk-ssh-ed25519", "sk-ecdsa-sha2-nistp256")


class SSHKeyBody(BaseModel):
    public_key: str


def _extract_fingerprint(public_key: str) -> str:
    """Extract SHA256 fingerprint using ssh-keygen."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=True) as f:
        f.write(public_key.strip() + "\n")
        f.flush()
        result = subprocess.run(
            ["ssh-keygen", "-lf", f.name],
            capture_output=True, text=True, timeout=5,
        )
    if result.returncode != 0:
        raise ValueError(f"Invalid SSH key: {result.stderr.strip()}")
    # Output format: "256 SHA256:abc123... comment (ED25519)"
    parts = result.stdout.strip().split()
    if len(parts) < 2:
        raise ValueError("Could not parse fingerprint")
    return parts[1]  # e.g. "SHA256:abc123..."


def _extract_key_comment(public_key: str) -> str:
    """Extract the comment (last field) from a public key line."""
    parts = public_key.strip().split()
    if len(parts) >= 3:
        return parts[-1]
    return "unnamed"


async def _sync_authorized_keys():
    """Regenerate authorized_keys from DB, preserving non-managed entries."""
    all_keys = await db.get_all_ssh_keys()

    def _write():
        # Read existing file, keep non-managed lines
        existing_lines = []
        try:
            with open(AUTHORIZED_KEYS_PATH, "r") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line or line.startswith("#"):
                        existing_lines.append(line)
                    elif MANAGED_MARKER not in line:
                        existing_lines.append(line)
        except FileNotFoundError:
            pass

        # Build managed lines from DB
        managed_lines = []
        for key_row in all_keys:
            pk = key_row["public_key"].strip()
            managed_lines.append(
                f'{COMMAND_PREFIX},no-port-forwarding,no-X11-forwarding,no-agent-forwarding {pk}'
            )

        # Write: existing non-managed lines first, then managed lines
        import os
        with open(AUTHORIZED_KEYS_PATH, "w") as f:
            for line in existing_lines:
                f.write(line + "\n")
            for line in managed_lines:
                f.write(line + "\n")
        os.chmod(AUTHORIZED_KEYS_PATH, 0o600)

    await asyncio.to_thread(_write)


@router.post("/ssh-keys")
async def add_ssh_key(body: SSHKeyBody, user: UserWithContext = Depends(get_current_user)):
    public_key = body.public_key.strip()

    # Validate key format
    if not any(public_key.startswith(prefix) for prefix in VALID_KEY_PREFIXES):
        raise HTTPException(status_code=400, detail="Invalid SSH public key format")

    # Extract fingerprint
    try:
        fingerprint = await asyncio.to_thread(_extract_fingerprint, public_key)
    except (ValueError, subprocess.TimeoutExpired) as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Extract name from key comment
    name = _extract_key_comment(public_key)

    # Store in DB
    try:
        key_row = await db.add_ssh_key(user.id, name, public_key, fingerprint)
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail="SSH key already registered")
        raise

    # Sync authorized_keys file
    await _sync_authorized_keys()

    return {"id": key_row["id"], "name": key_row["name"], "fingerprint": key_row["fingerprint"]}


@router.get("/ssh-keys")
async def list_ssh_keys(user: UserWithContext = Depends(get_current_user)):
    keys = await db.list_ssh_keys(user.id)
    return keys


@router.delete("/ssh-keys/{key_id}")
async def delete_ssh_key(key_id: int, user: UserWithContext = Depends(get_current_user)):
    deleted = await db.delete_ssh_key(key_id, user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="SSH key not found")

    # Sync authorized_keys file
    await _sync_authorized_keys()

    return {"success": True}
