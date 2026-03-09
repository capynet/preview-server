"""Config helpers — reads from organization/project tables instead of app_config.

This module provides compatibility functions that other parts of the codebase
call. They now read from the proper org/project tables.
"""

import logging
from typing import Optional

from app.database import get_db

logger = logging.getLogger(__name__)


async def load_config_to_settings():
    """No-op — settings are now loaded from organization tables per-request."""
    logger.info("Config loading skipped (multi-tenant: settings are per-org)")
