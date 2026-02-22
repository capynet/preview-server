"""Background task: auto-stop previews after inactivity."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from app import config_store
from app.database import get_all_previews
from app.routes.previews import get_docker_status

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300  # 5 minutes


async def auto_stop_loop():
    """Run every CHECK_INTERVAL_SECONDS, stopping idle previews."""
    # Wait a bit on startup to let the app fully initialize
    await asyncio.sleep(30)
    logger.info("Auto-stop background task started")

    while True:
        try:
            await _check_and_stop()
        except Exception as e:
            logger.error(f"Auto-stop loop error: {e}", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _check_and_stop():
    """Check all previews and stop those that exceed their inactivity threshold."""
    # Load global config
    global_enabled = await config_store.get_config("auto_stop_enabled")
    if global_enabled != "true":
        return

    global_minutes_str = await config_store.get_config("auto_stop_minutes")
    global_minutes = int(global_minutes_str) if global_minutes_str else 60

    previews = await get_all_previews()
    if not previews:
        return

    now = datetime.now(timezone.utc)
    stopped_count = 0

    for p in previews:
        project = p["project"]
        preview_name = p["preview_name"]
        preview_path = Path(p["path"]) if p.get("path") else None

        if not preview_path or not preview_path.exists():
            continue

        # Check per-project override
        proj_enabled = await config_store.get_config(f"auto_stop_{project}_enabled")
        if proj_enabled is not None:
            if proj_enabled != "true":
                continue
            proj_minutes_str = await config_store.get_config(f"auto_stop_{project}_minutes")
            threshold_minutes = int(proj_minutes_str) if proj_minutes_str else global_minutes
        else:
            threshold_minutes = global_minutes

        # Determine last activity
        last_accessed = p.get("last_accessed_at")
        last_deployed = p.get("last_deployed_at")

        # Use the most recent of last_accessed_at and last_deployed_at
        last_activity = None
        for ts in (last_accessed, last_deployed):
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if last_activity is None or dt > last_activity:
                        last_activity = dt
                except ValueError:
                    pass

        if not last_activity:
            continue

        # Check if inactive
        idle_seconds = (now - last_activity).total_seconds()
        if idle_seconds < threshold_minutes * 60:
            continue

        # Check if container is actually running
        compose_file = preview_path / "docker-compose.yml"
        if not compose_file.exists():
            continue

        status = await get_docker_status(preview_path)
        if status != "running":
            continue

        # Stop the containers
        logger.info(
            f"Auto-stopping {project}/{preview_name}: "
            f"idle for {int(idle_seconds / 60)} min (threshold: {threshold_minutes} min)"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "stop",
                cwd=str(preview_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)
            stopped_count += 1
            logger.info(f"Auto-stopped {project}/{preview_name}")
        except Exception as e:
            logger.error(f"Failed to auto-stop {project}/{preview_name}: {e}")

    if stopped_count:
        logger.info(f"Auto-stop: stopped {stopped_count} preview(s)")
