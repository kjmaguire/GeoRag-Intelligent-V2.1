"""Bronze store abstraction.

Architecture reference: §04p — Bronze tier (SeaweedFS) holds:
  - Original PDF  (key: pdfs/<pdf_id>_original.pdf)
  - Normalised PDF (key: pdfs/<pdf_id>.pdf)

Contract
--------
The HTTP endpoints (routers/pdf.py) accept a ``pdf_id`` in the request body
and expect the normalised PDF at the key ``pdfs/{pdf_id}.pdf``.  Callers are
responsible for storing the normalised bytes before submitting render
requests.  If the key is absent, the endpoint returns 404.

There is no standalone preflight stage in this repository — the module that
wrote the normalised bytes and a PreflightReport JSON was removed 2026-08-28
having never had a caller.  The live ingestion path
(services/ingest/pdf_report.py) opens pikepdf directly and does not populate
this store.

Lifespan integration
--------------------
Held on app.state.bronze_store — S3BronzeStore in any environment with
object-storage credentials configured (production, and any dev environment
running the full docker-compose stack), falling back to LocalFsBronzeStore
otherwise (a bare dev shell without SeaweedFS running). See app/main.py::

    app.state.bronze_store = S3BronzeStore()  # or LocalFsBronzeStore()

    # In route handlers:
    store: BronzeStore = request.app.state.bronze_store
    pdf_bytes = await store.get("pdfs/abc123.pdf")

Storage-abstraction plan PR4 (2026-07): S3BronzeStore replaced local disk as
the production default. Before this, the /pdf/* render endpoints and the
agentic-escalation PDF tool were reading LocalFsBronzeStore exclusively —
meaning in any deployment with more than one FastAPI instance, a PDF
normalised on one instance was invisible to render requests served by
another. The ingest pipeline itself writes bytes straight to SeaweedFS via
its own client (untouched by this PR; see PR5 in the same plan), so this
gap was specifically in the read path these two call sites use.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from georag_object_storage import Bucket, ObjectNotFoundError, get_async_storage_client

logger = logging.getLogger(__name__)

_DEFAULT_BRONZE_DIR = "/var/lib/georag/bronze"


# ---------------------------------------------------------------------------
# Protocol (interface contract)
# ---------------------------------------------------------------------------


@runtime_checkable
class BronzeStore(Protocol):
    """Protocol for Bronze-tier object storage.

    Both LocalFsBronzeStore and S3BronzeStore satisfy this interface so
    callers in routers/pdf.py, agent/agentic_escalation.py, and
    routers/assessment_summary.py are decoupled from which backend is
    actually active in a given environment.

    Keys use forward-slash path notation (e.g. ``pdfs/abc123.pdf``).
    The store is responsible for mapping these to its internal addressing scheme.
    """

    async def put(self, key: str, content: bytes) -> str:
        """Write ``content`` at ``key``.

        Returns the canonical URI of the stored object
        (e.g. ``file:///var/lib/georag/bronze/pdfs/abc123.pdf`` for the local
        store, or ``s3://bronze/pdfs/abc123.pdf`` for the real impl).
        """
        ...

    async def get(self, key: str) -> bytes | None:
        """Retrieve content at ``key``.

        Returns ``None`` if the key does not exist.
        """
        ...

    async def exists(self, key: str) -> bool:
        """Return True if ``key`` exists in the store."""
        ...


# ---------------------------------------------------------------------------
# Local filesystem implementation (dev-shell fallback)
# ---------------------------------------------------------------------------


class LocalFsBronzeStore:
    """Bronze store backed by the local filesystem.

    Fallback only — used when app/main.py's lifespan hook can't build a
    StorageConfig from the environment (a bare dev shell without the full
    docker-compose stack running). Not suitable for production because:
      - No replication or redundancy.
      - No S3-compatible API (cannot be accessed by Dagster or other services
        that talk directly to SeaweedFS in production).
      - No content-addressable deduplication.
      - Invisible across instances — a PDF normalised on one FastAPI
        instance isn't visible to render requests served by another.

    See S3BronzeStore below for the production implementation (storage-
    abstraction plan PR4). The Protocol interface is intentionally minimal
    so callers are decoupled from which of the two is active.

    Configuration
    -------------
    ``BRONZE_LOCAL_DIR`` env var controls the root directory.
    Default: ``/var/lib/georag/bronze``.
    """

    def __init__(self, base_dir: str | None = None) -> None:
        self._base = Path(base_dir or os.environ.get("BRONZE_LOCAL_DIR", _DEFAULT_BRONZE_DIR))
        self._base.mkdir(parents=True, exist_ok=True)
        logger.info("LocalFsBronzeStore ready: base_dir=%s", self._base)

    def _resolve(self, key: str) -> Path:
        """Map a slash-separated key to an absolute filesystem path.

        Strips leading slashes and normalises path separators so keys behave
        like S3 object keys regardless of the operating system.
        """
        # Prevent directory traversal: resolve() collapses .. components.
        candidate = (self._base / key.lstrip("/")).resolve()
        # Guard: resolved path must stay inside the base directory.
        try:
            candidate.relative_to(self._base.resolve())
        except ValueError as exc:
            raise ValueError(f"Bronze key '{key}' would escape the base directory") from exc
        return candidate

    async def put(self, key: str, content: bytes) -> str:
        """Write bytes to the local filesystem.

        Creates parent directories as needed.  Returns a ``file://`` URI.
        """
        import asyncio  # noqa: PLC0415

        path = self._resolve(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        await asyncio.to_thread(_write)
        uri = f"file://{path}"
        logger.debug("BronzeStore PUT key=%s uri=%s bytes=%d", key, uri, len(content))
        return uri

    async def get(self, key: str) -> bytes | None:
        """Read bytes from the local filesystem.

        Returns ``None`` if the file does not exist.
        """
        import asyncio  # noqa: PLC0415

        path = self._resolve(key)

        def _read() -> bytes | None:
            if not path.exists():
                return None
            return path.read_bytes()

        result = await asyncio.to_thread(_read)
        if result is None:
            logger.debug("BronzeStore GET key=%s -> NOT FOUND", key)
        else:
            logger.debug("BronzeStore GET key=%s -> %d bytes", key, len(result))
        return result

    async def exists(self, key: str) -> bool:
        """Return True if the key exists on disk."""
        import asyncio  # noqa: PLC0415

        path = self._resolve(key)
        return await asyncio.to_thread(path.exists)


# ---------------------------------------------------------------------------
# SeaweedFS / S3-compatible implementation (production)
# ---------------------------------------------------------------------------


class S3BronzeStore:
    """Bronze store backed by SeaweedFS/Azure Blob (STORAGE_BACKEND-aware).

    Wraps georag_object_storage's factory-selected async client, scoped to
    the Bucket.BRONZE logical bucket, and adapts its exception-raising
    get_bytes() to this module's None-on-missing BronzeStore contract.
    """

    def __init__(self) -> None:
        self._storage = get_async_storage_client()

    async def put(self, key: str, content: bytes) -> str:
        """Write ``content`` at ``key``.

        Returns an ``s3://`` URI — the generic scheme, not a vendor-named
        one (SeaweedFS today, potentially something else later): GeoRAG's
        storage layer is deliberately vendor-neutral end to end, including
        at the returned-URI level.
        """
        await self._storage.put_bytes(Bucket.BRONZE, key, content)
        uri = f"s3://{Bucket.BRONZE.value}/{key}"
        logger.debug("BronzeStore PUT key=%s uri=%s bytes=%d", key, uri, len(content))
        return uri

    async def get(self, key: str) -> bytes | None:
        """Retrieve content at ``key``.

        Returns ``None`` if the key does not exist.
        """
        try:
            result = await self._storage.get_bytes(Bucket.BRONZE, key)
        except ObjectNotFoundError:
            logger.debug("BronzeStore GET key=%s -> NOT FOUND", key)
            return None
        logger.debug("BronzeStore GET key=%s -> %d bytes", key, len(result))
        return result

    async def exists(self, key: str) -> bool:
        """Return True if ``key`` exists in the store."""
        return await self._storage.exists(Bucket.BRONZE, key)
