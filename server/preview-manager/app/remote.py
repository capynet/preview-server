"""Remote command execution via SSH for cloud VMs."""

import asyncio
import logging

from config.settings import settings

logger = logging.getLogger(__name__)

# SSH options for non-interactive, key-based connections
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "LogLevel=ERROR",
]


class RemoteExecutor:
    """Execute commands on a remote VM via SSH."""

    def __init__(self, ip: str, user: str = "root"):
        self.ip = ip
        self.user = user
        self._key_path = settings.hetzner_ssh_private_key_path

    def _ssh_base(self) -> list[str]:
        return ["ssh", "-i", self._key_path, *SSH_OPTS, f"{self.user}@{self.ip}"]

    async def run(self, *cmd: str, cwd: str | None = None) -> asyncio.subprocess.Process:
        """Execute a command remotely. Returns Process with stdout/stderr pipes.

        The caller is responsible for reading stdout/stderr and waiting.
        This allows reuse of _stream_progress() in deployment.py.
        """
        remote_cmd = list(cmd)
        if cwd:
            remote_cmd = ["cd", cwd, "&&"] + remote_cmd

        full_cmd = self._ssh_base() + ["--"] + remote_cmd
        logger.debug("SSH exec: %s", " ".join(remote_cmd))

        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return proc

    async def run_shell(self, cmd: str, cwd: str | None = None) -> asyncio.subprocess.Process:
        """Execute a shell command remotely (for pipes etc). Returns Process."""
        if cwd:
            cmd = f"cd {cwd} && {cmd}"

        full_cmd = self._ssh_base() + ["--", cmd]
        logger.debug("SSH shell: %s", cmd)

        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return proc

    async def upload_file(self, local: str, remote: str) -> None:
        """Upload a file via SCP."""
        cmd = [
            "scp", "-i", self._key_path, *SSH_OPTS,
            local, f"{self.user}@{self.ip}:{remote}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"SCP upload failed: {stderr.decode().strip()}")

    async def download_file(self, remote: str, local: str) -> None:
        """Download a file via SCP."""
        cmd = [
            "scp", "-i", self._key_path, *SSH_OPTS,
            f"{self.user}@{self.ip}:{remote}", local,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"SCP download failed: {stderr.decode().strip()}")

    async def wait_for_ssh(self, timeout: int = 120) -> None:
        """Wait until SSH is reachable on the VM."""
        deadline = asyncio.get_event_loop().time() + timeout
        attempt = 0

        while asyncio.get_event_loop().time() < deadline:
            attempt += 1
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._ssh_base(), "--", "true",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                if proc.returncode == 0:
                    logger.info("SSH ready on %s after %d attempt(s)", self.ip, attempt)
                    return
            except (asyncio.TimeoutError, Exception):
                pass
            await asyncio.sleep(2)

        raise RuntimeError(f"SSH not ready on {self.ip} after {timeout}s")
