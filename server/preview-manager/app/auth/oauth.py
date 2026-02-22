"""Multi-provider OAuth helpers"""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class OAuthUserInfo:
    email: str
    name: str
    avatar_url: Optional[str]
    provider: str
    provider_user_id: str
    provider_username: Optional[str]


class OAuthProvider:
    name: str

    def get_authorize_url(self, state: str) -> str:
        raise NotImplementedError

    async def exchange_code(self, code: str) -> str:
        raise NotImplementedError

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        raise NotImplementedError


class GitLabOAuth(OAuthProvider):
    name = "gitlab"

    def get_authorize_url(self, state: str) -> str:
        redirect_uri = f"{settings.oauth_redirect_uri_base}/gitlab"
        return (
            f"{settings.gitlab_url}/oauth/authorize"
            f"?client_id={settings.gitlab_oauth_client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&response_type=code"
            f"&scope=read_user"
            f"&state={state}"
        )

    async def exchange_code(self, code: str) -> str:
        redirect_uri = f"{settings.oauth_redirect_uri_base}/gitlab"
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
            return resp.json()["access_token"]

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.gitlab_url}/api/v4/user",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return OAuthUserInfo(
                email=data["email"],
                name=data.get("name", data.get("username", "")),
                avatar_url=data.get("avatar_url"),
                provider="gitlab",
                provider_user_id=str(data["id"]),
                provider_username=data.get("username"),
            )


class GoogleOAuth(OAuthProvider):
    name = "google"

    def get_authorize_url(self, state: str) -> str:
        redirect_uri = f"{settings.oauth_redirect_uri_base}/google"
        return (
            f"https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={settings.google_oauth_client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&response_type=code"
            f"&scope=openid+email+profile"
            f"&state={state}"
            f"&access_type=offline"
        )

    async def exchange_code(self, code: str) -> str:
        redirect_uri = f"{settings.oauth_redirect_uri_base}/google"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": settings.google_oauth_client_id,
                    "client_secret": settings.google_oauth_client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()["access_token"]

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return OAuthUserInfo(
                email=data["email"],
                name=data.get("name", ""),
                avatar_url=data.get("picture"),
                provider="google",
                provider_user_id=str(data["id"]),
                provider_username=data.get("email"),
            )


_providers: dict[str, OAuthProvider] = {
    "gitlab": GitLabOAuth(),
    "google": GoogleOAuth(),
}


def get_provider(name: str) -> OAuthProvider:
    provider = _providers.get(name)
    if not provider:
        raise ValueError(f"Unknown OAuth provider: {name}")
    return provider
