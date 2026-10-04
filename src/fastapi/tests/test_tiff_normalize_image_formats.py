"""tiff_normalize: image formats, partial runs, honest raster messages.

Runs the real ``normalize`` task body against a fake object store and fake
progress recorders (the same boundary tests/test_raster_ocr_routing.py uses),
with the real Pillow wrap.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.services.ingest.raster_metadata import RasterCaptureResult

WS = "a0000000-0000-0000-0000-00000000feed"
PJ = "b1000000-0000-0000-0000-0000000000a0"
RUN = "c2000000-0000-0000-0000-000000000009"


class _Store:
    def __init__(self, payload: bytes, *, present_meta: dict | None = None) -> None:
        self.payload = payload
        self.put: list[dict[str, Any]] = []
        self.present_meta = present_meta  # derived PDF already there

    def get_file(self, bucket: Any, key: str, file_path: str) -> None:
        Path(file_path).write_bytes(self.payload)

    def put_bytes(self, bucket: Any, key: str, data: bytes, **kw: Any) -> None:
        self.put.append({"key": key, "data": data, **kw})

    def head(self, bucket: Any, key: str) -> dict:
        if self.put:
            return {"size": len(self.put[-1]["data"]),
                    "metadata": self.put[-1].get("metadata") or {}}
        if self.present_meta is not None:
            return {"size": 1234, "metadata": self.present_meta}
        raise FileNotFoundError(key)


@pytest.fixture
def env(monkeypatch):
    from app.hatchet_workflows import tiff_normalize as tn

    calls: dict[str, Any] = {
        "completed_by_run": [], "legacy_completed": 0, "broadcast": [],
        "dispatched": [], "to_thread": [],
    }

    async def _noop(**kw):
        return None

    async def _lookup(**kw):
        return RUN

    async def _done_by_run(**kw):
        calls["completed_by_run"].append(kw)
        return True

    async def _done(**kw):
        calls["legacy_completed"] += 1

    async def _bcast(**kw):
        calls["broadcast"].append(kw)

    monkeypatch.setattr(tn.ingest_progress, "mark_started", _noop)
    monkeypatch.setattr(tn.ingest_progress, "lookup_active_run_id", _lookup)
    monkeypatch.setattr(tn.ingest_progress, "mark_completed_by_run", _done_by_run)
    monkeypatch.setattr(tn.ingest_progress, "mark_completed", _done)
    monkeypatch.setattr(tn.ingest_progress, "broadcast_terminal", _bcast)

    class _Ref:
        workflow_run_id = "wf-1"

    async def _dispatch(payload):
        calls["dispatched"].append(payload)
        return _Ref()

    monkeypatch.setattr(tn.ingest_pdf, "aio_run_no_wait", _dispatch)

    async def _capture(**kw):
        return RasterCaptureResult(written=False, reason="no_crs")

    monkeypatch.setattr(tn, "persist_raster_metadata", _capture)

    real_to_thread = tn.asyncio.to_thread

    async def _spy_to_thread(fn, *a, **kw):
        calls["to_thread"].append(getattr(fn, "__name__", repr(fn)))
        return await real_to_thread(fn, *a, **kw)

    monkeypatch.setattr(tn.asyncio, "to_thread", _spy_to_thread)

    def _use(store: _Store) -> None:
        monkeypatch.setattr(tn, "get_storage_client", lambda: store)

    return tn, calls, _use


def _input(tn, name: str):
    return tn.TiffNormalizeInput(
        workspace_id=WS, project_id=PJ,
        minio_key=f"tiff/{PJ}/20261004_101500_{name}",
        file_size=1000, correlation_token="tok",
    )


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (80, 50), (255, 255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def _tiff(n: int) -> bytes:
    imgs = [Image.new("L", (40, 30), i * 9) for i in range(n)]
    buf = io.BytesIO()
    imgs[0].save(buf, "TIFF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


@pytest.mark.parametrize("name", ["scan.png", "scan.bmp", "scan.gif", "scan.webp", "scan.JPG"])
@pytest.mark.asyncio
async def test_each_new_extension_is_accepted_and_dispatched(env, name) -> None:
    tn, calls, use = env
    fmt = {"png": "PNG", "bmp": "BMP", "gif": "GIF", "webp": "WEBP", "jpg": "JPEG"}[
        name.rsplit(".", 1)[1].lower()
    ]
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(buf, fmt)
    store = _Store(buf.getvalue())
    use(store)

    out = await tn.normalize.fn(_input(tn, name), object())

    assert out.page_count == 1
    assert out.ingest_pdf_workflow_run_id == "wf-1"
    assert store.put[0]["data"].startswith(b"%PDF-")
    assert calls["legacy_completed"] == 1          # clean run: unchanged path
    assert calls["completed_by_run"] == []
    assert out.warnings == []


@pytest.mark.asyncio
async def test_the_wrap_runs_off_the_event_loop(env) -> None:
    tn, calls, use = env
    use(_Store(_png()))
    await tn.normalize.fn(_input(tn, "scan.png"), object())
    assert "tiff_to_pdf" in calls["to_thread"]


@pytest.mark.asyncio
async def test_an_unsupported_extension_is_refused_with_the_supported_list(env) -> None:
    tn, _calls, use = env
    use(_Store(b"x"))
    with pytest.raises(tn.TiffNormalizeError, match=r"cannot wrap '\.xyz'.*\.webp"):
        await tn.normalize.fn(_input(tn, "scan.xyz"), object())


def test_supported_extensions_match_the_php_accept_list_and_the_zip_list() -> None:
    from app.hatchet_workflows.ingest_zip_archive import _RASTER_EXTS
    from app.hatchet_workflows.tiff_normalize import SUPPORTED_EXTENSIONS

    controller = (
        Path(__file__).resolve().parents[3]
        / "app" / "Http" / "Controllers" / "Api" / "V1" / "UploadController.php"
    )
    if not controller.exists():
        pytest.skip("Laravel app not mounted")
    m = re.search(r"private const RASTER_REPORT_EXTS = \[([^\]]*)\];", controller.read_text())
    assert m
    php = set(re.findall(r"'([a-z0-9]+)'", m.group(1)))

    assert {e.lstrip(".") for e in SUPPORTED_EXTENSIONS} == php
    assert php == set(_RASTER_EXTS)


class TestFrameLossMakesTheRunPartial:
    @pytest.mark.asyncio
    async def test_truncation_emits_a_warning_and_a_partial_close(
        self, env, monkeypatch,
    ) -> None:
        from app.services.ingest import tiff_to_pdf as wrap

        tn, calls, use = env
        monkeypatch.setattr(wrap, "MAX_FRAMES", 3)
        store = _Store(_tiff(5))
        use(store)

        out = await tn.normalize.fn(_input(tn, "report.tif"), object())

        (w,) = out.warnings
        assert w["code"] == "raster_frames_truncated"
        assert "3 of 5" in w["detail"]
        assert "Frames 4-5" in w["detail"]
        assert out.truncated_at_cap is True and out.total_frames == 5

        (done,) = calls["completed_by_run"]
        assert done["run_id"] == RUN
        assert done["warnings"] == out.warnings
        assert calls["legacy_completed"] == 0
        assert calls["broadcast"][0]["status"] == "partial"
        assert store.put[0]["metadata"]["tiff_total_frames"] == "5"
        assert store.put[0]["metadata"]["tiff_truncated"] == "true"

    @pytest.mark.asyncio
    async def test_the_warning_survives_a_retry_that_skips_the_wrap(self, env) -> None:
        tn, calls, use = env
        use(_Store(_tiff(2), present_meta={
            "derived_from_tiff_sha256": __import__("hashlib").sha256(_tiff(2)).hexdigest(),
            "tiff_frames": "3", "tiff_truncated": "true", "tiff_total_frames": "9",
        }))

        out = await tn.normalize.fn(_input(tn, "report.tif"), object())

        assert out.normalize_skipped is True
        assert [w["code"] for w in out.warnings] == ["raster_frames_truncated"]
        assert "3 of 9" in out.warnings[0]["detail"]
        assert calls["broadcast"][0]["status"] == "partial"

    @pytest.mark.asyncio
    async def test_an_old_derived_pdf_without_the_new_tags_still_warns(self, env) -> None:
        tn, _calls, use = env
        use(_Store(_tiff(2), present_meta={
            "derived_from_tiff_sha256": __import__("hashlib").sha256(_tiff(2)).hexdigest(),
            "tiff_frames": "500", "tiff_truncated": "true",
        }))

        out = await tn.normalize.fn(_input(tn, "report.tif"), object())

        assert out.warnings[0]["code"] == "raster_frames_truncated"
        assert "remaining frames are NOT in the index" in out.warnings[0]["detail"]

    @pytest.mark.asyncio
    async def test_an_animated_gif_warns_that_extra_frames_were_ignored(self, env) -> None:
        tn, calls, use = env
        frames = []
        for i in range(4):
            f = Image.new("RGB", (30, 20), (i * 60, 10, 200 - i * 40))
            f.putpixel((i, i), (255, 255, 255))
            frames.append(f.convert("P", palette=Image.Palette.ADAPTIVE, colors=8))
        buf = io.BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
        use(_Store(buf.getvalue()))

        out = await tn.normalize.fn(_input(tn, "anim.gif"), object())

        assert [w["code"] for w in out.warnings] == ["image_frames_ignored"]
        assert out.frames_ignored == 3
        assert calls["broadcast"][0]["status"] == "partial"

    @pytest.mark.asyncio
    async def test_a_complete_multi_page_tiff_stays_completed(self, env) -> None:
        tn, calls, use = env
        use(_Store(_tiff(4)))

        out = await tn.normalize.fn(_input(tn, "report.tif"), object())

        assert out.warnings == []
        assert calls["legacy_completed"] == 1
        assert calls["completed_by_run"] == []


class TestRasterSkippedMessageSaysWhatHappened:
    def _set(self, monkeypatch, tn, **kw) -> None:
        async def _capture(**_kw):
            return RasterCaptureResult(is_measurement_raster=True, **kw)

        monkeypatch.setattr(tn, "persist_raster_metadata", _capture)

    @pytest.mark.asyncio
    async def test_recorded_row_is_reported_as_recorded(self, env, monkeypatch) -> None:
        tn, calls, use = env
        use(_Store(b"II*\x00"))
        self._set(monkeypatch, tn, written=True, reason="recorded", crs="EPSG:32613")
        out = await tn.normalize.fn(_input(tn, "mag.tif"), object())
        assert out.ocr_skipped_reason.startswith("Recorded as a raster layer (EPSG:32613).")
        assert "queryable" in out.ocr_skipped_reason

    @pytest.mark.asyncio
    async def test_a_failed_write_is_not_called_recorded(self, env, monkeypatch) -> None:
        tn, calls, use = env
        use(_Store(b"II*\x00"))
        self._set(
            monkeypatch, tn, written=False, reason="persist_failed: boom",
            crs="EPSG:32613",
        )
        out = await tn.normalize.fn(_input(tn, "mag.tif"), object())
        text = out.ocr_skipped_reason
        assert "Recorded as" not in text
        assert text.startswith("NOT recorded as a raster layer: the database write failed")
        assert "queryable" not in text
        assert calls["completed_by_run"][0]["rows_written"] == 0

    @pytest.mark.asyncio
    async def test_a_retry_says_already_recorded(self, env, monkeypatch) -> None:
        tn, _calls, use = env
        use(_Store(b"II*\x00"))
        self._set(
            monkeypatch, tn, written=False, reason="already_recorded",
            crs="EPSG:32613",
        )
        out = await tn.normalize.fn(_input(tn, "mag.tif"), object())
        assert out.ocr_skipped_reason.startswith("Already recorded as a raster layer")
        assert "queryable" in out.ocr_skipped_reason

    @pytest.mark.asyncio
    async def test_a_dem_without_a_crs_warns_raster_crs_missing(
        self, env, monkeypatch,
    ) -> None:
        tn, calls, use = env
        use(_Store(b"II*\x00"))
        self._set(
            monkeypatch, tn, written=False, reason="no_crs_measurement_grid",
            warnings=[{"code": "raster_crs_missing", "detail": "no CRS"}],
        )
        out = await tn.normalize.fn(_input(tn, "dem.tif"), object())

        assert [w["code"] for w in out.warnings] == ["raster_crs_missing", "raster_not_ocred"]
        assert "carries no CRS" in out.ocr_skipped_reason
        assert "Recorded as" not in out.ocr_skipped_reason
        assert calls["dispatched"] == []
        assert calls["broadcast"][0]["status"] == "partial"
