"""Hetzner Storage Box backend — SFTP-based storage via asyncssh."""

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import asyncssh

from app.storage_backend import StorageBackend
from config.settings import settings

logger = logging.getLogger(__name__)


class StorageBoxManager(StorageBackend):
    """Manage base files and DB cache on a Hetzner Storage Box via SFTP."""

    def __init__(self):
        self._host = settings.storagebox_host
        self._port = settings.storagebox_port
        self._user = settings.storagebox_user
        self._password = settings.storagebox_password or None
        self._key_path = settings.storagebox_ssh_key_path or None
        self._base = settings.storagebox_base_path.rstrip("/")

    # ------------------------------------------------------------------
    # SFTP connection helper
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _sftp(self):
        """Open an SFTP session to the Storage Box."""
        opts = {
            "host": self._host,
            "port": self._port,
            "username": self._user,
            "known_hosts": None,  # Storage boxes rotate host keys
        }
        if self._key_path:
            opts["client_keys"] = [self._key_path]
        if self._password:
            opts["password"] = self._password

        conn = await asyncssh.connect(**opts)
        try:
            sftp = await conn.start_sftp_client()
            try:
                yield sftp
            finally:
                sftp.exit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _remote_path(self, key: str) -> str:
        """Build the full remote path from a logical key."""
        if self._base:
            return f"{self._base}/{key}"
        return key

    def _meta_path(self, key: str) -> str:
        """Sidecar metadata file path."""
        return self._remote_path(key) + ".meta.json"

    def _base_db_key(self, project: str) -> str:
        return f"base-files/{project}/db.sql.gz"

    def _base_files_key(self, project: str) -> str:
        return f"base-files/{project}/files.tar.gz"

    def _db_cache_key(self, project: str, cache_key: str) -> str:
        return f"db-cache/{project}/{cache_key}.tar.gz"

    def _key_for_kind(self, project: str, kind: str) -> str:
        return self._base_db_key(project) if kind == "db" else self._base_files_key(project)

    # ------------------------------------------------------------------
    # Internal SFTP helpers
    # ------------------------------------------------------------------

    async def _mkdir_p(self, sftp, path: str):
        """Recursive mkdir — like `mkdir -p`."""
        try:
            await sftp.stat(path)
            return  # already exists
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPError):
            pass
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent:
            await self._mkdir_p(sftp, parent)
        try:
            await sftp.mkdir(path)
        except asyncssh.SFTPError:
            pass  # race condition: another process created it

    async def _ensure_parent(self, sftp, remote_path: str):
        """Ensure parent directory of remote_path exists."""
        parent = remote_path.rsplit("/", 1)[0] if "/" in remote_path else ""
        if parent:
            await self._mkdir_p(sftp, parent)

    async def _file_exists(self, sftp, path: str) -> bool:
        try:
            await sftp.stat(path)
            return True
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPError):
            return False

    async def _read_metadata(self, sftp, key: str) -> dict:
        """Read sidecar .meta.json file."""
        meta_path = self._meta_path(key)
        try:
            async with sftp.open(meta_path, "r") as f:
                content = await f.read()
                return json.loads(content)
        except Exception:
            return {}

    async def _write_metadata(self, sftp, key: str, metadata: dict):
        """Write sidecar .meta.json file."""
        meta_path = self._meta_path(key)
        await self._ensure_parent(sftp, meta_path)
        async with sftp.open(meta_path, "w") as f:
            await f.write(json.dumps(metadata))

    # ------------------------------------------------------------------
    # Base files — upload / download
    # ------------------------------------------------------------------

    async def upload_base_db(
        self, project: str, file_path: Path, uncompressed_size: int = 0
    ) -> None:
        key = self._base_db_key(project)
        remote = self._remote_path(key)
        async with self._sftp() as sftp:
            await self._ensure_parent(sftp, remote)
            await sftp.put(str(file_path), remote)
            if uncompressed_size:
                await self._write_metadata(sftp, key, {"uncompressed_size": uncompressed_size})
        logger.info("Uploaded base DB to storagebox:%s", remote)

    async def upload_base_files(
        self, project: str, file_path: Path, uncompressed_size: int = 0
    ) -> None:
        key = self._base_files_key(project)
        remote = self._remote_path(key)
        async with self._sftp() as sftp:
            await self._ensure_parent(sftp, remote)
            await sftp.put(str(file_path), remote)
            if uncompressed_size:
                await self._write_metadata(sftp, key, {"uncompressed_size": uncompressed_size})
        logger.info("Uploaded base files to storagebox:%s", remote)

    async def download_base_db(self, project: str, dest_path: Path) -> None:
        key = self._base_db_key(project)
        remote = self._remote_path(key)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._sftp() as sftp:
            await sftp.get(remote, str(dest_path))
        logger.info("Downloaded base DB from storagebox:%s", remote)

    async def download_base_files(self, project: str, dest_path: Path) -> None:
        key = self._base_files_key(project)
        remote = self._remote_path(key)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._sftp() as sftp:
            await sftp.get(remote, str(dest_path))
        logger.info("Downloaded base files from storagebox:%s", remote)

    # ------------------------------------------------------------------
    # Base files — metadata & status
    # ------------------------------------------------------------------

    async def get_base_files_status(self, project: str) -> dict:
        result = {"db": None, "files": None}
        async with self._sftp() as sftp:
            for kind, key in [
                ("db", self._base_db_key(project)),
                ("files", self._base_files_key(project)),
            ]:
                remote = self._remote_path(key)
                try:
                    attrs = await sftp.stat(remote)
                    meta = await self._read_metadata(sftp, key)
                    mtime = ""
                    if attrs.mtime is not None:
                        mtime = datetime.fromtimestamp(attrs.mtime, tz=timezone.utc).isoformat()
                    result[kind] = {
                        "exists": True,
                        "size_bytes": attrs.size or 0,
                        "modified_at": mtime,
                        "uncompressed_size": meta.get("uncompressed_size", 0),
                    }
                except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPError):
                    pass
                except Exception:
                    pass
        return result

    async def base_db_exists(self, project: str) -> bool:
        key = self._base_db_key(project)
        remote = self._remote_path(key)
        async with self._sftp() as sftp:
            return await self._file_exists(sftp, remote)

    async def get_base_db_uncompressed_size(self, project: str) -> int:
        key = self._base_db_key(project)
        remote = self._remote_path(key)
        async with self._sftp() as sftp:
            try:
                meta = await self._read_metadata(sftp, key)
                size = meta.get("uncompressed_size", 0)
                if size == 0:
                    attrs = await sftp.stat(remote)
                    size = (attrs.size or 0) * 10
                return size
            except Exception:
                return 0

    async def get_base_files_uncompressed_size(self, project: str) -> int:
        key = self._base_files_key(project)
        async with self._sftp() as sftp:
            try:
                meta = await self._read_metadata(sftp, key)
                return meta.get("uncompressed_size", 0)
            except Exception:
                return 0

    # ------------------------------------------------------------------
    # Base files — streaming
    # ------------------------------------------------------------------

    async def stream_base_db(self, project: str):
        key = self._base_db_key(project)
        remote = self._remote_path(key)
        async with self._sftp() as sftp:
            async with sftp.open(remote, "rb") as f:
                while True:
                    chunk = await f.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

    async def stream_base_files(self, project: str):
        key = self._base_files_key(project)
        remote = self._remote_path(key)
        async with self._sftp() as sftp:
            async with sftp.open(remote, "rb") as f:
                while True:
                    chunk = await f.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

    # ------------------------------------------------------------------
    # Presigned URLs — not supported
    # ------------------------------------------------------------------

    @property
    def supports_presigned_urls(self) -> bool:
        return False

    async def set_object_metadata(
        self, project: str, kind: str, uncompressed_size: int
    ) -> None:
        if not uncompressed_size:
            return
        key = self._key_for_kind(project, kind)
        async with self._sftp() as sftp:
            await self._write_metadata(sftp, key, {"uncompressed_size": uncompressed_size})
        logger.info("Set metadata for %s: uncompressed_size=%d", key, uncompressed_size)

    # ------------------------------------------------------------------
    # DB cache
    # ------------------------------------------------------------------

    async def upload_db_cache(
        self, project: str, cache_key: str, file_path: Path
    ) -> None:
        key = self._db_cache_key(project, cache_key)
        remote = self._remote_path(key)
        async with self._sftp() as sftp:
            await self._ensure_parent(sftp, remote)
            await sftp.put(str(file_path), remote)
        logger.info("Uploaded DB cache to storagebox:%s", remote)

    async def download_db_cache(
        self, project: str, cache_key: str, dest_path: Path
    ) -> bool:
        key = self._db_cache_key(project, cache_key)
        remote = self._remote_path(key)
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            async with self._sftp() as sftp:
                await sftp.get(remote, str(dest_path))
            return True
        except Exception:
            return False

    async def db_cache_exists(self, project: str, cache_key: str) -> bool:
        key = self._db_cache_key(project, cache_key)
        remote = self._remote_path(key)
        async with self._sftp() as sftp:
            return await self._file_exists(sftp, remote)

    async def delete_db_cache(self, project: str) -> int:
        prefix = f"db-cache/{project}"
        remote_dir = self._remote_path(prefix)
        count = 0
        try:
            async with self._sftp() as sftp:
                if not await self._file_exists(sftp, remote_dir):
                    return 0
                entries = await sftp.listdir(remote_dir)
                for entry in entries:
                    if entry in (".", ".."):
                        continue
                    file_path = f"{remote_dir}/{entry}"
                    try:
                        await sftp.remove(file_path)
                        count += 1
                    except Exception:
                        pass
                logger.info("Deleted %d DB cache(s) for project '%s'", count, project)
        except Exception as e:
            logger.warning("Error deleting DB caches for %s: %s", project, e)
        return count

    # ------------------------------------------------------------------
    # VM commands
    # ------------------------------------------------------------------

    def _scp_opts(self) -> str:
        """Common SCP/SFTP options for VM commands."""
        opts = (
            f"-P {self._port} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"-q"
        )
        return opts

    def _scp_target(self, remote_path: str) -> str:
        """user@host:path string for scp."""
        return f"{self._user}@{self._host}:{remote_path}"

    def vm_download_to_stdout(self, key: str) -> str:
        """Download file from Storage Box to stdout via scp + cat.

        Downloads to a temp file first since scp doesn't support stdout.
        The temp file is cleaned up after cat finishes.
        """
        remote = self._remote_path(key)
        tmp = f"/tmp/_sb_dl_{abs(hash(key)) % 0xFFFF:04x}"
        return (
            f"scp {self._scp_opts()} "
            f"{self._scp_target(remote)} {tmp} && "
            f"cat {tmp}; rm -f {tmp}"
        )

    def vm_download_to_file(self, key: str, dest: str) -> str:
        remote = self._remote_path(key)
        return f"scp {self._scp_opts()} {self._scp_target(remote)} {dest}"

    def vm_upload_from_file(self, src: str, key: str) -> str:
        remote = self._remote_path(key)
        return f"scp {self._scp_opts()} {src} {self._scp_target(remote)}"
