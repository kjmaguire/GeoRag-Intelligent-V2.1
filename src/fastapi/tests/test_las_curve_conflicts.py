"""A same-named curve from another LAS file is judged, not silently replaced (audit finding 5).

``silver.well_log_curves`` is unique on ``(collar_id, curve_name)``. Both LAS
writers resolved a clash without saying so - ``ON CONFLICT DO UPDATE`` in
``las_ingester._insert_curve``, a ``DELETE`` of the file's names in
``ingest_well_logs`` - which is right for a re-upload of the same file and
wrong for two tool runs down one hole, which both carry GAMMA.

``las_curve_conflicts.decide`` is the one policy; the ingest_well_logs side of
it is in test_ingest_well_logs_workflow.py (TestSameNamedCurveFromAnotherFile).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("lasio")

from app.services.ingest.las_curve_conflicts import (  # noqa: E402
    StoredCurve,
    decide,
    fetch_stored_curves,
    replacement_warnings,
)
from app.services.ingest.las_ingester import _depth_range, ingest_las_file  # noqa: E402
from tests.test_las_collar_location import _EXISTING, _PJ, _WS, _Conn, _las  # noqa: E402


def stored(source: str | None = "run1.las", lo: float | None = 0.0,
           hi: float | None = 100.0) -> StoredCurve:
    return StoredCurve(name="GAMMA", source_file=source, min_depth=lo, max_depth=hi)


class TestDecide:
    def test_nothing_stored_is_a_plain_write(self) -> None:
        assert decide("GAMMA", None, new_file="a.las", new_min=0, new_max=10).action == "write"

    @pytest.mark.parametrize("stored_name", [
        "run1.las", "RUN1.LAS", "20260901_100000_run1.las", "20260901_100000_123456_run1.las",
    ])
    def test_the_same_file_is_idempotent_whatever_the_upload_stamp(self, stored_name: str) -> None:
        decision = decide(
            "GAMMA", stored(stored_name), new_file="20260929_081500_run1.las",
            new_min=500, new_max=600,        # even a different range: it is the same file
        )
        assert decision.action == "write"

    def test_another_file_over_the_same_depths_replaces(self) -> None:
        d = decide("GAMMA", stored(lo=0, hi=100), new_file="run2.las", new_min=50, new_max=150)
        assert d.action == "replace" and "overlapping" in d.reason

    @pytest.mark.parametrize(("lo", "hi"), [(100.0, 200.0), (150.0, 300.0), (-50.0, 0.0)])
    def test_another_file_over_different_depths_is_refused(self, lo: float, hi: float) -> None:
        """Touching is not overlapping: run 2 starting where run 1 stopped is
        the textbook complementary pair."""
        d = decide("GAMMA", stored(lo=0, hi=100), new_file="run2.las", new_min=lo, new_max=hi)
        assert d.action == "refuse" and d.stored is not None

    def test_an_unrecorded_stored_unit_cannot_be_compared_so_it_replaces_with_a_warning(self) -> None:
        d = decide("GAMMA", stored(lo=None, hi=None), new_file="run2.las", new_min=500, new_max=600)
        assert d.action == "replace" and "never recorded" in d.reason

    def test_a_stored_row_with_no_file_is_another_file(self) -> None:
        d = decide("GAMMA", stored(source=None), new_file="run2.las", new_min=0, new_max=50)
        assert d.action == "replace"


class _FetchConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls = 0

    async def fetch(self, _sql: str, *_args: Any) -> list[dict[str, Any]]:
        self.calls += 1
        return self.rows


class TestFetchStoredCurves:
    @pytest.mark.asyncio
    async def test_feet_become_metres_and_an_unrecorded_unit_stays_unknown(self) -> None:
        conn = _FetchConn([
            {"curve_name": "A", "source_file": "a.las", "min_depth": 100.0, "max_depth": 200.0,
             "depth_unit": "ft"},
            {"curve_name": "B", "source_file": "b.las", "min_depth": 10.0, "max_depth": 20.0,
             "depth_unit": "m"},
            {"curve_name": "C", "source_file": "c.las", "min_depth": 10.0, "max_depth": 20.0,
             "depth_unit": None},
        ])

        out = await fetch_stored_curves(conn, "c", ["A", "B", "C"])

        assert (out["A"].min_depth, out["A"].max_depth) == pytest.approx((30.48, 60.96))
        assert (out["B"].min_depth, out["B"].max_depth) == (10.0, 20.0)
        assert (out["C"].min_depth, out["C"].max_depth) == (None, None)

    @pytest.mark.asyncio
    async def test_no_names_means_no_query(self) -> None:
        conn = _FetchConn([])
        assert await fetch_stored_curves(conn, "c", []) == {}
        assert conn.calls == 0


class TestWarnings:
    def test_the_string_only_form_has_no_structured_list(self) -> None:
        d = decide("GAMMA", stored(), new_file="run2.las", new_min=50, new_max=150)
        (note,) = replacement_warnings(
            {"GAMMA": d}, {"GAMMA": (50.0, 150.0)}, new_file="run2.las", structured=False,
        )
        assert note["code"] == "curve_replaced_from_other_file"
        assert all(isinstance(v, str) for v in note.values())
        assert "'run1.las'" in note["detail"] and "0-100 m" in note["detail"]
        assert "50-150 m" in note["detail"]

    def test_nothing_to_say_when_everything_is_a_plain_write(self) -> None:
        d = decide("GAMMA", None, new_file="a.las", new_min=0, new_max=10)
        assert replacement_warnings({"GAMMA": d}, {"GAMMA": (0.0, 10.0)}, new_file="a.las") == []


def test_depth_range_ignores_leading_negative_depths_like_the_insert_does() -> None:
    assert _depth_range([-0.2, -0.1, 0.0, 5.0, 10.0]) == (0.0, 10.0)
    assert _depth_range([]) == (0.0, 0.0)
    assert _depth_range([-3.0, -2.0]) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# The ingest_las_file path (the archive / cluster writer)
# ---------------------------------------------------------------------------
# tests/test_las_collar_location.py::_las writes a feet LAS: STRT.F 0 -> 1.0
# ft, i.e. a GAMMA curve covering 0 - 0.3048 m.


def stored_gamma(source: str, lo: float, hi: float) -> dict[str, Any]:
    return {"curve_name": "GAMMA", "source_file": source, "min_depth": lo, "max_depth": hi,
            "depth_unit": "m"}


@pytest.mark.asyncio
async def test_an_overlapping_curve_from_another_file_replaces_and_warns(tmp_path: Path) -> None:
    conn = _Conn(by_hole_id=_EXISTING, stored_curves=[stored_gamma("run1.las", 0.0, 0.2)])

    result = await ingest_las_file(
        conn, str(_las(tmp_path / "run2.las")), workspace_id=_WS, project_id_override=_PJ,
    )

    assert conn.curve_writes == 1 and result.curves_inserted == 1
    (note,) = [w for w in result.warnings if w["code"] == "curve_replaced_from_other_file"]
    assert "'run1.las'" in note["detail"] and "'run2.las'" in note["detail"]
    assert all(isinstance(v, str) for v in note.values())    # LASIngestResult.warnings is dict[str, str]


@pytest.mark.asyncio
async def test_a_complementary_curve_from_another_file_is_refused(tmp_path: Path) -> None:
    conn = _Conn(by_hole_id=_EXISTING, stored_curves=[stored_gamma("run1.las", 500.0, 600.0)])

    result = await ingest_las_file(
        conn, str(_las(tmp_path / "run2.las")), workspace_id=_WS, project_id_override=_PJ,
    )

    assert conn.curve_writes == 0 and result.curves_inserted == 0
    assert [w["code"] for w in result.warnings] == ["curve_replacement_refused"]
    assert "500-600 m" in result.warnings[0]["detail"]


@pytest.mark.asyncio
async def test_reloading_the_same_file_is_silent(tmp_path: Path) -> None:
    conn = _Conn(by_hole_id=_EXISTING, stored_curves=[stored_gamma("run1.las", 500.0, 600.0)])

    result = await ingest_las_file(
        conn, str(_las(tmp_path / "run1.las")), workspace_id=_WS, project_id_override=_PJ,
    )

    assert conn.curve_writes == 1 and result.warnings == []
