"""Backend-selection seam for georag_object_storage.

``STORAGE_BACKEND`` picks the implementation. Only ``"s3_compatible"``
exists today — SeaweedFS, MinIO, and AWS S3 all speak the same API, which
covers every environment GeoRAG runs in right now. A future Azure Blob
backend plugs in here without touching any call site; this is the
deliberate, inert extension seam for that later phase.
"""

from __future__ import annotations

import os

from georag_object_storage.async_client import AsyncS3CompatibleStorage
from georag_object_storage.config import StorageConfig
from georag_object_storage.protocols import AsyncObjectStorage, ObjectStorage
from georag_object_storage.sync_client import S3CompatibleStorage

_DEFAULT_BACKEND = "s3_compatible"


def _backend_name() -> str:
    return os.environ.get("STORAGE_BACKEND", _DEFAULT_BACKEND)


def get_storage_client(config: StorageConfig | None = None) -> ObjectStorage:
    backend = _backend_name()
    if backend != "s3_compatible":
        raise NotImplementedError(f"STORAGE_BACKEND={backend!r} is not implemented yet")
    return S3CompatibleStorage(config or StorageConfig.from_env())


def get_async_storage_client(config: StorageConfig | None = None) -> AsyncObjectStorage:
    backend = _backend_name()
    if backend != "s3_compatible":
        raise NotImplementedError(f"STORAGE_BACKEND={backend!r} is not implemented yet")
    return AsyncS3CompatibleStorage(config or StorageConfig.from_env())
