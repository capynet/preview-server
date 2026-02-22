"""Auth Pydantic models"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Role(str, Enum):
    admin = "admin"
    manager = "manager"
    member = "member"
    viewer = "viewer"


# Hierarchy: admin > manager > member > viewer
ROLE_HIERARCHY = {
    Role.admin: 4,
    Role.manager: 3,
    Role.member: 2,
    Role.viewer: 1,
}


def has_min_role(user_role: Optional[Role], min_role: Role) -> bool:
    if user_role is None:
        return False
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(min_role, 0)


class User(BaseModel):
    id: int
    email: str
    name: str
    avatar_url: Optional[str] = None
    created_at: str
    updated_at: str


class UserWithRole(User):
    role: Optional[Role] = None


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
    name: str
    token_prefix: str
    created_at: str
    last_used_at: Optional[str] = None


class CLIAuthRequest(BaseModel):
    code: str
    status: str  # pending, approved, expired
    user_id: Optional[int] = None
    token: Optional[str] = None
    created_at: str


class CreateTokenRequest(BaseModel):
    name: str


class CLIRequestBody(BaseModel):
    code: str


class CLIApproveBody(BaseModel):
    code: str


class UpdateRoleBody(BaseModel):
    role: Role


class SetupBody(BaseModel):
    email: str
    name: str = ""
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


class InviteBody(BaseModel):
    email: str
    role: Role


class AcceptInviteBody(BaseModel):
    token: str
    name: str
    password: str


class AddProjectMemberBody(BaseModel):
    user_id: int
