"""WebSocket endpoints and helpers"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil
from fastapi import APIRouter, WebSocket, WebSocketException, status

from config.settings import settings
from app.auth import database as auth_db

logger = logging.getLogger(__name__)

router = APIRouter()


async def _authenticate_ws(websocket: WebSocket) -> int:
    """Authenticate a WebSocket connection. Returns user_id."""
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
    """Valkey-backed deployment log broadcaster.

    Works across processes: the deployer (in arq worker or in-process) publishes
    logs to Valkey, and WebSocket clients subscribe via Valkey pub/sub.
    """

    async def register(self, deployment_id: int):
        """Register a new deployment for log tracking in Valkey."""
        try:
            from app.valkey import get_valkey
            v = get_valkey()
            await v.sadd("active_deployments", str(deployment_id))
            await v.expire("active_deployments", 7200)
        except Exception:
            pass

    def get(self, deployment_id: int) -> Optional[dict]:
        """Compatibility: returns truthy dict. Actual check uses Valkey."""
        return {"exists": True}

    async def add_log(self, deployment_id: int, line: str):
        """Buffer log line in Valkey and publish for real-time streaming."""
        try:
            from app.valkey import buffer_deploy_log, publish_event
            await buffer_deploy_log(deployment_id, line)
            await publish_event(f"deploy_logs:{deployment_id}", {"type": "log", "line": line})
        except Exception:
            pass  # Don't break deployment if Valkey is down

    async def broadcast_deploy_status(self, deployment_id: int, status: str, duration: int):
        """Notify subscribers that the deploy phase changed (e.g. deploy done, post-deploy starting)."""
        try:
            from app.valkey import publish_event
            logger.info(f"Broadcasting deploy_status: deployment_id={deployment_id} status={status} duration={duration}")
            await publish_event(f"deploy_logs:{deployment_id}", {
                "type": "deploy_status",
                "status": status,
                "duration": duration,
            })
        except Exception as e:
            logger.warning(f"Failed to broadcast deploy_status: {e}")

    async def broadcast_phase_event(self, deployment_id: int, event_type: str, data: dict):
        """Broadcast a phase event (phases_init or phase_update) to subscribers.

        Also stores the latest phases snapshot in Valkey so late-joining clients
        can receive the current state on replay.
        """
        try:
            from app.valkey import publish_event, get_valkey
            msg = {"type": event_type, **data}
            await publish_event(f"deploy_logs:{deployment_id}", msg)

            # Store latest phases snapshot for replay on late-join
            if event_type == "phases_init":
                v = get_valkey()
                await v.set(
                    f"deploy_phases:{deployment_id}",
                    json.dumps(data["phases"]),
                    ex=7200,
                )
            elif event_type == "phase_update":
                # Update the stored snapshot
                v = get_valkey()
                raw = await v.get(f"deploy_phases:{deployment_id}")
                if raw:
                    phases = json.loads(raw)
                    phase = data["phase"]
                    for i, p in enumerate(phases):
                        if p["name"] == phase["name"]:
                            phases[i] = {**phases[i], **phase}
                            break
                    await v.set(
                        f"deploy_phases:{deployment_id}",
                        json.dumps(phases),
                        ex=7200,
                    )
        except Exception as e:
            logger.warning(f"Failed to broadcast {event_type}: {e}")

    async def complete(self, deployment_id: int, success: bool):
        """Mark deployment as complete in Valkey and notify subscribers."""
        try:
            from app.valkey import mark_deploy_complete, publish_event, get_valkey
            await mark_deploy_complete(deployment_id, success)
            await publish_event(f"deploy_logs:{deployment_id}", {"type": "complete", "success": success})
            v = get_valkey()
            await v.srem("active_deployments", str(deployment_id))
        except Exception:
            pass

    def unsubscribe(self, deployment_id: int, ws: WebSocket):
        """No-op — cleanup handled in WS endpoint via Valkey pubsub."""
        pass


deployment_log_broadcaster = DeploymentLogBroadcaster()


class PreviewListManager:
    """Manages WebSocket connections and broadcasts per-user-filtered preview list updates."""

    def __init__(self):
        # Each entry pairs the socket with the authenticated user so we can
        # filter the broadcast payload by the user's actual access.
        self.active_connections: list[tuple[WebSocket, int]] = []
        self.last_state: str = ""
        self.check_interval = 30  # seconds (fallback; docker_events provides real-time updates)
        self.background_task = None

    async def connect(self, websocket: WebSocket, user_id: int):
        """Accept new WebSocket connection and remember which user it belongs to."""
        await websocket.accept()
        self.active_connections.append((websocket, user_id))
        logger.info(f"Preview list WS connection (user_id={user_id}). Total: {len(self.active_connections)}")

        if self.background_task is None and len(self.active_connections) > 0:
            self.background_task = asyncio.create_task(self.check_and_broadcast_loop())

    def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection."""
        self.active_connections = [(ws, uid) for (ws, uid) in self.active_connections if ws is not websocket]
        logger.info(f"Preview list WS disconnected. Total: {len(self.active_connections)}")

        if len(self.active_connections) == 0 and self.background_task:
            self.background_task.cancel()
            self.background_task = None

    async def _push_filtered(self, msg_type: str, include_docker_status: bool = True) -> None:
        """Send each connected client their own access-filtered preview list."""
        if not self.active_connections:
            return

        from app.routes.previews import get_previews_for_user
        from app.auth import database as auth_db

        now = datetime.utcnow().isoformat()
        disconnected: list[WebSocket] = []

        for ws, user_id in list(self.active_connections):
            try:
                user = await auth_db.get_user_by_id(user_id)
                if not user:
                    disconnected.append(ws)
                    continue
                result = await get_previews_for_user(
                    user_id,
                    bool(user.get("is_superadmin")),
                    include_docker_status=include_docker_status,
                )
                await ws.send_json({
                    "type": msg_type,
                    "previews": result["previews"],
                    "total": result["total"],
                    "checked_at": now,
                })
            except Exception as e:
                logger.warning(f"Error sending filtered preview list to client (user_id={user_id}): {e}")
                disconnected.append(ws)

        for ws in disconnected:
            self.disconnect(ws)

    async def force_broadcast(self):
        """Immediately push an updated preview list to every connected client.

        Also publishes a Valkey event so other workers can pick it up.
        """
        try:
            from app.valkey import publish_event
            await publish_event("previews:global", {"action": "refresh"})
        except Exception:
            pass  # Valkey may not be available yet during startup

        try:
            await self._push_filtered("update", include_docker_status=False)
        except Exception as e:
            logger.error(f"Force broadcast error: {e}", exc_info=True)

    async def check_and_broadcast_loop(self):
        """Listen for Valkey pub/sub events and push per-user filtered updates."""
        from app.routes.previews import get_preview_list_base

        logger.info("Starting preview list event listener")

        pubsub = None
        try:
            from app.valkey import subscribe
            pubsub = await subscribe("previews:global")
            logger.info("Listening to Valkey pub/sub for preview updates")
        except Exception as e:
            logger.warning(f"Valkey pub/sub not available, falling back to polling: {e}")

        while len(self.active_connections) > 0:
            try:
                got_event = False
                if pubsub:
                    msg = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=self.check_interval),
                        timeout=self.check_interval + 1,
                    )
                    if msg and msg.get("type") == "message":
                        got_event = True
                else:
                    await asyncio.sleep(self.check_interval)

                # The state-change check uses the global list, since per-user
                # filtering is applied at send time. If the global state hasn't
                # changed, no client needs an update.
                global_result = await get_preview_list_base()
                current_state = json.dumps(global_result["previews"], sort_keys=True, default=str)

                if current_state != self.last_state:
                    logger.info(f"Preview list changed - broadcasting to {len(self.active_connections)} client(s)")
                    await self._push_filtered("update")
                    self.last_state = current_state

            except asyncio.CancelledError:
                logger.info("Preview list event listener cancelled")
                break
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error in preview list event listener: {e}", exc_info=True)
                await asyncio.sleep(self.check_interval)

        if pubsub:
            await pubsub.unsubscribe("previews:global")
            await pubsub.close()
        logger.info("Preview list event listener stopped")


