"""Organization CRUD routes."""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import get_current_user, get_org_context, require_org_role, require_superadmin
from app.auth.models import (
    AddEmailDomainBody,
    AddOrgMemberBody,
    CreateOrgBody,
    InviteBody,
    OrgRole,
    UpdateOrgBody,
    UpdateOrgMemberRoleBody,
    UserWithContext,
)
from app.database import (
    add_email_domain,
    add_org_member,
    create_organization,
    delete_organization,
    get_organization_by_slug,
    list_email_domains,
    list_org_members,
    list_organizations,
    list_user_organizations,
    match_email_domain,
    remove_email_domain,
    remove_org_member,
    update_org_member_role,
    update_organization,
)
from app.database import (
    create_org_invitation,
    delete_invitation,
    get_invitation_by_email,
    list_org_invitations,
)
from app.auth.email import send_invitation_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/orgs", tags=["organizations"])


def _validate_slug(slug: str) -> str:
    slug = slug.lower().strip()
    if not re.match(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$", slug):
        raise HTTPException(
            status_code=400,
            detail="Slug must be 3-40 chars, lowercase alphanumeric and hyphens, cannot start/end with hyphen",
        )
    return slug


# ---- Organization CRUD ----

@router.get("")
async def list_my_orgs(user: UserWithContext = Depends(get_current_user)):
    """List organizations the user belongs to. Superadmin sees all."""
    if user.is_superadmin:
        orgs = await list_organizations()
    else:
        orgs = await list_user_organizations(user.id)
    return {"organizations": orgs}


@router.post("")
async def create_org(body: CreateOrgBody, user: UserWithContext = Depends(get_current_user)):
    """Create a new organization. The creator becomes the owner."""
    slug = _validate_slug(body.slug)

    existing = await get_organization_by_slug(slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"Organization '{slug}' already exists")

    org = await create_organization(slug, body.name, color=body.color)
    await add_org_member(user.id, org["id"], OrgRole.owner.value)
    logger.info(f"Organization '{slug}' created by {user.email}")

    return org


@router.get("/{org}")
async def get_org(user: UserWithContext = Depends(get_org_context)):
    """Get organization details."""
    return user.org


@router.patch("/{org}")
async def update_org(body: UpdateOrgBody, user: UserWithContext = Depends(require_org_role(OrgRole.owner))):
    """Update organization settings (owner only)."""
    fields = body.model_dump(exclude_none=True)
    if not fields:
        return user.org
    org = await update_organization(user.org.id, **fields)
    return org


@router.delete("/{org}")
async def delete_org(user: UserWithContext = Depends(require_org_role(OrgRole.owner))):
    """Delete organization and all associated resources (owner only).

    Cascade order: destroy VMs → remove Caddy routes → delete previews → delete org.
    Database CASCADE handles org_members, projects, invitations, tokens, etc.
    """
    from app.database import get_all_previews, list_projects
    from app.routes.previews import delete_preview_internal
    from app.websockets import preview_list_manager

    org_id = user.org.id
    org_slug = user.org.slug

    # Get all previews for this org and clean up non-DB resources (VMs, Caddy, files)
    all_previews = await get_all_previews(org_id=org_id)
    for preview in all_previews:
        try:
            await delete_preview_internal(
                org_slug,
                preview.get("project_slug", ""),
                preview.get("project_id"),
                preview["preview_name"],
            )
        except Exception as e:
            logger.warning(f"Error cleaning up preview {preview['preview_name']} during org delete: {e}")

    # Database CASCADE deletes: org_members, projects, project_members,
    # previews, deployments, api_tokens, invitations, email_domains
    await delete_organization(org_id)

    await preview_list_manager.force_broadcast()

    logger.info(f"Organization '{org_slug}' deleted by {user.email}")
    return {"success": True}


# ---- Members ----

@router.get("/{org}/members")
async def list_members(user: UserWithContext = Depends(require_org_role(OrgRole.admin))):
    members = await list_org_members(user.org.id)
    return {"members": members}


@router.post("/{org}/members")
async def add_member(body: AddOrgMemberBody, user: UserWithContext = Depends(require_org_role(OrgRole.admin))):
    from app.auth import database as auth_db
    target = await auth_db.get_user_by_id(body.user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if body.role == OrgRole.owner:
        raise HTTPException(status_code=400, detail="Cannot assign owner role directly")
    await add_org_member(body.user_id, user.org.id, body.role.value)
    return {"success": True}


@router.patch("/{org}/members/{user_id}")
async def update_member_role(
    user_id: int, body: UpdateOrgMemberRoleBody,
    user: UserWithContext = Depends(require_org_role(OrgRole.admin)),
):
    if body.role == OrgRole.owner:
        raise HTTPException(status_code=400, detail="Cannot assign owner role")
    from app.database import get_org_member
    member = await get_org_member(user_id, user.org.id)
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    if member["role"] == OrgRole.owner.value:
        raise HTTPException(status_code=400, detail="Cannot change owner's role")
    await update_org_member_role(user_id, user.org.id, body.role.value)
    return {"success": True}


@router.delete("/{org}/members/{user_id}")
async def remove_member(user_id: int, user: UserWithContext = Depends(require_org_role(OrgRole.admin))):
    from app.database import get_org_member
    member = await get_org_member(user_id, user.org.id)
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    if member["role"] == OrgRole.owner.value:
        raise HTTPException(status_code=400, detail="Cannot remove the owner")
    await remove_org_member(user_id, user.org.id)
    return {"success": True}


# ---- Email Domains ----

@router.get("/{org}/email-domains")
async def list_domains(user: UserWithContext = Depends(require_org_role(OrgRole.owner))):
    domains = await list_email_domains(user.org.id)
    return {"domains": domains}


@router.post("/{org}/email-domains")
async def add_domain(body: AddEmailDomainBody, user: UserWithContext = Depends(require_org_role(OrgRole.owner))):
    # Check if domain is already taken by another org
    existing = await match_email_domain(f"test@{body.domain}")
    if existing:
        raise HTTPException(status_code=409, detail=f"Domain '{body.domain}' is already claimed by another organization")
    domain = await add_email_domain(user.org.id, body.domain, body.default_role.value)
    return domain


@router.delete("/{org}/email-domains/{domain_id}")
async def delete_domain(domain_id: int, user: UserWithContext = Depends(require_org_role(OrgRole.owner))):
    await remove_email_domain(domain_id)
    return {"success": True}


# ---- Invitations ----

@router.get("/{org}/invitations")
async def list_invitations(user: UserWithContext = Depends(require_org_role(OrgRole.admin))):
    invitations = await list_org_invitations(user.org.id)
    return {"invitations": invitations}


@router.post("/{org}/invitations")
async def create_invitation(body: InviteBody, user: UserWithContext = Depends(require_org_role(OrgRole.admin))):
    from app.auth import database as auth_db
    existing_user = await auth_db.get_user_by_email(body.email)
    if existing_user:
        raise HTTPException(status_code=400, detail="A user with this email already exists")

    existing_inv = await get_invitation_by_email(body.email, user.org.id)
    if existing_inv:
        raise HTTPException(status_code=400, detail="A pending invitation for this email already exists")

    # Resolve project_id if project_slug provided
    project_id = None
    if body.project_slug:
        from app.database import get_project_by_slug
        project = await get_project_by_slug(user.org.id, body.project_slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{body.project_slug}' not found")
        project_id = project["id"]

    invitation = await create_org_invitation(
        user.org.id, body.email, body.role.value, user.id, project_id
    )

    try:
        send_invitation_email(body.email, invitation["token"], body.role.value, user.name)
    except Exception:
        pass

    return invitation


@router.delete("/{org}/invitations/{invitation_id}")
async def cancel_invitation(invitation_id: int, user: UserWithContext = Depends(require_org_role(OrgRole.admin))):
    await delete_invitation(invitation_id)
    return {"success": True}
