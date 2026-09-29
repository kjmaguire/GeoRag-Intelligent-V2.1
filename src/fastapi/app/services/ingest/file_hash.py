"""Streaming SHA-256 of a file on disk, for use OFF the event loop.

ING-18 (audit 2026-09-29): several async ingesters hashed an upload with
``hashlib.sha256(path.read_bytes())`` directly inside ``async def`` - the
whole file read and hashed on the event loop, stalling every other task on
the worker for the duration, and the whole file held in memory besides.
Call as ``await asyncio.to_thread(sha256_file, path)``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_BLOCK = 1024 * 1024


def sha256_file(path: str | Path) -> str:
    """Hex SHA-256 of *path*, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["sha256_file"]
