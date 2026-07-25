"""boto3-backed synchronous S3-compatible object storage.

Client construction and error handling here are lifted from the
already-correct ``S3Resource`` in ``src/dagster/georag_dagster/resources.py``
(the one existing reusable boto3 wrapper in the codebase); that class
becomes a thin delegating wrapper around this one in a follow-up PR.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from georag_object_storage.buckets import Bucket
from georag_object_storage.config import StorageConfig
from georag_object_storage.exceptions import ObjectNotFoundError, ObjectStorageError

_NOT_FOUND_CODES = ("404", "NoSuchKey", "NoSuchBucket")


def build_boto3_client(config: StorageConfig):
    """Construct a boto3 S3 client from a ``StorageConfig``.

    Public — and separated out from :class:`S3CompatibleStorage` — so a
    caller that needs the raw boto3 client rather than the higher-level
    ``ObjectStorage`` interface (Dagster's ``S3Resource.get_client()``,
    used directly by several assets for paginator/list_objects_v2 calls
    with dynamic, non-``Bucket``-enum bucket names) can share the exact
    same construction logic instead of re-declaring it.
    """
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region,
        config=Config(signature_version="s3v4"),
    )


class S3CompatibleStorage:
    """Synchronous ``ObjectStorage`` implementation backed by boto3.

    Works against any S3-compatible endpoint (SeaweedFS, MinIO, AWS S3)
    via ``config.endpoint_url``.
    """

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._client = build_boto3_client(config)

    def _bucket_name(self, bucket: Bucket) -> str:
        return self._config.bucket_name(bucket)

    def bucket_exists(self, bucket: Bucket) -> bool:
        name = self._bucket_name(bucket)
        try:
            self._client.head_bucket(Bucket=name)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in _NOT_FOUND_CODES:
                return False
            raise ObjectStorageError(str(exc)) from exc

    def ensure_bucket(self, bucket: Bucket) -> None:
        if not self.bucket_exists(bucket):
            self._client.create_bucket(Bucket=self._bucket_name(bucket))

    def put_bytes(
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
            params["Metadata"] = metadata
        try:
            self._client.put_object(**params)
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc

    def put_file(
        self,
        bucket: Bucket,
        key: str,
        file_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        extra_args: dict[str, object] = {"ContentType": content_type}
        if metadata is not None:
            extra_args["Metadata"] = metadata
        try:
            self._client.upload_file(
                Filename=file_path,
                Bucket=self._bucket_name(bucket),
                Key=key,
                ExtraArgs=extra_args,
            )
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc

    def get_bytes(self, bucket: Bucket, key: str) -> bytes:
        name = self._bucket_name(bucket)
        try:
            resp = self._client.get_object(Bucket=name, Key=key)
            return resp["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in _NOT_FOUND_CODES:
                raise ObjectNotFoundError(name, key) from exc
            raise ObjectStorageError(str(exc)) from exc

    def get_file(self, bucket: Bucket, key: str, file_path: str) -> None:
        name = self._bucket_name(bucket)
        try:
            self._client.download_file(Bucket=name, Key=key, Filename=file_path)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in _NOT_FOUND_CODES:
                raise ObjectNotFoundError(name, key) from exc
            raise ObjectStorageError(str(exc)) from exc

    def exists(self, bucket: Bucket, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket_name(bucket), Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in _NOT_FOUND_CODES:
                return False
            raise ObjectStorageError(str(exc)) from exc

    def head(self, bucket: Bucket, key: str) -> dict:
        name = self._bucket_name(bucket)
        try:
            resp = self._client.head_object(Bucket=name, Key=key)
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

    def list_keys(self, bucket: Bucket, prefix: str = "") -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket_name(bucket), Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def delete(self, bucket: Bucket, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket_name(bucket), Key=key)
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc

    def copy(
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
                params["Metadata"] = metadata
            if content_type is not None:
                params["ContentType"] = content_type
        try:
            self._client.copy_object(**params)
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc

    def presign_get(self, bucket: Bucket, key: str, expires_in: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket_name(bucket), "Key": key},
            ExpiresIn=expires_in,
        )

    def presign_put(
        self, bucket: Bucket, key: str, expires_in: int = 3600, content_type: str = "application/octet-stream"
    ) -> str:
        return self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket_name(bucket), "Key": key, "ContentType": content_type},
            ExpiresIn=expires_in,
        )
