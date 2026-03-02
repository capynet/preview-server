"""Preview deployment logic — executed after webhook clones the repo."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from app.docker_compose import (
    detect_docroot,
    generate_docker_compose,
    parse_preview_yml,
    write_docker_compose,
)
from app.state import PreviewStateManager
from app.database import get_preview, create_deployment, finish_deployment
from app.overlay import get_base_files_dir, mount_overlay
from app import config_store
from config.settings import settings

logger = logging.getLogger(__name__)

# Timeouts per step (seconds)
TIMEOUT_DOCKER_UP = 300
TIMEOUT_COMPOSER = 600
TIMEOUT_IMPORT_DB = 600
TIMEOUT_IMPORT_FILES = 600
TIMEOUT_DRUSH = 300
TIMEOUT_DEPLOY_SCRIPT = 600
TIMEOUT_DEPLOY_STEP = 300

# Path to custom deploy step scripts
DEPLOY_STEPS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "deploy-steps"

# ANSI color codes for log output
BOLD = "\033[1m"
CYAN = "\033[1;36m"
GREEN = "\033[1;32m"
RED = "\033[1;31m"
YELLOW = "\033[0;33m"
DIM = "\033[0;90m"
RESET = "\033[0m"


def _fmt_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m {s}s"


class PreviewDeployer:
    """Deploy a preview environment using Docker Compose.

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
        deployment_id: int | None = None,
    ):
        self.project_name = project_name
        self.preview_name = preview_name
        self.branch = branch
        self.commit_sha = commit_sha
        self.triggered_by = triggered_by
        self.mr_iid = mr_iid

        self.preview_path = PreviewStateManager.get_preview_path(project_name, preview_name)
        self.container_prefix = f"{preview_name}-{project_name}"
        self.preview_url = f"https://{preview_name}-{project_name}.mr.preview-mr.com"
        self._preview_config: dict | None = None
        self._log_buffer: list[str] = []
        self._deployment_id: int | None = deployment_id
        self._step_timings: list[tuple[str, float, str]] = []  # (step, duration, status)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def is_new(self) -> bool:
        """Check if this is a first deploy (no previous successful deployment)."""
        state = await PreviewStateManager.load_state(self.project_name, self.preview_name)
        if not state:
            return True
        # If there's a previous successful deployment, this is an update
        return not state.get("last_deployed_at")

    async def is_creating(self) -> bool:
        state = await PreviewStateManager.load_state(self.project_name, self.preview_name)
        return state is not None and state["status"] == "creating"

    async def deploy(self) -> bool:
        """Entry point. Returns True on success."""
        if await self.is_creating():
            logger.warning(
                f"Skipping deploy for {self.project_name}/{self.preview_name}: "
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
            preview = await get_preview(self.project_name, self.preview_name)
            if preview:
                self._deployment_id = await create_deployment(
                    preview["id"], self.triggered_by
                )
                deployment_log_broadcaster.register(self._deployment_id)
                await preview_list_manager.force_broadcast()

        is_new = await self.is_new()
        deploy_type = "NEW" if is_new else "UPDATE"

        # Deploy header
        await self._log_raw(
            f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════╗{RESET}\n"
            f"{BOLD}{CYAN}║  {deploy_type} Deploy: {self.project_name}/{self.preview_name}{RESET}\n"
            f"{BOLD}{CYAN}╚══════════════════════════════════════════════════╝{RESET}\n"
            f"{DIM}Branch: {self.branch}  Commit: {self.commit_sha[:8]}{RESET}\n"
        )

        try:
            if is_new:
                logger.info(f"NEW deploy: {self.project_name}/{self.preview_name}")
                await self._deploy_new()
            else:
                logger.info(f"UPDATE deploy: {self.project_name}/{self.preview_name}")
                await self._deploy_update()

            duration = int((datetime.now(timezone.utc) - start).total_seconds())
            await self._save_state("active", duration=duration)

            # Success summary
            await self._log_summary(True, duration)

            if self._deployment_id:
                await finish_deployment(
                    self._deployment_id, "success",
                    log_output="\n".join(self._log_buffer),
                    duration=duration,
                )
                await deployment_log_broadcaster.complete(self._deployment_id, True)

            logger.info(
                f"Deploy OK: {self.project_name}/{self.preview_name} in {duration}s"
            )
            return True

        except Exception as e:
            duration = int((datetime.now(timezone.utc) - start).total_seconds())
            logger.error(
                f"Deploy FAILED: {self.project_name}/{self.preview_name}: {e}",
                exc_info=True,
            )
            await self._save_state("failed", error=str(e), duration=duration)

            # Failure summary
            await self._log_summary(False, duration, error=str(e))

            if self._deployment_id:
                await finish_deployment(
                    self._deployment_id, "failed",
                    log_output="\n".join(self._log_buffer),
                    error=str(e),
                    duration=duration,
                )
                await deployment_log_broadcaster.complete(self._deployment_id, False)

            return False

    # ------------------------------------------------------------------
    # New preview
    # ------------------------------------------------------------------

    async def _deploy_new(self):
        self._verify_base_files()
        await self._generate_compose()
        await self._docker_up()
        await self._wait_for_db()
        await self._composer_install()
        await self._import_db()
        await self._import_files()
        await self._run_deploy_steps("new")
        await self._run_project_deploy_script("new")

    # ------------------------------------------------------------------
    # Update preview
    # ------------------------------------------------------------------

    async def _deploy_update(self):
        await self._generate_compose()
        await self._docker_up()
        await self._composer_install()
        await self._run_deploy_steps("update")
        await self._run_project_deploy_script("update")

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _verify_base_files(self):
        db = Path(f"/backups/{self.project_name}-base.sql.gz")
        if not db.exists():
            raise RuntimeError(f"Base files missing: {db}")

    async def _generate_compose(self):
        """Parse preview.yml and generate docker-compose.yml."""
        step = "generate-compose"
        await self._log_step_start(step)
        t0 = time.monotonic()

        config = parse_preview_yml(self.preview_path)

        # Auto-detect docroot if not set explicitly in preview.yml
        yml_file = self.preview_path / "preview.yml"
        if not yml_file.exists() or "docroot" not in (
            __import__("yaml").safe_load(yml_file.read_text()) or {}
        ):
            config["docroot"] = detect_docroot(self.preview_path)

        self._preview_config = config

        # Load extra env vars: project-level + preview-level (preview overrides project)
        extra_env: dict[str, str] = {}
        try:
            import json
            project_env_json = await config_store.get_config(f"env_vars_{self.project_name}")
            if project_env_json:
                extra_env.update(json.loads(project_env_json))

            preview_row = await get_preview(self.project_name, self.preview_name)
            if preview_row and preview_row.get("env_vars"):
                preview_env = preview_row["env_vars"]
                if isinstance(preview_env, str):
                    preview_env = json.loads(preview_env)
                extra_env.update(preview_env)
        except Exception as e:
            logger.warning(f"Error loading extra env vars: {e}")

        compose = generate_docker_compose(
            self.project_name, self.preview_name, config,
            branch=self.branch, commit_sha=self.commit_sha,
            mr_iid=self.mr_iid,
            extra_env=extra_env if extra_env else None,
        )
        write_docker_compose(self.preview_path, compose)

        elapsed = time.monotonic() - t0
        info = f"php={config['php_version']} docroot={config['docroot']}"
        await self._log_step_end(step, elapsed, True, f"{DIM}{info}{RESET}")
        logger.info(f"[generate-compose] Generated docker-compose.yml")

    async def _docker_up(self):
        await self._run(
            "docker", "compose", "up", "-d", "--pull", "missing",
            step="docker-up",
            timeout=TIMEOUT_DOCKER_UP,
        )

    async def _wait_for_db(self):
        """Wait for MySQL to be ready to accept connections."""
        step = "wait-for-db"
        await self._log_step_start(step)
        t0 = time.monotonic()

        db_container = f"{self.container_prefix}-db"
        for attempt in range(30):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "exec", db_container,
                    "mysqladmin", "ping", "-h", "localhost", "-u", "root", "-proot",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode == 0:
                    elapsed = time.monotonic() - t0
                    await self._log_step_end(
                        step, elapsed, True,
                        f"{DIM}MySQL ready after {attempt + 1} attempt(s){RESET}",
                    )
                    logger.info(f"[wait-for-db] MySQL ready after {attempt + 1} attempts")
                    return
            except (asyncio.TimeoutError, Exception):
                pass
            await asyncio.sleep(2)

        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, False, "MySQL not ready after 60s")
        raise RuntimeError("[wait-for-db] MySQL not ready after 60s")

    async def _composer_install(self):
        await self._docker_exec(
            "composer", "install", "--no-interaction", "--no-progress",
            step="composer-install",
            timeout=TIMEOUT_COMPOSER,
        )

    async def _import_db(self):
        """Import database dump via gunzip piped to mysql."""
        db_path = f"/backups/{self.project_name}-base.sql.gz"
        db_container = f"{self.container_prefix}-db"

        # Use shell pipe: gunzip | docker exec mysql
        cmd = (
            f"gunzip -c {db_path} | docker exec -i {db_container} "
            f"mysql -u drupal -pdrupal drupal"
        )
        await self._run_shell(cmd, step="import-db", timeout=TIMEOUT_IMPORT_DB)

    async def _import_files(self):
        """Mount overlay filesystem for shared base files (skipped if none uploaded)."""
        base_dir = get_base_files_dir(self.project_name)
        if not base_dir.exists():
            # Create an empty files directory so Drupal can still function
            public_path = self._preview_config["env"].get(
                "PREV_FILE_PUBLIC_PATH", "sites/default/files"
            ) if self._preview_config else "sites/default/files"
            docroot = self._preview_config.get("docroot", "web") if self._preview_config else "web"
            files_dir = self.preview_path / docroot / public_path
            files_dir.mkdir(parents=True, exist_ok=True)
            await self._log(f"{DIM}No base files found — created empty {docroot}/{public_path}{RESET}")
            return

        step = "import-files"
        await self._log_step_start(step)
        t0 = time.monotonic()

        public_path = self._preview_config["env"].get(
            "PREV_FILE_PUBLIC_PATH", "sites/default/files"
        ) if self._preview_config else "sites/default/files"
        docroot = self._preview_config.get("docroot", "web") if self._preview_config else "web"

        await mount_overlay(
            self.project_name, self.preview_path,
            docroot=docroot, public_path=public_path,
        )

        elapsed = time.monotonic() - t0
        await self._log_step_end(
            step, elapsed, True,
            f"{DIM}Mounted overlay (base: {base_dir}){RESET}",
        )

    async def _drush(self, *args):
        await self._docker_exec(
            "vendor/bin/drush", *args,
            step=f"drush-{args[0]}",
            timeout=TIMEOUT_DRUSH,
        )

    async def _run_project_deploy_script(self, phase: str):
        """Run the project deploy script for a phase (new/update).

        Priority:
        1. Preview-specific override: scripts/preview/{phase}/{preview_name}-deploy.sh
        2. Script path defined in preview.yml deploy.{phase}
        3. Nothing — if no script is configured, skip entirely
        """
        # Check for preview-specific override first
        scripts_dir = self.preview_path / "scripts" / "preview" / phase
        preview_script = scripts_dir / f"{self.preview_name}-deploy.sh"

        if preview_script.exists():
            logger.info(f"Running preview-specific deploy script: {preview_script.name}")
            await self._docker_exec(
                "bash", f"/var/www/html/scripts/preview/{phase}/{preview_script.name}",
                step=f"project-deploy-script-preview-{phase}",
                timeout=TIMEOUT_DEPLOY_SCRIPT,
            )
            return

        # Use preview.yml config
        config = getattr(self, "_preview_config", None)
        deploy_path = config["deploy"][phase] if config else None

        if not deploy_path:
            logger.info(f"No deploy script configured for phase '{phase}', skipping")
            return

        # Verify the script exists in the project
        full_path = self.preview_path / deploy_path
        if not full_path.exists():
            raise RuntimeError(
                f"Deploy script not found: {deploy_path} "
                f"(configured in preview.yml deploy.{phase})"
            )

        logger.info(f"Running deploy script ({phase}): {deploy_path}")
        await self._docker_exec(
            "bash", f"/var/www/html/{deploy_path}",
            step=f"project-deploy-script-{phase}",
            timeout=TIMEOUT_DEPLOY_SCRIPT,
        )

    # ------------------------------------------------------------------
    # Custom deploy steps
    # ------------------------------------------------------------------

    async def _run_deploy_steps(self, phase: str):
        """Run *.sh scripts from deploy-steps/{phase}/ in sorted order."""
        steps_dir = DEPLOY_STEPS_DIR / phase
        if not steps_dir.is_dir():
            return

        scripts = sorted(steps_dir.glob("*.sh"))
        if not scripts:
            return

        env = self._build_step_env(phase)
        logger.info(f"Running {len(scripts)} deploy step(s) from {phase}/")

        for script in scripts:
            await self._run(
                "bash", str(script),
                step=f"deploy-step-{phase}/{script.name}",
                timeout=TIMEOUT_DEPLOY_STEP,
                env=env,
            )

    def _build_step_env(self, phase: str) -> dict:
        """Build environment variables passed to deploy step scripts."""
        import os
        env = os.environ.copy()
        env.update({
            "PREV_PROJECT_NAME": self.project_name,
            "PREV_PREVIEW_NAME": self.preview_name,
            "PREV_MR_IID": str(self.mr_iid) if self.mr_iid else "",
            "PREV_PATH": str(self.preview_path),
            "PREV_URL": self.preview_url,
            "PREV_CONTAINER_PREFIX": self.container_prefix,
            "PREV_BRANCH": self.branch,
            "PREV_COMMIT_SHA": self.commit_sha,
            "PREV_PHASE": phase,
        })
        return env

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _docker_exec(self, *cmd: str, step: str, timeout: int = 120) -> str:
        """Run a command inside the PHP container."""
        php_container = f"{self.container_prefix}-php"
        full_cmd = ("docker", "exec", php_container, *cmd)
        return await self._run(*full_cmd, step=step, timeout=timeout)

    async def _log_raw(self, text: str):
        """Append raw text to log buffer and broadcast."""
        from app.websockets import deployment_log_broadcaster
        self._log_buffer.append(text)
        if self._deployment_id:
            await deployment_log_broadcaster.add_log(self._deployment_id, text)

    async def _log_step_start(self, step: str):
        """Log the start of a deployment step with colored header."""
        await self._log_raw(f"\n{CYAN}⚙️ {step}{RESET}\n")

    async def _log_step_end(self, step: str, duration: float, success: bool, output: str):
        """Log the end of a step with duration and colored status."""
        dur_str = _fmt_duration(duration)
        if success:
            status_line = f"{GREEN}✓ {step}{RESET} {DIM}completed in {dur_str}{RESET}\n"
            self._step_timings.append((step, duration, "ok"))
        else:
            status_line = f"{RED}✗ {step}{RESET} {DIM}failed after {dur_str}{RESET}\n"
            self._step_timings.append((step, duration, "fail"))

        # Append command output (if any) before the status line
        if output.strip():
            self._log_buffer.append(output.strip())
            from app.websockets import deployment_log_broadcaster
            if self._deployment_id:
                await deployment_log_broadcaster.add_log(
                    self._deployment_id, output.strip() + "\n"
                )

        await self._log_raw(status_line + "\n")

    async def _log_summary(self, success: bool, total_duration: int, error: str | None = None):
        """Log a final deploy summary with step timings."""
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
                lines.append(f"  {icon} {step_name} {DIM}{_fmt_duration(step_dur)}{RESET}\n")

        lines.append(f"{BOLD}{'─' * 50}{RESET}\n")
        await self._log_raw("".join(lines))

    async def _run(self, *cmd: str, step: str, timeout: int = 120, env: dict | None = None) -> str:
        """Run a command inside the preview directory. Raises on failure."""
        logger.info(f"[{step}] Running: {' '.join(cmd)}")
        await self._log_step_start(step)
        t0 = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.preview_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            proc.kill()
            await self._log_step_end(step, elapsed, False, f"{RED}TIMEOUT after {timeout}s{RESET}")
            raise RuntimeError(f"[{step}] Timed out after {timeout}s")

        elapsed = time.monotonic() - t0
        output = stdout.decode() + stderr.decode()

        if proc.returncode != 0:
            await self._log_step_end(step, elapsed, False, output)
            raise RuntimeError(
                f"[{step}] Failed (exit {proc.returncode}):\n{output[-2000:]}"
            )

        await self._log_step_end(step, elapsed, True, output)
        logger.info(f"[{step}] OK ({_fmt_duration(elapsed)})")
        return output

    async def _run_shell(self, cmd: str, step: str, timeout: int = 120) -> str:
        """Run a shell command (for pipes). Raises on failure."""
        logger.info(f"[{step}] Running: {cmd}")
        await self._log_step_start(step)
        t0 = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(self.preview_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            proc.kill()
            await self._log_step_end(step, elapsed, False, f"{RED}TIMEOUT after {timeout}s{RESET}")
            raise RuntimeError(f"[{step}] Timed out after {timeout}s")

        elapsed = time.monotonic() - t0
        output = stdout.decode() + stderr.decode()

        if proc.returncode != 0:
            await self._log_step_end(step, elapsed, False, output)
            raise RuntimeError(
                f"[{step}] Failed (exit {proc.returncode}):\n{output[-2000:]}"
            )

        await self._log_step_end(step, elapsed, True, output)
        logger.info(f"[{step}] OK ({_fmt_duration(elapsed)})")
        return output

    async def _save_state(
        self,
        status: str,
        *,
        error: str | None = None,
        duration: int | None = None,
    ):
        now = datetime.now(timezone.utc).isoformat()
        existing = await PreviewStateManager.load_state(self.project_name, self.preview_name)

        fields = {
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "status": status,
            "url": self.preview_url,
            "path": str(self.preview_path),
        }

        if self.mr_iid is not None:
            fields["mr_id"] = self.mr_iid

        if not existing:
            fields["created_at"] = now

        if status in ("active", "failed"):
            fields["last_deployed_at"] = now
            fields["last_deployment_status"] = status
            fields["last_deployment_completed_at"] = now
            if error:
                fields["last_deployment_error"] = error
            if duration is not None:
                fields["last_deployment_duration"] = duration

        await PreviewStateManager.save_state(
            self.project_name, self.preview_name, **fields
        )
