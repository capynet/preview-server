"""WebSocket endpoints and helpers"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketException, status

from config.settings import settings
from app.auth import database as auth_db
from app.auth.models import Role, has_min_role

logger = logging.getLogger(__name__)

router = APIRouter()


async def _authenticate_ws(websocket: WebSocket, min_role: Role = Role.viewer) -> int:
    """Authenticate a WebSocket connection via token query param or cookie. Returns user_id."""
    token = websocket.query_params.get("token")
    user_id = None

    if token:
        tok = await auth_db.validate_api_token(token)
        if tok:
            user_id = tok["user_id"]

    if user_id is None:
        session_id = websocket.cookies.get("pm_session")
        if session_id:
            session = await auth_db.get_session(session_id)
            if session:
                user_id = session["user_id"]

    if user_id is None:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

    role_str = await auth_db.get_role(user_id)
    role = Role(role_str) if role_str else None
    if not has_min_role(role, min_role):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

    return user_id


@dataclass
class RunningAction:
    """Tracks a running action for a preview, allowing late-joining clients."""
    action: str
    command: str
    logs: list[dict] = field(default_factory=list)
    subscribers: list[WebSocket] = field(default_factory=list)
    complete: bool = False
    complete_message: Optional[dict] = None

    async def add_subscriber(self, ws: WebSocket):
        """Send buffered logs and subscribe to future ones."""
        # Send start message
        await ws.send_json({"type": "start", "action": self.action, "command": self.command})
        # Replay buffered logs
        for log_entry in self.logs:
            await ws.send_json(log_entry)
        if self.complete and self.complete_message:
            await ws.send_json(self.complete_message)
        else:
            self.subscribers.append(ws)

    async def broadcast(self, message: dict):
        """Send a message to all subscribers."""
        disconnected = []
        for ws in self.subscribers:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            if ws in self.subscribers:
                self.subscribers.remove(ws)

    def remove_subscriber(self, ws: WebSocket):
        if ws in self.subscribers:
            self.subscribers.remove(ws)


class ActionManager:
    """Tracks running actions per preview so late-joining clients can follow."""

    def __init__(self):
        self.running: dict[str, RunningAction] = {}

    def get(self, preview_name: str) -> Optional[RunningAction]:
        action = self.running.get(preview_name)
        if action and action.complete:
            # Keep completed actions for a short time so refreshing clients can see the result
            return action
        return action

    def start(self, preview_name: str, action: str, command: str) -> RunningAction:
        ra = RunningAction(action=action, command=command)
        self.running[preview_name] = ra
        return ra

    def finish(self, preview_name: str):
        """Mark action as done. Remove after a delay to allow late joiners."""
        action = self.running.get(preview_name)
        if action:
            action.complete = True
            # Clean up after 30 seconds
            asyncio.get_event_loop().call_later(30, lambda: self.running.pop(preview_name, None))


action_manager = ActionManager()


class PreviewListManager:
    """Manages WebSocket connections and broadcasts full preview list updates"""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self.last_state: str = ""
        self.check_interval = 10  # seconds
        self.background_task = None

    async def connect(self, websocket: WebSocket):
        """Accept new WebSocket connection"""
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"Preview list WS connection. Total: {len(self.active_connections)}")

        if self.background_task is None and len(self.active_connections) > 0:
            self.background_task = asyncio.create_task(self.check_and_broadcast_loop())

    def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection"""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"Preview list WS disconnected. Total: {len(self.active_connections)}")

        if len(self.active_connections) == 0 and self.background_task:
            self.background_task.cancel()
            self.background_task = None

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients"""
        disconnected = []

        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.warning(f"Error broadcasting preview list to client: {e}")
                disconnected.append(connection)

        for connection in disconnected:
            self.disconnect(connection)

    async def check_and_broadcast_loop(self):
        """Background task that checks for preview list changes and broadcasts updates (two-phase)"""
        from app.routes.previews import get_preview_list_base

        logger.info(f"Starting preview list background checker (interval: {self.check_interval}s)")

        while len(self.active_connections) > 0:
            try:
                result = await get_preview_list_base()
                current_state = json.dumps(result["previews"], sort_keys=True, default=str)

                if current_state != self.last_state:
                    logger.info(f"Preview list changed - broadcasting to {len(self.active_connections)} client(s)")
                    await self.broadcast({
                        "type": "update",
                        "previews": result["previews"],
                        "total": result["total"],
                        "checked_at": datetime.utcnow().isoformat()
                    })
                    self.last_state = current_state

                await asyncio.sleep(self.check_interval)

            except asyncio.CancelledError:
                logger.info("Preview list background checker cancelled")
                break
            except Exception as e:
                logger.error(f"Error in preview list background checker: {e}", exc_info=True)
                await asyncio.sleep(self.check_interval)

        logger.info("Preview list background checker stopped")


# Global manager instance
preview_list_manager = PreviewListManager()


async def stream_subprocess_output(
    command: list[str],
    cwd: str,
    websocket: WebSocket,
    timeout: int = 120
) -> tuple[bool, str]:
    """
    Execute a subprocess command and stream stdout via WebSocket.

    Returns:
        tuple[bool, str]: (success, final_message)
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )

        stdout_lines = []
        stderr_lines = []

        async def read_stream(stream, lines_list, stream_type):
            async for line in stream:
                decoded_line = line.decode('utf-8')
                lines_list.append(decoded_line)

                await websocket.send_json({
                    "type": "log",
                    "stream": stream_type,
                    "line": decoded_line
                })

        await asyncio.gather(
            read_stream(process.stdout, stdout_lines, "stdout"),
            read_stream(process.stderr, stderr_lines, "stderr")
        )

        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await websocket.send_json({
                "type": "error",
                "message": f"Command timeout after {timeout}s"
            })
            return False, f"Timeout after {timeout} seconds"

        success = process.returncode == 0

        if success:
            message = "Command completed successfully"
        else:
            stderr_output = ''.join(stderr_lines)
            message = f"Command failed: {stderr_output}"

        return success, message

    except Exception as e:
        logger.error(f"Error in stream_subprocess_output: {e}", exc_info=True)
        await websocket.send_json({
            "type": "error",
            "message": str(e)
        })
        return False, str(e)


