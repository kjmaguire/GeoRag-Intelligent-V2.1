"""One bad ZIP member must not sink the archive, and the byte cap is real.

WHY THIS FILE EXISTS
    ``_extract_zip_into`` opened every member with no per-member ``try``. A
    corrupt, CRC-failing or password-protected member raised out of the
    extractor, and with ``run_zip_ingest`` at ``retries=0`` a 400-file
    delivery that failed on file 12 dispatched NOTHING. The nested-archive
    handler did not catch the ``RuntimeError`` zipfile raises for an encrypted
    member either.

    The zip-bomb guard summed the DECLARED central-directory sizes, which an
    attacker writes, and the copy loop was not byte-capped. Bytes are now
    counted as they are written.

    Also pinned here (same file, same audit): two shapefiles with the same stem
    in different folders must reach ingest_spatial under different logical
    names, because it replaces earlier features by that name.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.hatchet_workflows import _archive_progress
from app.hatchet_workflows import ingest_zip_archive as module
from tests.test_zip_two_phase_dispatch import COLLAR_CSV, _Harness

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_RUN = "c2000000-0000-0000-0000-000000000007"


# ---------------------------------------------------------------------------
# Archive builders
# ---------------------------------------------------------------------------


def _good_and_corrupt_zip() -> bytes:
    """collars.csv + report.pdf are good; bad.pdf is STORED with a flipped byte."""
    marker = b"CORRUPT-ME-" * 8
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a_collars.csv", COLLAR_CSV)
        zf.writestr(zipfile.ZipInfo("b_bad.pdf"), marker, zipfile.ZIP_STORED)
        zf.writestr("c_report.pdf", "%PDF-1.4")
    raw = bytearray(buf.getvalue())
    at = bytes(raw).find(marker)
    assert at > 0
    raw[at + 3] ^= 0xFF  # payload changes, stored CRC does not
    return bytes(raw)


def _good_and_encrypted_zip() -> bytes:
    """b_locked.pdf has the "encrypted" bit set in its local AND central header.

    zipfile cannot write an encrypted member (it clears the flag), so the bit
    is set in the bytes. Reading one makes ``ZipFile.open`` raise RuntimeError.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a_collars.csv", COLLAR_CSV)
        zf.writestr("b_locked.pdf", b"ciphertext")
        zf.writestr("c_report.pdf", "%PDF-1.4")
    raw = bytearray(buf.getvalue())
    name = b"b_locked.pdf"
    pos = 0
    while (pos := bytes(raw).find(b"PK\x03\x04", pos)) != -1:
        if bytes(raw[pos + 30:pos + 30 + len(name)]) == name:
            raw[pos + 6] |= 0x1
        pos += 4
    pos = 0
    while (pos := bytes(raw).find(b"PK\x01\x02", pos)) != -1:
        if bytes(raw[pos + 46:pos + 46 + len(name)]) == name:
            raw[pos + 8] |= 0x1
        pos += 4
    return bytes(raw)


def _run_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, archive: bytes):
    h = _Harness(monkeypatch, tmp_path, {"placeholder.txt": "x"})
    h.store.zip_path.write_bytes(archive)
    terminal: dict[str, Any] = {}

    async def mark_terminal(**kw: Any) -> None:
        terminal.update(kw)

    monkeypatch.setattr(_archive_progress, "mark_terminal", mark_terminal)
    return h, terminal


def _dispatched(h: Any) -> set[str]:
    return {name for kind, name in h.events if kind == "dispatch"}


# ---------------------------------------------------------------------------
# 1. Per-member isolation, end to end
# ---------------------------------------------------------------------------


async def test_a_corrupt_member_is_named_the_others_dispatch_and_the_run_is_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    h, terminal = _run_with(monkeypatch, tmp_path, _good_and_corrupt_zip())

    result = await h.run()

    assert _dispatched(h) == {"a_collars.csv", "c_report.pdf"}, h.events
    (warning,) = [w for w in result["warnings"] if w["code"] == "archive_member_failed"]
    assert "b_bad.pdf" in warning["detail"] and "BadZipFile" in warning["detail"]
    # Only the member name and the exception CLASS are carried, never its text.
    assert "CRC" not in warning["detail"]

    # The closing accounting: the row has warnings, so it is not 'completed'.
    assert h.completed["rows_written"] == 2
    assert any(w["code"] == "archive_member_failed" for w in h.completed["warnings"])
    assert module.ingest_progress.terminal_status(
        rows_written=h.completed["rows_written"], warnings=h.completed["warnings"],
    ) == "partial"
    assert terminal["status"] == "partial"
    assert "could not be extracted" in terminal["error_text"]


async def test_an_encrypted_member_gets_its_own_warning_and_the_rest_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    h, terminal = _run_with(monkeypatch, tmp_path, _good_and_encrypted_zip())

    result = await h.run()

    assert _dispatched(h) == {"a_collars.csv", "c_report.pdf"}
    codes = [w["code"] for w in result["warnings"]]
    assert "archive_member_encrypted" in codes and "archive_member_failed" not in codes
    (warning,) = [w for w in result["warnings"] if w["code"] == "archive_member_encrypted"]
    assert "b_locked.pdf" in warning["detail"]
    assert terminal["status"] == "partial"