# Global manager instance
preview_list_manager = PreviewListManager()


class SystemResourcesManager:
    """Manages WebSocket connections for system resource metrics."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"System resources WS connection. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"System resources WS disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        disconnected = []
        for conn in self.active_connections:
            try:
                await conn.send_json(message)
            except Exception:
                disconnected.append(conn)
        for conn in disconnected:
            self.disconnect(conn)


system_resources_manager = SystemResourcesManager()

# Circular buffer: last 30 snapshots (2s interval = 60s of history)
_RESOURCE_HISTORY_MAX = 30
_resource_history: list[dict] = []


async def system_resources_loop():
    """Background loop that collects system resource metrics every 2 seconds.
    Always collects (to fill the history buffer), broadcasts only when clients are connected."""
    logger.info("Starting system resources broadcast loop")
    while True:
        try:
            mem = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=None)
            disk = psutil.disk_usage("/")

            # Count previews by docker status using network filter
            stats = {"total": 0, "running": 0, "paused": 0, "stopped": 0}
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "ps", "-a",
                    "--filter", "network=druploy-network",
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

            load1, load5, load15 = os.getloadavg()
            snapshot = {
                "memory_percent": mem.percent,
                "memory_available_gb": round(mem.available / (1024**3), 2),
                "memory_total_gb": round(mem.total / (1024**3), 2),
                "cpu_percent": cpu,
                "cpu_count": psutil.cpu_count(),
                "load_avg_1": round(load1, 2),
                "load_avg_5": round(load5, 2),
                "load_avg_15": round(load15, 2),
                "disk_percent": disk.percent,
                "disk_used_gb": round(disk.used / (1024**3), 2),
                "disk_total_gb": round(disk.total / (1024**3), 2),
            }

            # Always append to history buffer
            _resource_history.append(snapshot)
            if len(_resource_history) > _RESOURCE_HISTORY_MAX:
                _resource_history.pop(0)

            if system_resources_manager.active_connections:
                message = {
                    "type": "system_resources",
                    "resources": snapshot,
                    "stats": stats,
                    "disk_usage": _disk_usage_cache,
                }
                await system_resources_manager.broadcast(message)

            await asyncio.sleep(2)

        except asyncio.CancelledError:
            logger.info("System resources loop cancelled")
            break
        except Exception as e:
            logger.error(f"Error in system resources loop: {e}", exc_info=True)
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Disk usage monitoring (updated every 10 minutes)
# ---------------------------------------------------------------------------

_DISK_USAGE_DIRS = [
    ("/var/www/druploy", "Druploy code"),
    ("/var/lib/docker", "Docker (coordinator)"),
    ("/var/log", "System logs"),
    ("/var/lib/containerd", "Container images (runtime)"),
    ("/var/lib/registry", "Docker registry"),
]

_disk_usage_cache: dict = {"directories": [], "disk_total_bytes": 0, "updated_at": None}


def get_disk_usage_cache() -> dict:
    return _disk_usage_cache


async def disk_usage_loop():
    """Background loop that calculates disk usage of key directories.

    Runs in every uvicorn worker, but a Valkey single-writer lock ensures only
    ONE worker shells out to `sudo du` per interval. Otherwise each worker would
    run the whole batch independently, multiplying the disk I/O and the sudo
    journal entries by the worker count. The winner publishes the snapshot to
    Valkey; every worker mirrors it locally so the API and the 2s WS broadcast
    keep serving it without each hitting Valkey.
    """
    from app.valkey import acquire_compute_lock, set_disk_usage, get_disk_usage

    logger.info("Starting disk usage monitoring loop")
    INTERVAL = 600  # 10 minutes — disk usage changes slowly
    while True:
        try:
            # TTL just under the interval so exactly one worker recomputes per
            # cycle (the lock has expired by the time everyone wakes again).
            if await acquire_compute_lock("disk_usage", ttl=INTERVAL - 30):
                dirs = []
                for path, label in _DISK_USAGE_DIRS:
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "sudo", "du", "-sb", path,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                        size_bytes = int(stdout.decode().split()[0]) if proc.returncode == 0 else 0
                    except Exception:
                        size_bytes = 0
                    dirs.append({"path": path, "label": label, "size_bytes": size_bytes})

                dirs.sort(key=lambda d: d["size_bytes"], reverse=True)
                disk = psutil.disk_usage("/")
                snapshot = {
                    "directories": dirs,
                    "disk_total_bytes": disk.total,
                    "updated_at": datetime.utcnow().isoformat(),
                }
                await set_disk_usage(snapshot)
                logger.debug(f"Disk usage updated: {len(dirs)} directories")

            # Mirror the shared snapshot locally (winner and non-winners alike).
            shared = await get_disk_usage()
            if shared:
                _disk_usage_cache.update(shared)

            await asyncio.sleep(INTERVAL)

        except asyncio.CancelledError:
            logger.info("Disk usage loop cancelled")
            break
        except Exception as e:
            logger.error(f"Error in disk usage loop: {e}", exc_info=True)
            await asyncio.sleep(INTERVAL)


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
    WebSocket endpoint for real-time per-user preview list updates.

    The user is identified at authentication time and the broadcast payload is
    filtered to the previews they are allowed to see.
    """
    from app.routes.previews import get_preview_list_base, get_previews_for_user

    user_id = await _authenticate_ws(websocket)
    user_row = await auth_db.get_user_by_id(user_id)
    is_superadmin = bool(user_row and user_row.get("is_superadmin"))
    await preview_list_manager.connect(websocket, user_id)

    async def send_two_phase(msg_type: str):
        t_ws = time.monotonic()

        # Phase 1: filesystem only (fast), already filtered per user
        result = await get_previews_for_user(user_id, is_superadmin, include_docker_status=False)
        t_phase1 = time.monotonic()
        await websocket.send_json({
            "type": msg_type,
            "previews": result["previews"],
            "total": result["total"],
            "checked_at": datetime.utcnow().isoformat()
        })
        logger.info(f"[TIMING] WS phase 1 ({msg_type}): {t_phase1 - t_ws:.3f}s")

        # Phase 2: with Docker status (slow), still per-user filtered
        result = await get_previews_for_user(user_id, is_superadmin, include_docker_status=True)
        t_phase2 = time.monotonic()
        # last_state tracks the global preview list so change detection stays consistent
        global_state = await get_preview_list_base(include_docker_status=True)
        preview_list_manager.last_state = json.dumps(global_state["previews"], sort_keys=True, default=str)
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
                    result = await get_previews_for_user(user_id, is_superadmin, include_docker_status=True)
                    global_state = await get_preview_list_base(include_docker_status=True)
                    preview_list_manager.last_state = json.dumps(global_state["previews"], sort_keys=True, default=str)
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


