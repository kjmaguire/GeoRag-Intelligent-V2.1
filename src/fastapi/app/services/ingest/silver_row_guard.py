"""Row-level guards that mirror the silver drill tables' CHECK constraints.

Why this exists (ING-1, audit 2026-09-29)
-----------------------------------------
``ingest_tabular`` writes collars and intervals with ``executemany`` in
batches of 500. Postgres evaluates the CHECK constraints per row, but a
violation aborts the WHOLE statement batch — so one collar with a blank
total depth, a mine-grid RL of 9,650 or an up-hole dip of +60 failed the
entire file, and in a workbook it took every survey, lithology and assay
sheet down with it. The two older writers floored total depth to 0.01 m;
the main path defaulted it to 0.0, which the database rejects.

The rule these helpers enforce is Kyle's (2026-09-29): a value the database
would refuse fails THAT ROW or THAT FIELD, never the batch.

* An out-of-range OPTIONAL (nullable) field is blanked, the row is kept,
  and the blanking is reported.
* A value a NOT NULL column cannot do without (hole id, total depth, the
  interval bounds) skips the row, with a reason.
* Nothing is invented. There is no floor-to-0.01 and no clamp: a total depth
  of 0.01 m is a number nobody measured.

The bounds below are NOT a second opinion on geology. They are copies of the
constraints the migrations create, and
``tests/test_silver_row_guard_matches_migrations.py`` fails if a migration
changes one without this module following. Whether the database SHOULD allow
up-holes (dip > 0) or elevations above 9,000 m is a §04e decision for Kyle;
until it changes, the writer must not send what the table refuses.

Pure functions, no database and no Hatchet import, so the tests run anywhere.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("georag.ingest.silver_row_guard")

# ---------------------------------------------------------------------------
# Constraint mirrors — database/migrations/2026_04_13_100000_database_hardening.php
# ---------------------------------------------------------------------------

#: chk_total_depth_positive: ``total_depth > 0``. The column is NOT NULL.
COLLAR_TOTAL_DEPTH_EXCLUSIVE_MIN: float = 0.0
#: chk_elevation_range: ``elevation >= -500 AND elevation <= 9000``. Nullable.
COLLAR_ELEVATION_RANGE: tuple[float, float] = (-500.0, 9000.0)
#: chk_azimuth_range: ``azimuth >= 0 AND azimuth <= 360``. Nullable.
COLLAR_AZIMUTH_RANGE: tuple[float, float] = (0.0, 360.0)
#: chk_dip_range: ``dip >= -90 AND dip <= 0``. Nullable.
COLLAR_DIP_RANGE: tuple[float, float] = (-90.0, 0.0)
#: chk_rqd_range / chk_recovery_range: ``0..100`` or NULL.
LITHOLOGY_PERCENT_RANGE: tuple[float, float] = (0.0, 100.0)
#: silver_mineralization_valid_pct: ``0..100`` or NULL.
MINERALIZATION_PCT_RANGE: tuple[float, float] = (0.0, 100.0)

#: varchar widths from the create-table migrations. A value one character
#: too long raises StringDataRightTruncation and, like a CHECK, aborts the
#: whole batch.
COLLAR_TEXT_WIDTHS: dict[str, int] = {
    "hole_id": 50, "hole_id_canonical": 50, "hole_type": 20, "status": 20,
}
LITHOLOGY_TEXT_WIDTHS: dict[str, int] = {
    "lithology_code": 20, "grain_size": 20, "color": 50, "hardness": 20,
    "weathering": 20,
}
SAMPLE_TEXT_WIDTHS: dict[str, int] = {
    "sample_type": 20, "lab_id": 50, "qaqc_type": 20,
}
SURVEY_TEXT_WIDTHS: dict[str, int] = {"survey_method": 20}

#: The documented stand-in for a NOT NULL text column the source did not
#: fill. Already the writer's default for hole_type / status / survey_method /
#: sample_type; kept in one place so the guard and the writer agree.
UNKNOWN: str = "unknown"

#: How many example rows a warning quotes.
_MAX_EXAMPLES = 5


def finite(value: Any) -> float | None:
    """*value* as a finite float, or None.

    NaN and infinity are treated as missing, not as numbers: ``NaN > 0`` is
    TRUE in Postgres (NaN sorts above every number), so a NaN total depth
    would pass one CHECK and fail the next, and a NaN elevation fails
    ``<= 9000``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        # Not a number is "no value" here; the caller decides whether the
        # field is optional (blank) or required (skip the row, reported).
        log.debug("silver_row_guard: %r is not numeric", value, exc_info=True)
        return None
    return number if math.isfinite(number) else None


