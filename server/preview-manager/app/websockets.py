"""WebSocket endpoints and helpers"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil
import ptyprocess
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


# ---------------------------------------------------------------------------
# Deployment log broadcasting (for real-time deploy logs on detail page)
# ---------------------------------------------------------------------------

class DeploymentLogBroadcaster:
    """Tracks deployment log subscribers and broadcasts log lines in real-time."""

    def __init__(self):
        # key: deployment_id, value: dict with logs buffer and subscribers
        self._deployments: dict[int, dict] = {}

    def register(self, deployment_id: int):
        """Register a new deployment for broadcasting."""
        self._deployments[deployment_id] = {
            "logs": [],
            "subscribers": [],
            "complete": False,
        }

    def get(self, deployment_id: int) -> Optional[dict]:
        return self._deployments.get(deployment_id)

    async def add_log(self, deployment_id: int, line: str):
        """Buffer a log line and broadcast to subscribers."""
        entry = self._deployments.get(deployment_id)
        if not entry:
            return
        entry["logs"].append(line)
        disconnected = []
        for ws in entry["subscribers"]:
            try:
                await ws.send_json({"type": "log", "line": line})
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            if ws in entry["subscribers"]:
                entry["subscribers"].remove(ws)

    async def complete(self, deployment_id: int, success: bool):
        """Mark deployment as complete and notify subscribers."""
        entry = self._deployments.get(deployment_id)
        if not entry:
            return
        entry["complete"] = True
        msg = {"type": "complete", "success": success}
        for ws in entry["subscribers"]:
            try:
                await ws.send_json(msg)
            except Exception:
                pass
        # Clean up after 30 seconds
        asyncio.get_event_loop().call_later(30, lambda: self._deployments.pop(deployment_id, None))

    async def subscribe(self, deployment_id: int, ws: WebSocket):
        """Subscribe to a deployment's logs. Replays buffered logs first."""
        entry = self._deployments.get(deployment_id)
        if not entry:
            await ws.send_json({"type": "error", "message": "Deployment not found or already completed"})
            return
        # Replay buffered logs
        for line in entry["logs"]:
            await ws.send_json({"type": "log", "line": line})
        if entry["complete"]:
            await ws.send_json({"type": "complete", "success": True})
        else:
            entry["subscribers"].append(ws)

    def unsubscribe(self, deployment_id: int, ws: WebSocket):
        entry = self._deployments.get(deployment_id)
        if entry and ws in entry["subscribers"]:
            entry["subscribers"].remove(ws)


deployment_log_broadcaster = DeploymentLogBroadcaster()


class PreviewListManager:
    """Manages WebSocket connections and broadcasts full preview list updates"""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self.last_state: str = ""
        self.check_interval = 30  # seconds (fallback; docker_events provides real-time updates)
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


async def system_resources_loop():
    """Background loop that broadcasts system resource metrics every 2 seconds."""
    logger.info("Starting system resources broadcast loop")
    while True:
        try:
            if not preview_list_manager.active_connections:
                await asyncio.sleep(2)
                continue

            mem = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=None)
            disk = psutil.disk_usage('/')

            # Count previews by docker status using network filter
            stats = {"total": 0, "running": 0, "paused": 0, "stopped": 0}
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "ps", "-a",
                    "--filter", "network=preview-network",
                    "--format", "{{.State}}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode == 0:
                    states = stdout.decode().strip().split('\n') if stdout.decode().strip() else []
                    stats["total"] = len(states)
                    for s in states:
                        s = s.strip().lower()
                        if s == "running":
                            stats["running"] += 1
                        elif s == "paused":
                            stats["paused"] += 1
                        elif s in ("exited", "created", "dead"):
                            stats["stopped"] += 1
            except Exception as e:
                logger.debug(f"Error getting docker stats: {e}")

            message = {
                "type": "system_resources",
                "resources": {
                    "memory_percent": mem.percent,
                    "memory_available_gb": round(mem.available / (1024**3), 2),
                    "memory_total_gb": round(mem.total / (1024**3), 2),
                    "cpu_percent": cpu,
                    "cpu_count": psutil.cpu_count(),
                    "disk_percent": disk.percent,
                    "disk_used_gb": round(disk.used / (1024**3), 2),
                    "disk_total_gb": round(disk.total / (1024**3), 2),
                },
                "stats": stats,
            }

            await preview_list_manager.broadcast(message)
            await asyncio.sleep(2)

        except asyncio.CancelledError:
            logger.info("System resources loop cancelled")
            break
        except Exception as e:
            logger.error(f"Error in system resources loop: {e}", exc_info=True)
            await asyncio.sleep(5)


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
        result = await get_preview_list_base(include_docker_status=False)
        t_phase1 = time.monotonic()
        await websocket.send_json({
            "type": msg_type,
            "previews": result["previews"],
            "total": result["total"],
            "checked_at": datetime.utcnow().isoformat()
        })
        logger.info(f"[TIMING] WS phase 1 ({msg_type}): {t_phase1 - t_ws:.3f}s")

        # Phase 2: with Docker status (slow)
        result = await get_preview_list_base(include_docker_status=True)
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
                    result = await get_preview_list_base(include_docker_status=True)
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


@router.websocket("/ws/deployments/{deployment_id}/logs")
async def websocket_deployment_logs(websocket: WebSocket, deployment_id: int):
    """WebSocket endpoint for real-time deployment log streaming."""
    await _authenticate_ws(websocket, Role.viewer)
    await websocket.accept()

    entry = deployment_log_broadcaster.get(deployment_id)
    if not entry:
        await websocket.send_json({"type": "error", "message": "No active deployment with this ID"})
        await websocket.close()
        return

    await deployment_log_broadcaster.subscribe(deployment_id, websocket)

    try:
        while not entry.get("complete", False):
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
    except Exception:
        pass
    finally:
        deployment_log_broadcaster.unsubscribe(deployment_id, websocket)
        try:
            await websocket.close()
        except Exception:
            pass


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