@router.websocket("/ws/system-resources")
async def websocket_system_resources(websocket: WebSocket):
    """WebSocket endpoint for real-time system resource metrics."""
    await _authenticate_ws(websocket)
    await system_resources_manager.connect(websocket)

    # Send buffered history so the client has a chart immediately
    if _resource_history:
        try:
            await websocket.send_json({"type": "history", "snapshots": list(_resource_history), "disk_usage": _disk_usage_cache})
        except Exception:
            pass

    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
            except Exception:
                break
    except Exception:
        pass
    finally:
        system_resources_manager.disconnect(websocket)


@router.websocket("/ws/deployments/{deployment_id}/logs")
async def websocket_deployment_logs(websocket: WebSocket, deployment_id: int):
    """WebSocket endpoint for real-time deployment log streaming via Valkey."""
    await _authenticate_ws(websocket)
    await websocket.accept()

    logger.info(f"Deployment logs WS subscribed: deployment_id={deployment_id}")

    from app.valkey import get_deploy_log_buffer, get_deploy_complete, subscribe as valkey_subscribe

    # Replay buffered logs from Valkey
    try:
        buffered = await get_deploy_log_buffer(deployment_id)
        for line in buffered:
            await websocket.send_json({"type": "log", "line": line})
    except Exception as e:
        logger.warning(f"Error replaying deployment logs: {e}")

    # Replay current phases snapshot (if available)
    try:
        from app.valkey import get_valkey
        v = get_valkey()
        phases_raw = await v.get(f"deploy_phases:{deployment_id}")
        if phases_raw:
            phases = json.loads(phases_raw)
            await websocket.send_json({"type": "phases_init", "phases": phases})
    except Exception as e:
        logger.warning(f"Error replaying phases: {e}")

    # Check if already complete
    try:
        complete = await get_deploy_complete(deployment_id)
        if complete is not None:
            await websocket.send_json({"type": "complete", "success": complete})
            await websocket.close()
            return
    except Exception:
        pass

    # Subscribe to Valkey channel for live updates
    pubsub = None
    try:
        pubsub = await valkey_subscribe(f"deploy_logs:{deployment_id}")

        while True:
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0),
                    timeout=3.0,
                )
                if msg and msg.get("type") == "message":
                    data = json.loads(msg["data"])
                    msg_type = data.get("type", "unknown")
                    if msg_type != "log":
                        logger.info(f"Deploy WS relay: deployment_id={deployment_id} msg_type={msg_type}")
                    await websocket.send_json(data)
                    if msg_type == "complete":
                        break
            except asyncio.TimeoutError:
                # Keep alive — check if client is still connected
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
    except Exception as e:
        logger.info(f"Deployment logs WS closed: deployment_id={deployment_id}, reason={e}")
    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe(f"deploy_logs:{deployment_id}")
                await pubsub.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


