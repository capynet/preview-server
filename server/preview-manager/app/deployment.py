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
from app.database import get_preview, get_project, create_deployment, finish_deployment
from app.overlay import get_base_files_dir, mount_overlay
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

        self.force_new = False
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
        if self.force_new:
            return True
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
            f"\n{BOLD}{CYAN}{deploy_type} Deploy: {self.project_name}/{self.preview_name}{RESET}\n"
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
        self._write_internal_settings()

        # DB volume cache: skip SQL import if a cached volume exists
        from app.db_cache import compute_cache_key, cache_exists, import_volume, export_volume
        db_spec = self._preview_config["database"]
        dump_path = Path(f"/backups/{self.project_name}-base.sql.gz")
        cache_key = compute_cache_key(self.project_name, db_spec, dump_path)
        volume_name = f"{self.container_prefix}_db_data"
        use_cache = cache_exists(self.project_name, cache_key)

        if use_cache:
            await self._restore_db_cache(volume_name, cache_key)
            await self._docker_up()
            await self._wait_for_db()
        else:
            await self._docker_up()
            await self._wait_for_db()
            await self._import_db()
            await self._create_db_cache(volume_name, cache_key)

        await self._composer_install()
        await self._import_files()
        await self._run_deploy_steps("new")
        await self._run_project_deploy_script("new")

    # ------------------------------------------------------------------
    # Update preview
    # ------------------------------------------------------------------

    async def _deploy_update(self):
        await self._generate_compose()
        self._write_internal_settings()
        await self._docker_up()
        await self._run_deploy_steps("update")
        await self._run_project_deploy_script("update")

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _verify_base_files(self):
        db = Path(f"/backups/{self.project_name}-base.sql.gz")
        if not db.exists():
            raise RuntimeError(f"Base files missing: {db}")

    def _write_internal_settings(self):
        """Write settings.preview.internal.php and ensure settings.php includes it.

        This is injected by the deployer on every deploy (new and update)
        so it's always up-to-date, even if not committed to the repo.
        """
        docroot = self._preview_config.get("docroot", "web") if self._preview_config else "web"
        settings_dir = self.preview_path / docroot / "sites" / "default"
        settings_dir.mkdir(parents=True, exist_ok=True)

        # 1. Write settings.preview.internal.php
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
                logger.info("Appended preview include snippet to settings.php")
            elif "settings.preview.internal.php" not in content:
                # Old-style include (only settings.preview.php) — upgrade it
                old_include = "include __DIR__ . '/settings.preview.php';"
                new_include = (
                    "include __DIR__ . '/settings.preview.internal.php';\n"
                    "  if (file_exists(__DIR__ . '/settings.preview.php')) {\n"
                    "    include __DIR__ . '/settings.preview.php';\n"
                    "  }"
                )
                content = content.replace(old_include, new_include)
                settings_php.write_text(content)
                logger.info("Upgraded preview include snippet in settings.php")
        else:
            settings_php.write_text("<?php\n" + snippet)
            logger.info(f"Created {settings_php} with preview include snippet")

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
            proj = await get_project(self.project_name)
            if proj and proj.get("env_vars"):
                project_env = proj["env_vars"]
                if isinstance(project_env, str):
                    project_env = json.loads(project_env)
                extra_env.update(project_env)

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
        """Import database dump via gunzip piped to mysql, with progress."""
        db_path = f"/backups/{self.project_name}-base.sql.gz"
        db_container = f"{self.container_prefix}-db"

        # Get file size for pv progress
        db_file = Path(db_path)
        if db_file.exists():
            size_mb = db_file.stat().st_size / (1024 * 1024)
            # pv -f forces output to stderr, shows progress bar with size
            cmd = (
                f"pv -f -s {db_file.stat().st_size} {db_path} "
                f"| gunzip | docker exec -e MYSQL_PWD=drupal -i {db_container} "
                f"mysql -u drupal drupal"
            )
        else:
            cmd = (
                f"gunzip -c {db_path} | docker exec -e MYSQL_PWD=drupal -i {db_container} "
                f"mysql -u drupal drupal"
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

    async def _restore_db_cache(self, volume_name: str, cache_key: str):
        """Pre-populate the DB volume from cache before docker compose up."""
        from app.db_cache import import_volume, get_cache_path
        step = "restore-db-cache"
        await self._log_step_start(step)
        t0 = time.monotonic()

        cache_path = get_cache_path(self.project_name, cache_key)
        size_mb = cache_path.stat().st_size / (1024 * 1024)

        await import_volume(volume_name, self.project_name, cache_key)

        elapsed = time.monotonic() - t0
        await self._log_step_end(
            step, elapsed, True,
            f"{DIM}Restored from cache ({size_mb:.1f} MB){RESET}",
        )

    async def _create_db_cache(self, volume_name: str, cache_key: str):
        """Export the DB volume to cache after first import."""
        from app.db_cache import export_volume
        step = "create-db-cache"
        await self._log_step_start(step)
        t0 = time.monotonic()

        # Stop DB container for a clean snapshot
        db_container = f"{self.container_prefix}-db"
        proc = await asyncio.create_subprocess_exec(
            "docker", "stop", db_container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        try:
            cache_path = await export_volume(
                volume_name, self.project_name, cache_key
            )
            size_mb = cache_path.stat().st_size / (1024 * 1024)
        finally:
            # Restart DB container
            proc = await asyncio.create_subprocess_exec(
                "docker", "start", db_container,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            # Wait for DB to be ready again
            await self._wait_for_db()

        elapsed = time.monotonic() - t0
        await self._log_step_end(
            step, elapsed, True,
            f"{DIM}Cached for future previews ({size_mb:.1f} MB){RESET}",
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
        full_cmd = ("docker", "exec", "-e", "COLUMNS=200", php_container, *cmd)
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
                # Flush any lines accumulated in the last second
                if pending_chunks:
                    text = b"".join(pending_chunks).decode(errors="replace")
                    pending_chunks.clear()
                    await self._log_raw(text)
        except asyncio.TimeoutError:
            raise

        # Flush remaining chunks
        if pending_chunks:
            text = b"".join(pending_chunks).decode(errors="replace")
            pending_chunks.clear()
            await self._log_raw(text)

        await read_task
        await proc.wait()
        return (
            b"".join(stdout_chunks).decode(errors="replace"),
            b"".join(stderr_chunks).decode(errors="replace"),
        )

    async def _run(self, *cmd: str, step: str, timeout: int = 120, env: dict | None = None) -> str:
        """Run a command inside the preview directory. Raises on failure."""
        logger.info(f"[{step}] Running: {' '.join(cmd)}")
        await self._log_step_start(step)
        t0 = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.preview_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await self._stream_progress(proc, step, t0, timeout)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            await self._log_step_end(step, elapsed, False, f"{RED}TIMEOUT after {timeout}s{RESET}")
            raise RuntimeError(f"[{step}] Timed out after {timeout}s")

        elapsed = time.monotonic() - t0
        output = stdout + stderr

        if proc.returncode != 0:
            # Output already streamed; pass empty to avoid duplication
            await self._log_step_end(step, elapsed, False, "")
            raise RuntimeError(
                f"[{step}] Failed (exit {proc.returncode}):\n{output[-2000:]}"
            )

        # Output already streamed; pass empty to avoid duplication
        await self._log_step_end(step, elapsed, True, "")
        logger.info(f"[{step}] OK ({_fmt_duration(elapsed)})")
        return output

    async def _run_shell(self, cmd: str, step: str, timeout: int = 120) -> str:
        """Run a shell command (for pipes). Raises on failure."""
        logger.info(f"[{step}] Running: {cmd}")
        await self._log_step_start(step)
        t0 = time.monotonic()

        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(self.preview_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

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

        if status == "active":
            fields["last_deployed_at"] = now
        if status in ("active", "failed"):
            fields["last_deployment_status"] = status
            fields["last_deployment_completed_at"] = now
            if error:
                fields["last_deployment_error"] = error
            if duration is not None:
                fields["last_deployment_duration"] = duration

        await PreviewStateManager.save_state(
            self.project_name, self.preview_name, **fields
        )
