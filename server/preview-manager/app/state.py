"""Preview state management using filesystem JSON files"""

import json
import logging
from pathlib import Path
from typing import Optional

from config.settings import settings
from app.models import PreviewState

logger = logging.getLogger(__name__)


class PreviewStateManager:
    """Manage preview state using JSON files"""

    @staticmethod
    def get_preview_path(project: str, mr_id: int) -> Path:
        """Get path to preview directory"""
        preview_name = f"mr-{mr_id}"
        # Path structure: /var/www/previews/{project}/mr-{id}/
        return Path(settings.previews_base_path) / project / preview_name

    @staticmethod
    def get_state_file(project: str, mr_id: int) -> Path:
        """Get path to state JSON file"""
        preview_path = PreviewStateManager.get_preview_path(project, mr_id)
        return preview_path / ".preview-state.json"

    @staticmethod
    def load_state(project: str, mr_id: int) -> Optional[PreviewState]:
        """Load preview state from JSON file"""
        state_file = PreviewStateManager.get_state_file(project, mr_id)

        if not state_file.exists():
            return None

        try:
            with open(state_file, 'r') as f:
                data = json.load(f)
            return PreviewState(**data)
        except Exception as e:
            logger.error(f"Error loading state from {state_file}: {e}")
            return None

    @staticmethod
    def save_state(project: str, mr_id: int, state: PreviewState):
        """Save preview state to JSON file"""
        state_file = PreviewStateManager.get_state_file(project, mr_id)

        # Ensure directory exists
        state_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(state_file, 'w') as f:
                json.dump(state.dict(), f, indent=2)
            logger.info(f"State saved to {state_file}")
        except Exception as e:
            logger.error(f"Error saving state to {state_file}: {e}")
            raise

    @staticmethod
    def delete_state(project: str, mr_id: int):
        """Delete preview state file"""
        state_file = PreviewStateManager.get_state_file(project, mr_id)
        if state_file.exists():
            state_file.unlink()
            logger.info(f"State deleted: {state_file}")
