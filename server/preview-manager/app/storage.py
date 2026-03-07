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
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
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
        """Get uncompressed size of base DB from S3 metadata.
        If not available, estimate as compressed_size * 10 (SQL is highly compressible).
        """
        s3 = _get_s3()
        try:
            resp = await _run(s3.head_object, Bucket=self.bucket, Key=self._base_db_key(project))
            size = int(resp.get("Metadata", {}).get("uncompressed-size", 0))
            if size == 0:
                size = int(resp.get("ContentLength", 0)) * 10
            return size
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
    # Presigned URLs for direct upload
    # ------------------------------------------------------------------

    def _key_for_kind(self, project: str, kind: str) -> str:
        return self._base_db_key(project) if kind == "db" else self._base_files_key(project)

    async def generate_presigned_upload_url(
        self, project: str, kind: str, expires_in: int = 3600
    ) -> str:
        """Generate a presigned PUT URL for direct upload to S3 (<5 GB)."""
        s3 = _get_s3()
        key = self._key_for_kind(project, kind)
        url = await _run(
            s3.generate_presigned_url,
            "put_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )
        logger.info("Generated presigned upload URL for %s/%s", project, kind)
        return url

    async def create_multipart_upload(self, project: str, kind: str) -> str:
        """Initiate an S3 multipart upload. Returns the upload ID."""
        s3 = _get_s3()
        key = self._key_for_kind(project, kind)
        resp = await _run(
            s3.create_multipart_upload,
            Bucket=self.bucket, Key=key,
        )
        upload_id = resp["UploadId"]
        logger.info("Created multipart upload for %s/%s: %s", project, kind, upload_id)
        return upload_id

    async def generate_presigned_part_urls(
        self, project: str, kind: str, upload_id: str,
        num_parts: int, expires_in: int = 3600,
    ) -> list[str]:
        """Generate presigned URLs for each part of a multipart upload."""
        s3 = _get_s3()
        key = self._key_for_kind(project, kind)
        urls = []
        for part_num in range(1, num_parts + 1):
            url = await _run(
                s3.generate_presigned_url,
                "upload_part",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": part_num,
                },
                ExpiresIn=expires_in,
            )
            urls.append(url)
        logger.info("Generated %d presigned part URLs for %s/%s", num_parts, project, kind)
        return urls

    async def complete_multipart_upload(
        self, project: str, kind: str, upload_id: str,
        parts: list[dict],
    ) -> None:
        """Complete an S3 multipart upload. parts = [{"ETag": "...", "PartNumber": N}, ...]"""
        s3 = _get_s3()
        key = self._key_for_kind(project, kind)
        await _run(
            s3.complete_multipart_upload,
            Bucket=self.bucket, Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        logger.info("Completed multipart upload for %s/%s", project, kind)

    async def abort_multipart_upload(
        self, project: str, kind: str, upload_id: str,
    ) -> None:
        """Abort an S3 multipart upload."""
        s3 = _get_s3()
        key = self._key_for_kind(project, kind)
        try:
            await _run(
                s3.abort_multipart_upload,
                Bucket=self.bucket, Key=key, UploadId=upload_id,
            )
            logger.info("Aborted multipart upload %s for %s/%s", upload_id, project, kind)
        except Exception as e:
            logger.warning("Failed to abort multipart upload: %s", e)

    async def set_object_metadata(
        self, project: str, kind: str, uncompressed_size: int
    ) -> None:
        """Set metadata on an existing S3 object (copy-in-place)."""
        if not uncompressed_size:
            return
        s3 = _get_s3()
        key = self._key_for_kind(project, kind)
        await _run(
            s3.copy_object,
            Bucket=self.bucket,
            Key=key,
            CopySource={"Bucket": self.bucket, "Key": key},
            Metadata={"uncompressed-size": str(uncompressed_size)},
            MetadataDirective="REPLACE",
        )
        logger.info("Set metadata on %s: uncompressed-size=%d", key, uncompressed_size)

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