@dataclass
class RowIssues:
    """What the guards did to one file's rows, for the run's warnings."""

    #: ``(field, row, hole_id, value, reason)`` for each blanked value.
    blanked: list[tuple[str, Any, str, Any, str]] = field(default_factory=list)
    #: ``(row, hole_id, reason)`` for each skipped row.
    skipped: list[tuple[Any, str, str]] = field(default_factory=list)
    #: ``(row, file hole_id, stored hole_id)`` for a collar row that updated
    #: an existing collar spelled differently (ING-14).
    merged: list[tuple[Any, str, str]] = field(default_factory=list)

    def blank(self, fld: str, rec: dict[str, Any], value: Any, reason: str) -> None:
        self.blanked.append((fld, _row(rec), _hole(rec), value, reason))

    def skip(self, rec: dict[str, Any], reason: str) -> None:
        self.skipped.append((_row(rec), _hole(rec), reason))

    def __bool__(self) -> bool:
        return bool(self.blanked or self.skipped or self.merged)

    def skipped_details(self) -> list[dict[str, Any]]:
        """The skips in the parsers' ``skipped_details`` shape."""
        return [
            {
                "row": row if row is not None else 0,
                "code": "db_constraint_row_skipped",
                "reason": f"row {row}: {reason}" if row is not None else reason,
            }
            for row, _hole_id, reason in self.skipped
        ]


def _row(rec: dict[str, Any]) -> Any:
    return rec.get("_source_row")


def _hole(rec: dict[str, Any]) -> str:
    return str(rec.get("hole_id") or "").strip()


def _in_range(value: float, bounds: tuple[float, float]) -> bool:
    return bounds[0] <= value <= bounds[1]


def fit_text(
    rec: dict[str, Any], fld: str, width: int, issues: RowIssues,
    *, default: str | None = None,
) -> str | None:
    """``rec[fld]`` as text that fits a ``varchar(width)``.

    Too long -> *default* (None for a nullable column, ``UNKNOWN`` for a NOT
    NULL one) and a blanking reported. Never truncated: a cut-off lithology
    code is a different code.
    """
    raw = rec.get(fld)
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    if len(text) > width:
        issues.blank(
            fld, rec, text[:40],
            f"{len(text)} characters, the column holds {width}",
        )
        return default
    return text


