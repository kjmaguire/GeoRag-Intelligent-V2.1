"""Backend-selection seam for georag_object_storage.

``STORAGE_BACKEND`` picks the implementation. There is one value now:
``"s3_compatible"`` — SeaweedFS, MinIO and AWS S3 all speak the same API,
so it covers compose, on-premise and production alike.

``"azure_blob"`` landed 2026-07-30 with the Azure lift and was removed
2026-09-08 with the cloud (ADR-0022). The seam itself is kept, and so is
the retired value's NAME: a deployment that was never repointed hits
``_RETIRED_BACKENDS`` below and gets an error saying what happened, rather
than a ``NotImplementedError`` listing valid values and leaving the reader
to guess which one they used to have.
"""

from __future__ import annotations

import os

from georag_object_storage.async_client import AsyncS3CompatibleStorage
from georag_object_storage.config import StorageConfig
from georag_object_storage.protocols import AsyncObjectStorage, ObjectStorage
from georag_object_storage.sync_client import S3CompatibleStorage

_DEFAULT_BACKEND = "s3_compatible"
_KNOWN_BACKENDS = ("s3_compatible",)

#: Values that used to select a backend this package no longer has, and
#: what to do instead. The pattern is
#: ``app/services/ingest/ocr_engine.py``'s, and it exists for the same
#: reason: a well-formed setting naming a retired thing is the kind of
#: misconfiguration nothing else notices.
_RETIRED_BACKENDS = {
    "azure_blob": (
        "Azure Blob Storage was retired on 2026-09-08 (ADR-0022). "
        "Production object storage is AWS S3, reached through the "
        "s3_compatible backend with credentials from the ECS task role. "
        "Set STORAGE_BACKEND=s3_compatible and unset every AZURE_STORAGE_* "
        "variable; do NOT set AWS_ENDPOINT_URL, which would pin every call "
        "to one host instead of resolving the regional S3 endpoint."
    ),
}


def _reject_retired(backend: str) -> None:
    """Raise a useful error for a backend that used to exist."""
    explanation = _RETIRED_BACKENDS.get(backend)
    if explanation is not None:
        raise NotImplementedError(f"STORAGE_BACKEND={backend!r}: {explanation}")


def _backend_name() -> str:
    return os.environ.get("STORAGE_BACKEND", _DEFAULT_BACKEND)


def get_storage_client(config: StorageConfig | None = None) -> ObjectStorage:
    backend = _backend_name()
    if backend == "s3_compatible":
        return S3CompatibleStorage(config or StorageConfig.from_env())
    _reject_retired(backend)
    raise NotImplementedError(
        f"STORAGE_BACKEND={backend!r} is not implemented (known: {_KNOWN_BACKENDS})"
    )


def get_async_storage_client(config: StorageConfig | None = None) -> AsyncObjectStorage:
    backend = _backend_name()
    if backend == "s3_compatible":
        return AsyncS3CompatibleStorage(config or StorageConfig.from_env())
    _reject_retired(backend)
    raise NotImplementedError(
        f"STORAGE_BACKEND={backend!r} is not implemented (known: {_KNOWN_BACKENDS})"
    )
