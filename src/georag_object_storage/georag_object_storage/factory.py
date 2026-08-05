"""Backend-selection seam for georag_object_storage.

``STORAGE_BACKEND`` picks the implementation: ``"s3_compatible"`` (default —
SeaweedFS, MinIO, and AWS S3 all speak the same API) or ``"azure_blob"``
(Azure Blob Storage, landed 2026-07-30 as part of the Azure lift — see
``azure_config.py``/``azure_sync_client.py``/``azure_async_client.py``).

The ``config`` parameter on both factory functions is typed for the
S3-compatible backend's ``StorageConfig`` — it's ignored when
``STORAGE_BACKEND=azure_blob``, since the shapes genuinely differ
(S3 is endpoint/key/secret; Azure is account/credential/container).
Callers that need to pass Azure config explicitly (tests, mainly) should
construct the Azure client directly rather than going through this factory.
"""

from __future__ import annotations

import os

from georag_object_storage.async_client import AsyncS3CompatibleStorage
from georag_object_storage.config import StorageConfig
from georag_object_storage.protocols import AsyncObjectStorage, ObjectStorage
from georag_object_storage.sync_client import S3CompatibleStorage

_DEFAULT_BACKEND = "s3_compatible"
_KNOWN_BACKENDS = ("s3_compatible", "azure_blob")


def _backend_name() -> str:
    return os.environ.get("STORAGE_BACKEND", _DEFAULT_BACKEND)


def get_storage_client(config: StorageConfig | None = None) -> ObjectStorage:
    backend = _backend_name()
    if backend == "s3_compatible":
        return S3CompatibleStorage(config or StorageConfig.from_env())
    if backend == "azure_blob":
        from georag_object_storage.azure_config import AzureBlobConfig
        from georag_object_storage.azure_sync_client import AzureBlobStorage

        return AzureBlobStorage(AzureBlobConfig.from_env())
    raise NotImplementedError(
        f"STORAGE_BACKEND={backend!r} is not implemented (known: {_KNOWN_BACKENDS})"
    )


def get_async_storage_client(config: StorageConfig | None = None) -> AsyncObjectStorage:
    backend = _backend_name()
    if backend == "s3_compatible":
        return AsyncS3CompatibleStorage(config or StorageConfig.from_env())
    if backend == "azure_blob":
        from georag_object_storage.azure_async_client import AsyncAzureBlobStorage
        from georag_object_storage.azure_config import AzureBlobConfig

        return AsyncAzureBlobStorage(AzureBlobConfig.from_env())
    raise NotImplementedError(
        f"STORAGE_BACKEND={backend!r} is not implemented (known: {_KNOWN_BACKENDS})"
    )
