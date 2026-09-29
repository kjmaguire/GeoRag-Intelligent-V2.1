"""The ingest writers' row guards match the silver tables' constraints (ING-1).

WHY THIS FILE EXISTS
    ``ingest_tabular`` wrote collars in batches of 500 and let Postgres judge
    each row. One row the CHECKs refuse -- a blank total depth (the writer
    defaulted it to 0.0), a mine-grid RL of 9,650, an up-hole dip of +60 --
    aborted the whole batch, and in a workbook every sheet after it. The
    writer tests use a recording FakeConn that has no constraints, so none of
    that could show up in CI.

    ``silver_row_guard`` now checks every row before it is sent. These tests
    pin two things:

      * the guard's bounds are the MIGRATIONS' bounds, read out of the PHP
        files -- a migration that changes a CHECK or a varchar width without
        the guard following fails here instead of in production;
      * the per-row behaviour: optional out-of-range -> blanked and reported,
        NOT NULL missing -> row skipped and reported, nothing invented.

    No database and no Hatchet import, so this file runs on every PR. The
    same claims against a live Postgres are in
    test_ingest_constraint_rows_integration.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.ingest import silver_row_guard as guard
from app.services.ingest.silver_row_guard import (
    RowIssues,
    finite,
    guard_collar,
    guard_interval,
    guard_percent,
    issue_warnings,
)

MIGRATIONS = Path(__file__).resolve().parents[3] / "database" / "migrations"


def _migration_texts() -> list[tuple[str, str]]:
    return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(MIGRATIONS.glob("*.php"))]


def _check_clause(name: str) -> str:
    """The LAST ``ADD CONSTRAINT <name> CHECK (...)`` any migration creates."""
    pattern = re.compile(
        rf"ADD\s+CONSTRAINT\s+{name}\s+CHECK\s*\((?P<body>.*?)\)\s*['\"]",
        re.IGNORECASE | re.DOTALL,
    )
    found = [
        m.group("body") for _name, text in _migration_texts()
        for m in pattern.finditer(text)
    ]
    assert found, f"no migration creates {name}"
    return " ".join(found[-1].split())


def _numbers(clause: str) -> list[float]:
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", clause)]


class TestBoundsMatchMigrations:
    """Each mirror constant equals the CHECK the migrations create."""

    def test_total_depth(self) -> None:
        clause = _check_clause("chk_total_depth_positive")
        assert re.fullmatch(r"total_depth\s*>\s*0", clause), clause
        assert guard.COLLAR_TOTAL_DEPTH_EXCLUSIVE_MIN == 0.0

    @pytest.mark.parametrize(("name", "column", "bounds"), [
        ("chk_elevation_range", "elevation", guard.COLLAR_ELEVATION_RANGE),
        ("chk_azimuth_range", "azimuth", guard.COLLAR_AZIMUTH_RANGE),
        ("chk_dip_range", "dip", guard.COLLAR_DIP_RANGE),
        ("chk_rqd_range", "rqd", guard.LITHOLOGY_PERCENT_RANGE),
        ("chk_recovery_range", "recovery", guard.LITHOLOGY_PERCENT_RANGE),
    ])
    def test_range_checks(self, name: str, column: str, bounds: tuple[float, float]) -> None:
        clause = _check_clause(name)
        assert column in clause, clause
        assert tuple(_numbers(clause)) == bounds, (
            f"{name} is {clause!r} in the migrations but silver_row_guard "
            f"mirrors {bounds}; update the guard with the migration"
        )

    @pytest.mark.parametrize("name", [
        "chk_total_depth_positive", "chk_elevation_range", "chk_azimuth_range",
        "chk_dip_range", "chk_rqd_range", "chk_recovery_range",
    ])
    def test_no_later_migration_redefines_it(self, name: str) -> None:
        """A drop outside the hardening migration's own down() means a change."""
        for filename, text in _migration_texts():
            if filename.startswith("2026_04_13_100000_database_hardening"):
                continue
            assert not re.search(rf"DROP\s+CONSTRAINT\s+(IF\s+EXISTS\s+)?{name}\b", text), (
                f"{filename} drops {name}: silver_row_guard must be updated to match"
            )

    @pytest.mark.parametrize(("migration", "widths"), [
        ("2026_04_09_180100_create_collars_table.php",
         {k: v for k, v in guard.COLLAR_TEXT_WIDTHS.items() if k != "hole_id_canonical"}),
        ("2026_04_09_180300_create_lithology_logs_table.php", guard.LITHOLOGY_TEXT_WIDTHS),
        ("2026_04_09_180600_create_samples_table.php", guard.SAMPLE_TEXT_WIDTHS),
        ("2026_04_09_180200_create_surveys_table.php", guard.SURVEY_TEXT_WIDTHS),
    ])
    def test_varchar_widths(self, migration: str, widths: dict[str, int]) -> None:
        text = (MIGRATIONS / migration).read_text(encoding="utf-8")
        declared = {
            m.group(1): int(m.group(2))
            for m in re.finditer(r"->string\('(\w+)',\s*(\d+)\)", text)
        }
        for column, width in widths.items():
            assert declared.get(column) == width, (column, declared.get(column), width)

    def test_hole_id_canonical_width(self) -> None:
        text = (MIGRATIONS / "2026_04_18_130100_add_hole_id_canonical_to_collars.php").read_text()
        assert "hole_id_canonical VARCHAR(50)" in text
        assert guard.COLLAR_TEXT_WIDTHS["hole_id_canonical"] == 50


