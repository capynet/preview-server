"""Auto-rotation of organization GitLab access tokens.

GitLab personal/project/group access tokens expire. Instead of letting a lapsed
token break every deploy (HTTP Basic: Access denied on git clone), we rotate the
token via the GitLab rotate API *before* it expires and persist the new token +
expiry.

Two triggers, sharing the same logic:
- A daily cron job (``task_rotate_gitlab_tokens``) rotates tokens within the
  configured threshold window — the normal path, runs well ahead of expiry.
- A lazy backstop in ``_get_org_gitlab_token`` rotates on read if the cron ever
  missed it (e.g. worker downtime), so a deploy never clones with a dead token.

Concurrency: rotation revokes the old token immediately, so two processes must
never rotate at once. A Valkey compute-lock serializes rotation per org; losers
re-read the freshly-rotated token from the DB.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from config.settings import settings
from app.database import get_organization_by_id, update_organization
from app.valkey import acquire_compute_lock

logger = logging.getLogger(__name__)


def _parse_expiry(value: str | None) -> datetime | None:
    """Parse a GitLab expires_at ("2026-09-15" date or ISO datetime) to UTC datetime."""
    if not value:
        return None
    try:
        if len(value) == 10:  # date-only "YYYY-MM-DD"
            d = datetime.strptime(value, "%Y-%m-%d").date()
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _needs_rotation(expires_at: str | None) -> bool:
    """True when the token expires within the rotation threshold (or already expired).

    Returns False when expiry is unknown — a non-expiring PAT has nothing to rotate.
    """
    dt = _parse_expiry(expires_at)
    if dt is None:
        return False
    threshold = datetime.now(timezone.utc) + timedelta(
        days=settings.gitlab_token_rotation_threshold_days
    )
    return dt <= threshold


async def fetch_token_info(gitlab_url: str, token: str) -> dict | None:
    """Return GitLab's personal_access_tokens/self info dict, or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{gitlab_url}/api/v4/personal_access_tokens/self",
                headers={"PRIVATE-TOKEN": token},
            )
        if resp.status_code == 200:
            return resp.json()
        logger.warning(
            "GitLab token info returned HTTP %d (%s)", resp.status_code, gitlab_url
        )
    except httpx.RequestError as e:
        logger.warning("Could not fetch GitLab token info (%s): %s", gitlab_url, e)
    return None


async def _rotate(gitlab_url: str, token: str) -> tuple[str, str | None] | None:
    """Call the GitLab rotate API. Returns (new_token, new_expires_at) or None on failure.

    Note: a successful rotation revokes ``token`` immediately on GitLab's side.
    """
    new_expiry = (
        datetime.now(timezone.utc).date()
        + timedelta(days=settings.gitlab_token_rotation_lifetime_days)
    ).isoformat()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{gitlab_url}/api/v4/personal_access_tokens/self/rotate",
                headers={"PRIVATE-TOKEN": token},
                params={"expires_at": new_expiry},
            )
    except httpx.RequestError as e:
        logger.error("GitLab token rotation request failed (%s): %s", gitlab_url, e)
        return None

    if resp.status_code in (200, 201):
        data = resp.json()
        new_token = data.get("token")
        if not new_token:
            logger.error("GitLab rotation succeeded but returned no token (%s)", gitlab_url)
            return None
        return new_token, data.get("expires_at")

    logger.error(
        "GitLab token rotation failed (%s): HTTP %d %s",
        gitlab_url, resp.status_code, resp.text[:200],
    )
    return None


async def maybe_rotate_org_token(org: dict) -> str | None:
    """Rotate the org's GitLab token if it's near expiry. Returns the token to use.

    Safe to call on every token read: it makes no network call unless the stored
    expiry is within the rotation threshold.
    """
    token = org.get("gitlab_access_token")
    gitlab_url = org.get("gitlab_url")
    if not settings.gitlab_token_rotation_enabled or not token or not gitlab_url:
        return token
    if not _needs_rotation(org.get("gitlab_token_expires_at")):
        return token

    org_id = org["id"]

    # Serialize rotation per org — rotating revokes the old token immediately.
    if not await acquire_compute_lock(f"gitlab_rotate:{org_id}", ttl=120):
        # Another process is rotating; use whatever token is current in the DB.
        fresh = await get_organization_by_id(org_id)
        return fresh.get("gitlab_access_token", token) if fresh else token

    # Re-check after acquiring the lock — another process may have just rotated.
    fresh = await get_organization_by_id(org_id) or org
    current = fresh.get("gitlab_access_token") or token
    if not _needs_rotation(fresh.get("gitlab_token_expires_at")):
        return current

    logger.info(
        "Rotating GitLab token for org %s (expires_at=%s)",
        org_id, fresh.get("gitlab_token_expires_at"),
    )
    result = await _rotate(gitlab_url, current)
    if not result:
        # Rotation failed — keep using current token (still valid until it expires).
        return current

    new_token, new_expiry = result
    await update_organization(
        org_id, gitlab_access_token=new_token, gitlab_token_expires_at=new_expiry
    )
    logger.info("Rotated GitLab token for org %s (new expires_at=%s)", org_id, new_expiry)
    return new_token