def guard_collar(
    rec: dict[str, Any], issues: RowIssues,
    *, existing_total_depth: float | None = None,
) -> dict[str, Any] | None:
    """The collar's writable values, or None when the row must be skipped.

    ``existing_total_depth`` is the depth already stored for this hole (from
    an earlier upload or a LAS header). A row with no usable total depth
    keeps it rather than being skipped — that is the stored value, not a
    guess. With nothing stored the row is skipped: total_depth is NOT NULL
    and 0.0 / 0.01 would both be invented depths.
    """
    hole_id = _hole(rec)
    easting, northing = finite(rec.get("easting")), finite(rec.get("northing"))
    if not hole_id:
        issues.skip(rec, "no hole id")
        return None
    if len(hole_id) > COLLAR_TEXT_WIDTHS["hole_id"]:
        issues.skip(
            rec,
            f"hole id is {len(hole_id)} characters; the column holds "
            f"{COLLAR_TEXT_WIDTHS['hole_id']}",
        )
        return None
    if easting is None or northing is None:
        issues.skip(rec, "no readable easting/northing")
        return None

    total_depth = finite(rec.get("total_depth"))
    if total_depth is None or total_depth <= COLLAR_TOTAL_DEPTH_EXCLUSIVE_MIN:
        if existing_total_depth is not None and existing_total_depth > 0:
            total_depth = existing_total_depth
        else:
            shown = rec.get("total_depth")
            issues.skip(
                rec,
                "no total depth"
                if shown in (None, "") else f"total depth {shown!r} is not > 0",
            )
            return None

    elevation = finite(rec.get("elevation"))
    if elevation is not None and not _in_range(elevation, COLLAR_ELEVATION_RANGE):
        issues.blank(
            "elevation", rec, elevation,
            "outside -500..9000 m (feet or a mine-grid RL offset?)",
        )
        elevation = None

    azimuth = finite(rec.get("azimuth"))
    if azimuth is not None and not _in_range(azimuth, COLLAR_AZIMUTH_RANGE):
        issues.blank("azimuth", rec, azimuth, "outside 0..360")
        azimuth = None

    dip = finite(rec.get("dip"))
    if dip is not None and not _in_range(dip, COLLAR_DIP_RANGE):
        issues.blank(
            "dip", rec, dip,
            "outside -90..0 (the table stores down-holes as negative; an "
            "up-hole or a positive-down convention cannot be stored yet)",
        )
        dip = None

    canonical = fit_text(
        rec, "hole_id_canonical", COLLAR_TEXT_WIDTHS["hole_id_canonical"], issues,
    )

    # hole_type / status are varchar(20) NOT NULL, and free-text exports put
    # sentences in them ("Diamond core, HQ to 120 m then NQ"). The row is
    # kept, the short column gets the documented 'unknown', and the full text
    # goes to the unbounded sibling the readers already fall back to
    # (drill_type, hole_status — nl_summaries and the agent's collar tools
    # read both) rather than being thrown away (PG-10).
    overflow: dict[str, str | None] = {"drill_type": None, "hole_status": None}
    short: dict[str, str] = {}
    for fld, sibling in (("hole_type", "drill_type"), ("status", "hole_status")):
        text = str(rec.get(fld) or "").strip()
        width = COLLAR_TEXT_WIDTHS[fld]
        if len(text) > width:
            issues.blank(
                fld, rec, text[:40],
                f"{len(text)} characters, the column holds {width}; the full "
                f"text was kept in {sibling}",
            )
            overflow[sibling] = text
            short[fld] = UNKNOWN
        else:
            short[fld] = text or UNKNOWN

    return {
        "hole_id": hole_id,
        "hole_id_canonical": canonical,
        "easting": easting,
        "northing": northing,
        "elevation": elevation,
        "total_depth": total_depth,
        "azimuth": azimuth,
        "dip": dip,
        "hole_type": short["hole_type"],
        "status": short["status"],
        "drill_type": overflow["drill_type"],
        "hole_status": overflow["hole_status"],
    }


def guard_interval(
    rec: dict[str, Any], issues: RowIssues,
) -> tuple[float, float] | None:
    """``(from, to)`` for a NOT NULL, ``from < to`` interval, or None (skipped)."""
    from_d, to_d = finite(rec.get("from_depth")), finite(rec.get("to_depth"))
    if from_d is None or to_d is None:
        issues.skip(rec, "interval has no readable from/to depth")
        return None
    if to_d <= from_d:
        issues.skip(rec, f"to depth {to_d:g} is not below from depth {from_d:g}")
        return None
    return from_d, to_d