async def test_a_clean_archive_still_closes_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a_collars.csv", COLLAR_CSV)
        zf.writestr("c_report.pdf", "%PDF-1.4")
    h, terminal = _run_with(monkeypatch, tmp_path, buf.getvalue())

    result = await h.run()

    assert result["warnings"] == []
    assert terminal["status"] == "completed" and terminal["error_text"] is None


# ---------------------------------------------------------------------------
# 1b. The extractor on its own
# ---------------------------------------------------------------------------


def test_extractor_returns_warnings_and_leaves_no_partial_file(tmp_path: Path) -> None:
    archive = tmp_path / "in.zip"
    archive.write_bytes(_good_and_corrupt_zip())
    budget = module._ExtractBudget()

    warnings = module._extract_zip_into(archive, tmp_path / "x", budget)

    assert [(w["code"], w["file"], w["reason"]) for w in warnings] == [
        ("archive_member_failed", "b_bad.pdf", "BadZipFile"),
    ]
    assert not (tmp_path / "x" / "b_bad.pdf").exists()
    assert (tmp_path / "x" / "a_collars.csv").read_text() == COLLAR_CSV


def test_bytes_are_counted_once_as_written_not_declared_twice(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", "a" * 1000)
    archive = tmp_path / "in.zip"
    archive.write_bytes(buf.getvalue())
    budget = module._ExtractBudget()

    module._extract_zip_into(archive, tmp_path / "x", budget)

    assert budget.bytes == 1000


def test_a_forged_size_header_cannot_get_past_the_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The header says 10 bytes; the stream delivers far more than the cap."""
    monkeypatch.setattr(module, "_MAX_TOTAL_UNCOMPRESSED", 4096)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bomb.csv", "0123456789")  # declared file_size == 10
    archive = tmp_path / "in.zip"
    archive.write_bytes(buf.getvalue())

    def lying_open(self: zipfile.ZipFile, name: Any, *a: Any, **k: Any) -> Any:
        return io.BytesIO(b"x" * (module._COPY_CHUNK * 3))

    monkeypatch.setattr(zipfile.ZipFile, "open", lying_open)
    budget = module._ExtractBudget()

    with pytest.raises(module._ArchiveBudgetExceeded, match="zip-bomb guard"):
        module._extract_zip_into(archive, tmp_path / "x", budget)

    assert not (tmp_path / "x" / "bomb.csv").exists(), "the partial file is removed"


def test_a_bomb_aborts_the_whole_outer_archive_not_just_one_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A breach of the cap is an archive-level refusal, never a per-member skip."""
    monkeypatch.setattr(module, "_MAX_TOTAL_UNCOMPRESSED", 100)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", "a" * 60)
        zf.writestr("b.csv", "b" * 60)  # declared total 120 > 100
    archive = tmp_path / "in.zip"
    archive.write_bytes(buf.getvalue())

    with pytest.raises(ValueError, match="zip-bomb guard"):
        module._extract_zip_into(archive, tmp_path / "x", module._ExtractBudget())


def test_a_nested_zip_that_breaches_the_cap_is_left_whole_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("big.csv", "x" * 10)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("keep.csv", "a\n")
        zf.writestr("inner.zip", inner.getvalue())
    archive = tmp_path / "in.zip"
    archive.write_bytes(outer.getvalue())
    root = tmp_path / "x"
    budget = module._ExtractBudget()
    module._extract_zip_into(archive, root, budget)
    monkeypatch.setattr(module, "_MAX_TOTAL_UNCOMPRESSED", budget.bytes + 5)

    warnings = module._expand_nested_archives(root, budget)

    assert [w["code"] for w in warnings] == ["archive_nested_zip_not_expanded"]
    assert (root / "inner.zip").exists()
    assert not (root / "inner__unzipped").exists()


def test_member_warnings_collapse_to_one_line_per_code_with_reason_class() -> None:
    summary = module._member_warning_summaries([
        {"code": "archive_member_failed", "file": "a.pdf", "reason": "BadZipFile", "detail": "d"},
        {"code": "archive_member_failed", "file": "b.las", "reason": "EOFError", "detail": "d"},
        {"code": "archive_member_encrypted", "file": "c.csv", "reason": "RuntimeError", "detail": "d"},
    ])
    by_code = {s["code"]: s["detail"] for s in summary}
    assert "2 archive member(s)" in by_code["archive_member_failed"]
    assert "a.pdf [BadZipFile]" in by_code["archive_member_failed"]
    assert "password-protected" in by_code["archive_member_encrypted"]


# ---------------------------------------------------------------------------
# 2. Same-stem shapefiles in different folders stay distinct
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, bucket: Any, key: str, data: bytes) -> None:
        self.objects[key] = data

    def put_file(self, bucket: Any, key: str, file_path: str) -> None:
        self.objects[key] = Path(file_path).read_bytes()


