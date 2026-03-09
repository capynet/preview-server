"""Preview state management using SQLite database."""

import logging
from pathlib import Path
from typing import Optional

from config.settings import settings
from app.database import get_preview, upsert_preview, delete_preview_from_db

logger = logging.getLogger(__name__)


class PreviewStateManager:
    """Manage preview state using SQLite database."""

    @staticmethod
    def get_preview_path(org_slug: str, project_slug: str, preview_name: str) -> Path:
        """Get path to preview directory."""
        return Path(settings.previews_base_path) / org_slug / project_slug / preview_name

    @staticmethod
    async def load_state(project_id: int, preview_name: str) -> Optional[dict]:
        """Load preview state from database."""
        return await get_preview(project_id, preview_name)

    @staticmethod
    async def save_state(project_id: int, preview_name: str, **fields) -> dict:
        """Save preview state to database (upsert)."""
        result = await upsert_preview(project_id, preview_name, **fields)
        logger.info(f"State saved for project_id={project_id}/{preview_name}")
        return result

    @staticmethod
    async def delete_state(project_id: int, preview_name: str):
        """Delete preview state from database."""
        await delete_preview_from_db(project_id, preview_name)
        logger.info(f"State deleted for project_id={project_id}/{preview_name}")
