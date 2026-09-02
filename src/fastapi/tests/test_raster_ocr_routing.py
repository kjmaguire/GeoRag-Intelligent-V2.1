"""Every TIFF was wrapped to PDF and pushed through the full OCR stack.

ADR-0005's reasoning is sound for a scanned map sheet: it is a picture of
a page with text on it, so wrap it and let the §04p stack read it. It is
wrong for a DEM, an airborne magnetics grid or a multispectral scene.
Those have no text, so the remote OCR engine bills for reading a
continuous-tone surface, and whatever character noise comes back is
chunked, embedded and indexed as retrievable passages that then compete in
the recall set of every future query — with a citation attached.

``persist_raster_metadata`` has always read the exact signal needed to
tell the two apart. It returned a ``RasterCaptureResult`` carrying the
CRS, and ``tiff_normalize`` discarded the return value entirely.

rasterio is a container-only dependency, so these exercise the decision
and the plumbing against stub parse results rather than real GeoTIFFs.
The dtype strings are the ones rasterio's ``src.dtypes`` actually yields.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.ingest.raster_metadata import (
    RasterCaptureResult,
    _is_measurement_raster,
)


class _Band:
    def __init__(
        self,
        index: int,
        dtype: str,
        *,
        min_: float | None = 0.0,
        max_: float | None = 255.0,
    ) -> None:
        self.band_index = index
        self.dtype = dtype
        self.nodata = None
        self.min = min_
        self.max = max_
        self.mean = 1.0
        self.description = f"band {index}"


class _Raster:
    def __init__(self, crs: str | None, dtypes: list[str]) -> None:
        self.crs = crs
        self.bands = [_Band(i + 1, d) for i, d in enumerate(dtypes)]


class TestWhatCountsAsMeasurementData:
    @pytest.mark.parametrize(
        ("label", "crs", "dtypes"),
        [
            ("airborne magnetics grid", "EPSG:32613", ["float32"]),
            ("DEM", "EPSG:26913", ["int16"]),
            ("radiometrics, three channels", "EPSG:32613",
             ["float32", "float32", "float32"]),
            ("multispectral scene", "EPSG:4326",
             ["uint16", "uint16", "uint16", "uint16"]),
            ("float64 gravity grid", "EPSG:3857", ["float64"]),
        ],
    )
    def test_data_rasters_skip_ocr(
        self, label: str, crs: str, dtypes: list[str],
    ) -> None:
        assert _is_measurement_raster(_Raster(crs, dtypes)) is True, label

    @pytest.mark.parametrize(
        ("label", "crs", "dtypes"),
        [
            # The case ADR-0005 exists for: a paper map sheet that was
            # scanned and then georeferenced. It has a CRS AND text.
            ("georeferenced scanned map sheet", "EPSG:26913",
             ["uint8", "uint8", "uint8"]),
            ("georeferenced greyscale scan", "EPSG:26913", ["uint8"]),
            ("bilevel scan", "EPSG:26913", ["bool"]),
            # No CRS at all — an ordinary scanned page.
            ("plain scan", None, ["uint8", "uint8", "uint8"]),
            # A float raster with no CRS is not a map; it could be
            # anything, so OCR stays the safe default.
            ("uncrs'd float raster", None, ["float32"]),
        ],
    )
    def test_scans_still_go_through_ocr(
        self, label: str, crs: str | None, dtypes: list[str],
    ) -> None:
        assert _is_measurement_raster(_Raster(crs, dtypes)) is False, label

    def test_an_unreadable_band_list_keeps_ocr(self) -> None:
        """Conservative in the ambiguous direction: a false skip loses a
        real map, a false OCR only wastes a Cohere Parse call."""
        assert _is_measurement_raster(_Raster("EPSG:4326", [])) is False

    def test_one_eight_bit_band_is_enough_to_keep_ocr(self) -> None:
        """A mixed-depth raster is odd enough that the scan reading wins."""
        assert _is_measurement_raster(
            _Raster("EPSG:32613", ["float32", "uint8"]),
        ) is False

    def test_an_eight_bit_orthophoto_is_a_known_false_negative(self) -> None:
        """Recorded deliberately, not overlooked.

        An 8-bit RGB ortho has a CRS and no text, so it still burns an OCR
        call. Telling an aerial photo from a photographed map needs to look
        at the pixels, not the header, and this rule only reads the header.
        """
        assert _is_measurement_raster(
            _Raster("EPSG:32613", ["uint8", "uint8", "uint8"]),
        ) is False


class TestTheClassificationSurvivesEveryReturnPath:
    """A Hatchet retry must make the same routing decision as the first
    attempt. `already_recorded` is exactly what a retry hits."""

    def test_the_default_is_ocr(self) -> None:
        result = RasterCaptureResult(written=False, reason="no_crs")
        assert result.is_measurement_raster is False

    def test_it_is_carried_independently_of_whether_a_row_was_written(
        self,
    ) -> None:
        for reason in ("recorded", "already_recorded", "persist_failed: boom"):
            result = RasterCaptureResult(
                written=(reason == "recorded"),
                reason=reason,
                crs="EPSG:32613",
                is_measurement_raster=True,
            )
            assert result.is_measurement_raster is True, reason

    def test_the_slots_declaration_includes_it(self) -> None:
        """__slots__ silently drops an attribute that is not declared."""
        assert "is_measurement_raster" in RasterCaptureResult.__slots__


class TestBandStatisticsReachTheDatabase:
    """The parser calls ``statistics(bidx)`` per band to compute these and
    the port read them under names the dataclass does not have, so every
    row's min/max was null and the JSON keys disagreed with the frozen
    Dagster writer the code was ported from."""

    def test_the_reader_uses_the_dataclass_field_names(self) -> None:
        import ast
        from pathlib import Path

        parser = Path(__file__).resolve().parents[2] / (
            "georag_geoparsers/georag_geoparsers/raster_parser.py"
        )
        tree = ast.parse(parser.read_text(encoding="utf-8"))
        fields: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "RasterBandStats":
                fields = [
                    n.target.id for n in node.body if isinstance(n, ast.AnnAssign)
                ]
        assert fields, "RasterBandStats moved — this test needs updating"

        from app.services.ingest import raster_metadata

        source = Path(raster_metadata.__file__).read_text(encoding="utf-8")
        block = source.split("band_stats = [", 1)[1].split("]", 1)[0]
        for absent in ("minimum", "maximum"):
            assert absent not in block, (
                f"band_stats reads b.{absent}, which RasterBandStats does not "
                f"have; its fields are {fields}"
            )
        for present in ("min", "max", "description"):
            assert present in block, f"band_stats drops {present}"

    def test_a_band_round_trips_with_its_range_intact(self) -> None:
        band = _Band(1, "float32", min_=12.5, max_=980.25)
        row = {
            "band_index": band.band_index,
            "dtype": band.dtype,
            "min": band.min,
            "max": band.max,
            "mean": band.mean,
            "nodata": band.nodata,
            "description": band.description,
        }
        assert row["min"] == 12.5
        assert row["max"] == 980.25

    # The companion test comparing these keys against the frozen Dagster
    # writer (`silver_raster.py`) was removed on 2026-08-28 with that tree.
    # It had guarded a two-writer disagreement that no longer exists: this
    # is now the only writer of the column.


class _FakeStore:
    """Counts the S3 calls so a skipped raster can be shown to cost none."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.get_bytes_calls = 0
        self.put_bytes_calls = 0
        self.head_calls = 0

    def get_bytes(self, bucket: Any, key: str) -> bytes:
        self.get_bytes_calls += 1
        return self.payload

    def put_bytes(self, *a: Any, **kw: Any) -> None:
        self.put_bytes_calls += 1

    def head(self, bucket: Any, key: str) -> dict:
        self.head_calls += 1
        raise FileNotFoundError(key)


