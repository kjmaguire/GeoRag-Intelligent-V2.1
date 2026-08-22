"""``get_file`` must stream, and must not block the event loop doing it.

The async implementation used to call ``readall()`` into a bytes object and
then write it — so the one method whose whole purpose is "put this object on
disk without holding it" peaked at the object's full size in memory. On the
Hatchet worker that is a 1.5 GB PDF resident before anything has looked at
it, on a container with 8 Gi.

Mocked at the SDK boundary, like ``test_azure_sync_client.py``: there is no
Azurite in this environment, so these prove the translation logic, not the
wire protocol.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import ResourceNotFoundError

from georag_object_storage.azure_config import AzureBlobConfig
from georag_object_storage.buckets import Bucket
from georag_object_storage.exceptions import ObjectNotFoundError

pytestmark = pytest.mark.asyncio


@pytest.fixture
def config():
    return AzureBlobConfig(
        connection_string=(
            "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=eA==;"
            "EndpointSuffix=core.windows.net"
        ),
        account_url=None,
        container_names={Bucket.BRONZE: "bronze", Bucket.EXPORTS: "exports"},
    )


@pytest.fixture
def mock_service():
    with patch(
        "georag_object_storage.azure_async_client.BlobServiceClient"
    ) as mock_cls:
        service = MagicMock()
        mock_cls.from_connection_string.return_value = service
        yield service


class _Downloader:
    """Stands in for the aio StorageStreamDownloader."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.readall_calls = 0
        self.readinto_calls = 0
        self.write_threads: list[int] = []

    def chunks(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()

    async def readall(self):  # pragma: no cover — must never be reached
        self.readall_calls += 1
        return b"".join(self._chunks)

    async def readinto(self, stream):  # pragma: no cover
        self.readinto_calls += 1
        for chunk in self._chunks:
            stream.write(chunk)
        return sum(len(c) for c in self._chunks)


def _wire(mock_service, downloader):
    blob_client = MagicMock()

    async def _download_blob(*a, **kw):
        return downloader

    blob_client.download_blob = _download_blob
    (
        mock_service.get_container_client.return_value.get_blob_client
    ).return_value = blob_client
    return blob_client


def _client(config):
    from georag_object_storage.azure_async_client import AsyncAzureBlobStorage

    return AsyncAzureBlobStorage(config)


async def test_the_file_is_written_correctly(config, mock_service, tmp_path):
    chunks = [b"%PDF-1.7", b"x" * 4096, b"%%EOF"]
    downloader = _Downloader(chunks)
    _wire(mock_service, downloader)
    target = tmp_path / "out.pdf"

    await _client(config).get_file(Bucket.BRONZE, "reports/p/a.pdf", str(target))

    assert target.read_bytes() == b"".join(chunks)


async def test_the_whole_object_is_never_materialised(
    config, mock_service, tmp_path,
):
    """readall() is the bug. It must not be on this path at any size."""
    downloader = _Downloader([b"a" * 1024, b"b" * 1024])
    _wire(mock_service, downloader)

    await _client(config).get_file(
        Bucket.BRONZE, "reports/p/a.pdf", str(tmp_path / "out.bin"),
    )

    assert downloader.readall_calls == 0


async def test_writes_do_not_land_on_the_event_loop(
    config, mock_service, tmp_path,
):
    """A multi-GB download writing on the loop starves the heartbeats.

    Hatchet decides a task is dead when its heartbeat stops, so a blocking
    write loop does not merely slow the download — it gets the run
    re-queued underneath itself. This codebase has hit that shape of bug
    more than once, which is why the chunk writes go to a thread.
    """
    loop_thread = threading.get_ident()
    seen: list[int] = []

    real_to_thread = asyncio.to_thread

    async def _tracking_to_thread(fn, *args, **kwargs):
        def _wrapped(*a, **kw):
            seen.append(threading.get_ident())
            return fn(*a, **kw)

        return await real_to_thread(_wrapped, *args, **kwargs)

    downloader = _Downloader([b"a" * 4096, b"b" * 4096, b"c" * 4096])
    _wire(mock_service, downloader)

    with patch("asyncio.to_thread", _tracking_to_thread):
        await _client(config).get_file(
            Bucket.BRONZE, "reports/p/a.pdf", str(tmp_path / "out.bin"),
        )

    assert seen, "nothing was pushed off the loop — writes are blocking it"
    assert all(tid != loop_thread for tid in seen)


async def test_the_handle_is_closed_even_when_a_chunk_raises(
    config, mock_service, tmp_path,
):
    class _Exploding(_Downloader):
        def chunks(self):
            async def _gen():
                yield b"partial"
                raise RuntimeError("connection reset mid-download")

            return _gen()

    _wire(mock_service, _Exploding([]))
    target = tmp_path / "out.bin"

    from georag_object_storage.exceptions import ObjectStorageError

    with pytest.raises(ObjectStorageError):
        await _client(config).get_file(
            Bucket.BRONZE, "reports/p/a.pdf", str(target),
        )

    # The partial file is the caller's to clean up (ingest_pdf downloads to
    # a .tmp and unlinks it on any exception), but the handle must not be
    # left open — on Windows that would make the unlink itself fail.
    assert target.exists()
    target.unlink()


async def test_a_missing_blob_still_raises_object_not_found(
    config, mock_service, tmp_path,
):
    blob_client = MagicMock()

    async def _download_blob(*a, **kw):
        raise ResourceNotFoundError("nope")

    blob_client.download_blob = _download_blob
    (
        mock_service.get_container_client.return_value.get_blob_client
    ).return_value = blob_client

    with pytest.raises(ObjectNotFoundError):
        await _client(config).get_file(
            Bucket.BRONZE, "reports/p/missing.pdf", str(tmp_path / "out.bin"),
        )
