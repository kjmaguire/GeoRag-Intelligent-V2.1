"""Uploads are not read and hashed on the event loop (ING-18 / ING-17).

``hashlib.sha256(path.read_bytes())`` inside an ``async def`` reads and
hashes the whole file on the worker's event loop, stalling every other task
on it (and holds the file in memory). The async ingesters now call
``await asyncio.to_thread(sha256_file, path)``.

las_ingester.py was the one holdout (it was waiting on the LAS depth-unit
change, ING-8); it hashes through ``to_thread(sha256_file, ...)`` too now
(audit finding 20), and is in the list below.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from app.services.ingest.file_hash import sha256_file

APP = Path(__file__).resolve().parents[1] / "app"

_INLINE_HASH = re.compile(r"hashlib\.sha256\([^)]*read_bytes\(\)")


@pytest.mark.parametrize("module", [
    "services/ingest/xlsx_ingester.py",
    "services/ingest/cameco_log_ingester.py",
    "services/ingest/csv_collar_ingester.py",
    "services/ingest/las_ingester.py",
    "hatchet_workflows/tiff_normalize.py",
    "hatchet_workflows/ingest_zip_archive.py",
])
def test_no_inline_read_and_hash(module: str) -> None:
    text = (APP / module).read_text(encoding="utf-8")
    assert not _INLINE_HASH.search(text), module


def test_las_ingester_hashes_through_to_thread() -> None:
    text = (APP / "services/ingest/las_ingester.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(sha256_file, p)" in text
    assert "import hashlib" not in text


def test_tiff_normalize_does_not_download_to_memory() -> None:
    text = (APP / "hatchet_workflows/tiff_normalize.py").read_text(encoding="utf-8")
    assert "store.get_bytes" not in text


def test_sha256_file_streams_the_same_digest(tmp_path: Path) -> None:
    data = b"x" * (3 * 1024 * 1024 + 17)
    path = tmp_path / "blob.bin"
    path.write_bytes(data)
    assert sha256_file(path) == hashlib.sha256(data).hexdigest()