@router.websocket("/ws/previews/{project_name}/{preview_name}/terminal")
async def websocket_terminal(
    websocket: WebSocket,
    project_name: str,
    preview_name: str,
    container: str = "php",
):
    """
    Interactive terminal WebSocket endpoint.
    Spawns a PTY running 'docker exec -it <container> bash' and bridges I/O.

    Query params:
        container: service name suffix (default: "php")

    Client → Server messages:
        {"type": "input", "data": "..."}
        {"type": "resize", "cols": N, "rows": N}

    Server → Client messages:
        {"type": "output", "data": "..."}
        {"type": "exit", "code": N}
        {"type": "error", "message": "..."}
    """
    await _authenticate_ws(websocket, Role.manager)
    await websocket.accept()

    container_name = f"{preview_name}-{project_name}-{container}"

    # Verify container is running
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Running}}", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0 or stdout.decode().strip() != "true":
            await websocket.send_json({"type": "error", "message": f"Container '{container_name}' is not running"})
            await websocket.close()
            return
    except Exception as e:
        await websocket.send_json({"type": "error", "message": f"Failed to check container: {e}"})
        await websocket.close()
        return

    # Spawn PTY with docker exec
    pty = None
    try:
        pty = ptyprocess.PtyProcess.spawn(
            ["docker", "exec", "-it", container_name, "bash"],
            dimensions=(24, 80),
        )

        INACTIVITY_TIMEOUT = 15 * 60  # 15 minutes
        last_input_time = time.monotonic()

        async def read_pty():
            """Read from PTY and send to WebSocket."""
            loop = asyncio.get_event_loop()
            while pty.isalive():
                try:
                    data = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: pty.read(4096)),
                        timeout=1.0,
                    )
                    if data:
                        await websocket.send_json({"type": "output", "data": data})
                except asyncio.TimeoutError:
                    # Check inactivity timeout
                    if time.monotonic() - last_input_time > INACTIVITY_TIMEOUT:
                        await websocket.send_json({"type": "error", "message": "Session timed out due to inactivity"})
                        return
                    continue
                except EOFError:
                    break
                except Exception:
                    break

            # Process exited
            exit_code = pty.exitstatus if pty.exitstatus is not None else -1
            try:
                await websocket.send_json({"type": "exit", "code": exit_code})
            except Exception:
                pass

        async def read_ws():
            """Read from WebSocket and write to PTY."""
            nonlocal last_input_time
            while True:
                try:
                    raw = await websocket.receive_text()
                    msg = json.loads(raw)
                    if msg.get("type") == "input":
                        last_input_time = time.monotonic()
                        pty.write(msg["data"])
                    elif msg.get("type") == "resize":
                        cols = msg.get("cols", 80)
                        rows = msg.get("rows", 24)
                        pty.setwinsize(rows, cols)
                except Exception:
                    break

        # Run both directions concurrently
        done, pending = await asyncio.wait(
            [asyncio.create_task(read_pty()), asyncio.create_task(read_ws())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    except Exception as e:
        logger.error(f"Terminal WebSocket error: {e}", exc_info=True)
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if pty and pty.isalive():
            pty.terminate(force=True)
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/ws/previews/{project_name}/{preview_name}/action")
async def websocket_preview_action(
    websocket: WebSocket,
    project_name: str,
    preview_name: str,
    action: str
):
    """
    WebSocket endpoint to execute preview actions with log streaming.
    If an action is already running for this preview, the client joins as a follower.

    Query params:
        action: "stop" | "start" | "restart" | "drush-uli"
    """
    await _authenticate_ws(websocket, Role.viewer)
    await websocket.accept()

    try:
        # Use composite key to avoid collisions between projects
        action_key = f"{project_name}/{preview_name}"

        # Check if there's already a running action for this preview
        existing = action_manager.get(action_key)
        if existing and not existing.complete:
            logger.info(f"Client joining existing {existing.action} action for {action_key}")
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

        # Find preview path directly using project_name
        preview_path = Path(settings.previews_base_path) / project_name / preview_name

        if not preview_path.is_dir():
            await websocket.send_json({
                "type": "error",
                "message": f"Preview '{preview_name}' not found"
            })
            await websocket.close()
            return

        # Build command based on action
        php_container = f"{preview_name}-{project_name}-php"
        if action == "stop":
            command = ["docker", "compose", "stop"]
            timeout = 60
        elif action == "start":
            command = ["docker", "compose", "up", "-d"]
            timeout = 120
        elif action == "restart":
            command = ["docker", "compose", "restart"]
            timeout = 120
        elif action == "drush-uli":
            preview_url = f"https://{preview_name}-{project_name}.mr.preview-mr.com"
            command = ["docker", "exec", php_container, "vendor/bin/drush", "uli", f"--uri={preview_url}"]
            timeout = 30
        else:
            await websocket.send_json({
                "type": "error",
                "message": f"Unknown action: {action}"
            })
            await websocket.close()
            return

        # Register running action and add this client as first subscriber
        running_action = action_manager.start(action_key, action, " ".join(command))
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
        action_manager.finish(action_key)

        try:
            await websocket.close()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        action_manager.finish(action_key)
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
            await websocket.close()
        except:
            pass
