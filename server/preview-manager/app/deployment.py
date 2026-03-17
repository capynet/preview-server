"""Preview deployment logic — deploy previews on ephemeral Hetzner Cloud VMs."""

import asyncio
import hashlib
import hmac as hmac_mod
import json
import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.docker_compose import (
    detect_docroot,
    generate_docker_compose,
    parse_preview_yml,
    write_docker_compose,
)
from app.state import PreviewStateManager
from app.database import (
    get_preview, get_project, create_deployment, finish_deployment,
    update_deployment_status, update_preview_vm, compute_url_hash,
)
from app.caddy_api import caddy_manager
from app.cloud import cloud_manager
from app.storage import storage_manager
from app.remote import RemoteExecutor
from config.settings import settings

logger = logging.getLogger(__name__)


class DeployCancelled(Exception):
    """Raised when a deploy is cancelled by a new rebuild request."""
    pass

# Timeouts per step (seconds)
TIMEOUT_DOCKER_UP = 300
TIMEOUT_COMPOSER = 600
TIMEOUT_IMPORT_DB = 600
TIMEOUT_IMPORT_FILES = 600
TIMEOUT_DRUSH = 300
TIMEOUT_DEPLOY_SCRIPT = 36000
TIMEOUT_POST_DEPLOY = 18000  # 5 hours — post-deploy can run heavy tasks (indexing, etc.)
TIMEOUT_DEPLOY_STEP = 300

# Path to custom deploy step scripts (local on coordinator)
DEPLOY_STEPS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "deploy-steps"

# Preview working directory inside the VM
VM_PREVIEW_DIR = "/var/www/preview"

# ANSI color codes for log output
BOLD = "\033[1m"
CYAN = "\033[1;36m"
GREEN = "\033[1;32m"
RED = "\033[1;31m"
YELLOW = "\033[0;33m"
DIM = "\033[0;90m"
RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Deploy Phases — ordered list of phases per deploy type
# ---------------------------------------------------------------------------

DEPLOY_PHASES: dict[str, list[dict]] = {
    "new": [
        {"name": "provision_agent",   "label": "Provisioning agent",      "required": True},
        {"name": "wait_agent",        "label": "Waiting for agent",       "required": True},
        {"name": "git_clone",         "label": "Cloning repository",      "required": True},
        {"name": "parse_config",      "label": "Parsing configuration",   "required": True},
        {"name": "generate_compose",  "label": "Configuring environment", "required": True},
        {"name": "generate_settings", "label": "Generating settings",     "required": True},
        {"name": "docker_pull",       "label": "Pulling Docker images",   "required": True},
        {"name": "docker_up",         "label": "Starting containers",     "required": True},
        {"name": "wait_for_db",       "label": "Waiting for database",    "required": True},
        {"name": "import_db",         "label": "Importing database",      "required": True},
        {"name": "import_files",      "label": "Importing files",         "required": True},
        {"name": "composer_install",  "label": "Installing dependencies", "required": True},
        {"name": "deploy_script",     "label": "Running deploy script",   "required": True},
        {"name": "post_deploy",       "label": "Running post-deploy",     "required": False},
    ],
    "update": [
        {"name": "provision_agent",   "label": "Provisioning agent",      "required": True},
        {"name": "wait_agent",        "label": "Waiting for agent",       "required": True},
        {"name": "git_fetch",         "label": "Fetching latest changes", "required": True},
        {"name": "parse_config",      "label": "Parsing configuration",   "required": True},
        {"name": "generate_compose",  "label": "Configuring environment", "required": True},
        {"name": "generate_settings", "label": "Generating settings",     "required": True},
        {"name": "docker_up",         "label": "Starting containers",     "required": True},
        {"name": "composer_install",  "label": "Installing dependencies", "required": True},
        {"name": "deploy_script",     "label": "Running deploy script",   "required": True},
        {"name": "post_deploy",       "label": "Running post-deploy",     "required": False},
    ],
}

# Map coordinator step names to phase names
_COORDINATOR_STEP_TO_PHASE = {
    "provision-agent": "provision_agent",
    "wait-agent": "wait_agent",
}


class PhaseTracker:
    """Tracks deploy phases and broadcasts updates to the frontend."""

    def __init__(self, deploy_type: str, deployment_id: int | None):
        self.deployment_id = deployment_id
        self.deploy_type = deploy_type
        template = DEPLOY_PHASES.get(deploy_type, DEPLOY_PHASES["update"])
        self.phases: list[dict] = [
            {**p, "status": "pending", "duration": None}
            for p in template
        ]
        self._phase_index: dict[str, int] = {
            p["name"]: i for i, p in enumerate(self.phases)
        }

    async def init_broadcast(self):
        """Send the initial phases list to the frontend."""
        if not self.deployment_id:
            return
        from app.websockets import deployment_log_broadcaster
        await deployment_log_broadcaster.broadcast_phase_event(
            self.deployment_id, "phases_init", {"phases": self.phases}
        )

    async def start_phase(self, name: str):
        """Mark a phase as running."""
        idx = self._phase_index.get(name)
        if idx is None:
            return
        self.phases[idx]["status"] = "running"
        await self._broadcast_update(self.phases[idx])

    async def end_phase(self, name: str, success: bool, duration: float | None = None):
        """Mark a phase as success/failed."""
        idx = self._phase_index.get(name)
        if idx is None:
            return
        self.phases[idx]["status"] = "success" if success else "failed"
        if duration is not None:
            self.phases[idx]["duration"] = round(duration, 1)
        await self._broadcast_update(self.phases[idx])

    async def skip_phase(self, name: str):
        """Mark a phase as skipped."""
        idx = self._phase_index.get(name)
        if idx is None:
            return
        self.phases[idx]["status"] = "skipped"
        await self._broadcast_update(self.phases[idx])

    def compute_final_status(self) -> str:
        """Compute final deploy status based on required phases."""
        required_failed = any(
            p["status"] == "failed" for p in self.phases if p["required"]
        )
        if required_failed:
            return "failed"
        non_required_failed = any(
            p["status"] == "failed" for p in self.phases if not p["required"]
        )
        if non_required_failed:
            return "warning"
        return "success"

    async def sync_agent_phases(self):
        """Read agent phase states from Valkey and merge into our tracking."""
        if not self.deployment_id:
            return
        try:
            from app.valkey import get_valkey
            r = get_valkey()
            agent_data = await r.hgetall(f"agent_phases:{self.deployment_id}")
            for name_bytes, data_bytes in agent_data.items():
                name = name_bytes if isinstance(name_bytes, str) else name_bytes.decode()
                phase_data = json.loads(data_bytes)
                idx = self._phase_index.get(name)
                if idx is not None:
                    self.phases[idx]["status"] = phase_data.get("status", self.phases[idx]["status"])
                    if phase_data.get("duration") is not None:
                        self.phases[idx]["duration"] = phase_data["duration"]
        except Exception as e:
            logger.warning(f"Failed to sync agent phases from Valkey: {e}")

    def to_json(self) -> str:
        """Serialize phases to JSON for DB storage."""
        return json.dumps(self.phases)

    async def _broadcast_update(self, phase: dict):
        if not self.deployment_id:
            return
        from app.websockets import deployment_log_broadcaster
        await deployment_log_broadcaster.broadcast_phase_event(
            self.deployment_id, "phase_update", {"phase": phase}
        )


def _fmt_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m {s}s"