def guard_percent(
    rec: dict[str, Any], fld: str, bounds: tuple[float, float], issues: RowIssues,
) -> float | None:
    """An optional percentage, blanked (and reported) when outside *bounds*."""
    value = finite(rec.get(fld))
    if value is not None and not _in_range(value, bounds):
        issues.blank(fld, rec, value, f"outside {bounds[0]:g}..{bounds[1]:g}")
        return None
    return value


def issue_warnings(
    issues: RowIssues, *, label: str, table: str,
) -> list[dict[str, Any]]:
    """The Ingestion Runs warnings for *issues* (``message`` + ``detail``)."""
    out: list[dict[str, Any]] = []
    if issues.skipped:
        examples = "; ".join(
            f"{'row ' + str(row) + ' ' if row is not None else ''}"
            f"{hole or '(no hole id)'}: {reason}"
            for row, hole, reason in issues.skipped[:_MAX_EXAMPLES]
        )
        more = len(issues.skipped) - min(len(issues.skipped), _MAX_EXAMPLES)
        out.append({
            "code": "db_constraint_rows_skipped",
            "message": (
                f"{len(issues.skipped)} {table} row(s) in {label} could not be "
                f"stored and were skipped"
            ),
            "detail": (
                f"{len(issues.skipped)} row(s) of {label} are missing a value "
                f"the {table} table cannot do without, so those rows were left "
                f"out and every other row was written: {examples}"
                + (f" (and {more} more)" if more else "")
                + ". Fix the values in the source and re-upload the file."
            )[:900],
            "rows": len(issues.skipped),
        })
    if issues.blanked:
        by_field: dict[str, int] = {}
        for fld, *_rest in issues.blanked:
            by_field[fld] = by_field.get(fld, 0) + 1
        examples = "; ".join(
            f"{hole or '?'} {fld}={value!r} ({reason})"
            for fld, _row_no, hole, value, reason in issues.blanked[:_MAX_EXAMPLES]
        )
        counts = ", ".join(f"{fld} x{n}" for fld, n in sorted(by_field.items()))
        out.append({
            "code": "db_constraint_values_blanked",
            "message": (
                f"{len(issues.blanked)} {table} value(s) in {label} were outside "
                f"what the table accepts and were left blank; the rows were kept"
            ),
            "detail": (
                f"{label}: {counts}. These values are outside the range the "
                f"{table} table stores, so the field was stored empty rather "
                f"than guessed at, and the row was kept. Examples: {examples}."
            )[:900],
            "fields": by_field,
        })
    if issues.merged:
        examples = ", ".join(
            f"{given!r} -> {stored!r}" for _row_no, given, stored in issues.merged[:_MAX_EXAMPLES]
        )
        out.append({
            "code": "hole_id_matched_existing_collar",
            "message": (
                f"{len(issues.merged)} hole id(s) in {label} matched an existing "
                f"collar spelled differently and updated it"
            ),
            "detail": (
                f"{label} spells {len(issues.merged)} hole id(s) differently from "
                f"the collar already stored (separators or case only), so the "
                f"existing collar was updated instead of a second one being "
                f"created: {examples}. The stored spelling was kept."
            )[:900],
        })
    return out


__all__ = [
    "COLLAR_AZIMUTH_RANGE",
    "COLLAR_DIP_RANGE",
    "COLLAR_ELEVATION_RANGE",
    "COLLAR_TEXT_WIDTHS",
    "COLLAR_TOTAL_DEPTH_EXCLUSIVE_MIN",
    "LITHOLOGY_PERCENT_RANGE",
    "LITHOLOGY_TEXT_WIDTHS",
    "MINERALIZATION_PCT_RANGE",
    "SAMPLE_TEXT_WIDTHS",
    "SURVEY_TEXT_WIDTHS",
    "UNKNOWN",
    "RowIssues",
    "finite",
    "fit_text",
    "guard_collar",
    "guard_interval",
    "guard_percent",
    "issue_warnings",
]