def _collar(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "hole_id": "DH-1", "easting": 500000.0, "northing": 6000000.0,
        "total_depth": 150.0, "_source_row": 2,
    }
    base.update(overrides)
    return base


class TestGuardCollar:
    def test_clean_row_passes_through(self) -> None:
        issues = RowIssues()
        values = guard_collar(_collar(elevation=450.0, dip=-60.0, azimuth=90.0), issues)
        assert values is not None
        assert (values["elevation"], values["dip"], values["azimuth"]) == (450.0, -60.0, 90.0)
        assert values["hole_type"] == values["status"] == "unknown"
        assert not issues

    @pytest.mark.parametrize("td", [None, "", 0, 0.0, -5, float("nan")])
    def test_missing_total_depth_skips_the_row_never_zero(self, td: object) -> None:
        issues = RowIssues()
        assert guard_collar(_collar(total_depth=td), issues) is None
        assert len(issues.skipped) == 1
        assert issues.skipped[0][0] == 2   # source row carried through

    def test_missing_total_depth_keeps_the_stored_one(self) -> None:
        issues = RowIssues()
        values = guard_collar(_collar(total_depth=None), issues, existing_total_depth=212.5)
        assert values is not None and values["total_depth"] == 212.5
        assert not issues.skipped

    @pytest.mark.parametrize(("fld", "value"), [
        ("elevation", 9650.0),     # mine-grid RL offset
        ("elevation", 12000.0),    # feet
        ("elevation", -600.0),
        ("dip", 60.0),             # up-hole / positive-down convention
        ("dip", -91.0),
        ("azimuth", 361.0),
        ("azimuth", -1.0),
    ])
    def test_out_of_range_optional_is_blanked_and_row_kept(
        self, fld: str, value: float,
    ) -> None:
        issues = RowIssues()
        values = guard_collar(_collar(**{fld: value}), issues)
        assert values is not None and values[fld] is None
        assert [b[0] for b in issues.blanked] == [fld]
        assert not issues.skipped

    @pytest.mark.parametrize(("fld", "value"), [
        ("elevation", 9000.0), ("elevation", -500.0), ("dip", 0.0),
        ("dip", -90.0), ("azimuth", 0.0), ("azimuth", 360.0),
    ])
    def test_boundaries_are_inclusive_like_the_check(self, fld: str, value: float) -> None:
        issues = RowIssues()
        values = guard_collar(_collar(**{fld: value}), issues)
        assert values is not None and values[fld] == value
        assert not issues

    def test_overlong_hole_type_and_status_keep_row_and_text(self) -> None:
        """PG-10: the row stays, the short column says unknown, the words survive."""
        issues = RowIssues()
        long_type = "Diamond core, HQ to 120 m then NQ to EOH"
        long_status = "Completed, cemented and collar capped 2019"
        values = guard_collar(_collar(hole_type=long_type, status=long_status), issues)
        assert values is not None
        assert values["hole_type"] == "unknown" and values["drill_type"] == long_type
        assert values["status"] == "unknown" and values["hole_status"] == long_status
        assert sorted(b[0] for b in issues.blanked) == ["hole_type", "status"]

    @pytest.mark.parametrize("rec", [
        _collar(hole_id=""), _collar(hole_id="X" * 51),
        _collar(easting=None), _collar(northing="abc"),
    ])
    def test_unwritable_identity_or_position_skips(self, rec: dict[str, object]) -> None:
        issues = RowIssues()
        assert guard_collar(rec, issues) is None
        assert len(issues.skipped) == 1


class TestGuardIntervals:
    def test_inverted_or_missing_interval_is_skipped(self) -> None:
        issues = RowIssues()
        assert guard_interval({"from_depth": 3, "to_depth": 3}, issues) is None
        assert guard_interval({"from_depth": None, "to_depth": 3}, issues) is None
        assert guard_interval({"from_depth": 1, "to_depth": 3}, issues) == (1.0, 3.0)
        assert len(issues.skipped) == 2

    def test_recovery_over_100_is_blanked(self) -> None:
        issues = RowIssues()
        rec = {"recovery": 102, "rqd": 85}
        assert guard_percent(rec, "recovery", guard.LITHOLOGY_PERCENT_RANGE, issues) is None
        assert guard_percent(rec, "rqd", guard.LITHOLOGY_PERCENT_RANGE, issues) == 85.0
        assert [b[0] for b in issues.blanked] == ["recovery"]


def test_finite_treats_nan_and_inf_as_missing() -> None:
    assert finite("nan") is None
    assert finite(float("inf")) is None
    assert finite(" 12.5 ") == 12.5
    assert finite(True) is None


def test_warnings_have_message_and_detail() -> None:
    """The Ingestion Runs page renders ``detail`` (falling back to ``code``)."""
    issues = RowIssues()
    guard_collar(_collar(total_depth=None), issues)
    guard_collar(_collar(dip=55.0), issues)
    issues.merged.append((4, "SRE09-6", "SRE09_6"))
    warnings = issue_warnings(issues, label="collars.csv", table="collar")
    assert [w["code"] for w in warnings] == [
        "db_constraint_rows_skipped",
        "db_constraint_values_blanked",
        "hole_id_matched_existing_collar",
    ]
    for w in warnings:
        assert w["message"] and w["detail"]
    assert "row 2" in warnings[0]["detail"]
    assert "'SRE09-6' -> 'SRE09_6'" in warnings[2]["detail"]
    assert issues.skipped_details()[0]["code"] == "db_constraint_row_skipped"
