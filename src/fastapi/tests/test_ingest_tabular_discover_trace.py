"""A Discover/MapInfo drillhole-TRACE export yields collars AND surveys.

WHY THIS FILE EXISTS
    The trace path shipped verified only by a manual probe against one file.
    That is not coverage: the probe proves the happy path on the one delivery
    that happened to be on the workstation, and every guard in these functions
    exists for an input that delivery does not contain.

    A trace export writes one row per SEGMENT of a desurveyed hole, all rows
    carrying the same CollarID. Three separate failures live in that shape:

      * Fed row by row to ``_COLLAR_SQL`` (ON CONFLICT DO UPDATE) the collar
        is written and then OVERWRITTEN by a segment midpoint. The hole lands
        tens of metres from where it was drilled, silently. Hence the
        collapse.
      * ``or 0.0`` on an unreadable depth makes every blank a perfect match
        for the depth-0 collar test, so a hole whose collar row is missing
        adopts an arbitrary midpoint as its collar -- the same wrong answer,
        arriving through the coercion rather than the data.
      * A hole with no depth-0 segment has no exported collar position at
        all. The shallowest midpoint is NOT the collar, and guessing is the
        error the collapse exists to avoid.

    The survey half has its own distinction worth pinning: an absent dip
    VALUE means 0 (Discover writes 0 for a horizontal trench), but an absent
    dip COLUMN means unknown. ``or 0.0`` collapses those two into a
    fabricated horizontal hole.

WHAT RUNS WHERE
    Everything except the last class is pure and runs anywhere. The last
    class opens the real ``Sitka_trD.DAT`` and SKIPS when the delivery is
    absent -- a skip there is not a pass.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.hatchet_workflows.ingest_tabular import (
    _collapse_discover_traces,
    _discover_trace_columns,
    _trace_survey_stations,
)

#: The column names the real Discover export uses, so the header matcher is
#: exercised on the shape it actually meets rather than on tidy synonyms.
TRACE_COLUMNS = [
    "CollarID_d", "Depth_db", "Azimuth_db", "Dip_db",
    "MidX_db", "MidY_db", "MidZ_db", "SegmentLen",
]

REDSTAR = Path(os.environ.get("GEORAG_REDSTAR_DIR", "C:/Users/GeoRAG/Desktop/RedStar"))
REAL_TRACE = REDSTAR / "Apollo Sitka" / "Trench" / "Sitka_tr" / "Sitka_trD.DAT"


def _row(hole: str, depth, azimuth=322.8, dip=0.0, x=500.0, y=6000.0):
    """One trace segment, keyed the way the real export keys it."""
    return {
        "CollarID_d": hole, "Depth_db": depth, "Azimuth_db": azimuth,
        "Dip_db": dip, "MidX_db": x, "MidY_db": y, "MidZ_db": 0.0,
        "SegmentLen": depth,
    }


class TestTraceDetection:
    def test_a_trace_export_is_recognised(self) -> None:
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        assert mapped is not None
        for key in ("hole_id", "depth", "mid_x", "mid_y", "segment_len"):
            assert key in mapped

    def test_a_plain_collar_table_is_not_a_trace(self) -> None:
        # The discrimination that matters: a collar table has coordinates and
        # a depth but no notion of a segment. Matching it here would send a
        # perfectly good collar table through the collapse, which drops every
        # hole that has no depth-0 row -- i.e. all of them.
        assert _discover_trace_columns(
            ["HoleID", "Easting", "Northing", "Elevation", "Depth", "Azimuth", "Dip"],
        ) is None

    def test_segment_length_alone_is_not_enough(self) -> None:
        # MidX/MidY are what make the row a desurveyed midpoint. Without them
        # there is nothing to collapse and no collar position to recover.
        assert _discover_trace_columns(
            ["CollarID", "Depth", "SegmentLen", "Azimuth"],
        ) is None


class TestCollarCollapse:
    def test_one_collar_per_hole_taken_from_the_depth_zero_row(self) -> None:
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        rows = [
            _row("TR002", 0.0, x=1000.0, y=7000.0),
            _row("TR002", 61.5, x=1018.7, y=7024.4),
            _row("TR003", 0.0, x=2000.0, y=8000.0),
            _row("TR003", 32.0, x=1985.4, y=8006.5),
        ]
        collars = _collapse_discover_traces(rows, mapped)

        assert len(collars) == 2
        by_hole = {c["hole_id"]: c for c in collars}
        # The collar keeps the DEPTH-0 coordinates, not the midpoint's. This
        # is the assertion that fails if the ON CONFLICT overwrite returns.
        assert by_hole["TR002"]["easting"] == 1000.0
        assert by_hole["TR002"]["northing"] == 7000.0
        assert by_hole["TR003"]["easting"] == 2000.0

    def test_file_order_does_not_decide_which_row_is_the_collar(self) -> None:
        # A re-export can interleave holes and emit segments deepest-first.
        # "The first row I saw" would then be an arbitrary midpoint.
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        rows = [
            _row("TR002", 61.5, x=1018.7, y=7024.4),
            _row("TR002", 0.0, x=1000.0, y=7000.0),
        ]
        collars = _collapse_discover_traces(rows, mapped)
        assert len(collars) == 1
        assert collars[0]["easting"] == 1000.0

    def test_a_hole_with_no_depth_zero_segment_is_skipped_not_guessed(self) -> None:
        # Its collar position was never exported. The shallowest midpoint is
        # not the collar, and a hole placed at a midpoint is worse than a
        # hole that is absent -- the geologist can see the second one.
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        rows = [
            _row("TR009", 15.0, x=3000.0, y=9000.0),
            _row("TR009", 40.0, x=3020.0, y=9030.0),
        ]
        assert _collapse_discover_traces(rows, mapped) == []

    def test_an_unreadable_depth_is_not_treated_as_depth_zero(self) -> None:
        # The `or 0.0` bug: a blank depth becomes a perfect match for the
        # collar test, so this hole would adopt the blank row's coordinates.
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        rows = [
            _row("TR010", "", x=9999.0, y=9999.0),
            _row("TR010", 25.0, x=3020.0, y=9030.0),
        ]
        collars = _collapse_discover_traces(rows, mapped)
        # No readable depth-0 row survives, so the hole is skipped rather
        # than planted at the blank row's coordinates.
        assert [c for c in collars if c["easting"] == 9999.0] == []

    def test_a_hole_with_no_readable_depth_at_all_is_dropped(self) -> None:
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        rows = [_row("TR011", ""), _row("TR011", None)]
        assert _collapse_discover_traces(rows, mapped) == []

    def test_a_blank_hole_id_does_not_become_a_hole(self) -> None:
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        rows = [_row("", 0.0), _row("   ", 0.0)]
        assert _collapse_discover_traces(rows, mapped) == []


class TestSurveyStations:
    def test_every_segment_becomes_a_station(self) -> None:
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        rows = [_row("TR002", 0.0), _row("TR002", 61.5)]
        stations = _trace_survey_stations(rows, mapped)

        assert len(stations) == 2
        assert [s["depth"] for s in stations] == [0.0, 61.5]
        assert {s["survey_method"] for s in stations} == {"desurveyed_trace"}

    def test_the_depth_zero_station_is_kept(self) -> None:
        # A survey that starts below the collar leaves the first stretch of
        # hole unconstrained. For a 61.5 m trench that is the whole thing.
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        stations = _trace_survey_stations([_row("TR002", 0.0)], mapped)
        assert [s["depth"] for s in stations] == [0.0]

    def test_a_station_without_an_azimuth_constrains_nothing_and_is_dropped(self) -> None:
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        stations = _trace_survey_stations([_row("TR002", 10.0, azimuth=None)], mapped)
        assert stations == []

    def test_an_unreadable_depth_is_dropped_not_placed_at_zero(self) -> None:
        # A station silently placed at 0 m bends the trajectory back to the
        # collar -- the trajectory equivalent of the collar-overwrite bug.
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        stations = _trace_survey_stations([_row("TR002", "")], mapped)
        assert stations == []

    def test_a_blank_dip_value_means_horizontal(self) -> None:
        # Discover writes 0 for a horizontal trench, and sometimes leaves it
        # blank meaning the same thing. The column IS present, so 0 is a
        # reading of the data rather than an invention.
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        stations = _trace_survey_stations([_row("TR002", 10.0, dip=None)], mapped)
        assert len(stations) == 1
        assert stations[0]["dip"] == 0.0

    def test_a_missing_dip_column_means_unknown_not_horizontal(self) -> None:
        # The distinction `or 0.0` destroys. With no dip column anywhere, an
        # inclined hole would be recorded as flat -- a fabricated fact.
        columns = [c for c in TRACE_COLUMNS if c != "Dip_db"]
        mapped = _discover_trace_columns(columns)
        assert mapped is not None and "dip" not in mapped

        row = _row("TR002", 10.0)
        row.pop("Dip_db")
        stations = _trace_survey_stations([row], mapped)
        assert len(stations) == 1
        assert stations[0]["dip"] is None

    def test_stations_carry_the_hole_id_the_collar_write_will_resolve(self) -> None:
        # The stations are resolved to collar_id through _collar_index, which
        # is keyed on this exact string. A trimmed/untrimmed mismatch here
        # orphans every station.
        mapped = _discover_trace_columns(TRACE_COLUMNS)
        rows = [_row("  TR002  ", 0.0)]
        collars = _collapse_discover_traces(rows, mapped)
        stations = _trace_survey_stations(rows, mapped)
        assert collars[0]["hole_id"] == stations[0]["hole_id"] == "TR002"


@pytest.mark.skipif(not REAL_TRACE.exists(), reason="RedStar delivery not present")
class TestAgainstTheRealDelivery:
    """Sitka_trD.DAT: five trenches, ten segments.

    A skip here is not a pass -- these numbers are the delivery's, and they
    are what proves the synthetic rows above describe the real shape.
    """

    @staticmethod
    def _load():
        from app.hatchet_workflows.ingest_tabular import _read_mapinfo_dat_table

        rows = _read_mapinfo_dat_table(REAL_TRACE)
        mapped = _discover_trace_columns(list(rows[0]))
        assert mapped is not None, "the real export must be recognised as a trace"
        return rows, mapped

    def test_five_trenches_collapse_out_of_ten_segments(self) -> None:
        rows, mapped = self._load()
        collars = _collapse_discover_traces(rows, mapped)
        assert {c["hole_id"] for c in collars} == {
            "TR002-Sitka", "TR003-Sitka", "TR004-Sitka",
            "TR005-Sitka", "TR006-Sitka",
        }

    def test_ten_stations_two_per_trench_each_starting_at_the_collar(self) -> None:
        rows, mapped = self._load()
        stations = _trace_survey_stations(rows, mapped)
        assert len(stations) == 10

        by_hole: dict[str, list[float]] = {}
        for s in stations:
            by_hole.setdefault(s["hole_id"], []).append(s["depth"])

        assert by_hole["TR002-Sitka"] == [0.0, 61.5]
        assert by_hole["TR003-Sitka"] == [0.0, 32.0]
        for depths in by_hole.values():
            assert 0.0 in depths, "every hole needs a station at its collar"

    def test_the_trenches_are_horizontal(self) -> None:
        rows, mapped = self._load()
        stations = _trace_survey_stations(rows, mapped)
        # Trenches, not drillholes: a non-zero dip here would mean the dip
        # column was misread, which is how the .DAT binary-double trap shows
        # up in this file.
        assert {s["dip"] for s in stations} == {0.0}
        assert round(next(
            s["azimuth"] for s in stations if s["hole_id"] == "TR002-Sitka"
        ), 1) == 322.8