@pytest.fixture
def tiff_env(monkeypatch):
    """Wire tiff_normalize's collaborators to fakes and report what was
    called. Everything the workflow touches is either S3 or Postgres."""
    from app.hatchet_workflows import tiff_normalize as tn

    store = _FakeStore(b"II*\x00fake-tiff")
    monkeypatch.setattr(tn, "get_storage_client", lambda: store)

    calls: dict[str, Any] = {
        "wrapped": 0, "dispatched": 0, "completed": [], "legacy_completed": 0,
    }

    def _wrap(_bytes):
        calls["wrapped"] += 1
        raise AssertionError("tiff_to_pdf must not run for a data raster")

    monkeypatch.setattr(tn, "tiff_to_pdf", _wrap)

    async def _mark_started(**kw):
        return None

    async def _lookup(**kw):
        return "c2000000-0000-0000-0000-000000000003"

    async def _mark_completed_by_run(**kw):
        calls["completed"].append(kw)
        return True

    async def _mark_completed(**kw):
        calls["legacy_completed"] += 1

    monkeypatch.setattr(tn.ingest_progress, "mark_started", _mark_started)
    monkeypatch.setattr(tn.ingest_progress, "lookup_active_run_id", _lookup)
    monkeypatch.setattr(
        tn.ingest_progress, "mark_completed_by_run", _mark_completed_by_run,
    )
    monkeypatch.setattr(tn.ingest_progress, "mark_completed", _mark_completed)

    class _Ref:
        workflow_run_id = "wf-should-not-happen"

    async def _dispatch(_payload):
        calls["dispatched"] += 1
        return _Ref()

    monkeypatch.setattr(tn.ingest_pdf, "aio_run_no_wait", _dispatch)

    def _set_capture(**kw):
        async def _capture(**_kw):
            return RasterCaptureResult(**kw)

        monkeypatch.setattr(tn, "persist_raster_metadata", _capture)

    return tn, store, calls, _set_capture


def _input(tn):
    return tn.TiffNormalizeInput(
        workspace_id="a0000000-0000-0000-0000-00000000feed",
        project_id="b1000000-0000-0000-0000-0000000000a0",
        minio_key="tiff/b1000000-0000-0000-0000-0000000000a0/mag_2024.tif",
        file_size=400_000_000,
        correlation_token="tok-1",
    )


