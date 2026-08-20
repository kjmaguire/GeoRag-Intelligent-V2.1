"""aioboto3-backed asynchronous S3-compatible object storage.

Same backend/error-handling shape as :mod:`sync_client`; kept as a separate
implementation rather than an async wrapper around the sync client because
aioboto3 and boto3 sessions are not interchangeable and FastAPI code must
never block the event loop with the sync client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aioboto3
from botocore.client import Config
from botocore.exceptions import ClientError

from georag_object_storage.buckets import Bucket
from georag_object_storage.config import StorageConfig
from georag_object_storage.exceptions import ObjectNotFoundError, ObjectStorageError
from georag_object_storage.metadata import validate_metadata

_NOT_FOUND_CODES = ("404", "NoSuchKey", "NoSuchBucket")


def async_client_kwargs(config: StorageConfig) -> dict:
    """Return the kwargs for ``aioboto3.Session().client("s3", **kwargs)``.

    Public — the async counterpart to :func:`sync_client.build_boto3_client`,
    for callers that need a raw aiobotocore client for operations outside
    the higher-level ``AsyncObjectStorage`` interface (dynamic/arbitrary
    bucket names — e.g. ``backup_seaweedfs.py``'s cross-bucket snapshot
    copy, or ``outbox_dispatcher.py``'s per-row target bucket). aioboto3
    clients are async context managers and can't be handed back as a plain
    object the way ``build_boto3_client()`` returns a sync boto3 client, so
    callers do their own::

        async with aioboto3.Session().client("s3", **async_client_kwargs(config)) as client:
            ...
    """
    return {
        "endpoint_url": config.endpoint_url,
        "aws_access_key_id": config.access_key,
        "aws_secret_access_key": config.secret_key,
        "region_name": config.region,
        "config": Config(signature_version="s3v4"),
    }


class AsyncS3CompatibleStorage:
    """Async ``AsyncObjectStorage`` implementation backed by aioboto3.

    Each method opens its own client via an async context manager —
    aioboto3's documented pattern — rather than holding one client open,
    since aioboto3 sessions are not safe to share across concurrent tasks.
    """

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._session = aioboto3.Session()

    def _bucket_name(self, bucket: Bucket) -> str:
        return self._config.bucket_name(bucket)

    def _client(self):
        return self._session.client("s3", **async_client_kwargs(self._config))

    async def bucket_exists(self, bucket: Bucket) -> bool:
        name = self._bucket_name(bucket)
        async with self._client() as client:
            try:
                await client.head_bucket(Bucket=name)
                return True
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _NOT_FOUND_CODES:
                    return False
                raise ObjectStorageError(str(exc)) from exc

    async def ensure_bucket(self, bucket: Bucket) -> None:
        if not await self.bucket_exists(bucket):
            async with self._client() as client:
                await client.create_bucket(Bucket=self._bucket_name(bucket))

    async def put_bytes(
        self,
        bucket: Bucket,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        params: dict[str, object] = {
            "Bucket": self._bucket_name(bucket),
            "Key": key,
            "Body": data,
            "ContentType": content_type,
        }
        if metadata is not None:
            params["Metadata"] = validate_metadata(metadata)
        async with self._client() as client:
            try:
                await client.put_object(**params)
            except ClientError as exc:
                raise ObjectStorageError(str(exc)) from exc

    async def put_file(
        self,
        bucket: Bucket,
        key: str,
        file_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        extra_args: dict[str, object] = {"ContentType": content_type}
        if metadata is not None:
            extra_args["Metadata"] = validate_metadata(metadata)
        async with self._client() as client:
            try:
                await client.upload_file(
                    Filename=file_path,
                    Bucket=self._bucket_name(bucket),
                    Key=key,
                    ExtraArgs=extra_args,
                )
            except ClientError as exc:
                raise ObjectStorageError(str(exc)) from exc

    async def get_bytes(self, bucket: Bucket, key: str) -> bytes:
        name = self._bucket_name(bucket)
        async with self._client() as client:
            try:
                resp = await client.get_object(Bucket=name, Key=key)
                async with resp["Body"] as stream:
                    return await stream.read()
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _NOT_FOUND_CODES:
                    raise ObjectNotFoundError(name, key) from exc
                raise ObjectStorageError(str(exc)) from exc

    async def get_file(self, bucket: Bucket, key: str, file_path: str) -> None:
        name = self._bucket_name(bucket)
        async with self._client() as client:
            try:
                await client.download_file(Bucket=name, Key=key, Filename=file_path)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _NOT_FOUND_CODES:
                    raise ObjectNotFoundError(name, key) from exc
                raise ObjectStorageError(str(exc)) from exc

    async def exists(self, bucket: Bucket, key: str) -> bool:
        async with self._client() as client:
            try:
                await client.head_object(Bucket=self._bucket_name(bucket), Key=key)
                return True
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _NOT_FOUND_CODES:
                    return False
                raise ObjectStorageError(str(exc)) from exc

    async def head(self, bucket: Bucket, key: str) -> dict:
        name = self._bucket_name(bucket)
        async with self._client() as client:
            try:
                resp = await client.head_object(Bucket=name, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _NOT_FOUND_CODES:
                    raise ObjectNotFoundError(name, key) from exc
                raise ObjectStorageError(str(exc)) from exc
        return {
            "size": resp.get("ContentLength"),
            "etag": resp.get("ETag", "").strip('"'),
            "last_modified": resp.get("LastModified"),
            "content_type": resp.get("ContentType"),
            "metadata": resp.get("Metadata", {}),
        }

    async def list_keys(self, bucket: Bucket, prefix: str = "") -> AsyncIterator[str]:
        async with self._client() as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket_name(bucket), Prefix=prefix):
                for obj in page.get("Contents", []):
                    yield obj["Key"]

    async def delete(self, bucket: Bucket, key: str) -> None:
        async with self._client() as client:
            try:
                await client.delete_object(Bucket=self._bucket_name(bucket), Key=key)
            except ClientError as exc:
                raise ObjectStorageError(str(exc)) from exc

    async def copy(
        self,
        src_bucket: Bucket,
        src_key: str,
        dst_bucket: Bucket,
        dst_key: str,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> None:
        params: dict[str, object] = {
            "CopySource": {"Bucket": self._bucket_name(src_bucket), "Key": src_key},
            "Bucket": self._bucket_name(dst_bucket),
            "Key": dst_key,
        }
        if metadata is not None or content_type is not None:
            params["MetadataDirective"] = "REPLACE"
            if metadata is not None:
                params["Metadata"] = validate_metadata(metadata)
            if content_type is not None:
                params["ContentType"] = content_type
        async with self._client() as client:
            try:
                await client.copy_object(**params)
            except ClientError as exc:
                raise ObjectStorageError(str(exc)) from exc

    async def presign_get(self, bucket: Bucket, key: str, expires_in: int = 3600) -> str:
        async with self._client() as client:
            return await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket_name(bucket), "Key": key},
                ExpiresIn=expires_in,
            )

    async def presign_put(
        self, bucket: Bucket, key: str, expires_in: int = 3600, content_type: str = "application/octet-stream"
    ) -> str:
        async with self._client() as client:
            return await client.generate_presigned_url(
                "put_object",
                Params={"Bucket": self._bucket_name(bucket), "Key": key, "ContentType": content_type},
                ExpiresIn=expires_in,
            )
