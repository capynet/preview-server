"""Auth Pydantic models — multi-tenant with org-scoped roles."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class OrgRole(str, Enum):
    owner = "owner"
    admin = "admin"
    member = "member"
    viewer = "viewer"


# Hierarchy: owner > admin > member > viewer
ORG_ROLE_HIERARCHY = {
    OrgRole.owner: 4,
    OrgRole.admin: 3,
    OrgRole.member: 2,
    OrgRole.viewer: 1,
}


def has_min_role(user_role: Optional[OrgRole], min_role: OrgRole) -> bool:
    if user_role is None:
        return False
    return ORG_ROLE_HIERARCHY.get(user_role, 0) >= ORG_ROLE_HIERARCHY.get(min_role, 0)


class User(BaseModel):
    id: int
    email: str
    name: str
    avatar_url: Optional[str] = None
    is_superadmin: bool = False
    created_at: str
    updated_at: str


class Organization(BaseModel):
    id: int
    slug: str
    name: str
    avatar_url: Optional[str] = None
    gitlab_url: Optional[str] = None
    auto_erase_enabled: bool = False
    auto_erase_days: int = 10
    created_at: str
    updated_at: str


class OrgMembership(BaseModel):
    organization_id: int
    org_slug: str
    org_name: str
    role: OrgRole


class UserWithContext(User):
    """User with optional org context (resolved per-request)."""
    org: Optional[Organization] = None
    org_role: Optional[OrgRole] = None


class OAuthAccount(BaseModel):
    id: int
    user_id: int
    provider: str
    provider_user_id: str
    provider_username: Optional[str] = None
    created_at: str


class Session(BaseModel):
    id: str
    user_id: int
    created_at: str
    expires_at: str


class APIToken(BaseModel):
    id: int
    user_id: int
    organization_id: int
    name: str
    token_prefix: str
    created_at: str
    last_used_at: Optional[str] = None


class CLIAuthRequest(BaseModel):
    code: str
    status: str  # pending, approved, expired
    organization_id: Optional[int] = None
    user_id: Optional[int] = None
    token: Optional[str] = None
    created_at: str


# ---- Request/Response bodies ----

class SetupBody(BaseModel):
    email: str
    name: str = ""
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


class CreateOrgBody(BaseModel):
    name: str
    slug: str
    color: Optional[str] = "#6366f1"


class UpdateOrgBody(BaseModel):
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    color: Optional[str] = None
    auto_erase_enabled: Optional[bool] = None
    auto_erase_days: Optional[int] = None


class InviteBody(BaseModel):
    email: str
    role: OrgRole
    project_slug: Optional[str] = None


class AcceptInviteBody(BaseModel):
    token: str
    name: str
    password: str


class AddOrgMemberBody(BaseModel):
    user_id: int
    role: OrgRole = OrgRole.member


class UpdateOrgMemberRoleBody(BaseModel):
    role: OrgRole


class AddEmailDomainBody(BaseModel):
    domain: str
    default_role: OrgRole = OrgRole.member


class CreateTokenRequest(BaseModel):
    name: str


class CLIRequestBody(BaseModel):
    code: str
    org_slug: Optional[str] = None


class CLIApproveBody(BaseModel):
    code: str
    org_slug: str


class AddProjectMemberBody(BaseModel):
    user_id: int


class GitLabConnectRequest(BaseModel):
    gitlab_url: str
    token: str


class EnableProjectRequest(BaseModel):
    path_with_namespace: str = ""
    name: str = ""
    web_url: str = ""
    default_branch: str = "main"


class CreateBranchPreviewRequest(BaseModel):
    branch: str