async def _route(
    monkeypatch: pytest.MonkeyPatch, path: Path, root: Path, workflow: str = "spatial",
) -> tuple[dict[str, int], _Store, list[Any]]:
    sent: list[Any] = []

    async def aio_run_no_wait(payload: Any) -> Any:
        sent.append(payload)
        return SimpleNamespace(workflow_run_id=f"wf-{len(sent)}")

    monkeypatch.setattr(module.ingest_spatial, "aio_run_no_wait", aio_run_no_wait)
    monkeypatch.setattr(module.tiff_normalize, "aio_run_no_wait", aio_run_no_wait)

    async def no_sleep(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    counts = dict.fromkeys(module._COUNT_KEYS, 0)
    store = _Store()
    await module._ingest_one(
        file_path=path, ext=path.suffix.lower().lstrip("."), conn=SimpleNamespace(),  # type: ignore[arg-type]
        store=store, input=module.IngestZipArchiveInput(  # type: ignore[arg-type]
            minio_key="zips/a.zip", workspace_id=_WS, project_id=_PJ, run_id=_RUN,
        ),
        counts=counts, archive_root=root,
    )
    return counts, store, sent


def _shapefile(dir_: Path, stem: str = "faults") -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    for ext in ("shp", "shx", "dbf", "prj"):
        (dir_ / f"{stem}.{ext}").write_bytes(f"{dir_.name}-{ext}".encode())
    return dir_ / f"{stem}.shp"


async def test_same_stem_shapefiles_in_different_folders_get_distinct_logical_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    keys: list[str] = []
    inner_names: list[list[str]] = []
    for year in ("2019", "2021"):
        shp = _shapefile(tmp_path / year)
        counts, store, _ = await _route(monkeypatch, shp, tmp_path)
        assert counts["spatial"] == 1
        (key, data), = store.objects.items()
        keys.append(key)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            inner_names.append(sorted(zf.namelist()))

    names = [k.rsplit("/", 1)[-1] for k in keys]
    logical = [module._safe_filename(n) for n in names]
    # ingest_spatial strips the upload stamp and replaces by what is left.
    from app.hatchet_workflows.ingest_spatial import _logical_source_name

    l19, l21 = (_logical_source_name(n) for n in logical)
    assert l19 != l21
    for name in (l19, l21):
        assert name.startswith("faults__") and name.endswith(".zip")  # stem stays readable
    # Every sidecar carries the SAME tag as its .shp, or GDAL cannot pair them.
    for names_in_bundle, logical_name in zip(inner_names, (l19, l21), strict=True):
        tag = logical_name[len("faults"):-len(".zip")]
        assert names_in_bundle == sorted(f"faults{tag}.{e}" for e in ("dbf", "prj", "shp", "shx"))


async def test_the_tag_is_deterministic_so_a_reupload_still_replaces_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    shp = _shapefile(tmp_path / "2019")
    _, first, _ = await _route(monkeypatch, shp, tmp_path)
    _, second, _ = await _route(monkeypatch, shp, tmp_path)

    strip = lambda s: next(iter(s.objects)).rsplit("/", 1)[-1].split("_", 3)[-1]  # noqa: E731
    assert strip(first) == strip(second)


async def test_a_shapefile_at_the_archive_root_keeps_its_plain_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    shp = _shapefile(tmp_path, "faults")
    _, store, _ = await _route(monkeypatch, shp, tmp_path)

    (key,) = store.objects
    assert key.endswith("_faults.zip")
    with zipfile.ZipFile(io.BytesIO(store.objects[key])) as zf:
        assert sorted(zf.namelist()) == ["faults.dbf", "faults.prj", "faults.shp", "faults.shx"]


async def test_a_loose_geojson_in_two_folders_is_also_kept_apart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    names = []
    for year in ("2019", "2021"):
        f = tmp_path / year / "faults.geojson"
        f.parent.mkdir()
        f.write_text("{}")
        _, store, _ = await _route(monkeypatch, f, tmp_path)
        names.append(next(iter(store.objects)).rsplit("/", 1)[-1])
    assert names[0] != names[1] and all(n.endswith(".geojson") for n in names)


# ---------------------------------------------------------------------------
# 3. Standalone scanned images go to tiff_normalize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ["png", "bmp", "gif", "webp", "tif", "jpg"])
async def test_standalone_images_are_routed_to_tiff_normalize(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ext: str,
) -> None:
    f = tmp_path / f"scan.{ext}"
    f.write_bytes(b"\x00\x01")

    counts, store, sent = await _route(monkeypatch, f, tmp_path)

    assert counts["tif"] == 1 and counts["unknown"] == 0
    assert next(iter(store.objects)).startswith(f"tiff/{_PJ}/")
    assert type(sent[0]).__name__ == "TiffNormalizeInput"
