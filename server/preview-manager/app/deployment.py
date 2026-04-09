"""Preview deployment logic — deploy previews on ephemeral Hetzner Cloud VMs."""

import asyncio
import hashlib
import hmac as hmac_mod
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.error
from urllib.parse import urlparse

from app.state import PreviewStateManager
from app.database import (
    get_preview, get_project, create_deployment, finish_deployment,
    update_preview_vm, compute_url_hash,
)
from app.caddy_api import caddy_manager
from app.cloud import cloud_manager
from app.remote import RemoteExecutor
from config.settings import settings

logger = logging.getLogger(__name__)


class DeployCancelled(Exception):
    """Raised when a deploy is cancelled by a new rebuild request."""
    pass

# Timeouts per step (seconds)
TIMEOUT_DEPLOY_SCRIPT = 36000
TIMEOUT_POST_DEPLOY = 18000  # 5 hours — post-deploy can run heavy tasks (indexing, etc.)

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
        {"name": "deploy_script",        "label": "Running deploy script",   "required": True},
        {"name": "restart_webserver",    "label": "Restarting webserver",    "required": True},
        {"name": "post_deploy",          "label": "Running post-deploy",    "required": False},
    ],
    "update": [
        {"name": "provision_agent",   "label": "Provisioning agent",      "required": True},
        {"name": "wait_agent",        "label": "Waiting for agent",       "required": True},
        {"name": "git_fetch",         "label": "Fetching latest changes", "required": True},
        {"name": "parse_config",      "label": "Parsing configuration",   "required": True},
        {"name": "generate_compose",  "label": "Configuring environment", "required": True},
        {"name": "generate_settings", "label": "Generating settings",     "required": True},
        {"name": "docker_up",         "label": "Starting containers",     "required": True},
        {"name": "deploy_script",        "label": "Running deploy script",   "required": True},
        {"name": "restart_webserver",    "label": "Restarting webserver",    "required": True},
        {"name": "post_deploy",          "label": "Running post-deploy",    "required": False},
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
        self._phase_start_times: dict[str, float] = {}

    async def init_broadcast(self):
        """Send the initial phases list to the frontend."""
        if not self.deployment_id:
            return
        from app.websockets import deployment_log_broadcaster
        await deployment_log_broadcaster.broadcast_phase_event(
            self.deployment_id, "phases_init", {"phases": self.phases}
        )

    async def start_phase(self, name: str):
        """Mark a phase as running.

        Also marks all earlier phases that are still pending/running as success,
        since the agent must have completed them to reach this point.
        """
        idx = self._phase_index.get(name)
        if idx is None:
            return
        now = time.monotonic()
        # Mark all preceding pending/running phases as success with computed duration
        for i in range(idx):
            if self.phases[i]["status"] in ("pending", "running"):
                phase_name = self.phases[i]["name"]
                start_t = self._phase_start_times.get(phase_name)
                if start_t is not None:
                    self.phases[i]["duration"] = round(now - start_t, 1)
                self.phases[i]["status"] = "success"
                await self._broadcast_update(self.phases[i])
        self.phases[idx]["status"] = "running"
        self._phase_start_times[name] = now
        await self._broadcast_update(self.phases[idx])

    async def end_phase(self, name: str, success: bool, duration: float | None = None):
        """Mark a phase as success/failed."""
        idx = self._phase_index.get(name)
        if idx is None:
            return
        self.phases[idx]["status"] = "success" if success else "failed"
        if duration is not None:
            self.phases[idx]["duration"] = round(duration, 1)
        elif name in self._phase_start_times:
            self.phases[idx]["duration"] = round(
                time.monotonic() - self._phase_start_times[name], 1
            )
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
            # Retry a few times in case of race condition with WS handler
            agent_data = None
            for attempt in range(3):
                agent_data = await r.hgetall(f"agent_phases:{self.deployment_id}")
                if agent_data:
                    break
                await asyncio.sleep(0.5)

            if not agent_data:
                logger.warning(f"No agent phases found in Valkey for deployment {self.deployment_id}")
                return

            merged = 0
            for name_bytes, data_bytes in agent_data.items():
                name = name_bytes if isinstance(name_bytes, str) else name_bytes.decode()
                phase_data = json.loads(data_bytes)
                idx = self._phase_index.get(name)
                if idx is not None:
                    self.phases[idx]["status"] = phase_data.get("status", self.phases[idx]["status"])
                    if phase_data.get("duration") is not None:
                        self.phases[idx]["duration"] = phase_data["duration"]
                    merged += 1
            logger.info(f"Synced {merged} agent phases from Valkey for deployment {self.deployment_id}")
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

        # Hash-based domain and container prefix
        url_hash = compute_url_hash(self.org_slug, self.project_slug, preview_name)
        self.container_prefix = url_hash
        self.domain = f"{url_hash}.{settings.preview_domain}"
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
            # Another deploy is in progress — cancel it and take over
            logger.info(f"Taking over from previous deploy for {self.project_slug}/{self.preview_name}")
            preview = await get_preview(self.project_id, self.preview_name)
            if preview:
                from app.database import get_running_deployment
                existing = await get_running_deployment(preview["id"])
                if existing and existing["id"] != self._deployment_id:
                    await finish_deployment(
                        existing["id"], "cancelled",
                        error="Superseded by new deploy",
                    )

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

            # Save stack info from agent result (fallback if mid-deploy fetch missed it)
            if agent_result and agent_result.get("stack"):
                self._agent_stack = agent_result["stack"]

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
                    # Fetch project public_paths for Caddy bypass
                    public_paths = None
                    proj = await get_project(self.project_id)
                    if proj and proj.get("public_paths"):
                        try:
                            public_paths = json.loads(proj["public_paths"]) or None
                        except (ValueError, TypeError):
                            pass
                    await caddy_manager.add_preview_routes(
                        url_hash, self._vm_ip, public_paths=public_paths,
                    )
                except Exception as e:
                    logger.warning(f"Failed to add Caddy route for {self.domain}: {e}")

            logger.info(
                f"Deploy OK: {self.project_slug}/{self.preview_name} in {deploy_duration}s"
            )

            # Post/update GitLab MR comment (non-fatal)
            if self.mr_iid is not None and self.org_id is not None:
                try:
                    from app.gitlab_comment import post_or_update_mr_comment
                    # Get domain_aliases from DB
                    preview_data = await get_preview(self.project_id, self.preview_name)
                    aliases = None
                    if preview_data and preview_data.get("domain_aliases"):
                        try:
                            aliases = json.loads(preview_data["domain_aliases"])
                        except (ValueError, TypeError):
                            pass
                    url_hash = compute_url_hash(self.org_slug, self.project_slug, self.preview_name)
                    await post_or_update_mr_comment(
                        org_id=self.org_id,
                        project_id=self.project_id,
                        preview_name=self.preview_name,
                        mr_iid=self.mr_iid,
                        preview_url=self.preview_url,
                        url_hash=url_hash,
                        branch=self.branch,
                        commit_sha=self.commit_sha,
                        deploy_duration=deploy_duration,
                        stack_info=self._agent_stack if hasattr(self, '_agent_stack') else None,
                        domain_aliases=aliases,
                    )
                except Exception as e:
                    logger.warning(f"Failed to post MR comment: {e}")

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

            if self._deployment_id:
                if not already_finished:
                    await finish_deployment(
                        self._deployment_id, "failed",
                        log_output="\n".join(self._log_buffer),
                        error=str(e),
                        duration=duration,
                        phases=phases_json,
                    )
                    await deployment_log_broadcaster.complete(self._deployment_id, False)
                elif phases_json:
                    # Agent already finished but we need to store phases
                    from app.database import get_pool
                    pool = await get_pool()
                    await pool.execute(
                        "UPDATE deployments SET phases = $1 WHERE id = $2",
                        phases_json, self._deployment_id,
                    )

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
        """Create a new VM or reuse an existing one (tries warm pool first)."""
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

            # Try to claim a pre-created VM from the warm pool
            server = await cloud_manager.claim_pool_vm(
                vm_name, project_id=self.project_id, preview_name=self.preview_name,
            )
            if server:
                await self._log_raw(
                    f"{DIM}Claimed VM from warm pool (ip={server.data_model.public_net.ipv4.ip}){RESET}\n"
                )
                # Immediately trigger pool replenishment in background
                try:
                    from arq import create_pool as create_arq_pool
                    from arq.connections import RedisSettings
                    pool = await create_arq_pool(RedisSettings.from_dsn(settings.valkey_url))
                    await pool.enqueue_job("task_replenish_warm_pool", _job_id="replenish_warm_pool")
                    await pool.aclose()
                    logger.info("Enqueued warm pool replenishment after claim")
                except Exception as e:
                    logger.warning(f"Failed to enqueue pool replenishment: {e}")
            else:
                # No pool VM available — create one from scratch
                server = await self._step_create_vm(vm_name)

            self._vm_id = server.data_model.id
            self._vm_ip = server.data_model.public_net.ipv4.ip
            # Update VM info in DB
            preview = await get_preview(self.project_id, self.preview_name)
            if preview:
                await update_preview_vm(preview["id"], self._vm_id, self._vm_ip)

    async def _provision_agent(self):
        """Install and start the druploy-agent on the VM via SSH.

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
                    # Agent running — restart to update binary (needs root for systemctl)
                    executor = RemoteExecutor(self._vm_ip, user="root")
                    proc = await executor.run_shell("systemctl restart druploy-agent")
                    await proc.communicate()
                    elapsed = time.monotonic() - t0
                    await self._log_step_end(step, elapsed, True, f"{DIM}Agent updated{RESET}")
                    return
        except Exception:
            pass

        # Agent not running — wait for SSH first (needs root for install)
        executor = RemoteExecutor(self._vm_ip, user="root")
        await executor.wait_for_ssh(timeout=120)

        # Install the agent: download binary + create systemd service
        api_url = settings.api_url
        agent_bin = "/var/www/preview/bin/druploy-agent"
        install_cmd = (
            # Create directories
            f"mkdir -p /var/www/preview/bin && "
            # Download agent binary
            f"curl -sf -o {agent_bin} {api_url}/api/internal/agent/download && "
            f"chmod +x {agent_bin} && "
            f"chown -R preview:www-data /var/www/preview/bin && "
            # Write env config
            f"echo 'PREVIEW_SERVER_URL={api_url}' > /etc/druploy-agent.env && "
            # Create update script
            f"cat > /var/www/preview/bin/druploy-agent-update << 'UPDATESCRIPT'\n"
            f"#!/bin/bash\n"
            f"set -euo pipefail\n"
            f"source /etc/druploy-agent.env 2>/dev/null || true\n"
            f"AGENT_URL=\"${{PREVIEW_SERVER_URL:-{api_url}}}/api/internal/agent/download\"\n"
            f"AGENT_BIN=/var/www/preview/bin/druploy-agent\n"
            f"curl -sf -o \"${{AGENT_BIN}}.new\" \"$AGENT_URL\" && "
            f"chmod +x \"${{AGENT_BIN}}.new\" && "
            f"mv \"${{AGENT_BIN}}.new\" \"$AGENT_BIN\" || true\n"
            f"UPDATESCRIPT\n"
            f"chmod +x /var/www/preview/bin/druploy-agent-update && "
            f"chown -R preview:www-data /var/www/preview/bin && "
            # Create systemd service (runs agent as preview user)
            f"cat > /etc/systemd/system/druploy-agent.service << 'SERVICEFILE'\n"
            f"[Unit]\n"
            f"Description=Druploy Agent\n"
            f"After=docker.service\n"
            f"Requires=docker.service\n"
            f"\n"
            f"[Service]\n"
            f"Type=simple\n"
            f"User=preview\n"
            f"Group=www-data\n"
            f"ExecStartPre=/var/www/preview/bin/druploy-agent-update\n"
            f"ExecStart=/var/www/preview/bin/druploy-agent\n"
            f"Restart=always\n"
            f"RestartSec=5\n"
            f"Environment=PORT=8022\n"
            f"Environment=HOME=/var/www/preview\n"
            f"EnvironmentFile=-/etc/druploy-agent.env\n"
            f"\n"
            f"[Install]\n"
            f"WantedBy=multi-user.target\n"
            f"SERVICEFILE\n"
            f"systemctl daemon-reload && "
            f"systemctl enable druploy-agent && "
            f"systemctl start druploy-agent"
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

        log_offset = 0
        current_phase = "deploy"
        current_step = ""
        deadline = time.monotonic() + timeout
        poll_interval = 2  # seconds
        poll_url = f"http://{self._vm_ip}:8022/deploy/logs/{self._deployment_id}"
        status_url = f"http://{self._vm_ip}:8022/deploy/status"
        info_url = f"http://{self._vm_ip}:8022/info"
        fetched_info = False

        def _poll(offset: int) -> dict | None:
            try:
                req = urllib.request.urlopen(f"{poll_url}?offset={offset}", timeout=10)
                return json.loads(req.read())
            except Exception:
                return None

        def _poll_status() -> dict | None:
            try:
                req = urllib.request.urlopen(status_url, timeout=5)
                return json.loads(req.read())
            except Exception:
                return None

        while time.monotonic() < deadline:
            await self._check_cancelled()
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
                    if self._phase_tracker:
                        # Do a final status poll to get the actual step where it ended
                        final_status = await asyncio.to_thread(_poll_status)
                        final_step = (final_status or {}).get("step", "") or current_step
                        success = result.get("success", False)

                        if final_step and final_step != current_step:
                            # Agent advanced past what we last saw — mark intermediates
                            if current_step:
                                await self._phase_tracker.end_phase(current_step, True)
                            # start_phase will mark all skipped phases as success
                            await self._phase_tracker.start_phase(final_step)

                        if final_step:
                            await self._phase_tracker.end_phase(final_step, success)
                        elif current_step:
                            await self._phase_tracker.end_phase(current_step, success)

                    return result

            # Poll agent status to track current step for phase updates
            if self._phase_tracker:
                status_data = await asyncio.to_thread(_poll_status)
                if status_data:
                    agent_status = status_data.get("status", "")
                    step = status_data.get("step", "")

                    if agent_status == "running" and step and step != current_step:
                        # Previous step finished successfully, new step started
                        if current_step:
                            await self._phase_tracker.end_phase(current_step, True)

                        # Fetch VM info once agent is past generate_compose
                        post_compose_steps = {"generate_settings", "docker_pull", "docker_up", "wait_for_db", "import_db", "import_files", "deploy_script", "restart_webserver", "post_deploy"}
                        if not fetched_info and step in post_compose_steps:
                            fetched_info = await self._fetch_and_save_vm_info(info_url)

                        current_step = step
                        await self._phase_tracker.start_phase(step)
                    elif agent_status in ("success", "failed") and current_step:
                        # Agent finished — mark last step
                        await self._phase_tracker.end_phase(
                            current_step, agent_status == "success",
                        )
                        current_step = ""

            await asyncio.sleep(poll_interval)

        raise RuntimeError(f"Agent deploy timed out after {timeout}s")

    async def _fetch_and_save_vm_info(self, info_url: str) -> bool:
        """Fetch /info from VM agent and save stack + domain_aliases to DB."""
        try:
            def _fetch():
                req = urllib.request.urlopen(info_url, timeout=5)
                return json.loads(req.read())

            data = await asyncio.to_thread(_fetch)
            fields = {}

            stack = data.get("stack")
            if stack:
                fields["stack_info"] = json.dumps(stack)
                self._agent_stack = stack

            aliases = data.get("domain_aliases")
            if aliases:
                fields["domain_aliases"] = json.dumps(aliases)

            if fields:
                await PreviewStateManager.save_state(
                    self.project_id, self.preview_name, **fields
                )
                logger.info(f"Saved VM info mid-deploy for {self.project_slug}/{self.preview_name}")
                try:
                    from app.valkey import publish_event
                    await publish_event("previews:global", {"type": "preview_updated"})
                except Exception:
                    pass
            return True
        except Exception as e:
            logger.warning(f"Failed to fetch VM info mid-deploy: {e}")
            return False

    def _generate_callback_token(self) -> str:
        """Generate a short-lived HMAC token for agent WebSocket auth."""
        expiry = int(time.time()) + 3600  # 1 hour
        payload = f"{self._deployment_id}:{expiry}"
        sig = hmac_mod.new(
            settings.secret_key.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"{payload}:{sig}"

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

    # (Legacy SSH step methods removed — all deploy execution is handled by the VM agent)

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
        "wait-agent": "Waiting for agent",
        "post-deploy-new": "Running post-deploy script",
        "post-deploy-update": "Running post-deploy script",
    }

    def _step_label(self, step: str) -> str:
        return self._STEP_LABELS.get(step, step)

    async def _log_step_start(self, step: str):
        await self._log_raw(f"\n\n\n{CYAN}⚙️ {self._step_label(step)}{RESET}\n{DIM}────────────────────────────────────────────────────────────────────{RESET}\n\n")
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
                expose = self._preview_config.get("expose") or {}
                fields["expose_config"] = json.dumps(expose)
            # Save stack info (fallback — may already be saved mid-deploy)
            if hasattr(self, '_agent_stack') and self._agent_stack:
                fields["stack_info"] = json.dumps(self._agent_stack)
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