async def _stream_subprocess_with_action(
    command: list[str],
    cwd: str,
    running_action: RunningAction,
    timeout: int = 120,
    process: asyncio.subprocess.Process | None = None,
) -> tuple[bool, str]:
    """Execute a subprocess, buffer logs in RunningAction and broadcast to subscribers."""
    try:
        if process is None:
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


@router.websocket("/ws/previews/{org_slug}/{project_slug}/{preview_name}/action")
async def websocket_preview_action(
    websocket: WebSocket,
    org_slug: str,
    project_slug: str,
    preview_name: str,
    action: str
):
    """
    WebSocket endpoint to execute preview actions with log streaming.
    If an action is already running for this preview, the client joins as a follower.

    Query params:
        action: "stop" | "start" | "restart" | "drush-uli"
    """
    await _authenticate_ws(websocket)
    await websocket.accept()

    try:
        # Use composite key to avoid collisions between projects
        action_key = f"{org_slug}/{project_slug}/{preview_name}"

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

        # Resolve org and project
        from app.database import get_organization_by_slug, get_project_by_slug, get_preview, compute_url_hash, update_preview_vm

        org = await get_organization_by_slug(org_slug)
        if not org:
            await websocket.send_json({"type": "error", "message": "Organization not found"})
            await websocket.close()
            return

        project = await get_project_by_slug(org["id"], project_slug)
        if not project:
            await websocket.send_json({"type": "error", "message": "Project not found"})
            await websocket.close()
            return

        # Look up preview to get VM IP for SSH execution
        preview = await get_preview(project["id"], preview_name)
        if not preview:
            await websocket.send_json({
                "type": "error",
                "message": f"Preview '{preview_name}' not found"
            })
            await websocket.close()
            return

        vm_ip = preview.get("vm_ip")

        # Build command based on action
        from app.database import compute_url_hash
        url_hash = preview.get("url_hash", compute_url_hash(org_slug, project_slug, preview_name))
        php_container = f"{url_hash}-php"
        if action == "stop":
            # For cloud: use REST endpoint to destroy VM
            running_action = action_manager.start(action_key, action, "stop VM")
            await running_action.add_subscriber(websocket)
            try:
                from app.cloud import cloud_manager
                from app.caddy_api import caddy_manager
                if preview.get("vm_id"):
                    await cloud_manager.destroy_vm(preview["vm_id"])
                    url_hash = compute_url_hash(org_slug, project_slug, preview_name)
                    domain = f"{url_hash}.{settings.preview_domain}"
                    await caddy_manager.remove_preview_route(domain)
                    await update_preview_vm(preview["id"], None, None)
                success, message = True, "VM destroyed, volume kept"
            except Exception as e:
                success, message = False, str(e)
        elif action in ("start", "restart", "drush-uli"):
            if not vm_ip:
                await websocket.send_json({
                    "type": "error",
                    "message": "Preview VM is not running. Visit the preview URL to wake it up."
                })
                await websocket.close()
                return

            from app.remote import RemoteExecutor
            executor = RemoteExecutor(vm_ip)
            if action == "start":
                ssh_cmd = "cd /var/www/preview/code && docker compose up -d"
                timeout = 120
            elif action == "restart":
                ssh_cmd = "cd /var/www/preview/code && docker compose restart"
                timeout = 120
            else:  # drush-uli
                from app.routes.previews import _resolve_drush_uri
                url_hash = compute_url_hash(org_slug, project_slug, preview_name)
                drush_uri = _resolve_drush_uri(org_slug, project_slug, preview_name, url_hash)
                ssh_cmd = f"docker exec {php_container} vendor/bin/drush uli --uri={drush_uri}"
                timeout = 30

            running_action = action_manager.start(action_key, action, ssh_cmd)
            await running_action.add_subscriber(websocket)

            proc = await executor.run_shell(ssh_cmd)
            command_for_stream = executor._ssh_base() + ["--", ssh_cmd]
            success, message = await _stream_subprocess_with_action(
                command=command_for_stream,
                cwd="/tmp",
                running_action=running_action,
                timeout=timeout,
                process=proc,
            )
        else:
            await websocket.send_json({
                "type": "error",
                "message": f"Unknown action: {action}"
            })
            await websocket.close()
            return

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


