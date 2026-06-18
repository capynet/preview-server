"""Daily rotation of organization GitLab access tokens nearing expiry.

Iterates every org with a connected GitLab token and rotates the ones whose
expiry falls within the configured threshold window. The actual rotation +
locking lives in ``app.gitlab_token.maybe_rotate_org_token``.
"""

import logging

from app.database import list_organizations
from app.gitlab_token import maybe_rotate_org_token
from config.settings import settings

logger = logging.getLogger(__name__)


async def rotate_gitlab_tokens():
    if not settings.gitlab_token_rotation_enabled:
        logger.info("GitLab token rotation disabled (gitlab_token_rotation_enabled=false)")
        return

    orgs = await list_organizations()
    rotated = 0
    checked = 0
    for org in orgs:
        if not org.get("gitlab_access_token"):
            continue
        checked += 1
        before = org.get("gitlab_access_token")
        try:
            after = await maybe_rotate_org_token(org)
            if after and after != before:
                rotated += 1
        except Exception as e:
            logger.error("GitLab token rotation failed for org %s: %s", org.get("id"), e)

    logger.info(
        "GitLab token rotation cron: checked %d connected org(s), rotated %d",
        checked, rotated,
    )