@router.websocket("/ws/previews")
async def websocket_previews(websocket: WebSocket):
    """
    WebSocket endpoint for real-time full preview list updates.
    Real-time preview list updates via WebSocket.
    """
    from app.routes.previews import get_preview_list_base

    await _authenticate_ws(websocket, Role.viewer)
    await preview_list_manager.connect(websocket)

    async def send_two_phase(msg_type: str):
        t_ws = time.monotonic()

        # Phase 1: filesystem only (fast)
        result = await get_preview_list_base(include_ddev_status=False)
        t_phase1 = time.monotonic()
        await websocket.send_json({
            "type": msg_type,
            "previews": result["previews"],
            "total": result["total"],
            "checked_at": datetime.utcnow().isoformat()
        })
        logger.info(f"[TIMING] WS phase 1 ({msg_type}): {t_phase1 - t_ws:.3f}s")

        # Phase 2: with DDEV status (slow)
        result = await get_preview_list_base(include_ddev_status=True)
        t_phase2 = time.monotonic()
        preview_list_manager.last_state = json.dumps(result["previews"], sort_keys=True, default=str)
        await websocket.send_json({
            "type": "update",
            "previews": result["previews"],
            "total": result["total"],
            "checked_at": datetime.utcnow().isoformat()
        })
        logger.info(f"[TIMING] WS phase 2 (update): {t_phase2 - t_phase1:.3f}s, total={time.monotonic() - t_ws:.3f}s")

    try:
        await send_two_phase("initial")

        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                if msg == "refresh":
                    result = await get_preview_list_base(include_ddev_status=True)
                    preview_list_manager.last_state = json.dumps(result["previews"], sort_keys=True, default=str)
                    await websocket.send_json({
                        "type": "update",
                        "previews": result["previews"],
                        "total": result["total"],
                        "checked_at": datetime.utcnow().isoformat()
                    })
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping"})
                except:
                    break
            except:
                break

    except Exception as e:
        logger.info(f"Preview list WebSocket connection closed: {e}")
    finally:
        preview_list_manager.disconnect(websocket)