def _compute_db_cache_key(project: str, db_spec: str, dump_path: Path) -> str:
    """Compute a cache key for a DB dump (local file)."""
    h = hashlib.md5()
    with open(dump_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    sanitized = db_spec.replace(":", "-").replace("/", "-")
    return f"{sanitized}-{h.hexdigest()[:16]}"


class PreviewDeployer:
    """Deploy a preview environment on a Hetzner Cloud VM.

    Handles both new previews (full setup) and updates (code-only refresh).
    """

    def __init__(
        self,
        project_name: str,
        preview_name: str,
        branch: str,
        commit_sha: str,
        triggered_by: str | None = None,
        mr_iid: int | None = None,
        mr_title: str | None = None,
        target_branch: str | None = None,
        deployment_id: int | None = None,
        *,
        org_slug: str = "",
        project_slug: str = "",
        project_id: int | None = None,
        org_id: int | None = None,
    ):
        # Multi-tenant identifiers
        self.org_slug = org_slug
        self.org_id = org_id
        self.project_slug = project_slug or project_name
        self.project_id = project_id
        # Keep project_name as alias for backward compat in container naming
        self.project_name = self.project_slug

        self.preview_name = preview_name
        self.branch = branch
        self.commit_sha = commit_sha
        self.triggered_by = triggered_by
        self.mr_iid = mr_iid
        self.mr_title = mr_title
        self.target_branch = target_branch

        self.force_new = False
        self.preview_path = PreviewStateManager.get_preview_path(
            self.org_slug, self.project_slug, preview_name
        )
        self.container_prefix = f"{preview_name}-{self.project_slug}"

        # Hash-based domain
        url_hash = compute_url_hash(self.org_slug, self.project_slug, preview_name)
        self.domain = f"{url_hash}.mr.preview-mr.com"
        self.preview_url = f"https://{self.domain}"

        self._preview_config: dict | None = None
        self._log_buffer: list[str] = []
        self._deployment_id: int | None = deployment_id
        self._step_timings: list[tuple[str, float, str]] = []
        self._executor: RemoteExecutor | None = None
        self._vm_id: int | None = None
        self._vm_ip: str | None = None
        self._phase_tracker: PhaseTracker | None = None

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def _check_cancelled(self):
        """Check if this deploy was cancelled by a new rebuild request."""
        from app.valkey import is_deploy_cancelled
        deploy_key = f"{self.project_slug}/{self.preview_name}"
        if await is_deploy_cancelled(deploy_key):
            raise DeployCancelled(f"Deploy cancelled for {deploy_key}")

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def is_new(self) -> bool:
        """Check if this is a first deploy (no previous successful deployment)."""
        if self.force_new:
            return True
        state = await PreviewStateManager.load_state(self.project_id, self.preview_name)
        if not state:
            return True
        return not state.get("last_deployed_at")

    async def is_creating(self) -> bool:
        state = await PreviewStateManager.load_state(self.project_id, self.preview_name)
        return state is not None and state["status"] == "creating"

    async def deploy(self) -> bool:
        """Entry point. Returns True on success."""
        if await self.is_creating():
            logger.warning(
                f"Skipping deploy for {self.project_slug}/{self.preview_name}: "
                "already creating"
            )
            return False

        await self._save_state("creating")
        self._log_buffer = []
        self._step_timings = []
        start = datetime.now(timezone.utc)

        # Create deployment record in DB (or reuse one created earlier)
        from app.websockets import deployment_log_broadcaster, preview_list_manager
        if not self._deployment_id:
            preview = await get_preview(self.project_id, self.preview_name)
            if preview:
                from app.database import get_running_deployment
                existing = await get_running_deployment(preview["id"])
                if existing:
                    self._deployment_id = existing["id"]
                else:
                    self._deployment_id = await create_deployment(
                        preview["id"], self.triggered_by
                    )
                await deployment_log_broadcaster.register(self._deployment_id)
                await preview_list_manager.force_broadcast()

        # Reset post_deploy_status from previous deploy
        await self._update_post_deploy_status(None)

        is_new = await self.is_new()
        deploy_type = "NEW" if is_new else "UPDATE"

        # Initialize phase tracker
        phase_type = "new" if is_new else "update"
        self._phase_tracker = PhaseTracker(phase_type, self._deployment_id)
        await self._phase_tracker.init_broadcast()

        # Deploy header
        await self._log_raw(
            f"\n{BOLD}{CYAN}{deploy_type} Deploy: {self.project_slug}/{self.preview_name}{RESET}\n"
            f"{DIM}Branch: {self.branch}  Commit: {self.commit_sha[:8]}{RESET}\n"
        )

        try:
            # Use agent-based deployment
            logger.info(f"{deploy_type} deploy (agent): {self.project_slug}/{self.preview_name}")
            agent_result = await self._deploy_via_agent(is_new)

            deploy_duration = int((datetime.now(timezone.utc) - start).total_seconds())

            # If there was a post-deploy phase, notify the UI
            if agent_result and agent_result.get("had_post_deploy"):
                await deployment_log_broadcaster.broadcast_deploy_status(
                    self._deployment_id, "success", deploy_duration
                )

            # Sync agent phases from Valkey and compute final status
            if self._phase_tracker:
                await self._phase_tracker.sync_agent_phases()
            final_status = self._phase_tracker.compute_final_status() if self._phase_tracker else "success"

            # Finalize deployment record with logs and phases
            if self._deployment_id:
                await finish_deployment(
                    self._deployment_id, final_status,
                    log_output="\n".join(self._log_buffer),
                    duration=deploy_duration,
                    phases=self._phase_tracker.to_json() if self._phase_tracker else None,
                )
                await deployment_log_broadcaster.complete(self._deployment_id, final_status != "failed")

            await self._save_state("active", duration=deploy_duration)

            # Register Caddy routes (main + aliases + exposed services)
            if self._vm_ip:
                try:
                    url_hash = compute_url_hash(self.org_slug, self.project_slug, self.preview_name)
                    await caddy_manager.add_preview_routes(
                        url_hash, self._vm_ip,
                    )
                except Exception as e:
                    logger.warning(f"Failed to add Caddy route for {self.domain}: {e}")

            logger.info(
                f"Deploy OK: {self.project_slug}/{self.preview_name} in {deploy_duration}s"
            )

            await preview_list_manager.force_broadcast()
            return True

        except DeployCancelled:
            duration = int((datetime.now(timezone.utc) - start).total_seconds())
            logger.info(
                f"Deploy CANCELLED: {self.project_slug}/{self.preview_name} after {duration}s"
            )
            await self._log_raw(
                f"\n{YELLOW}Deploy cancelled — a new rebuild was requested{RESET}\n"
            )

            phases_json = self._phase_tracker.to_json() if self._phase_tracker else None
            if self._deployment_id:
                await finish_deployment(
                    self._deployment_id, "cancelled",
                    log_output="\n".join(self._log_buffer),
                    error="Cancelled by new rebuild request",
                    duration=duration,
                    phases=phases_json,
                )
                await deployment_log_broadcaster.complete(self._deployment_id, False)

            return False

        except Exception as e:
            duration = int((datetime.now(timezone.utc) - start).total_seconds())
            logger.error(
                f"Deploy FAILED: {self.project_slug}/{self.preview_name}: {e}",
                exc_info=True,
            )
            await self._save_state("failed", error=str(e), duration=duration)

            # Sync agent phases if available
            if self._phase_tracker:
                await self._phase_tracker.sync_agent_phases()
            phases_json = self._phase_tracker.to_json() if self._phase_tracker else None

            # Check if the agent already finished the deployment (e.g. agent
            # reported failure via WS before the deployer raised).
            from app.valkey import get_valkey
            try:
                r = get_valkey()
                already_finished = await r.get(f"agent_deploy_result:{self._deployment_id}")
            except Exception:
                already_finished = None

            if not already_finished and self._deployment_id:
                await finish_deployment(
                    self._deployment_id, "failed",
                    log_output="\n".join(self._log_buffer),
                    error=str(e),
                    duration=duration,
                    phases=phases_json,
                )
                await deployment_log_broadcaster.complete(self._deployment_id, False)

            return False

    # ------------------------------------------------------------------
    # Agent-based deployment (delegates to VM agent)
    # ------------------------------------------------------------------

    async def _deploy_via_agent(self, is_new: bool):
        """Delegate deploy execution to the VM agent running on the VM."""
        import urllib.request

        # 1. Create/reuse VM
        await self._ensure_vm()

        # 2. Provision agent on VM (install + start if not running)
        await self._provision_agent()

        # 3. Wait for agent health check
        await self._wait_for_agent()

        # 4. Check if agent is already running a deploy for this preview
        #    If so, attach to it (poll its logs) instead of starting a new one.
        #    This handles re-enqueued jobs after worker restart.
        try:
            status_data = await asyncio.to_thread(
                lambda: json.loads(
                    urllib.request.urlopen(
                        f"http://{self._vm_ip}:8022/deploy/status", timeout=5
                    ).read()
                )
            )
            agent_status = status_data.get("status", "idle")
            agent_deploy_id = status_data.get("deployment_id", 0)

            if agent_status == "running":
                # Agent already running a deploy — attach to it
                self._deployment_id = agent_deploy_id
                await self._log_raw(
                    f"{DIM}Agent already running deploy #{agent_deploy_id}, attaching...{RESET}\n"
                )
                result = await self._wait_for_agent_completion()
                if not result["success"]:
                    raise RuntimeError(result.get("error", "Deploy failed on VM"))
                return result

            if agent_status in ("success", "failed") and agent_deploy_id == self._deployment_id:
                # Agent already finished this exact deploy — return its result
                result_data = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(
                            f"http://{self._vm_ip}:8022/deploy/logs/{self._deployment_id}?offset=0",
                            timeout=10,
                        ).read()
                    )
                )
                result = result_data.get("result")
                if result:
                    # Relay all logs
                    content = result_data.get("content", "")
                    if content:
                        await self._log_raw(content)
                    if not result["success"]:
                        raise RuntimeError(result.get("error", "Deploy failed on VM"))
                    return result
        except (urllib.error.URLError, Exception) as e:
            logger.warning(f"Could not check agent status: {e}")

        # 5. Build deploy job payload and POST to agent
        job = await self._build_agent_job(is_new)

        await self._log_raw(f"{DIM}Sending deploy job to agent...{RESET}\n")
        try:
            resp_data = await asyncio.to_thread(
                lambda: urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://{self._vm_ip}:8022/deploy",
                        data=json.dumps(job).encode(),
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=30,
                ).read()
            )
        except Exception as e:
            raise RuntimeError(f"Agent rejected deploy: {e}")

        await self._log_raw(f"{DIM}Agent accepted job, waiting for completion...{RESET}\n")

        # 6. Wait for agent completion
        result = await self._wait_for_agent_completion()

        if not result["success"]:
            raise RuntimeError(result.get("error", "Deploy failed on VM"))

        return result

    async def _ensure_vm(self):
        """Create a new VM or reuse an existing one."""
        preview = await get_preview(self.project_id, self.preview_name)
        existing_vm_id = preview.get("vm_id") if preview else None
        existing_vm_ip = preview.get("vm_ip") if preview else None

        if existing_vm_id and existing_vm_ip:
            self._vm_id = existing_vm_id
            self._vm_ip = existing_vm_ip
            logger.info(f"Reusing existing VM {self._vm_id} ({self._vm_ip})")
            await self._log_raw(
                f"{DIM}Reusing existing VM {self._vm_id} ({self._vm_ip}){RESET}\n"
            )
        else:
            raw = f"{self.project_slug}-{self.preview_name}"
            vm_name = f"prev-{hashlib.md5(raw.encode()).hexdigest()[:8]}"
            server = await self._step_create_vm(vm_name)
            self._vm_id = server.data_model.id
            self._vm_ip = server.data_model.public_net.ipv4.ip
            # Update VM info in DB
            preview = await get_preview(self.project_id, self.preview_name)
            if preview:
                await update_preview_vm(preview["id"], self._vm_id, self._vm_ip)

    async def _provision_agent(self):
        """Install and start the preview-agent on the VM via SSH.

        This ensures the agent is running even if the VM snapshot doesn't
        include it. On subsequent deploys for the same VM, this is a no-op
        because the agent is already running.
        """
        step = "provision-agent"
        await self._log_step_start(step)
        t0 = time.monotonic()

        # Check if agent is already running — if so, restart to get latest version
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"http://{self._vm_ip}:8022/health")
                if resp.status_code == 200:
                    # Agent running — restart to update binary
                    executor = RemoteExecutor(self._vm_ip)
                    proc = await executor.run_shell("systemctl restart preview-agent")
                    await proc.communicate()
                    elapsed = time.monotonic() - t0
                    await self._log_step_end(step, elapsed, True, f"{DIM}Agent updated{RESET}")
                    return
        except Exception:
            pass

        # Agent not running — wait for SSH first
        executor = RemoteExecutor(self._vm_ip)
        await executor.wait_for_ssh(timeout=120)

        # Install the agent: download binary + create systemd service
        api_url = f"https://api.preview-mr.com"
        install_cmd = (
            # Download agent binary
            f"curl -sf -o /usr/local/bin/preview-agent {api_url}/api/internal/agent/download && "
            f"chmod +x /usr/local/bin/preview-agent && "
            # Write env config
            f"echo 'PREVIEW_SERVER_URL={api_url}' > /etc/preview-agent.env && "
            # Create update script
            f"cat > /usr/local/bin/preview-agent-update << 'UPDATESCRIPT'\n"
            f"#!/bin/bash\n"
            f"set -euo pipefail\n"
            f"source /etc/preview-agent.env 2>/dev/null || true\n"
            f"AGENT_URL=\"${{PREVIEW_SERVER_URL:-{api_url}}}/api/internal/agent/download\"\n"
            f"curl -sf -o /usr/local/bin/preview-agent.new \"$AGENT_URL\" && "
            f"chmod +x /usr/local/bin/preview-agent.new && "
            f"mv /usr/local/bin/preview-agent.new /usr/local/bin/preview-agent || true\n"
            f"UPDATESCRIPT\n"
            f"chmod +x /usr/local/bin/preview-agent-update && "
            # Create systemd service
            f"cat > /etc/systemd/system/preview-agent.service << 'SERVICEFILE'\n"
            f"[Unit]\n"
            f"Description=Preview Agent\n"
            f"After=docker.service\n"
            f"Requires=docker.service\n"
            f"\n"
            f"[Service]\n"
            f"Type=simple\n"
            f"ExecStartPre=/usr/local/bin/preview-agent-update\n"
            f"ExecStart=/usr/local/bin/preview-agent\n"
            f"Restart=always\n"
            f"RestartSec=5\n"
            f"Environment=PORT=8022\n"
            f"EnvironmentFile=-/etc/preview-agent.env\n"
            f"\n"
            f"[Install]\n"
            f"WantedBy=multi-user.target\n"
            f"SERVICEFILE\n"
            f"systemctl daemon-reload && "
            f"systemctl enable preview-agent && "
            f"systemctl start preview-agent"
        )

        proc = await executor.run_shell(install_cmd)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            output = (stdout.decode() + stderr.decode())[-500:]
            raise RuntimeError(f"[{step}] Failed to install agent:\n{output}")

        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, True, f"{DIM}Agent installed and started{RESET}")

    async def _wait_for_agent(self, timeout: int = 120):
        """Wait for the VM agent to be reachable on port 8022."""
        step = "wait-agent"
        await self._log_step_start(step)
        t0 = time.monotonic()

        import httpx
        max_attempts = timeout // 2
        for i in range(max_attempts):
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"http://{self._vm_ip}:8022/health")
                    if resp.status_code == 200:
                        elapsed = time.monotonic() - t0
                        await self._log_step_end(
                            step, elapsed, True,
                            f"{DIM}Agent ready{RESET}",
                        )
                        return
            except Exception:
                pass
            await asyncio.sleep(2)

        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, False, "Agent not reachable")
        raise RuntimeError(
            f"Agent not reachable on {self._vm_ip}:8022 after {timeout}s"
        )

    async def _build_agent_job(self, is_new: bool) -> dict:
        """Build the deploy job payload for the agent."""
        from app.routes.gitlab import _get_org_gitlab_token

        gitlab_url, gitlab_token = await _get_org_gitlab_token(self.org_id)
        parsed = urlparse(gitlab_url)
        gitlab_host = parsed.hostname

        # Get project to find the gitlab path
        project = await get_project(self.project_id)
        project_path = project.get("gitlab_project_path", "") if project else ""

        git_clone_url = f"https://oauth2:{gitlab_token}@{gitlab_host}/{project_path}.git"

        url_hash = compute_url_hash(
            self.org_slug, self.project_slug, self.preview_name
        )

        # Compute terminal secret (same logic as _upload_compose_and_settings)
        terminal_secret = hmac_mod.new(
            settings.secret_key.encode(),
            f"terminal:{self.org_slug}:{self.project_slug}:{self.preview_name}".encode(),
            hashlib.sha256,
        ).hexdigest()

        # Load extra env vars from project and preview
        extra_env: dict[str, str] = {}
        try:
            proj = await get_project(self.project_id)
            if proj and proj.get("env_vars"):
                project_env = proj["env_vars"]
                if isinstance(project_env, str):
                    project_env = json.loads(project_env)
                extra_env.update(project_env)

            preview_row = await get_preview(self.project_id, self.preview_name)
            if preview_row and preview_row.get("env_vars"):
                preview_env = preview_row["env_vars"]
                if isinstance(preview_env, str):
                    preview_env = json.loads(preview_env)
                extra_env.update(preview_env)
        except Exception as e:
            logger.warning(f"Error loading extra env vars for agent job: {e}")

        # Check if composer proxy is enabled for this org
        composer_proxy_url = ""
        if self.org_id:
            from app.database import get_organization_by_id
            org = await get_organization_by_id(self.org_id)
            proxy_enabled = org.get("composer_proxy_enabled", 0) if org else 0
            if proxy_enabled and settings.composer_proxy_url:
                composer_proxy_url = settings.composer_proxy_url

        # Build storage config
        storage_config: dict = {"type": settings.storage_backend}
        if settings.storage_backend == "s3":
            storage_config.update({
                "endpoint": settings.hetzner_s3_endpoint,
                "access_key": settings.hetzner_s3_access_key,
                "secret_key": settings.hetzner_s3_secret_key,
                "bucket": settings.hetzner_s3_bucket,
                "base_db_key": f"base-files/{self.project_slug}/db.sql.gz",
                "base_files_key": f"base-files/{self.project_slug}/files.tar.gz",
            })
        elif settings.storage_backend == "storagebox":
            storage_config.update({
                "host": settings.storagebox_host,
                "port": settings.storagebox_port,
                "user": settings.storagebox_user,
                "password": settings.storagebox_password,
                "base_path": settings.storagebox_base_path,
                "base_db_key": f"base-files/{self.project_slug}/db.sql.gz",
                "base_files_key": f"base-files/{self.project_slug}/files.tar.gz",
            })

        return {
            "deployment_id": self._deployment_id,
            "phase": "new" if is_new else "update",
            "force_new": self.force_new,
            "org_slug": self.org_slug,
            "project_slug": self.project_slug,
            "project_id": self.project_id,
            "preview_name": self.preview_name,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "mr_iid": self.mr_iid,
            "mr_title": self.mr_title,
            "target_branch": self.target_branch,
            "git_clone_url": git_clone_url,
            "composer_proxy_url": composer_proxy_url,
            "docker_registry": settings.docker_registry,
            "url_hash": url_hash,
            "domain": self.domain,
            "preview_url": self.preview_url,
            "terminal_secret": terminal_secret,
            "storage": storage_config,
            "env_vars": extra_env,
            "callback_url": f"ws://91.99.157.66:8000/ws/internal/agent",
            "callback_token": self._generate_callback_token(),
        }

    async def _wait_for_agent_completion(self, timeout: int = 36000):
        """Poll the agent for deploy logs and completion status."""
        import urllib.request
        import urllib.error

        log_offset = 0
        current_phase = "deploy"
        deadline = time.monotonic() + timeout
        poll_interval = 2  # seconds
        poll_url = f"http://{self._vm_ip}:8022/deploy/logs/{self._deployment_id}"

        def _poll(offset: int) -> dict | None:
            try:
                req = urllib.request.urlopen(f"{poll_url}?offset={offset}", timeout=10)
                return json.loads(req.read())
            except Exception:
                return None

        while time.monotonic() < deadline:
            data = await asyncio.to_thread(_poll, log_offset)

            if data:
                content = data.get("content", "")
                if content:
                    await self._log_raw(content)
                    log_offset = data.get("size", log_offset)

                # Check for phase change (deploy → post_deploy)
                phase = data.get("phase") or "deploy"
                if phase != current_phase:
                    logger.info(f"Phase change detected: {current_phase} → {phase} (deployment {self._deployment_id})")
                    current_phase = phase
                    if phase == "post_deploy":
                        from app.websockets import deployment_log_broadcaster
                        try:
                            await deployment_log_broadcaster.broadcast_deploy_status(
                                self._deployment_id, "success", 0
                            )
                            logger.info(f"broadcast_deploy_status sent for deployment {self._deployment_id}")
                        except Exception as e:
                            logger.error(f"Failed to broadcast deploy_status: {e}", exc_info=True)

                result = data.get("result")
                if result is not None:
                    return result

            await asyncio.sleep(poll_interval)

            await asyncio.sleep(poll_interval)

        raise RuntimeError(f"Agent deploy timed out after {timeout}s")

    def _generate_callback_token(self) -> str:
        """Generate a short-lived HMAC token for agent WebSocket auth."""
        expiry = int(time.time()) + 3600  # 1 hour
        payload = f"{self._deployment_id}:{expiry}"
        sig = hmac_mod.new(
            settings.secret_key.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"{payload}:{sig}"

    # ------------------------------------------------------------------
    # New preview (cloud) — legacy SSH-based, kept as dead code
    # ------------------------------------------------------------------

    async def _deploy_new(self):
        # 1. Verify base files exist in S3
        await self._verify_base_files()
        await self._check_cancelled()

        # 2. Create VM or reuse existing one
        preview = await get_preview(self.project_id, self.preview_name)
        existing_vm_id = preview.get("vm_id") if preview else None
        existing_vm_ip = preview.get("vm_ip") if preview else None

        if existing_vm_id and existing_vm_ip:
            # Reuse existing VM
            self._vm_id = existing_vm_id
            self._vm_ip = existing_vm_ip
            self._executor = RemoteExecutor(self._vm_ip)
            logger.info(f"Reusing existing VM {self._vm_id} ({self._vm_ip})")
            await self._log_raw(f"{DIM}Reusing existing VM {self._vm_id} ({self._vm_ip}){RESET}\n")

            # Stop existing containers and remove volumes (DB will be reimported)
            proc = await self._executor.run_shell(
                f"cd {VM_PREVIEW_DIR}/code && docker compose down --remove-orphans -v 2>/dev/null; true"
            )
            await proc.communicate()
        else:
            # Create new VM
            raw = f"{self.project_slug}-{self.preview_name}"
            vm_name = f"prev-{hashlib.md5(raw.encode()).hexdigest()[:8]}"
            server = await self._step_create_vm(vm_name)
            self._vm_id = server.data_model.id
            self._vm_ip = server.data_model.public_net.ipv4.ip
            self._executor = RemoteExecutor(self._vm_ip)
            # Update VM info in DB using preview_id
            preview = await get_preview(self.project_id, self.preview_name)
            if preview:
                await update_preview_vm(preview["id"], self._vm_id, self._vm_ip)

            # 3. Wait for SSH
            await self._step_wait_ssh()

        await self._check_cancelled()

        # 4. Setup workspace directory
        await self._step_setup_vm()

        # 5. Sync code from coordinator to VM (delete=True for clean state)
        await self._step_sync_code(delete=True)
        await self._check_cancelled()

        # 6. Generate and upload docker-compose.yml
        await self._generate_compose()
        self._write_internal_settings()
        await self._upload_compose_and_settings()

        # 7. Pull images from private registry
        await self._step_pull_images()
        await self._check_cancelled()

        # 8. Start containers and import DB (cache disabled for now)
        await self._docker_up()
        await self._wait_for_db()
        await self._import_db()
        await self._check_cancelled()

        # 9. Composer install
        await self._composer_install()

        # 10. Import files from S3
        await self._import_files()
        await self._check_cancelled()

        # 11. Deploy steps and deploy script
        await self._run_deploy_steps("new")
        await self._run_project_deploy_script("new")

        # 12. Activate Redis cache backend (phase 2)
        await self._activate_redis_cache()

        await self._reload_webserver()

        # Done — traffic is proxied via wake_preview middleware

    # ------------------------------------------------------------------
    # Update preview (cloud) — legacy SSH-based, kept as dead code
    # ------------------------------------------------------------------

    async def _deploy_update(self):
        # Load existing VM info from DB
        preview = await get_preview(self.project_id, self.preview_name)
        self._vm_id = preview.get("vm_id") if preview else None
        self._vm_ip = preview.get("vm_ip") if preview else None

        if not self._vm_id:
            # No VM — treat as new deploy
            raise RuntimeError("No VM found for update deploy — use rebuild with force_new")

        self._executor = RemoteExecutor(self._vm_ip)

        # Sync updated code from coordinator to VM
        await self._step_sync_code()
        await self._check_cancelled()

        # Generate and upload compose + settings
        await self._generate_compose()
        self._write_internal_settings()
        await self._upload_compose_and_settings()

        # Docker up + deploy (images already present from initial deploy)
        await self._docker_up()
        await self._check_cancelled()
        await self._run_deploy_steps("update")
        await self._run_project_deploy_script("update")

        # Activate Redis cache backend (phase 2)
        await self._activate_redis_cache()

        await self._reload_webserver()

        # Done — traffic is proxied via wake_preview middleware

    # ------------------------------------------------------------------
    # Cloud infrastructure steps
    # ------------------------------------------------------------------

    async def _step_create_vm(self, name: str):
        step = "create-vm"
        await self._log_step_start(step)
        await self._log_raw(f"{DIM}Provisioning new VM. This usually takes 2-3 minutes...{RESET}\n")
        t0 = time.monotonic()

        # Start VM creation and poll progress
        create_task = asyncio.ensure_future(cloud_manager.create_vm(name, project_id=self.project_id, preview_name=self.preview_name))
        last_elapsed = 0
        while not create_task.done():
            await asyncio.sleep(10)
            elapsed = int(time.monotonic() - t0)
            if elapsed > last_elapsed:
                await self._log_raw(f"\r{DIM}  Waiting... {elapsed}s elapsed{RESET}\n")
                last_elapsed = elapsed

        try:
            server = create_task.result()
        except RuntimeError as e:
            elapsed = time.monotonic() - t0
            await self._log_step_end(step, elapsed, False, f"{RED}{e}{RESET}")
            raise

        elapsed = time.monotonic() - t0
        ip = server.data_model.public_net.ipv4.ip
        await self._log_step_end(step, elapsed, True, f"{DIM}IP: {ip}{RESET}")
        return server

    async def _step_wait_ssh(self):
        step = "wait-ssh"
        await self._log_step_start(step)
        t0 = time.monotonic()
        await cloud_manager.wait_for_vm_ready(self._vm_id, timeout=300)
        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, True, f"{DIM}SSH ready{RESET}")

    async def _step_setup_vm(self):
        """Create workspace directory on the VM and ensure required tools."""
        step = "setup-vm"
        await self._log_step_start(step)
        t0 = time.monotonic()

        setup_cmd = (
            f"mkdir -p {VM_PREVIEW_DIR}/code && "
            f"(which aws >/dev/null 2>&1 || pip3 install -q awscli --break-system-packages) && "
            f"(which pv >/dev/null 2>&1 || apt-get install -yqq pv >/dev/null 2>&1)"
        )

        proc = await self._executor.run_shell(setup_cmd)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"VM setup failed: {stderr.decode().strip()}")

        # Storage Box backend: copy SSH key to VM for SCP access
        if settings.storage_backend == "storagebox" and settings.storagebox_ssh_key_path:
            import base64
            key_data = open(settings.storagebox_ssh_key_path, "rb").read()
            key_b64 = base64.b64encode(key_data).decode()
            vm_key_path = "/root/.ssh/storagebox_key"
            cmd = f"echo '{key_b64}' | base64 -d > {vm_key_path} && chmod 600 {vm_key_path} >/dev/null 2>&1"
            proc = await self._executor.run_shell(cmd)
            await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError("Failed to deploy storage key to VM")

        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, True, "")

    async def _step_pull_images(self):
        """Configure VM to use private registry and pull images."""
        if not settings.docker_registry:
            return

        step = "pull-images"
        t0 = time.monotonic()

        # Configure Docker on VM to allow insecure registry (HTTP) — only if not already configured
        registry_host = settings.docker_registry
        check = await self._executor.run_shell(
            f"grep -q '{registry_host}' /etc/docker/daemon.json 2>/dev/null && echo OK || echo MISSING"
        )
        stdout, _ = await check.communicate()
        if b"MISSING" in stdout:
            daemon_cfg = f'{{"insecure-registries": ["{registry_host}"]}}'
            await self._executor.run_shell(
                f"echo '{daemon_cfg}' > /etc/docker/daemon.json && systemctl restart docker"
            )
            await asyncio.sleep(3)

        # Pull all images via docker compose
        pull_cmd = f"cd {VM_PREVIEW_DIR}/code && docker compose pull --quiet"
        await self._run_remote_shell(pull_cmd, step, timeout=TIMEOUT_DOCKER_UP)

    async def _step_sync_code(self, *, delete: bool = False):
        """Sync code from coordinator to VM via rsync (no git clone on VM needed)."""
        step = "sync-code"
        await self._log_step_start(step)
        t0 = time.monotonic()

        code_dir = f"{VM_PREVIEW_DIR}/code"

        await self._log_raw(
            f"{DIM}  rsync {self.preview_path} → {self._vm_ip}:{code_dir}{RESET}\n"
        )

        # Ensure target directory exists
        proc = await self._executor.run_shell(f"mkdir -p {code_dir}")
        await proc.communicate()

        await self._executor.rsync_to(str(self.preview_path), code_dir, delete=delete)

        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, True, "")


    async def _upload_compose_and_settings(self):
        """Upload docker-compose.yml and settings files to the VM."""
        step = "upload-config"
        await self._log_step_start(step)
        t0 = time.monotonic()

        compose_file = self.preview_path / "docker-compose.yml"
        if compose_file.exists():
            await self._executor.upload_file(
                str(compose_file),
                f"{VM_PREVIEW_DIR}/code/docker-compose.yml",
            )

        # Upload settings files
        docroot = self._preview_config.get("docroot", "web") if self._preview_config else "web"
        settings_dir = self.preview_path / docroot / "sites" / "default"

        for fname in ("settings.preview.internal.php", "settings.php"):
            local_file = settings_dir / fname
            if local_file.exists():
                remote_dir = f"{VM_PREVIEW_DIR}/code/{docroot}/sites/default"
                # Ensure remote dir exists
                await self._executor.run_shell(f"mkdir -p {remote_dir}")
                proc = await (await self._executor.run_shell(f"mkdir -p {remote_dir}")).communicate() if False else None
                await self._executor.upload_file(
                    str(local_file),
                    f"{remote_dir}/{fname}",
                )

        # Write .env with TERMINAL_SECRET for the terminal sidecar
        import hashlib
        import hmac as hmac_mod
        terminal_secret = hmac_mod.new(
            settings.secret_key.encode(),
            f"terminal:{self.org_slug}:{self.project_slug}:{self.preview_name}".encode(),
            hashlib.sha256,
        ).hexdigest()
        env_cmd = f"echo 'TERMINAL_SECRET={terminal_secret}' > {VM_PREVIEW_DIR}/code/.env"
        proc = await self._executor.run_shell(env_cmd)
        await proc.communicate()

        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, True, "")

    # ------------------------------------------------------------------
    # Docker steps (executed on VM via SSH)
    # ------------------------------------------------------------------

    async def _docker_up(self):
        await self._run_remote(
            "docker", "compose", "up", "-d", "--pull", "missing",
            step="docker-up",
            timeout=TIMEOUT_DOCKER_UP,
            cwd=f"{VM_PREVIEW_DIR}/code",
        )

    async def _wait_for_db(self):
        """Wait for MySQL to be ready on the VM."""
        step = "wait-for-db"
        await self._log_step_start(step)
        t0 = time.monotonic()

        db_container = f"{self.container_prefix}-db"
        for attempt in range(30):
            attempt_num = attempt + 1
            # Root ping
            proc = await self._executor.run_shell(
                f"docker exec {db_container} mysqladmin ping -h localhost -u root -proot 2>/dev/null"
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                await self._log_raw(
                    f"{DIM}  [{attempt_num}/30] Waiting for MySQL to accept connections...{RESET}\n"
                )
                await asyncio.sleep(2)
                continue

            # App user check
            proc = await self._executor.run_shell(
                f"docker exec -e MYSQL_PWD=drupal {db_container} mysql -u drupal -e 'SELECT 1' drupal 2>/dev/null"
            )
            await proc.communicate()
            if proc.returncode == 0:
                elapsed = time.monotonic() - t0
                await self._log_step_end(
                    step, elapsed, True,
                    f"{DIM}MySQL ready after {attempt_num} attempt(s){RESET}",
                )
                return

            await self._log_raw(
                f"{DIM}  [{attempt_num}/30] MySQL up, waiting for user 'drupal' to be ready...{RESET}\n"
            )
            await asyncio.sleep(2)

        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, False, "MySQL not ready after 60s")
        raise RuntimeError("[wait-for-db] MySQL not ready after 60s")

    async def _composer_install(self):
        env = {}
        if self.org_id:
            from app.database import get_organization_by_id
            from config.settings import settings as app_settings
            org = await get_organization_by_id(self.org_id)
            proxy_enabled = org.get("composer_proxy_enabled", 0) if org else 0
            proxy_url = app_settings.composer_proxy_url
            if proxy_enabled and proxy_url:
                env["HTTPS_PROXY"] = proxy_url
                env["HTTP_PROXY"] = proxy_url
                display_url = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
                await self._log_raw(f"{DIM}Using composer proxy: {display_url}{RESET}\n")
        await self._docker_exec(
            "composer", "install", "--no-interaction", "--no-progress",
            step="composer-install",
            timeout=TIMEOUT_COMPOSER,
            env=env,
        )

    async def _import_db(self):
        """Download base DB from storage and stream directly into MySQL."""
        step = "import-db"
        await self._log_step_start(step)
        t0 = time.monotonic()

        db_container = f"{self.container_prefix}-db"
        storage_key = f"base-files/{self.project_slug}/db.sql.gz"

        # Log file size info
        size_bytes = 0
        status = await storage_manager.get_base_files_status(self.project_slug)
        if status.get("db"):
            size_bytes = status["db"].get("size_bytes", 0)
            size_mb = size_bytes / (1024 * 1024)
            await self._log_raw(f"{DIM}Dump size: {size_mb:.1f} MB (compressed){RESET}\n")

        await self._log_raw(f"{DIM}Downloading and importing database...{RESET}\n")

        download_cmd = storage_manager.vm_download_to_stdout(storage_key)
        use_pv = storage_manager.supports_presigned_urls
        if use_pv:
            size_flag = f"-s {size_bytes}" if size_bytes else ""
            import_cmd = (
                f"{download_cmd} "
                f"| pv {size_flag} "
                f"| gunzip "
                f"| docker exec -e MYSQL_PWD=drupal -i {db_container} mysql -u drupal drupal"
            )
        else:
            import_cmd = (
                f"{download_cmd} "
                f"| gunzip "
                f"| docker exec -e MYSQL_PWD=drupal -i {db_container} mysql -u drupal drupal"
            )

        proc = await self._executor.run_shell(import_cmd, pty=use_pv)
        stdout, stderr = await self._stream_progress(proc, step, t0, TIMEOUT_IMPORT_DB)
        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            await self._log_step_end(step, elapsed, False, "")
            output = (stdout + stderr)[-2000:]
            raise RuntimeError(f"[{step}] Failed (exit {proc.returncode}):\n{output}")

        await self._log_step_end(step, elapsed, True, "")

    async def _import_files(self):
        """Download base files from storage to VM and extract."""
        has_files = await storage_manager.get_base_files_uncompressed_size(self.project_slug)
        if not has_files:
            # Check if files exist in S3 at all
            status = await storage_manager.get_base_files_status(self.project_slug)
            if not status.get("files"):
                docroot = self._preview_config.get("docroot", "web") if self._preview_config else "web"
                public_path = "sites/default/files"
                if self._preview_config:
                    public_path = self._preview_config.get("env", {}).get("PREV_FILE_PUBLIC_PATH", public_path)
                mkdir_cmd = f"mkdir -p {VM_PREVIEW_DIR}/code/{docroot}/{public_path}"
                proc = await self._executor.run_shell(mkdir_cmd)
                await proc.communicate()
                await self._log_raw(f"{DIM}No base files found — created empty directory{RESET}\n")
                return

        step = "import-files"
        await self._log_step_start(step)
        t0 = time.monotonic()

        storage_key = f"base-files/{self.project_slug}/files.tar.gz"
        docroot = self._preview_config.get("docroot", "web") if self._preview_config else "web"
        public_path = "sites/default/files"
        if self._preview_config:
            public_path = self._preview_config.get("env", {}).get("PREV_FILE_PUBLIC_PATH", public_path)

        files_dir = f"{VM_PREVIEW_DIR}/code/{docroot}/{public_path}"

        # Log file size info
        size_bytes = 0
        status = await storage_manager.get_base_files_status(self.project_slug)
        if status.get("files"):
            size_bytes = status["files"].get("size_bytes", 0)
            size_mb = size_bytes / (1024 * 1024)
            await self._log_raw(f"{DIM}Archive size: {size_mb:.1f} MB{RESET}\n")

        await self._log_raw(f"{DIM}Downloading and extracting files...{RESET}\n")

        download_cmd = storage_manager.vm_download_to_stdout(storage_key)
        use_pv = storage_manager.supports_presigned_urls
        if use_pv:
            size_flag = f"-s {size_bytes}" if size_bytes else ""
            pv_part = f"| pv {size_flag} "
        else:
            pv_part = ""
        import_cmd = (
            f"mkdir -p {files_dir} && "
            f"{download_cmd} "
            f"{pv_part}"
            f"| tar xzf - -C {files_dir} && "
            f"chown -R 33:33 {files_dir} && "
            f"chmod -R a+rX {files_dir} && "
            f"echo \"Extracted $(find {files_dir} -type f | wc -l) files\""
        )
        proc = await self._executor.run_shell(import_cmd, pty=use_pv)
        stdout, stderr = await self._stream_progress(proc, step, t0, TIMEOUT_IMPORT_FILES)
        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            # Clean up partial extraction to free disk space
            cleanup_cmd = f"rm -rf {files_dir}"
            cleanup_proc = await self._executor.run_shell(cleanup_cmd)
            await cleanup_proc.communicate()
            await self._log_raw(f"{DIM}Cleaned up partial files to free disk space{RESET}\n")

            await self._log_step_end(step, elapsed, False, "")
            output = (stdout + stderr)[-2000:]
            raise RuntimeError(f"[{step}] Import failed (exit {proc.returncode}):\n{output}")

        await self._log_step_end(step, elapsed, True, "")

    async def _restore_db_cache(self, cache_key: str):
        """Download DB cache from storage to VM and restore the Docker volume."""
        step = "restore-db-cache"
        await self._log_step_start(step)
        t0 = time.monotonic()

        storage_key = f"db-cache/{self.project_slug}/{cache_key}.tar.gz"
        volume_name = f"{self.container_prefix}_db_data"

        download_cmd = storage_manager.vm_download_to_file(storage_key, "/tmp/db-cache.tar.gz")
        restore_cmd = (
            f"docker volume create {volume_name} && "
            f"{download_cmd} && "
            f"docker run --rm -v {volume_name}:/data -v /tmp:/cache alpine "
            f"tar xzf /cache/db-cache.tar.gz -C /data && "
            f"rm -f /tmp/db-cache.tar.gz"
        )

        proc = await self._executor.run_shell(restore_cmd)
        stdout, stderr = await self._stream_progress(proc, step, t0, 120)
        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            await self._log_step_end(step, elapsed, False, "")
            raise RuntimeError(f"[{step}] Failed to restore DB cache")

        await self._log_step_end(step, elapsed, True, f"{DIM}Restored from cache{RESET}")

    async def _create_db_cache(self, cache_key: str):
        """Export DB volume on VM and upload to storage cache."""
        step = "create-db-cache"
        await self._log_step_start(step)
        t0 = time.monotonic()

        db_container = f"{self.container_prefix}-db"
        volume_name = f"{self.container_prefix}_db_data"

        # Stop DB for clean snapshot
        proc = await self._executor.run_shell(f"docker stop {db_container}")
        await proc.communicate()

        try:
            storage_key = f"db-cache/{self.project_slug}/{cache_key}.tar.gz"
            upload_cmd = storage_manager.vm_upload_from_file("/tmp/db-cache.tar.gz", storage_key)
            export_cmd = (
                f"docker run --rm -v {volume_name}:/data:ro -v /tmp:/cache alpine "
                f"tar czf /cache/db-cache.tar.gz -C /data . && "
                f"{upload_cmd} && "
                f"rm -f /tmp/db-cache.tar.gz"
            )
            proc = await self._executor.run_shell(export_cmd)
            stdout, stderr = await self._stream_progress(proc, step, t0, 300)

            if proc.returncode != 0:
                logger.warning("Failed to create DB cache, continuing anyway")
        finally:
            # Restart DB
            proc = await self._executor.run_shell(f"docker start {db_container}")
            await proc.communicate()
            await self._wait_for_db()

        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, True, f"{DIM}Cached for future previews{RESET}")

    async def _activate_redis_cache(self):
        """Phase 2: activate Redis as cache backend after deploy completes.

        During deploy, Drupal uses DB cache so all cache tables are created.
        After deploy, we create a flag file and rebuild cache so Redis takes over.
        """
        config = getattr(self, "_preview_config", None)
        if not config:
            return

        # Check if redis or valkey is enabled
        has_redis = config.get("services", {}).get("redis")
        has_valkey = config.get("services", {}).get("valkey")
        if not has_redis and not has_valkey:
            return

        php_container = f"{self.container_prefix}-php"
        step = "activate-redis"
        await self._log_step_start(step)
        t0 = time.monotonic()

        try:
            # Create flag file so settings.php enables Redis as cache backend
            proc = await self._executor.run_shell(
                f"docker exec {php_container} touch /tmp/.preview_redis_ready"
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)

            # Rebuild cache with Redis now active
            docroot = config.get("docroot", "web")
            proc = await self._executor.run_shell(
                f"docker exec {php_container} bash -c "
                f"'cd /var/www/html && vendor/bin/drush cr'"
            )
            stdout, stderr = await self._stream_progress(proc, step, t0, 120)
            elapsed = time.monotonic() - t0

            if proc.returncode != 0:
                # Non-fatal — Redis will activate on next cache rebuild
                await self._log_step_end(step, elapsed, True, f"{YELLOW}drush cr failed, Redis will activate on next rebuild{RESET}")
                return

            await self._log_step_end(step, elapsed, True, "")
        except Exception as e:
            elapsed = time.monotonic() - t0
            await self._log_step_end(step, elapsed, True, f"{YELLOW}Skipped: {e}{RESET}")

    async def _reload_webserver(self):
        """Send graceful restart to LiteSpeed."""
        php_container = f"{self.container_prefix}-php"
        try:
            proc = await self._executor.run_shell(
                f"docker exec {php_container} /usr/local/lsws/bin/lswsctrl restart 2>/dev/null"
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception as e:
            logger.warning(f"[reload-webserver] Failed (non-fatal): {e}")

    async def _run_project_deploy_script(self, phase: str):
        """Run the project deploy script for a phase (new/update)."""
        config = getattr(self, "_preview_config", None)
        deploy_path = config["deploy"][phase] if config else None

        if not deploy_path:
            return

        logger.info(f"Running deploy script ({phase}): {deploy_path}")
        await self._docker_exec(
            "bash", f"/var/www/html/{deploy_path}",
            step=f"project-deploy-script-{phase}",
            timeout=TIMEOUT_DEPLOY_SCRIPT,
            pty=True,
        )

    async def _run_project_post_deploy_inline(self, phase: str):
        """Run post-deploy script inline (same deployment record).

        Non-fatal: a failure is logged but does not fail the deploy.
        """
        config = getattr(self, "_preview_config", None)
        deploy_path = config["post_deploy"][phase] if config else None

        if not deploy_path:
            return

        logger.info(f"Running post-deploy script ({phase}): {deploy_path}")
        await self._update_post_deploy_status("running")

        try:
            await self._log_raw(
                f"\n{BOLD}{CYAN}POST-DEPLOY ({phase}){RESET}\n"
                f"{DIM}Script: {deploy_path}{RESET}\n"
            )
            await self._docker_exec(
                "bash", f"/var/www/html/{deploy_path}",
                step=f"post-deploy-{phase}",
                timeout=TIMEOUT_POST_DEPLOY,
                pty=True,
            )
            await self._update_post_deploy_status("success")
            logger.info(f"Post-deploy OK: {self.project_slug}/{self.preview_name}")
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Post-deploy script ({phase}) failed (non-fatal): {error_msg}")
            # Show the last part of the error output so the user can see what went wrong
            error_lines = error_msg.split("\n")
            if len(error_lines) > 1:
                # The error includes captured output — show it
                await self._log_raw(f"\n{YELLOW}⚠ Post-deploy script failed (non-fatal):{RESET}\n")
                for line in error_lines[1:]:
                    if line.strip():
                        await self._log_raw(f"{DIM}{line}{RESET}\n")
            else:
                await self._log_raw(f"\n{YELLOW}⚠ Post-deploy script failed (non-fatal): {error_msg}{RESET}\n")
            await self._update_post_deploy_status("failed")

    async def _run_project_post_deploy_script(self, phase: str):
        """Run the project post-deploy script for a phase (new/update).
        Called standalone (e.g. manual re-run from UI).
        Creates its own deployment record with separate logs.
        Non-fatal: a failure here is logged but does not fail the deploy."""
        config = getattr(self, "_preview_config", None)
        deploy_path = config["post_deploy"][phase] if config else None

        if not deploy_path:
            return

        await self._run_post_deploy_with_record(phase, deploy_path)

    async def _run_post_deploy_with_record(self, phase: str, deploy_path: str):
        """Execute post-deploy script with its own deployment record and log streaming."""
        from app.websockets import deployment_log_broadcaster, preview_list_manager

        logger.info(f"Running post-deploy script ({phase}): {deploy_path}")

        # Create a separate deployment record for post-deploy
        preview = await get_preview(self.project_id, self.preview_name)
        if not preview:
            return
        post_deploy_id = await create_deployment(
            preview["id"], self.triggered_by, deploy_type="post_deploy"
        )
        await deployment_log_broadcaster.register(post_deploy_id)
        await preview_list_manager.force_broadcast()

        # Use a separate log buffer for post-deploy
        post_log_buffer: list[str] = []
        original_log_buffer = self._log_buffer
        self._log_buffer = post_log_buffer
        original_deployment_id = self._deployment_id
        self._deployment_id = post_deploy_id

        await self._update_post_deploy_status("running")
        start = datetime.now(timezone.utc)

        try:
            await self._log_raw(
                f"\n{BOLD}{CYAN}POST-DEPLOY ({phase}): {self.project_slug}/{self.preview_name}{RESET}\n"
                f"{DIM}Script: {deploy_path}{RESET}\n"
            )
            await self._docker_exec(
                "bash", f"/var/www/html/{deploy_path}",
                step=f"post-deploy-{phase}",
                timeout=TIMEOUT_POST_DEPLOY,
                pty=True,
            )
            duration = int((datetime.now(timezone.utc) - start).total_seconds())
            await self._update_post_deploy_status("success")
            await finish_deployment(
                post_deploy_id, "success",
                log_output="\n".join(post_log_buffer),
                duration=duration,
            )
            await deployment_log_broadcaster.complete(post_deploy_id, True)
            logger.info(f"Post-deploy OK: {self.project_slug}/{self.preview_name} in {duration}s")
        except Exception as e:
            duration = int((datetime.now(timezone.utc) - start).total_seconds())
            logger.warning(f"Post-deploy script ({phase}) failed (non-fatal): {e}")
            await self._log_raw(f"\n{YELLOW}⚠ Post-deploy script failed: {e}{RESET}\n")
            await self._update_post_deploy_status("failed")
            await finish_deployment(
                post_deploy_id, "failed",
                log_output="\n".join(post_log_buffer),
                error=str(e),
                duration=duration,
            )
            await deployment_log_broadcaster.complete(post_deploy_id, False)
        finally:
            # Restore original log buffer and deployment ID
            self._log_buffer = original_log_buffer
            self._deployment_id = original_deployment_id

    async def _update_post_deploy_status(self, status: str | None):
        """Update the post_deploy_status field on the preview."""
        try:
            await PreviewStateManager.save_state(
                self.project_id, self.preview_name,
                post_deploy_status=status,
            )
            from app.valkey import publish_event
            await publish_event("previews:global", {
                "action": "state_change",
                "preview_name": self.preview_name,
                "project_slug": self.project_slug,
                "post_deploy_status": status,
            })
        except Exception:
            pass

    async def _run_deploy_steps(self, phase: str):
        """Run *.sh scripts from deploy-steps/{phase}/ on the VM."""
        steps_dir = DEPLOY_STEPS_DIR / phase
        if not steps_dir.is_dir():
            return

        scripts = sorted(steps_dir.glob("*.sh"))
        if not scripts:
            return

        logger.info(f"Running {len(scripts)} deploy step(s) from {phase}/")

        for script in scripts:
            # Upload script to VM and execute
            remote_script = f"/tmp/deploy-step-{script.name}"
            await self._executor.upload_file(str(script), remote_script)

            env_vars = self._build_step_env(phase)
            env_export = " ".join(f"{k}='{v}'" for k, v in env_vars.items())

            proc = await self._executor.run_shell(
                f"export {env_export} && bash {remote_script}",
                cwd=f"{VM_PREVIEW_DIR}/code",
                pty=True,
            )
            stdout, stderr = await self._stream_progress(
                proc, f"deploy-step-{phase}/{script.name}",
                time.monotonic(), TIMEOUT_DEPLOY_STEP,
            )
            if proc.returncode != 0:
                output = (stdout + stderr)[-2000:]
                raise RuntimeError(
                    f"[deploy-step-{phase}/{script.name}] Failed (exit {proc.returncode}):\n{output}"
                )

    def _build_step_env(self, phase: str) -> dict:
        """Build environment variables passed to deploy step scripts."""
        return {
            "PREV_ORG_SLUG": self.org_slug,
            "PREV_PROJECT_NAME": self.project_slug,
            "PREV_PREVIEW_NAME": self.preview_name,
            "PREV_MR_IID": str(self.mr_iid) if self.mr_iid else "",
            "PREV_PATH": f"{VM_PREVIEW_DIR}/code",
            "PREV_URL": self.preview_url,
            "PREV_CONTAINER_PREFIX": self.container_prefix,
            "PREV_BRANCH": self.branch,
            "PREV_COMMIT_SHA": self.commit_sha,
            "PREV_PHASE": phase,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _docker_exec(self, *cmd: str, step: str, timeout: int = 120, env: dict[str, str] | None = None, pty: bool = True) -> str:
        """Run a command inside the PHP container on the VM.

        PTY is enabled by default so commands see a real terminal
        (colors, progress bars, line-buffered output).
        """
        php_container = f"{self.container_prefix}-php"
        # GIT_CONFIG vars tell git to trust /var/www/html (avoids "dubious ownership" with PTY)
        docker_flags = (
            "-t -w /var/www/html -e COLUMNS=200"
            " -e GIT_CONFIG_COUNT=1"
            " -e GIT_CONFIG_KEY_0=safe.directory"
            " -e GIT_CONFIG_VALUE_0=/var/www/html"
            if pty else
            "-w /var/www/html -e COLUMNS=200"
        )
        if env:
            for k, v in env.items():
                docker_flags += f" -e {k}={v}"
        shell_cmd = f"docker exec {docker_flags} {php_container} {' '.join(cmd)}"
        return await self._run_remote_shell(shell_cmd, step=step, timeout=timeout, pty=pty)

    async def _run_remote(self, *cmd: str, step: str, timeout: int = 120, cwd: str | None = None) -> str:
        """Run a command on the VM via SSH. Raises on failure."""
        logger.info(f"[{step}] Running on VM: {' '.join(cmd)}")
        await self._log_step_start(step)
        t0 = time.monotonic()

        proc = await self._executor.run(*cmd, cwd=cwd)

        try:
            stdout, stderr = await self._stream_progress(proc, step, t0, timeout)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            await self._log_step_end(step, elapsed, False, f"{RED}TIMEOUT after {timeout}s{RESET}")
            raise RuntimeError(f"[{step}] Timed out after {timeout}s")

        elapsed = time.monotonic() - t0
        output = stdout + stderr

        if proc.returncode != 0:
            await self._log_step_end(step, elapsed, False, "")
            raise RuntimeError(
                f"[{step}] Failed (exit {proc.returncode}):\n{output[-2000:]}"
            )

        await self._log_step_end(step, elapsed, True, "")
        logger.info(f"[{step}] OK ({_fmt_duration(elapsed)})")
        return output

    async def _run_remote_shell(self, cmd: str, step: str, timeout: int = 120, pty: bool = False) -> str:
        """Run a shell command on the VM via SSH. Raises on failure.

        When pty=True, allocates a pseudo-terminal so the remote command
        sees an interactive terminal (colors, progress bars, line buffering).
        """
        logger.info(f"[{step}] Running on VM: {cmd}")
        await self._log_step_start(step)
        t0 = time.monotonic()

        proc = await self._executor.run_shell(cmd, cwd=f"{VM_PREVIEW_DIR}/code", pty=pty)

        try:
            stdout, stderr = await self._stream_progress(proc, step, t0, timeout)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            await self._log_step_end(step, elapsed, False, f"{RED}TIMEOUT after {timeout}s{RESET}")
            raise RuntimeError(f"[{step}] Timed out after {timeout}s")

        elapsed = time.monotonic() - t0
        output = stdout + stderr

        if proc.returncode != 0:
            await self._log_step_end(step, elapsed, False, "")
            raise RuntimeError(
                f"[{step}] Failed (exit {proc.returncode}):\n{output[-2000:]}"
            )

        await self._log_step_end(step, elapsed, True, "")
        logger.info(f"[{step}] OK ({_fmt_duration(elapsed)})")
        return output

    async def _verify_base_files(self):
        """Verify base DB exists in storage."""
        exists = await storage_manager.base_db_exists(self.project_slug)
        if not exists:
            raise RuntimeError(
                f"Base database not found for project '{self.project_slug}'. "
                f"Upload with: preview push db"
            )

    def _write_internal_settings(self):
        """Write settings.preview.internal.php, drush aliases, and ensure settings.php includes it.

        Files are written locally and then uploaded to the VM.
        """
        docroot = self._preview_config.get("docroot", "web") if self._preview_config else "web"
        settings_dir = self.preview_path / docroot / "sites" / "default"
        settings_dir.mkdir(parents=True, exist_ok=True)

        # 1. Generate drush site aliases for domain aliases (preview.site.yml)
        alias_prefixes = self._preview_config.get("domain_aliases", []) if self._preview_config else []
        if alias_prefixes:
            drush_sites_dir = self.preview_path / "drush" / "sites"
            drush_sites_dir.mkdir(parents=True, exist_ok=True)
            aliases = {}
            for prefix in alias_prefixes:
                alias_domain = f"{prefix}--{self.domain}"
                aliases[prefix] = {"uri": f"https://{alias_domain}"}
            import yaml as _yaml
            alias_file = drush_sites_dir / "preview.site.yml"
            alias_file.write_text(
                "# Managed by Preview Manager — overwritten on every deploy.\n"
                + _yaml.dump(aliases, default_flow_style=False, sort_keys=False)
            )
            logger.info(f"Wrote drush aliases: {', '.join(f'@preview.{p}' for p in alias_prefixes)}")

        # 2. Write settings.preview.internal.php
        internal = settings_dir / "settings.preview.internal.php"
        internal.write_text("""\
<?php

/**
 * @file
 * Internal preview environment settings — managed by Preview Manager.
 *
 * DO NOT EDIT. This file is overwritten on every deploy.
 * Use settings.preview.php for custom overrides.
 */

// Database connection.
$databases['default']['default'] = [
  'database' => getenv('PREV_DB_NAME'),
  'username' => getenv('PREV_DB_USER'),
  'password' => getenv('PREV_DB_PASSWORD'),
  'host' => getenv('PREV_DB_HOST'),
  'port' => '3306',
  'driver' => 'mysql',
  'prefix' => '',
  'collation' => 'utf8mb4_general_ci',
  'pdo' => [
    \\PDO::MYSQL_ATTR_SSL_VERIFY_SERVER_CERT => FALSE,
  ],
];

// Trusted host patterns — allow the preview domain and any aliases.
$settings['trusted_host_patterns'][] = '^' . preg_quote(getenv('PREV_DOMAIN')) . '$';
if (getenv('PREV_DOMAIN_ALIASES')) {
  foreach (explode(',', getenv('PREV_DOMAIN_ALIASES')) as $_alias) {
    $settings['trusted_host_patterns'][] = '^' . preg_quote(trim($_alias)) . '$';
  }
}

// Reverse proxy — Drupal is behind Caddy + Python middleware.
// Without this, Drupal ignores X-Forwarded-Proto and sees HTTP,
// causing session/CSRF issues with form tokens.
$settings['reverse_proxy'] = TRUE;
$settings['reverse_proxy_addresses'] = ['172.16.0.0/12', '10.0.0.0/8', '127.0.0.1'];

// File system paths.
$settings['file_public_path'] = getenv('PREV_FILE_PUBLIC_PATH');
$settings['file_private_path'] = getenv('PREV_FILE_PRIVATE_PATH');
$settings['file_temp_path'] = getenv('PREV_FILE_TEMP_PATH');
$config['locale.settings']['translation']['path'] = getenv('PREV_FILE_TRANSLATIONS_PATH');

// Config sync directory — auto-detect common locations.
foreach (['../config/sync', '../config', 'sites/default/config/sync'] as $_candidate) {
  if (is_dir(DRUPAL_ROOT . '/' . $_candidate)) {
    $settings['config_sync_directory'] = $_candidate;
    break;
  }
}

// Hash salt — override if not already set upstream.
if (empty($settings['hash_salt'])) {
  $settings['hash_salt'] = getenv('PREV_PROJECT_NAME') . '-preview';
}

// Redis / Valkey cache backend — two-phase activation.
// Phase 1 (during deploy): connection settings are configured so the module works,
//   but Redis is NOT the default cache backend. Drupal uses DB cache, ensuring all
//   cache tables are created properly when new modules are enabled.
// Phase 2 (after deploy): the deployer creates /tmp/.preview_redis_ready, and on the
//   next request Redis becomes the default cache backend.
$_redis_host = getenv('PREV_REDIS_HOST');
if ($_redis_host) {
  $settings['redis.connection']['interface'] = 'PhpRedis';
  $settings['redis.connection']['host'] = $_redis_host;
  $settings['redis.connection']['port'] = 6379;
  // Only activate Redis as default cache backend after deploy completes.
  if (file_exists('/tmp/.preview_redis_ready')) {
    if (isset($databases['default']['default'])) {
      try {
        $_db = $databases['default']['default'];
        $_pdo = new \\PDO(
          "mysql:host={$_db['host']};port={$_db['port']};dbname={$_db['database']}",
          $_db['username'],
          $_db['password']
        );
        $_result = $_pdo->query("SELECT 1 FROM key_value WHERE collection = 'system.schema' AND name = 'redis' LIMIT 1");
        if ($_result && $_result->fetch()) {
          $settings['cache']['default'] = 'cache.backend.redis';
          $_redis_services = DRUPAL_ROOT . '/modules/contrib/redis/example.services.yml';
          if (file_exists($_redis_services)) {
            $settings['container_yamls'][] = $_redis_services;
          }
        }
      } catch (\\Exception $e) {
        // DB not ready yet — skip Redis cache backend.
      }
    }
  }
}
""")
        logger.info(f"Wrote {internal}")

        # 2. Ensure settings.php has the preview include snippet
        settings_php = settings_dir / "settings.php"
        snippet = """
// Preview environment settings.
if (getenv('PREV_IS_PREVIEW')) {
  include __DIR__ . '/settings.preview.internal.php';
  if (file_exists(__DIR__ . '/settings.preview.php')) {
    include __DIR__ . '/settings.preview.php';
  }
}
"""
        if settings_php.exists():
            content = settings_php.read_text()
            if "PREV_IS_PREVIEW" not in content:
                settings_php.write_text(content.rstrip() + "\n" + snippet)
            elif "settings.preview.internal.php" not in content:
                old_include = "include __DIR__ . '/settings.preview.php';"
                new_include = (
                    "include __DIR__ . '/settings.preview.internal.php';\n"
                    "  if (file_exists(__DIR__ . '/settings.preview.php')) {\n"
                    "    include __DIR__ . '/settings.preview.php';\n"
                    "  }"
                )
                content = content.replace(old_include, new_include)
                settings_php.write_text(content)
        else:
            settings_php.write_text("<?php\n" + snippet)

    async def _generate_compose(self):
        """Parse preview.yml and generate docker-compose.yml locally."""
        step = "configuring-env"
        await self._log_step_start(step)
        t0 = time.monotonic()

        config = parse_preview_yml(self.preview_path)

        yml_file = self.preview_path / "preview.yml"
        if not yml_file.exists() or "docroot" not in (
            __import__("yaml").safe_load(yml_file.read_text()) or {}
        ):
            config["docroot"] = detect_docroot(self.preview_path)

        self._preview_config = config

        # Load extra env vars
        extra_env: dict[str, str] = {}
        try:
            import json
            proj = await get_project(self.project_id)
            if proj and proj.get("env_vars"):
                project_env = proj["env_vars"]
                if isinstance(project_env, str):
                    project_env = json.loads(project_env)
                extra_env.update(project_env)

            preview_row = await get_preview(self.project_id, self.preview_name)
            if preview_row and preview_row.get("env_vars"):
                preview_env = preview_row["env_vars"]
                if isinstance(preview_env, str):
                    preview_env = json.loads(preview_env)
                extra_env.update(preview_env)
        except Exception as e:
            logger.warning(f"Error loading extra env vars: {e}")

        url_hash = compute_url_hash(self.org_slug, self.project_slug, self.preview_name)
        compose = generate_docker_compose(
            self.project_slug, self.preview_name, config,
            branch=self.branch, commit_sha=self.commit_sha,
            mr_iid=self.mr_iid,
            extra_env=extra_env if extra_env else None,
            url_hash=url_hash,
            org_slug=self.org_slug,
        )
        write_docker_compose(self.preview_path, compose)

        elapsed = time.monotonic() - t0
        info = f"php={config['php_version']} docroot={config['docroot']}"
        await self._log_step_end(step, elapsed, True, f"{DIM}{info}{RESET}")

    # ------------------------------------------------------------------
    # Log / stream helpers (same interface as before)
    # ------------------------------------------------------------------

    async def _log_raw(self, text: str):
        from app.websockets import deployment_log_broadcaster
        self._log_buffer.append(text)
        if self._deployment_id:
            await deployment_log_broadcaster.add_log(self._deployment_id, text)

    _STEP_LABELS: dict[str, str] = {
        "create-vm": "Creating virtual machine",
        "wait-ssh": "Waiting for SSH",
        "wait-agent": "Waiting for agent",
        "setup-vm": "Setting up VM",
        "pull-images": "Pulling Docker images",
        "sync-code": "Syncing code to VM",
        "upload-config": "Uploading configuration",
        "configuring-env": "Configuring environment",
        "docker-up": "Starting containers",
        "wait-for-db": "Waiting for database",
        "composer-install": "Installing dependencies",
        "import-db": "Importing database",
        "import-files": "Importing files",
        "restore-db-cache": "Restoring DB from cache",
        "create-db-cache": "Caching database",
        "activate-redis": "Activating Redis",
        "project-deploy-script-new": "Running deploy script",
        "project-deploy-script-update": "Running deploy script",
        "post-deploy-new": "Running post-deploy script",
        "post-deploy-update": "Running post-deploy script",
    }

    def _step_label(self, step: str) -> str:
        return self._STEP_LABELS.get(step, step)

    async def _log_step_start(self, step: str):
        await self._log_raw(f"\n{CYAN}⚙️ {self._step_label(step)}{RESET}\n")
        # Update phase tracker for coordinator steps
        phase_name = _COORDINATOR_STEP_TO_PHASE.get(step)
        if phase_name and self._phase_tracker:
            await self._phase_tracker.start_phase(phase_name)

    async def _log_step_end(self, step: str, duration: float, success: bool, output: str):
        label = self._step_label(step)
        dur_str = _fmt_duration(duration)
        if success:
            status_line = f"{GREEN}✓ {label}{RESET} {DIM}completed in {dur_str}{RESET}\n"
            self._step_timings.append((step, duration, "ok"))
        else:
            status_line = f"{RED}✗ {label}{RESET} {DIM}failed after {dur_str}{RESET}\n"
            self._step_timings.append((step, duration, "fail"))

        if output.strip():
            self._log_buffer.append(output.strip())
            from app.websockets import deployment_log_broadcaster
            if self._deployment_id:
                await deployment_log_broadcaster.add_log(
                    self._deployment_id, output.strip() + "\n"
                )

        await self._log_raw(status_line + "\n")

        # Update phase tracker for coordinator steps
        phase_name = _COORDINATOR_STEP_TO_PHASE.get(step)
        if phase_name and self._phase_tracker:
            await self._phase_tracker.end_phase(phase_name, success, duration)

    async def _log_summary(self, success: bool, total_duration: int, error: str | None = None):
        dur_str = _fmt_duration(total_duration)
        lines = [f"\n{BOLD}{'─' * 50}{RESET}\n"]

        if success:
            lines.append(f"{GREEN}{BOLD}✓ Deploy completed successfully in {dur_str}{RESET}\n")
        else:
            lines.append(f"{RED}{BOLD}✗ Deploy failed after {dur_str}{RESET}\n")
            if error:
                lines.append(f"{RED}  Error: {error}{RESET}\n")

        if self._step_timings:
            lines.append(f"\n{DIM}Step timings:{RESET}\n")
            for step_name, step_dur, step_status in self._step_timings:
                icon = f"{GREEN}✓{RESET}" if step_status == "ok" else f"{RED}✗{RESET}"
                lines.append(f"  {icon} {self._step_label(step_name)} {DIM}{_fmt_duration(step_dur)}{RESET}\n")

        lines.append(f"{BOLD}{'─' * 50}{RESET}\n")
        await self._log_raw("".join(lines))

    async def _stream_progress(self, proc, step: str, t0: float, timeout: int):
        """Read stdout/stderr, streaming output chunks in real-time."""
        stdout_chunks = []
        stderr_chunks = []
        pending_chunks = []

        async def _read(stream, buf):
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                buf.append(chunk)
                pending_chunks.append(chunk)

        read_task = asyncio.gather(
            _read(proc.stdout, stdout_chunks),
            _read(proc.stderr, stderr_chunks),
        )

        try:
            while not read_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(read_task), timeout=0.1)
                except asyncio.TimeoutError:
                    if time.monotonic() - t0 > timeout:
                        proc.kill()
                        await read_task
                        raise asyncio.TimeoutError()
                if pending_chunks:
                    text = b"".join(pending_chunks).decode(errors="replace")
                    # PTY mode produces \r\n line endings — normalize to \n
                    text = text.replace("\r\n", "\n")
                    pending_chunks.clear()
                    await self._log_raw(text)
        except asyncio.TimeoutError:
            raise

        if pending_chunks:
            text = b"".join(pending_chunks).decode(errors="replace")
            text = text.replace("\r\n", "\n")
            pending_chunks.clear()
            await self._log_raw(text)

        await read_task
        await proc.wait()
        return (
            b"".join(stdout_chunks).decode(errors="replace"),
            b"".join(stderr_chunks).decode(errors="replace"),
        )

    async def _save_state(
        self,
        status: str,
        *,
        error: str | None = None,
        duration: int | None = None,
    ):
        now = datetime.now(timezone.utc).isoformat()
        existing = await PreviewStateManager.load_state(self.project_id, self.preview_name)

        url_hash = compute_url_hash(self.org_slug, self.project_slug, self.preview_name)

        fields = {
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "status": status,
            "url_hash": url_hash,
            "url": self.preview_url,
            "path": str(self.preview_path),
        }

        if self.mr_iid is not None:
            fields["mr_id"] = self.mr_iid
        if self.mr_title is not None:
            fields["mr_title"] = self.mr_title
        if self.target_branch is not None:
            fields["target_branch"] = self.target_branch

        if not existing:
            # Preview was deleted while deploy was running — don't recreate it
            if status in ("failed", "active"):
                logger.warning(
                    f"Preview {self.project_slug}/{self.preview_name} was deleted during deploy, "
                    f"skipping state save (status={status})"
                )
                return
            fields["created_at"] = now

        if status == "active":
            fields["last_deployed_at"] = now
            # Save expose config so the middleware can route exposed services
            if self._preview_config:
                import json
                expose = self._preview_config.get("expose") or {}
                fields["expose_config"] = json.dumps(expose)
        if status in ("active", "failed"):
            fields["last_deployment_status"] = status
            fields["last_deployment_completed_at"] = now
            if error:
                fields["last_deployment_error"] = error
            if duration is not None:
                fields["last_deployment_duration"] = duration

        await PreviewStateManager.save_state(
            self.project_id, self.preview_name, **fields
        )

        # Notify WebSocket layer via Valkey pub/sub
        try:
            from app.valkey import publish_event
            await publish_event("previews:global", {
                "action": "state_change",
                "preview_name": self.preview_name,
                "project_slug": self.project_slug,
                "status": status,
            })
        except Exception:
            pass  # Valkey may not be available