# ---------------------------------------------------------------------------
# Agent callback WebSocket (VM agent → server log relay)
# ---------------------------------------------------------------------------

_AGENT_STEP_LABELS = {
    "git_clone": "Cloning repository",
    "git_fetch": "Fetching latest changes",
    "parse_config": "Parsing configuration",
    "generate_compose": "Generating environment",
    "generate_settings": "Generating settings",
    "docker_pull": "Pulling Docker images",
    "docker_up": "Starting containers",
    "wait_for_db": "Waiting for database",
    "import_db": "Importing database",
    "import_files": "Importing files",
    "composer_install": "Installing dependencies",
    "deploy_script": "Running deploy script",
    "post_deploy": "Running post-deploy script",
}


def _agent_step_label(name: str) -> str:
    return _AGENT_STEP_LABELS.get(name, name.replace("_", " ").title())


def _validate_agent_token(token: str) -> int | None:
    """Validate agent callback token. Returns deployment_id or None."""
    if not token:
        return None
    parts = token.split(":")
    if len(parts) != 3:
        return None
    deployment_id_str, expiry_str, sig = parts[0], parts[1], parts[2]

    try:
        if int(time.time()) > int(expiry_str):
            return None
    except (ValueError, TypeError):
        return None

    payload = f"{deployment_id_str}:{expiry_str}"
    expected = hmac.new(
        settings.secret_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None

    try:
        return int(deployment_id_str)
    except (ValueError, TypeError):
        return None


@router.websocket("/ws/internal/agent")
async def websocket_agent_callback(websocket: WebSocket):
    """Receive deploy logs from VM agent and relay to Valkey pub/sub."""
    token = websocket.query_params.get("token", "")
    deployment_id_param = websocket.query_params.get("deployment_id", "")

    # Validate token
    validated_id = _validate_agent_token(token)
    if validated_id is None:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

    # If deployment_id param is provided, it must match the token
    if deployment_id_param:
        try:
            if int(deployment_id_param) != validated_id:
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
        except (ValueError, TypeError):
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

    deployment_id = validated_id

    await websocket.accept()
    logger.info(f"Agent WS connected: deployment_id={deployment_id}")

    # Ensure deployment log broadcaster is registered
    await deployment_log_broadcaster.register(deployment_id)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"Agent WS invalid JSON: {raw[:200]}")
                continue
            msg_type = data.get("type")
            msg_type = data.get("type")

            if msg_type == "log":
                # Relay log line to existing deployment log broadcaster
                await deployment_log_broadcaster.add_log(
                    deployment_id, data.get("line", "")
                )

            elif msg_type == "step":
                # Step status update -- log it as a formatted line
                name = data.get("name", "")
                step_status = data.get("status", "")
                duration = data.get("duration", 0)
                error_msg = data.get("error", "")

                # Build phase data for broadcast + Valkey storage
                phase_data = {
                    "name": name,
                    "label": _agent_step_label(name),
                    "required": name != "post_deploy",
                }

                if step_status == "running":
                    await deployment_log_broadcaster.add_log(
                        deployment_id,
                        f"\n\n\n\033[0;36m\u2699\ufe0f {_agent_step_label(name)}\033[0m\n\033[0;90m{'─' * 68}\033[0m\n\n",
                    )
                    phase_data.update({"status": "running", "duration": None})
                elif step_status == "done":
                    dur_str = f"{int(duration)}s" if duration else ""
                    await deployment_log_broadcaster.add_log(
                        deployment_id,
                        f"\033[1;32m\u2713 {_agent_step_label(name)}\033[0m"
                        f" \033[0;90mcompleted in {dur_str}\033[0m\n\n",
                    )
                    phase_data.update({"status": "success", "duration": round(duration, 1) if duration else None})
                elif step_status == "failed":
                    dur_str = f"{int(duration)}s" if duration else ""
                    line = (
                        f"\033[1;31m\u2717 {_agent_step_label(name)}\033[0m"
                        f" \033[0;90mfailed after {dur_str}\033[0m\n"
                    )
                    if error_msg:
                        line += f"  Error: {error_msg}\n"
                    await deployment_log_broadcaster.add_log(deployment_id, line)
                    phase_data.update({"status": "failed", "duration": round(duration, 1) if duration else None})

                # Broadcast phase_update to frontend
                await deployment_log_broadcaster.broadcast_phase_event(
                    deployment_id, "phase_update", {"phase": phase_data}
                )

                # Store in Valkey so deployer can reconstruct final state
                try:
                    from app.valkey import get_valkey
                    r = get_valkey()
                    await r.hset(
                        f"agent_phases:{deployment_id}",
                        name,
                        json.dumps(phase_data),
                    )
                    await r.expire(f"agent_phases:{deployment_id}", 3600)
                except Exception as e:
                    logger.warning(f"Failed to store agent phase in Valkey: {e}")

            elif msg_type == "complete":
                success = data.get("success", False)
                duration = data.get("duration", 0)
                error_msg = data.get("error", "")

                # Update deployment record
                from app.database import finish_deployment
                dep_status = "success" if success else "failed"
                await finish_deployment(
                    deployment_id, dep_status,
                    error=error_msg if error_msg else None,
                    duration=int(duration) if duration else None,
                )
                await deployment_log_broadcaster.complete(deployment_id, success)

                # Store result in Valkey so the deployer can read it
                from app.valkey import get_valkey
                r = get_valkey()
                result = json.dumps({
                    "success": success,
                    "error": error_msg,
                    "duration": duration,
                })
                await r.set(
                    f"agent_deploy_result:{deployment_id}", result, ex=3600
                )
                await r.publish(
                    f"agent_deploy_done:{deployment_id}", result
                )


                logger.info(
                    f"Agent deploy complete: deployment_id={deployment_id} "
                    f"success={success} duration={duration}s"
                )
                break

            elif msg_type == "post_deploy_status":
                # Update post-deploy status in DB
                pd_status = data.get("status")
                preview_name = data.get("preview_name", "")
                project_id = data.get("project_id")
                if preview_name and project_id and pd_status:
                    try:
                        await PreviewStateManager.save_state(
                            int(project_id), preview_name,
                            post_deploy_status=pd_status,
                        )
                        from app.valkey import publish_event
                        await publish_event("previews:global", {
                            "action": "state_change",
                            "preview_name": preview_name,
                            "post_deploy_status": pd_status,
                        })
                    except Exception as e:
                        logger.warning(f"Failed to update post_deploy_status: {e}")

    except Exception as e:
        logger.warning(f"Agent WS error (deployment_id={deployment_id}): {e}", exc_info=True)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
