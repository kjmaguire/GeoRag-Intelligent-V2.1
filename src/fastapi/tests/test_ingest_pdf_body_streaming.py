"""The PDF body must be streamed to disk, not held in memory three times.

`preflight` downloaded the whole body to compute sha256 and the page count,
`_parse_body` downloaded it a second time, and the bytes were then pickled
through the subprocess pool's pipe — a second copy in the parent's send
buffer — and materialised a third time in the child, which wrote them
straight back out to /tmp, which is the only form the parser ever wanted.
A 1.5 GB scanned map atlas passes the 2 GB cap and then needs several GB of
headroom on an 8 Gi worker: cgroup OOM kill, BrokenProcessPool, one retry,
same OOM, terminal failure for a file that parses fine when streamed.

The size cap had the same shape of problem in reverse: it was applied to
``len(body)`` *after* the whole object was already resident, so the one case
it existed for — a file too big to hold — was also the one case where it
could not help.

These tests use a fake storage client that counts its calls, so "downloaded
once" and "never downloaded" are assertions rather than hopes.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
import types
from unittest.mock import patch

import pytest

from app.hatchet_workflows import ingest_pdf as mod

_MB = 1024 * 1024

#: Minimal well-formed-enough PDF: the magic bytes are what preflight reads.
_PDF = b"%PDF-1.7" + bytes([10]) + b"x" * 4096 + bytes([10]) + b"%%EOF"
_PDF_SHA = hashlib.sha256(_PDF).hexdigest()


@pytest.fixture(autouse=True)
def _fake_pikepdf():
    """Stand in for pikepdf, which is a container-only dependency.

    The point of these tests is our I/O path, not the PDF library — but the
    stub still opens the file it is given by PATH, so a regression that
    hands pikepdf a BytesIO over a full in-memory copy would fail here.
    """
    fake = types.ModuleType("pikepdf")

    class _PasswordError(Exception):
        pass

    class _Pdf:
        def __init__(self, path):
            if not isinstance(path, str):
                raise TypeError(
                    f"pikepdf.open must be given a path, got {type(path)!r} — "
                    "opening a BytesIO means the whole body is resident again"
                )
            with open(path, "rb") as fh:
                if not fh.read(5).startswith(b"%PDF-"):
                    raise ValueError("not a pdf")
            self.pages = [object()] * 3

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake.open = _Pdf
    fake.PasswordError = _PasswordError
    real = sys.modules.get("pikepdf")
    sys.modules["pikepdf"] = fake
    try:
        yield
    finally:
        if real is None:
            sys.modules.pop("pikepdf", None)
        else:
            sys.modules["pikepdf"] = real


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "pdfcache"
    d.mkdir()
    monkeypatch.setattr(mod, "_PDF_BODY_CACHE_DIR", str(d))
    return d


class FakeStorage:
    """Counts every call, and streams to the handle like the real client."""

    def __init__(self, body: bytes = _PDF, declared_size: int | None = None):
        self.body = body
        self.declared_size = declared_size
        self.get_file_calls = 0
        self.get_bytes_calls = 0
        self.head_calls = 0

    async def get_file(self, bucket, key, file_path):
        self.get_file_calls += 1
        with open(file_path, "wb") as fh:
            fh.write(self.body)

    async def get_bytes(self, bucket, key):  # pragma: no cover — must not run
        self.get_bytes_calls += 1
        return self.body

    async def head(self, bucket, key):
        self.head_calls += 1
        size = self.declared_size
        return {"size": len(self.body) if size is None else size}


class _Input:
    """Enough of IngestPdfInput for preflight; project_id=None skips the
    lifecycle DB probe, which is not what these tests are about."""

    workspace_id = "a0000000-0000-0000-0000-00000000feed"
    project_id = None
    minio_key = "reports/b1000000-0000-0000-0000-0000000000a0/atlas.pdf"


async def _preflight(storage) -> object:
    with patch.object(mod, "get_async_storage_client", lambda: storage):
        # .fn unwraps the Hatchet Task decorator.
        return await mod.preflight.fn(_Input(), ctx=None)


class TestPreflightStreams:
    @pytest.mark.asyncio
    async def test_the_body_is_downloaded_once_and_never_as_bytes(
        self, cache_dir,
    ) -> None:
        storage = FakeStorage()
        out = await _preflight(storage)

        assert out.valid is True
        assert storage.get_file_calls == 1
        # get_bytes is the whole-object-in-RAM path. Nothing on the ingest
        # route may use it, at any size.
        assert storage.get_bytes_calls == 0

    @pytest.mark.asyncio
    async def test_the_hash_is_computed_from_the_file_and_is_correct(
        self, cache_dir,
    ) -> None:
        """Streaming must not change the sha — it is the dedupe key for
        silver.reports, so a drift here silently re-ingests every document."""
        out = await _preflight(FakeStorage())
        assert out.sha256 == _PDF_SHA
        assert out.file_size == len(_PDF)

    @pytest.mark.asyncio
    async def test_the_path_is_handed_to_parse_on_preflight_out(
        self, cache_dir,
    ) -> None:
        out = await _preflight(FakeStorage())
        assert out.body_path
        assert os.path.exists(out.body_path)
        assert os.path.getsize(out.body_path) == len(_PDF)

    @pytest.mark.asyncio
    async def test_page_count_still_comes_back(self, cache_dir) -> None:
        out = await _preflight(FakeStorage())
        assert out.page_count == 3


class TestOversizeIsRejectedBeforeDownloading:
    @pytest.mark.asyncio
    async def test_a_3gb_object_is_never_downloaded_at_all(
        self, cache_dir,
    ) -> None:
        """The cap used to be applied to len(body) after the download.

        Rejecting a 3 GB upload required first pulling 3 GB into the worker's
        RAM — the exact thing the cap exists to prevent.
        """
        storage = FakeStorage(declared_size=3 * 1024 * _MB)
        out = await _preflight(storage)

        assert out.valid is False
        assert "exceeds 2 GB" in (out.error or "")
        assert storage.head_calls == 1
        assert storage.get_file_calls == 0
        assert storage.get_bytes_calls == 0

    @pytest.mark.asyncio
    async def test_a_head_that_understates_the_size_is_still_caught(
        self, cache_dir, monkeypatch,
    ) -> None:
        """HEAD is metadata; a re-uploaded object can disagree with it."""
        monkeypatch.setattr(mod, "_MAX_PDF_BYTES", 1024)
        storage = FakeStorage(body=b"%PDF-1.7" + b"x" * 4096, declared_size=10)
        out = await _preflight(storage)

        assert out.valid is False
        assert "exceeds" in (out.error or "")
        # It did have to download to find out — that is the point of the
        # second check — but the file must not be left behind.
        assert storage.get_file_calls == 1
        assert not [p for p in os.listdir(cache_dir) if p.startswith("body.")]

    @pytest.mark.asyncio
    async def test_a_backend_that_refuses_head_still_ingests(
        self, cache_dir,
    ) -> None:
        """A HEAD that fails is not a reason to reject an upload.

        The GET that follows fails for the same reason if the object is
        genuinely unreachable, so falling through is strictly safer than
        turning a permissions gap into rejected uploads.
        """
        class NoHead(FakeStorage):
            async def head(self, bucket, key):
                self.head_calls += 1
                raise RuntimeError("HeadObject not permitted for this identity")

        storage = NoHead()
        out = await _preflight(storage)

        assert out.valid is True
        assert storage.head_calls == 1
        assert storage.get_file_calls == 1


class TestRejectionsDoNotLeakFiles:
    @pytest.mark.asyncio
    async def test_a_non_pdf_leaves_nothing_behind(self, cache_dir) -> None:
        storage = FakeStorage(body=b"PK" + bytes([3, 4]) + b" a zip, not a pdf")
        out = await _preflight(storage)

        assert out.valid is False
        assert out.error == "missing %PDF- magic bytes"
        assert out.body_path is None
        assert os.listdir(cache_dir) == []

    @pytest.mark.asyncio
    async def test_a_password_protected_pdf_leaves_nothing_behind(
        self, cache_dir,
    ) -> None:
        import pikepdf

        def _boom(path):
            raise pikepdf.PasswordError("needs a passphrase")

        with patch.object(pikepdf, "open", _boom):
            out = await _preflight(FakeStorage())

        assert out.valid is False
        assert out.encrypted is True
        assert out.body_path is None
        assert os.listdir(cache_dir) == []


class TestParseReusesPreflightsFile:
    @pytest.mark.asyncio
    async def test_no_second_download_when_the_file_is_on_this_worker(
        self, cache_dir,
    ) -> None:
        storage = FakeStorage()
        out = await _preflight(storage)
        assert storage.get_file_calls == 1

        with patch.object(mod, "get_async_storage_client", lambda: storage):
            path = await mod._resolve_body_path(_Input.minio_key, out.model_dump())

        assert path == out.body_path
        assert storage.get_file_calls == 1, "parse downloaded the body again"

    @pytest.mark.asyncio
    async def test_a_missing_file_is_re_fetched_not_an_error(
        self, cache_dir,
    ) -> None:
        """Hatchet may schedule preflight and parse on different workers.

        The cached path then points at a file on a machine we are not, which
        is an ordinary cache miss — not a failure.
        """
        storage = FakeStorage()
        out = await _preflight(storage)
        os.unlink(out.body_path)

        with patch.object(mod, "get_async_storage_client", lambda: storage):
            path = await mod._resolve_body_path(_Input.minio_key, out.model_dump())

        assert os.path.exists(path)
        assert storage.get_file_calls == 2

    @pytest.mark.asyncio
    async def test_an_empty_preflight_dict_still_resolves(
        self, cache_dir,
    ) -> None:
        """`pre` is a plain dict rebuilt from Hatchet's task output."""
        storage = FakeStorage()
        with patch.object(mod, "get_async_storage_client", lambda: storage):
            path = await mod._resolve_body_path(_Input.minio_key, {})
        assert os.path.exists(path)
        assert storage.get_file_calls == 1