async def _stream_subprocess_with_action(
    command: list[str],
    cwd: str,
    running_action: RunningAction,
    timeout: int = 120
) -> tuple[bool, str]:
    """Execute a subprocess, buffer logs in RunningAction and broadcast to subscribers."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )

        async def read_stream(stream, stream_type):
            async for line in stream:
                decoded_line = line.decode('utf-8')
                msg = {"type": "log", "stream": stream_type, "line": decoded_line}
                running_action.logs.append(msg)
                await running_action.broadcast(msg)

        await asyncio.gather(
            read_stream(process.stdout, "stdout"),
            read_stream(process.stderr, "stderr")
        )

        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            error_msg = {"type": "error", "message": f"Command timeout after {timeout}s"}
            running_action.logs.append(error_msg)
            await running_action.broadcast(error_msg)
            return False, f"Timeout after {timeout} seconds"

        success = process.returncode == 0
        if success:
            message = "Command completed successfully"
        else:
            stderr_output = ''.join(
                entry["line"] for entry in running_action.logs if entry.get("stream") == "stderr"
            )
            message = f"Command failed: {stderr_output}"

        return success, message

    except Exception as e:
        logger.error(f"Error in _stream_subprocess_with_action: {e}", exc_info=True)
        error_msg = {"type": "error", "message": str(e)}
        running_action.logs.append(error_msg)
        await running_action.broadcast(error_msg)
        return False, str(e)


@router.websocket("/ws/previews/{preview_name}/action")
async def websocket_preview_action(
    websocket: WebSocket,
    preview_name: str,
    action: str
):
    """
    WebSocket endpoint to execute preview actions with log streaming.
    If an action is already running for this preview, the client joins as a follower.

    Query params:
        action: "stop" | "start" | "restart" | "drush-uli"
    """
    await _authenticate_ws(websocket, Role.member)
    await websocket.accept()

    try:
        # Check if there's already a running action for this preview
        existing = action_manager.get(preview_name)
        if existing and not existing.complete:
            logger.info(f"Client joining existing {existing.action} action for {preview_name}")
            await existing.add_subscriber(websocket)
            # Keep connection alive until action completes or client disconnects
            try:
                while not existing.complete:
                    try:
                        await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
            except Exception:
                pass
            finally:
                existing.remove_subscriber(websocket)
            try:
                await websocket.close()
            except Exception:
                pass
            return

        # Find preview path and project
        preview_path = None
        project_name = None
        for project_dir in Path(settings.previews_base_path).iterdir():
            if not project_dir.is_dir():
                continue
            for preview_dir in project_dir.iterdir():
                if preview_dir.name == preview_name:
                    preview_path = preview_dir
                    project_name = project_dir.name
                    break
            if preview_path:
                break

        if not preview_path:
            await websocket.send_json({
                "type": "error",
                "message": f"Preview '{preview_name}' not found"
            })
            await websocket.close()
            return

        # Build command based on action
        if action == "stop":
            command = ["ddev", "stop"]
            timeout = 60
        elif action == "start":
            command = ["ddev", "start"]
            timeout = 120
        elif action == "restart":
            command = ["ddev", "restart"]
            timeout = 120
        elif action == "drush-uli":
            preview_url = f"https://{preview_name}-{project_name}.mr.preview-mr.com"
            command = ["ddev", "drush", "uli", f"--uri={preview_url}"]
            timeout = 30
        else:
            await websocket.send_json({
                "type": "error",
                "message": f"Unknown action: {action}"
            })
            await websocket.close()
            return

        # Register running action and add this client as first subscriber
        running_action = action_manager.start(preview_name, action, " ".join(command))
        await running_action.add_subscriber(websocket)

        success, message = await _stream_subprocess_with_action(
            command=command,
            cwd=str(preview_path),
            running_action=running_action,
            timeout=timeout
        )

        complete_msg = {
            "type": "complete",
            "success": success,
            "message": message,
            "action": action
        }
        running_action.complete_message = complete_msg
        await running_action.broadcast(complete_msg)
        action_manager.finish(preview_name)

        try:
            await websocket.close()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        action_manager.finish(preview_name)
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
            await websocket.close()
        except:
            pass
