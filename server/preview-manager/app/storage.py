"""Object Storage (S3) manager for base files and DB cache."""

import asyncio
import logging
from functools import partial
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

from config.settings import settings

logger = logging.getLogger(__name__)


def _get_s3():
    return boto3.client(
        "s3",
        endpoint_url=settings.hetzner_s3_endpoint,
        aws_access_key_id=settings.hetzner_s3_access_key,
        aws_secret_access_key=settings.hetzner_s3_secret_key,
        config=BotoConfig(signature_version="s3v4"),
    )


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(partial(func, *args, **kwargs))


class ObjectStorageManager:
    """Manage base files and DB cache in S3-compatible object storage."""

    @property
    def bucket(self) -> str:
        return settings.hetzner_s3_bucket

    # ------------------------------------------------------------------
    # Base files
    # ------------------------------------------------------------------

    def _base_db_key(self, project: str) -> str:
        return f"base-files/{project}/db.sql.gz"

    def _base_files_key(self, project: str) -> str:
        return f"base-files/{project}/files.tar.gz"

    async def upload_base_db(
        self, project: str, file_path: Path, uncompressed_size: int = 0
    ) -> None:
        """Upload base database dump to S3."""
        s3 = _get_s3()
        key = self._base_db_key(project)
        metadata = {}
        if uncompressed_size:
            metadata["uncompressed-size"] = str(uncompressed_size)
        await _run(
            s3.upload_file,
            str(file_path), self.bucket, key,
            ExtraArgs={"Metadata": metadata} if metadata else None,
        )
        logger.info("Uploaded base DB to s3://%s/%s", self.bucket, key)

    async def upload_base_files(
        self, project: str, file_path: Path, uncompressed_size: int = 0
    ) -> None:
        """Upload base files archive to S3."""
        s3 = _get_s3()
        key = self._base_files_key(project)
        metadata = {}
        if uncompressed_size:
            metadata["uncompressed-size"] = str(uncompressed_size)
        await _run(
            s3.upload_file,
            str(file_path), self.bucket, key,
            ExtraArgs={"Metadata": metadata} if metadata else None,
        )
        logger.info("Uploaded base files to s3://%s/%s", self.bucket, key)

    async def download_base_db(self, project: str, dest_path: Path) -> None:
        """Download base database dump from S3."""
        s3 = _get_s3()
        key = self._base_db_key(project)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        await _run(s3.download_file, self.bucket, key, str(dest_path))
        logger.info("Downloaded base DB from s3://%s/%s", self.bucket, key)

    async def download_base_files(self, project: str, dest_path: Path) -> None:
        """Download base files archive from S3."""
        s3 = _get_s3()
        key = self._base_files_key(project)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        await _run(s3.download_file, self.bucket, key, str(dest_path))
        logger.info("Downloaded base files from s3://%s/%s", self.bucket, key)

    async def get_base_files_status(self, project: str) -> dict:
        """Get metadata for base DB and files. Returns dict with db/files info."""
        s3 = _get_s3()
        result = {"db": None, "files": None}

        for kind, key in [
            ("db", self._base_db_key(project)),
            ("files", self._base_files_key(project)),
        ]:
            try:
                resp = await _run(s3.head_object, Bucket=self.bucket, Key=key)
                metadata = resp.get("Metadata", {})
                result[kind] = {
                    "exists": True,
                    "size_bytes": resp["ContentLength"],
                    "modified_at": resp["LastModified"].isoformat(),
                    "uncompressed_size": int(metadata.get("uncompressed-size", 0)),
                }
            except s3.exceptions.ClientError:
                pass
            except Exception:
                pass

        return result

    async def base_db_exists(self, project: str) -> bool:
        """Check if base DB exists in S3."""
        s3 = _get_s3()
        try:
            await _run(s3.head_object, Bucket=self.bucket, Key=self._base_db_key(project))
            return True
        except Exception:
            return False

    async def get_base_db_uncompressed_size(self, project: str) -> int:
        """Get uncompressed size of base DB from S3 metadata."""
        s3 = _get_s3()
        try:
            resp = await _run(s3.head_object, Bucket=self.bucket, Key=self._base_db_key(project))
            return int(resp.get("Metadata", {}).get("uncompressed-size", 0))
        except Exception:
            return 0

    async def get_base_files_uncompressed_size(self, project: str) -> int:
        """Get uncompressed size of base files from S3 metadata."""
        s3 = _get_s3()
        try:
            resp = await _run(s3.head_object, Bucket=self.bucket, Key=self._base_files_key(project))
            return int(resp.get("Metadata", {}).get("uncompressed-size", 0))
        except Exception:
            return 0

    async def stream_base_db(self, project: str):
        """Stream base DB from S3 as an async generator of chunks."""
        s3 = _get_s3()
        key = self._base_db_key(project)
        resp = await _run(s3.get_object, Bucket=self.bucket, Key=key)
        body = resp["Body"]

        def _read_chunk():
            return body.read(64 * 1024)

        while True:
            chunk = await asyncio.to_thread(_read_chunk)
            if not chunk:
                break
            yield chunk

    async def stream_base_files(self, project: str):
        """Stream base files from S3 as an async generator of chunks."""
        s3 = _get_s3()
        key = self._base_files_key(project)
        resp = await _run(s3.get_object, Bucket=self.bucket, Key=key)
        body = resp["Body"]

        def _read_chunk():
            return body.read(64 * 1024)

        while True:
            chunk = await asyncio.to_thread(_read_chunk)
            if not chunk:
                break
            yield chunk

    # ------------------------------------------------------------------
    # DB cache
    # ------------------------------------------------------------------

    def _db_cache_key(self, project: str, cache_key: str) -> str:
        return f"db-cache/{project}/{cache_key}.tar.gz"

    async def upload_db_cache(
        self, project: str, cache_key: str, file_path: Path
    ) -> None:
        """Upload a DB cache archive to S3."""
        s3 = _get_s3()
        key = self._db_cache_key(project, cache_key)
        await _run(s3.upload_file, str(file_path), self.bucket, key)
        logger.info("Uploaded DB cache to s3://%s/%s", self.bucket, key)

    async def download_db_cache(
        self, project: str, cache_key: str, dest_path: Path
    ) -> bool:
        """Download a DB cache from S3. Returns True if found, False otherwise."""
        s3 = _get_s3()
        key = self._db_cache_key(project, cache_key)
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            await _run(s3.download_file, self.bucket, key, str(dest_path))
            return True
        except Exception:
            return False

    async def db_cache_exists(self, project: str, cache_key: str) -> bool:
        """Check if a DB cache exists in S3."""
        s3 = _get_s3()
        try:
            await _run(
                s3.head_object,
                Bucket=self.bucket,
                Key=self._db_cache_key(project, cache_key),
            )
            return True
        except Exception:
            return False

    async def delete_db_cache(self, project: str) -> int:
        """Delete all DB caches for a project. Returns count deleted."""
        s3 = _get_s3()
        prefix = f"db-cache/{project}/"
        count = 0
        try:
            resp = await _run(
                s3.list_objects_v2, Bucket=self.bucket, Prefix=prefix
            )
            objects = resp.get("Contents", [])
            if objects:
                delete_req = {"Objects": [{"Key": obj["Key"]} for obj in objects]}
                await _run(
                    s3.delete_objects, Bucket=self.bucket, Delete=delete_req
                )
                count = len(objects)
                logger.info("Deleted %d DB cache(s) for project '%s'", count, project)
        except Exception as e:
            logger.warning("Error deleting DB caches for %s: %s", project, e)
        return count


# Singleton
storage_manager = ObjectStorageManager()
