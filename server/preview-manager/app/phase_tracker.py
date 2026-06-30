"""Deploy phase definitions + PhaseTracker.

Extracted from ``deployment.py`` to keep that module focused on the deploy
orchestration itself. ``DEPLOY_PHASES`` is the ordered list of phases per deploy
type; ``PhaseTracker`` tracks their state and broadcasts updates to the frontend.
"""

import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)


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
