"""Background task: listen to Docker container events for real-time status updates."""

import asyncio
import json
import logging
import time

from app.database import get_all_previews

logger = logging.getLogger(__name__)

# Docker events that indicate a container state change
RELEVANT_ACTIONS = {"start", "stop", "die", "restart", "kill", "pause", "unpause"}

# Debounce: wait this many seconds after an event before broadcasting,
# to group related events (e.g., php + db stopping together)
DEBOUNCE_SECONDS = 2


async def docker_events_loop():
    """Listen to Docker container events and broadcast preview status changes."""
    await asyncio.sleep(5)
    logger.info("Docker events listener started")

    while True:
        try:
            await _listen_events()
        except asyncio.CancelledError:
            logger.info("Docker events listener cancelled")
            raise
        except Exception as e:
            logger.error(f"Docker events listener error: {e}", exc_info=True)
        await asyncio.sleep(3)  # Reconnect delay


async def _listen_events():
    """Run docker events and process the stream."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "events",
        "--filter", "type=container",
        "--format", "{{json .}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    logger.info("Docker events subprocess started (PID %s)", proc.pid)

    # Load known previews for matching container names
    preview_prefixes = await _build_preview_prefixes()
    last_prefix_refresh = time.monotonic()

    pending_broadcast = False
    debounce_task = None

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break  # Process ended

            try:
                event = json.loads(line.decode().strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            action = event.get("Action", "").split(":")[0]  # "exec_start: ..." → "exec_start"
            if action not in RELEVANT_ACTIONS:
                continue

            # Extract container name
            actor = event.get("Actor", {})
            container_name = actor.get("Attributes", {}).get("name", "")
            if not container_name:
                continue

            # Refresh preview prefixes periodically (every 60s)
            if time.monotonic() - last_prefix_refresh > 60:
                preview_prefixes = await _build_preview_prefixes()
                last_prefix_refresh = time.monotonic()

            # Check if this container belongs to a known preview
            matched = _match_preview(container_name, preview_prefixes)
            if not matched:
                continue

            project, preview_name = matched
            logger.info(
                f"Docker event: {action} on {container_name} "
                f"(preview: {project}/{preview_name})"
            )

            # Debounce: schedule a broadcast after DEBOUNCE_SECONDS
            if debounce_task and not debounce_task.done():
                debounce_task.cancel()
            debounce_task = asyncio.create_task(_debounced_broadcast())

    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

        if debounce_task and not debounce_task.done():
            debounce_task.cancel()

    rc = proc.returncode
    if rc is not None and rc != 0:
        stderr = await proc.stderr.read()
        logger.warning(f"Docker events process exited with code {rc}: {stderr.decode().strip()}")


async def _debounced_broadcast():
    """Wait for debounce period then broadcast current preview state."""
    await asyncio.sleep(DEBOUNCE_SECONDS)

    from app.websockets import preview_list_manager

    if not preview_list_manager.active_connections:
        return

    try:
        from app.routes.previews import get_preview_list_base
        from datetime import datetime

        result = await get_preview_list_base(include_docker_status=True)
        current_state = json.dumps(result["previews"], sort_keys=True, default=str)

        if current_state != preview_list_manager.last_state:
            preview_list_manager.last_state = current_state
            await preview_list_manager.broadcast({
                "type": "update",
                "previews": result["previews"],
                "total": result["total"],
                "checked_at": datetime.utcnow().isoformat(),
            })
            logger.info(
                f"Docker events: broadcasted status update to "
                f"{len(preview_list_manager.active_connections)} client(s)"
            )
    except Exception as e:
        logger.error(f"Docker events broadcast error: {e}", exc_info=True)


async def _build_preview_prefixes() -> list[tuple[str, str]]:
    """Build list of (preview_name, project) tuples from DB for matching."""
    try:
        previews = await get_all_previews()
        return [(p["preview_name"], p["project"]) for p in previews]
    except Exception:
        return []


def _match_preview(
    container_name: str, prefixes: list[tuple[str, str]]
) -> tuple[str, str] | None:
    """Match a container name to a (project, preview_name) tuple.

    Container format: {preview_name}-{project}-{service}
    e.g., mr-13-drupal-test-2-php → preview_name="mr-13", project="drupal-test-2"
    """
    for preview_name, project in prefixes:
        prefix = f"{preview_name}-{project}-"
        if container_name.startswith(prefix):
            return (project, preview_name)
    return None
