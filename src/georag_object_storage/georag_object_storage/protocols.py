"""Vendor-neutral object-storage interfaces.

``Protocol``-based (matching the existing convention in
``app/services/bronze_store.py``) rather than ABC, so any class satisfying
the method signatures counts as an implementation without explicit
inheritance — including a future Azure Blob backend.

The method surface covers everything actually used across the codebase
today (confirmed via a full grep of every boto3/aioboto3 call site): no
multipart upload, since nothing calls it anywhere. Three additions came
out of the storage-abstraction plan's PR5a migration, once the Hatchet
ingestion workflows turned out to need surface PR1's original grep pass
missed: ``copy()``'s ``metadata``/``content_type`` (ingest_pdf.py's
figure-persist rename), ``put_bytes()``/``put_file()``'s ``metadata``
(tiff_normalize.py's provenance tags), and ``get_file()`` (ingest_zip_
archive.py's ``download_file``-to-disk pattern, mirroring ``put_file()``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Protocol, runtime_checkable

from georag_object_storage.buckets import Bucket


@runtime_checkable
class ObjectStorage(Protocol):
    """Synchronous object-storage interface."""

    def bucket_exists(self, bucket: Bucket) -> bool: ...

    def ensure_bucket(self, bucket: Bucket) -> None: ...

    def put_bytes(
        self,
        bucket: Bucket,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    def put_file(
        self,
        bucket: Bucket,
        key: str,
        file_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    def get_bytes(self, bucket: Bucket, key: str) -> bytes: ...

    def get_file(self, bucket: Bucket, key: str, file_path: str) -> None: ...

    def exists(self, bucket: Bucket, key: str) -> bool: ...

    def head(self, bucket: Bucket, key: str) -> dict: ...

    def list_keys(self, bucket: Bucket, prefix: str = "") -> Iterator[str]: ...

    def delete(self, bucket: Bucket, key: str) -> None: ...

    def copy(
        self,
        src_bucket: Bucket,
        src_key: str,
        dst_bucket: Bucket,
        dst_key: str,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> None: ...

    def presign_get(self, bucket: Bucket, key: str, expires_in: int = 3600) -> str: ...

    def presign_put(
        self, bucket: Bucket, key: str, expires_in: int = 3600, content_type: str = "application/octet-stream"
    ) -> str: ...


@runtime_checkable
class AsyncObjectStorage(Protocol):
    """Async object-storage interface — same surface as :class:`ObjectStorage`."""

    async def bucket_exists(self, bucket: Bucket) -> bool: ...

    async def ensure_bucket(self, bucket: Bucket) -> None: ...

    async def put_bytes(
        self,
        bucket: Bucket,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    async def put_file(
        self,
        bucket: Bucket,
        key: str,
        file_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    async def get_bytes(self, bucket: Bucket, key: str) -> bytes: ...

    async def get_file(self, bucket: Bucket, key: str, file_path: str) -> None: ...

    async def exists(self, bucket: Bucket, key: str) -> bool: ...

    async def head(self, bucket: Bucket, key: str) -> dict: ...

    def list_keys(self, bucket: Bucket, prefix: str = "") -> AsyncIterator[str]: ...

    async def delete(self, bucket: Bucket, key: str) -> None: ...

    async def copy(
        self,
        src_bucket: Bucket,
        src_key: str,
        dst_bucket: Bucket,
        dst_key: str,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> None: ...

    async def presign_get(self, bucket: Bucket, key: str, expires_in: int = 3600) -> str: ...

    async def presign_put(
        self, bucket: Bucket, key: str, expires_in: int = 3600, content_type: str = "application/octet-stream"
    ) -> str: ...
