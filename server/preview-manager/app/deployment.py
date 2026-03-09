"""Preview deployment logic — deploy previews on ephemeral Hetzner Cloud VMs."""

import asyncio
import hashlib
import logging
import tempfile
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
from app.database import (
    get_preview, get_project, create_deployment, finish_deployment,
    update_preview_vm, compute_url_hash,
)
from app.caddy_api import caddy_manager
from app.cloud import cloud_manager
from app.storage import storage_manager
from app.remote import RemoteExecutor
from config.settings import settings

logger = logging.getLogger(__name__)

# Timeouts per step (seconds)
TIMEOUT_DOCKER_UP = 300
TIMEOUT_COMPOSER = 600
TIMEOUT_IMPORT_DB = 600
TIMEOUT_IMPORT_FILES = 600
TIMEOUT_DRUSH = 300
TIMEOUT_DEPLOY_SCRIPT = 36000
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
                self._deployment_id = await create_deployment(
                    preview["id"], self.triggered_by
                )
                deployment_log_broadcaster.register(self._deployment_id)
                await preview_list_manager.force_broadcast()

        is_new = await self.is_new()
        deploy_type = "NEW" if is_new else "UPDATE"

        # Deploy header
        await self._log_raw(
            f"\n{BOLD}{CYAN}{deploy_type} Deploy: {self.project_slug}/{self.preview_name}{RESET}\n"
            f"{DIM}Branch: {self.branch}  Commit: {self.commit_sha[:8]}{RESET}\n"
        )

        try:
            if is_new:
                logger.info(f"NEW deploy: {self.project_slug}/{self.preview_name}")
                await self._deploy_new()
            else:
                logger.info(f"UPDATE deploy: {self.project_slug}/{self.preview_name}")
                await self._deploy_update()

            duration = int((datetime.now(timezone.utc) - start).total_seconds())
            await self._save_state("active", duration=duration)

            # Register Caddy route so static assets bypass the Python proxy
            if self._vm_ip:
                try:
                    await caddy_manager.add_preview_route(self.domain, self._vm_ip)
                except Exception as e:
                    logger.warning(f"Failed to add Caddy route for {self.domain}: {e}")

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
                f"Deploy OK: {self.project_slug}/{self.preview_name} in {duration}s"
            )
            return True

        except Exception as e:
            duration = int((datetime.now(timezone.utc) - start).total_seconds())
            logger.error(
                f"Deploy FAILED: {self.project_slug}/{self.preview_name}: {e}",
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
    # New preview (cloud)
    # ------------------------------------------------------------------

    async def _deploy_new(self):
        # 1. Verify base files exist in S3
        await self._verify_base_files()

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

            # Stop existing containers
            proc = await self._executor.run_shell(
                f"cd {VM_PREVIEW_DIR}/code && docker compose down --remove-orphans 2>/dev/null; true"
            )
            await proc.communicate()
        else:
            # Create new VM
            vm_name = f"prev-{self.project_slug}-{self.preview_name}"
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

        # 4. Setup workspace directory
        await self._step_setup_vm()

        # 5. Clone repo via SSH
        await self._step_clone_repo()

        # 6. Generate and upload docker-compose.yml
        await self._generate_compose()
        self._write_internal_settings()
        await self._upload_compose_and_settings()

        # 7. Pull images from private registry
        await self._step_pull_images()

        # 8. Check DB cache in S3
        db_spec = self._preview_config["database"]
        # Download base DB to temp for cache key computation
        tmp_db = Path(tempfile.mktemp(suffix=".sql.gz"))
        try:
            await storage_manager.download_base_db(self.project_slug, tmp_db)
            cache_key = _compute_db_cache_key(self.project_slug, db_spec, tmp_db)
        finally:
            tmp_db.unlink(missing_ok=True)

        use_cache = await storage_manager.db_cache_exists(self.project_slug, cache_key)

        if use_cache:
            await self._restore_db_cache(cache_key)
            await self._docker_up()
            await self._wait_for_db()
        else:
            await self._docker_up()
            await self._wait_for_db()
            await self._import_db()
            await self._create_db_cache(cache_key)

        # 9. Composer install
        await self._composer_install()

        # 10. Import files from S3
        await self._import_files()

        # 11. Deploy steps and deploy script
        await self._run_deploy_steps("new")
        await self._run_project_deploy_script("new")

        # 12. Activate Redis cache backend (phase 2)
        await self._activate_redis_cache()

        await self._reload_webserver()

        # Done — traffic is proxied via wake_preview middleware

    # ------------------------------------------------------------------
    # Update preview (cloud)
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

        # Git pull
        await self._step_git_pull()

        # Generate and upload compose + settings
        await self._generate_compose()
        self._write_internal_settings()
        await self._upload_compose_and_settings()

        # Docker up + deploy (images already present from initial deploy)
        await self._docker_up()
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
        create_task = asyncio.ensure_future(cloud_manager.create_vm(name))
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
            f"(which aws >/dev/null 2>&1 || pip3 install -q awscli --break-system-packages)"
        )
        proc = await self._executor.run_shell(setup_cmd)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"VM setup failed: {stderr.decode().strip()}")

        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, True, "")

    async def _step_pull_images(self):
        """Configure VM to use private registry and pull images."""
        if not settings.docker_registry:
            return

        step = "pull-images"
        await self._log_step_start(step)
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

        elapsed = time.monotonic() - t0
        await self._log_step_end(step, elapsed, True, f"{DIM}{registry_host}{RESET}")

    async def _step_clone_repo(self):
        """Clone the repository on the VM."""
        step = "clone-repo"
        await self._log_step_start(step)
        t0 = time.monotonic()

        # Get org-specific GitLab token
        gitlab_url, token = await self._get_gitlab_credentials()
        from urllib.parse import urlparse
        parsed = urlparse(gitlab_url)

        # Get project path from DB
        proj = await get_project(self.project_id)
        if not proj or not proj.get("gitlab_project_path"):
            raise RuntimeError(f"Project path not found for project_id={self.project_id}")
        project_path = proj["gitlab_project_path"]

        clone_url = f"https://oauth2:{token}@{parsed.hostname}/{project_path}.git"
        code_dir = f"{VM_PREVIEW_DIR}/code"

        clone_cmd = (
            f"rm -rf {code_dir}/* {code_dir}/.* 2>/dev/null; "
            f"git clone --depth 1 --branch {self.branch} '{clone_url}' {code_dir}"
        )
        proc = await self._executor.run_shell(clone_cmd)
        stdout, stderr = await self._stream_progress(proc, step, t0, 120)
        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            await self._log_step_end(step, elapsed, False, "")
            raise RuntimeError(f"[{step}] Clone failed (exit {proc.returncode})")

        await self._log_step_end(step, elapsed, True, "")

    async def _step_git_pull(self):
        """Update code on the VM via git pull."""
        step = "git-pull"
        await self._log_step_start(step)
        t0 = time.monotonic()

        # Get org-specific GitLab token
        gitlab_url, token = await self._get_gitlab_credentials()
        from urllib.parse import urlparse
        parsed = urlparse(gitlab_url)

        # Get project path from DB
        proj = await get_project(self.project_id)
        if not proj or not proj.get("gitlab_project_path"):
            raise RuntimeError(f"Project path not found for project_id={self.project_id}")
        project_path = proj["gitlab_project_path"]

        remote_url = f"https://oauth2:{token}@{parsed.hostname}/{project_path}.git"
        code_dir = f"{VM_PREVIEW_DIR}/code"

        pull_cmd = (
            f"cd {code_dir} && "
            f"git remote set-url origin '{remote_url}' && "
            f"git fetch origin {self.branch} && "
            f"git reset --hard origin/{self.branch}"
        )
        proc = await self._executor.run_shell(pull_cmd)
        stdout, stderr = await self._stream_progress(proc, step, t0, 120)
        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            await self._log_step_end(step, elapsed, False, "")
            raise RuntimeError(f"[{step}] Git pull failed (exit {proc.returncode})")

        await self._log_step_end(step, elapsed, True, "")

    async def _get_gitlab_credentials(self) -> tuple[str, str]:
        """Get GitLab URL and access token for the organization."""
        if self.org_id:
            from app.routes.gitlab import _get_org_gitlab_token
            return await _get_org_gitlab_token(self.org_id)

        # Fallback: look up org_id from org_slug
        if self.org_slug:
            from app.database import get_organization_by_slug
            org = await get_organization_by_slug(self.org_slug)
            if org:
                self.org_id = org["id"]
                from app.routes.gitlab import _get_org_gitlab_token
                return await _get_org_gitlab_token(self.org_id)

        raise RuntimeError("No org_id or org_slug available to get GitLab credentials")

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
            # Root ping
            proc = await self._executor.run_shell(
                f"docker exec {db_container} mysqladmin ping -h localhost -u root -proot 2>/dev/null"
            )
            await proc.communicate()
            if proc.returncode != 0:
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
                    f"{DIM}MySQL ready after {attempt + 1} attempt(s){RESET}",
                )
                return

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
        """Download base DB from S3 to VM and import into MySQL."""
        step = "import-db"
        await self._log_step_start(step)
        t0 = time.monotonic()

        db_container = f"{self.container_prefix}-db"
        s3_key = f"base-files/{self.project_slug}/db.sql.gz"

        # Log file size info
        status = await storage_manager.get_base_files_status(self.project_slug)
        if status.get("db"):
            size_mb = status["db"].get("size_bytes", 0) / (1024 * 1024)
            await self._log_raw(f"{DIM}Dump size: {size_mb:.1f} MB (compressed){RESET}\n")

        await self._log_raw(f"{DIM}Downloading and importing database...{RESET}\n")

        # Download from S3 to VM, then pipe to mysql
        import_cmd = (
            f"aws s3 cp s3://{storage_manager.bucket}/{s3_key} - "
            f"--endpoint-url {settings.hetzner_s3_endpoint} "
            f"| gunzip | docker exec -e MYSQL_PWD=drupal -i {db_container} mysql -u drupal drupal"
        )

        # Set AWS credentials on the VM for the S3 download
        env_cmd = (
            f"export AWS_ACCESS_KEY_ID={settings.hetzner_s3_access_key} && "
            f"export AWS_SECRET_ACCESS_KEY={settings.hetzner_s3_secret_key} && "
        )
        proc = await self._executor.run_shell(env_cmd + import_cmd)
        stdout, stderr = await self._stream_progress(proc, step, t0, TIMEOUT_IMPORT_DB)
        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            await self._log_step_end(step, elapsed, False, "")
            output = (stdout + stderr)[-2000:]
            raise RuntimeError(f"[{step}] Failed (exit {proc.returncode}):\n{output}")

        await self._log_step_end(step, elapsed, True, "")

    async def _import_files(self):
        """Download base files from S3 to VM and extract."""
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

        s3_key = f"base-files/{self.project_slug}/files.tar.gz"
        docroot = self._preview_config.get("docroot", "web") if self._preview_config else "web"
        public_path = "sites/default/files"
        if self._preview_config:
            public_path = self._preview_config.get("env", {}).get("PREV_FILE_PUBLIC_PATH", public_path)

        files_dir = f"{VM_PREVIEW_DIR}/code/{docroot}/{public_path}"

        # Log file size info
        status = await storage_manager.get_base_files_status(self.project_slug)
        if status.get("files"):
            size_mb = status["files"].get("size_bytes", 0) / (1024 * 1024)
            await self._log_raw(f"{DIM}Archive size: {size_mb:.1f} MB{RESET}\n")

        # Stream directly from S3 into tar (no temp file needed)
        await self._log_raw(f"{DIM}Downloading and extracting files...{RESET}\n")
        import_cmd = (
            f"export AWS_ACCESS_KEY_ID={settings.hetzner_s3_access_key} && "
            f"export AWS_SECRET_ACCESS_KEY={settings.hetzner_s3_secret_key} && "
            f"mkdir -p {files_dir} && "
            f"aws s3 cp s3://{storage_manager.bucket}/{s3_key} - "
            f"--endpoint-url {settings.hetzner_s3_endpoint} | "
            f"tar xzf - -C {files_dir} && "
            f"chown -R 33:33 {files_dir} && "
            f"chmod -R a+rX {files_dir} && "
            f"echo \"Extracted $(find {files_dir} -type f | wc -l) files\""
        )
        proc = await self._executor.run_shell(import_cmd)
        stdout, stderr = await self._stream_progress(proc, step, t0, TIMEOUT_IMPORT_FILES)
        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            # Clean up partial extraction to free disk space
            cleanup_cmd = f"rm -rf {files_dir} && mkdir -p {files_dir}"
            cleanup_proc = await self._executor.run_shell(cleanup_cmd)
            await cleanup_proc.communicate()
            await self._log_raw(f"{DIM}Cleaned up partial files to free disk space{RESET}\n")

            await self._log_step_end(step, elapsed, False, "")
            output = (stdout + stderr)[-2000:]
            raise RuntimeError(f"[{step}] Import failed (exit {proc.returncode}):\n{output}")

        await self._log_step_end(step, elapsed, True, "")

    async def _restore_db_cache(self, cache_key: str):
        """Download DB cache from S3 to VM and restore the Docker volume."""
        step = "restore-db-cache"
        await self._log_step_start(step)
        t0 = time.monotonic()

        s3_key = f"db-cache/{self.project_slug}/{cache_key}.tar.gz"
        volume_name = f"{self.container_prefix}_db_data"

        restore_cmd = (
            f"export AWS_ACCESS_KEY_ID={settings.hetzner_s3_access_key} && "
            f"export AWS_SECRET_ACCESS_KEY={settings.hetzner_s3_secret_key} && "
            f"docker volume create {volume_name} && "
            f"aws s3 cp s3://{storage_manager.bucket}/{s3_key} /tmp/db-cache.tar.gz "
            f"--endpoint-url {settings.hetzner_s3_endpoint} && "
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

        await self._log_step_end(step, elapsed, True, f"{DIM}Restored from S3 cache{RESET}")

    async def _create_db_cache(self, cache_key: str):
        """Export DB volume on VM and upload to S3 cache."""
        step = "create-db-cache"
        await self._log_step_start(step)
        t0 = time.monotonic()

        db_container = f"{self.container_prefix}-db"
        volume_name = f"{self.container_prefix}_db_data"

        # Stop DB for clean snapshot
        proc = await self._executor.run_shell(f"docker stop {db_container}")
        await proc.communicate()

        try:
            export_cmd = (
                f"docker run --rm -v {volume_name}:/data:ro -v /tmp:/cache alpine "
                f"tar czf /cache/db-cache.tar.gz -C /data . && "
                f"export AWS_ACCESS_KEY_ID={settings.hetzner_s3_access_key} && "
                f"export AWS_SECRET_ACCESS_KEY={settings.hetzner_s3_secret_key} && "
                f"aws s3 cp /tmp/db-cache.tar.gz "
                f"s3://{storage_manager.bucket}/db-cache/{self.project_slug}/{cache_key}.tar.gz "
                f"--endpoint-url {settings.hetzner_s3_endpoint} && "
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
        )

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

    async def _docker_exec(self, *cmd: str, step: str, timeout: int = 120) -> str:
        """Run a command inside the PHP container on the VM."""
        php_container = f"{self.container_prefix}-php"
        shell_cmd = f"docker exec -e COLUMNS=200 {php_container} {' '.join(cmd)}"
        return await self._run_remote_shell(shell_cmd, step=step, timeout=timeout)

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

    async def _run_remote_shell(self, cmd: str, step: str, timeout: int = 120) -> str:
        """Run a shell command on the VM via SSH. Raises on failure."""
        logger.info(f"[{step}] Running on VM: {cmd}")
        await self._log_step_start(step)
        t0 = time.monotonic()

        proc = await self._executor.run_shell(cmd, cwd=f"{VM_PREVIEW_DIR}/code")

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
        """Verify base DB exists in S3."""
        exists = await storage_manager.base_db_exists(self.project_slug)
        if not exists:
            raise RuntimeError(
                f"Base database not found in S3 for project '{self.project_slug}'. "
                f"Upload with: preview push db"
            )

    def _write_internal_settings(self):
        """Write settings.preview.internal.php and ensure settings.php includes it.

        Files are written locally and then uploaded to the VM.
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
        step = "generate-compose"
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

    async def _log_step_start(self, step: str):
        await self._log_raw(f"\n{CYAN}⚙️ {step}{RESET}\n")

    async def _log_step_end(self, step: str, duration: float, success: bool, output: str):
        dur_str = _fmt_duration(duration)
        if success:
            status_line = f"{GREEN}✓ {step}{RESET} {DIM}completed in {dur_str}{RESET}\n"
            self._step_timings.append((step, duration, "ok"))
        else:
            status_line = f"{RED}✗ {step}{RESET} {DIM}failed after {dur_str}{RESET}\n"
            self._step_timings.append((step, duration, "fail"))

        if output.strip():
            self._log_buffer.append(output.strip())
            from app.websockets import deployment_log_broadcaster
            if self._deployment_id:
                await deployment_log_broadcaster.add_log(
                    self._deployment_id, output.strip() + "\n"
                )

        await self._log_raw(status_line + "\n")

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
                if pending_chunks:
                    text = b"".join(pending_chunks).decode(errors="replace")
                    pending_chunks.clear()
                    await self._log_raw(text)
        except asyncio.TimeoutError:
            raise

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
            self.project_id, self.preview_name, **fields
        )