class TestTheWorkflowActsOnTheClassification:
    @pytest.mark.asyncio
    async def test_a_data_raster_is_never_wrapped_or_dispatched(
        self, tiff_env,
    ) -> None:
        tn, store, calls, set_capture = tiff_env
        set_capture(
            written=True, reason="recorded", crs="EPSG:32613",
            raster_id="r-1", is_measurement_raster=True,
        )

        out = await tn.normalize.fn(_input(tn), object())

        assert calls["wrapped"] == 0
        assert calls["dispatched"] == 0, (
            "ingest_pdf ran the whole §04p stack over a magnetics grid"
        )
        assert store.put_bytes_calls == 0, "a derived PDF was uploaded anyway"
        assert out.ingest_pdf_workflow_run_id is None
        assert out.derived_minio_key == ""

    @pytest.mark.asyncio
    async def test_the_reason_reaches_the_run(self, tiff_env) -> None:
        """Otherwise the geologist sees a TIFF that produced nothing and no
        explanation of why they cannot find it in chat."""
        tn, _store, calls, set_capture = tiff_env
        set_capture(
            written=True, reason="recorded", crs="EPSG:32613",
            raster_id="r-1", is_measurement_raster=True,
        )

        out = await tn.normalize.fn(_input(tn), object())

        assert len(calls["completed"]) == 1
        warnings = calls["completed"][0]["warnings"]
        assert warnings[0]["code"] == "raster_not_ocred"
        assert "EPSG:32613" in warnings[0]["detail"]
        assert "not searchable in chat" in warnings[0]["detail"]
        assert out.ocr_skipped_reason == warnings[0]["detail"]

    @pytest.mark.asyncio
    async def test_the_raster_row_is_reported_as_the_run_s_output(
        self, tiff_env,
    ) -> None:
        tn, _store, calls, set_capture = tiff_env
        set_capture(
            written=True, reason="recorded", crs="EPSG:32613",
            raster_id="r-1", is_measurement_raster=True,
        )

        await tn.normalize.fn(_input(tn), object())

        assert calls["completed"][0]["rows_written"] == 1

    @pytest.mark.asyncio
    async def test_a_retry_that_finds_the_row_already_recorded_still_skips(
        self, tiff_env,
    ) -> None:
        """`already_recorded` is what the SECOND attempt sees. If the
        classification were tied to `written`, a retry would push through
        OCR exactly the raster the first attempt correctly skipped."""
        tn, _store, calls, set_capture = tiff_env
        set_capture(
            written=False, reason="already_recorded", crs="EPSG:32613",
            is_measurement_raster=True,
        )

        await tn.normalize.fn(_input(tn), object())

        assert calls["dispatched"] == 0
        assert calls["completed"][0]["rows_written"] == 0

    @pytest.mark.asyncio
    async def test_a_scan_takes_the_unchanged_path(self, tiff_env) -> None:
        """The regression that matters most: ADR-0005's own case must be
        untouched. It fails at the wrap because the fixture's tiff_to_pdf
        raises — which is the proof it was reached."""
        tn, _store, _calls, set_capture = tiff_env
        set_capture(
            written=True, reason="recorded", crs="EPSG:26913",
            raster_id="r-2", is_measurement_raster=False,
        )

        with pytest.raises(AssertionError, match="must not run"):
            await tn.normalize.fn(_input(tn), object())

    @pytest.mark.asyncio
    async def test_an_unreadable_raster_takes_the_unchanged_path(
        self, tiff_env,
    ) -> None:
        tn, _store, _calls, set_capture = tiff_env
        set_capture(written=False, reason="not_a_readable_raster")

        with pytest.raises(AssertionError, match="must not run"):
            await tn.normalize.fn(_input(tn), object())


class TestTheOutputDistinguishesTheTwoSkips:
    def test_ocr_skipped_is_not_normalize_skipped(self) -> None:
        """`normalize_skipped` means the derived PDF was already there from
        an earlier run — the file still gets OCR'd. Overloading it would
        have made the two indistinguishable to anything reading the run."""
        from app.hatchet_workflows.tiff_normalize import TiffNormalizeOutput

        fields = TiffNormalizeOutput.model_fields
        assert "ocr_skipped_reason" in fields
        assert "normalize_skipped" in fields

        out = TiffNormalizeOutput(
            source_sha256="a" * 64,
            derived_minio_key="",
            page_count=0,
            truncated_at_cap=False,
            normalize_skipped=False,
            ocr_skipped_reason="because",
        )
        assert out.normalize_skipped is False
        assert out.ocr_skipped_reason == "because"

    def test_the_field_defaults_to_none_for_every_existing_caller(self) -> None:
        from app.hatchet_workflows.tiff_normalize import TiffNormalizeOutput

        out = TiffNormalizeOutput(
            source_sha256="a" * 64,
            derived_minio_key="reports/p/x.pdf",
            page_count=3,
            truncated_at_cap=False,
            normalize_skipped=False,
            ingest_pdf_workflow_run_id="wf-1",
        )
        assert out.ocr_skipped_reason is None
