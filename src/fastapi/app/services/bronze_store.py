"""Bronze store abstraction — Phase 1.A stub.

THIS IS A STUB.  The real implementation (SeaweedFS via S3-compatible API)
will replace LocalFsBronzeStore in a follow-up phase once the SeaweedFS
service is integrated with the ingestion pipeline.  The Protocol interface
defined here is the contract that the real impl must satisfy.

Architecture reference: §04p — Bronze tier (SeaweedFS) holds:
  - Original PDF  (key: pdfs/<pdf_id>_original.pdf)
  - Normalised PDF after Stage 1 preflight (key: pdfs/<pdf_id>.pdf)
  - PreflightReport JSON (key: pdfs/<pdf_id>_preflight.json)

Phase 1.A contract
------------------
The HTTP endpoints (routers/pdf.py) accept a ``pdf_id`` in the request body
and expect the normalised PDF at the key ``pdfs/{pdf_id}.pdf``.  Callers are
responsible for running preflight and storing the normalised bytes before
submitting render requests.  If the key is absent, the endpoint returns 404.

Lifespan integration
--------------------
Held on app.state.bronze_store::

    app.state.bronze_store = LocalFsBronzeStore()

    # In route handlers:
    store: BronzeStore = request.app.state.bronze_store
    pdf_bytes = await store.get("pdfs/abc123.pdf")
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_DEFAULT_BRONZE_DIR = "/var/lib/georag/bronze"


# ---------------------------------------------------------------------------
# Protocol (interface contract)
# ---------------------------------------------------------------------------


@runtime_checkable
class BronzeStore(Protocol):
    """Protocol for Bronze-tier object storage.

    Both LocalFsBronzeStore (Phase 1.A stub) and the future SeaweedFsBronzeStore
    must satisfy this interface so callers in routers/pdf.py are decoupled from
    the storage backend.

    Keys use forward-slash path notation (e.g. ``pdfs/abc123.pdf``).
    The store is responsible for mapping these to its internal addressing scheme.
    """

    async def put(self, key: str, content: bytes) -> str:
        """Write ``content`` at ``key``.

        Returns the canonical URI of the stored object
        (e.g. ``file:///var/lib/georag/bronze/pdfs/abc123.pdf`` for the local
        stub, or ``seaweedfs://vol1/pdfs/abc123.pdf`` for the real impl).
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
# Local filesystem implementation (Phase 1.A stub)
# ---------------------------------------------------------------------------


class LocalFsBronzeStore:
    """Bronze store backed by the local filesystem.

    STUB — for development and CI only.  Not suitable for production because:
      - No replication or redundancy.
      - No S3-compatible API (cannot be accessed by Dagster or other services
        that will talk directly to SeaweedFS in production).
      - No content-addressable deduplication.

    Replace with SeaweedFsBronzeStore (or an S3-client backed impl) in the
    Phase 1.B follow-up.  The Protocol interface is intentionally minimal so
    the swap is a one-line change in the lifespan hook.

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