class TestSubprocessTakesAPath:
    def test_the_wrapper_signature_is_a_path_not_bytes(self) -> None:
        """Guards the boundary that carried the extra two copies.

        Reverting to bytes here would silently reintroduce the pickle-through
        -the-pipe copy and the child-side copy, with no test failing anywhere
        else — the parse still works, it just needs 3× the memory.
        """
        names = mod._run_parser_subprocess.__code__.co_varnames[:1]
        assert names == ("body_path",)

    def test_the_wrapper_does_not_delete_the_caller_s_file(
        self, tmp_path,
    ) -> None:
        """The parse task owns the file now.

        Deleting it in the child covered only the path where the child
        finishes — not the 3300 s hard timeout, not a BrokenProcessPool, and
        not a preflight-created body for a parse that never started.
        """
        import inspect

        src = inspect.getsource(mod._run_parser_subprocess)
        assert "_os.unlink(cached_path)" not in src
        # ...and the parse task must be the one that does it.
        assert "os.unlink(body_path)" in inspect.getsource(mod._parse_body)


class TestCacheReaper:
    def test_stale_bodies_are_removed_and_live_ones_are_not(
        self, cache_dir,
    ) -> None:
        """Every creator deletes its own file, but only if its frame runs.

        A SIGKILLed worker, a hard timeout, a broken pool and a
        preflight/parse split across workers all leave a body behind. A
        handful of orphaned 1.5 GB atlases fills a worker's ephemeral disk.
        """
        stale = cache_dir / "body.stale.pdf"
        stale.write_bytes(b"x" * 16)
        old = time.time() - (mod._PDF_BODY_CACHE_TTL_S + 3600)
        os.utime(stale, (old, old))

        fresh = cache_dir / "body.fresh.pdf"
        fresh.write_bytes(b"x" * 16)

        assert mod._reap_pdf_body_cache() == 1
        assert not stale.exists()
        assert fresh.exists(), "reaped a file a live parse could still be using"

    def test_the_ttl_outlives_the_longest_possible_parse(self) -> None:
        """3300 s hard subprocess cap + Hatchet's retry backoff.

        If the TTL ever drops below that, the reaper starts deleting bodies
        out from under running parses, which fails as a confusing
        FileNotFoundError inside the parser rather than as a cache problem.
        """
        assert mod._PDF_BODY_CACHE_TTL_S > 3300 * 1.5

    def test_a_missing_cache_dir_is_not_an_error(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            mod, "_PDF_BODY_CACHE_DIR", str(tmp_path / "never_created"),
        )
        assert mod._reap_pdf_body_cache() == 0

    @pytest.mark.asyncio
    async def test_the_reaper_runs_on_the_download_path(
        self, cache_dir,
    ) -> None:
        """Nothing else calls it, so a download is where it has to happen."""
        stale = cache_dir / "body.stale.pdf"
        stale.write_bytes(b"x" * 16)
        old = time.time() - (mod._PDF_BODY_CACHE_TTL_S + 3600)
        os.utime(stale, (old, old))

        await _preflight(FakeStorage())
        assert not stale.exists()
