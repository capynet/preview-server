"""Abstract storage backend interface.

All storage backends (S3, Storage Box, etc.) must implement these methods.
"""

from pathlib import Path
from typing import AsyncIterator


class StorageBackend:
    """Base class for storage backends."""

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def supports_presigned_urls(self) -> bool:
        """Whether this backend supports presigned URL uploads."""
        return False

    # ------------------------------------------------------------------
    # Base files — upload / download
    # ------------------------------------------------------------------

    async def upload_base_db(
        self, project: str, file_path: Path, uncompressed_size: int = 0
    ) -> None:
        raise NotImplementedError

    async def upload_base_files(
        self, project: str, file_path: Path, uncompressed_size: int = 0
    ) -> None:
        raise NotImplementedError

    async def download_base_db(self, project: str, dest_path: Path) -> None:
        raise NotImplementedError

    async def download_base_files(self, project: str, dest_path: Path) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Base files — metadata & status
    # ------------------------------------------------------------------

    async def get_base_files_status(self, project: str) -> dict:
        """Return {"db": {...} | None, "files": {...} | None}."""
        raise NotImplementedError

    async def base_db_exists(self, project: str) -> bool:
        raise NotImplementedError

    async def get_base_db_uncompressed_size(self, project: str) -> int:
        raise NotImplementedError

    async def get_base_files_uncompressed_size(self, project: str) -> int:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Base files — streaming
    # ------------------------------------------------------------------

    async def stream_base_db(self, project: str) -> AsyncIterator[bytes]:
        raise NotImplementedError
        yield  # type: ignore[misc]

    async def stream_base_files(self, project: str) -> AsyncIterator[bytes]:
        raise NotImplementedError
        yield  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Presigned URLs (S3-only by default)
    # ------------------------------------------------------------------

    async def generate_presigned_upload_url(
        self, project: str, kind: str, expires_in: int = 3600
    ) -> str:
        raise NotImplementedError("This backend does not support presigned URLs")

    async def create_multipart_upload(self, project: str, kind: str) -> str:
        raise NotImplementedError("This backend does not support presigned URLs")

    async def generate_presigned_part_urls(
        self, project: str, kind: str, upload_id: str,
        num_parts: int, expires_in: int = 3600,
    ) -> list[str]:
        raise NotImplementedError("This backend does not support presigned URLs")

    async def complete_multipart_upload(
        self, project: str, kind: str, upload_id: str,
        parts: list[dict],
    ) -> None:
        raise NotImplementedError("This backend does not support presigned URLs")

    async def abort_multipart_upload(
        self, project: str, kind: str, upload_id: str,
    ) -> None:
        raise NotImplementedError("This backend does not support presigned URLs")

    async def set_object_metadata(
        self, project: str, kind: str, uncompressed_size: int
    ) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # DB cache
    # ------------------------------------------------------------------

    async def upload_db_cache(
        self, project: str, cache_key: str, file_path: Path
    ) -> None:
        raise NotImplementedError

    async def download_db_cache(
        self, project: str, cache_key: str, dest_path: Path
    ) -> bool:
        raise NotImplementedError

    async def db_cache_exists(self, project: str, cache_key: str) -> bool:
        raise NotImplementedError

    async def delete_db_cache(self, project: str) -> int:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # VM commands — shell commands for use on preview VMs
    # ------------------------------------------------------------------

    def vm_download_to_stdout(self, key: str) -> str:
        """Return shell command that downloads `key` and writes to stdout.

        Used for streaming pipelines: ``cmd | pv | gunzip | mysql``.
        """
        raise NotImplementedError

    def vm_download_to_file(self, key: str, dest: str) -> str:
        """Return shell command that downloads `key` to *dest* on the VM."""
        raise NotImplementedError

    def vm_upload_from_file(self, src: str, key: str) -> str:
        """Return shell command that uploads *src* from the VM to *key*."""
        raise NotImplementedError
