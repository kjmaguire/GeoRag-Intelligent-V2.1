"""CSV Collar Parser — Bronze → Silver ingestion for drill hole collar data.

Accepts a CSV file path or file-like object, auto-detects column name variations
across common survey software exports, validates each row, and returns a list of
validated collar dicts ready for Silver schema insertion.

Dip sign convention is auto-detected (down-negative vs down-positive) and
normalised to down-negative (the silver.collars CHECK constraint convention).

Parse quality metrics are emitted as structured log output so the caller can
record them in Dagster materialisation metadata.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from io import StringIO
from pathlib import Path
from typing import IO, Any, Union

import polars as pl

from georag_geoparsers._csv_io import (
    DEFAULT_NULL_VALUES,
    detect_delimiter,
    open_csv_with_encoding,
    transform_decimal_comma,
)
from georag_geoparsers._dip_convention import DipConvention, detect_dip_convention, normalize_dip
from georag_geoparsers._drill_schema import (
    COLLAR_ALIASES,
    COLLAR_REQUIRED,
    ELEVATION_BOUNDS,
    coordinate_bounds,
    coordinate_family_conflict,
    detect_coordinate_mode,
)
from georag_geoparsers._header_match import build_column_map
from georag_geoparsers._hole_id import canonicalize, suggest_collisions
from georag_geoparsers._vendor_aliases import merge_vendor_aliases

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column name alias maps — keys are canonical names, values are accepted
# aliases. Order within each list reflects preference when multiple aliases
# are present.
#
# The vocabulary itself lives in _drill_schema, which the FastAPI writer and
# the sheet classifier read from the same module: three drifting copies of
# this dict is what let the classifier accept a sheet the writer then
# rejected in full. Re-exported under the historical names because
# _sheet_classifier and several tests import them from here.
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict[str, list[str]] = COLLAR_ALIASES
REQUIRED_FIELDS: frozenset[str] = COLLAR_REQUIRED

# Numeric fields that must be castable to float
NUMERIC_FIELDS: frozenset[str] = frozenset({"easting", "northing", "elevation", "total_depth", "azimuth", "dip"})

# Sanity ranges for the fields whose bounds do not depend on the coordinate
# system. Easting and northing are NOT here: their bounds are chosen per
# file by _drill_schema.detect_coordinate_mode, because a fixed UTM window
# rejected decimal degrees, local mine grids and State Plane feet alike.
RANGE_CHECKS: dict[str, tuple[float, float]] = {
    "elevation":   ELEVATION_BOUNDS,
    "total_depth": (0.0,        10_000.0),     # Deepest drill hole sanity check
    "azimuth":     (0.0,        360.0),
    "dip":         (-90.0,      90.0),
}

# Warning / skip codes
_CODE_ENCODING_NON_UTF8 = "encoding_non_utf8"
_CODE_DIP_CONVENTION = "dip_convention_normalized"
_CODE_DIP_AMBIGUOUS = "dip_convention_ambiguous"
_CODE_MISSING_REQUIRED = "missing_required"
_CODE_NUMERIC_CAST = "numeric_cast_failed"
_CODE_RANGE = "range_check_failed"
_CODE_DECIMAL_COMMA = "decimal_comma_detected"
_CODE_COORD_FAMILY_CONFLICT = "coordinate_family_conflict"


# ---------------------------------------------------------------------------
# Parse result dataclass
# ---------------------------------------------------------------------------

PARSER_VERSION = "2.0.0"


@dataclass
class CollarParseResult:
    """Container for a completed parse run."""
    records: list[dict]
    total_rows: int
    valid_rows: int
    skipped_rows: int
    unmapped_columns: list[str]
    column_map: dict[str, str]          # canonical → original CSV column name
    skipped_details: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    detected_encoding: str = "utf-8"
    dip_convention: str = "down_negative"
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def parse_quality_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return round(self.valid_rows / self.total_rows * 100, 2)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_column_map(
    csv_columns: list[str],
    *,
    aliases: dict[str, list[str]] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Map canonical field names to the first matching CSV column alias found.

    Matching is delegated to ``_header_match``, which folds case, separators
    and unit suffixes, so ``Hole ID`` / ``Hole_ID`` / ``HOLEID`` and
    ``Depth_m`` / ``Depth (m)`` all land on the same field. It used to be an
    exact case-sensitive test here, which is why a file was rejected in full
    for spelling a header with a space.

    Returns:
        column_map   — {canonical_name: csv_column_name}
        unmapped     — CSV columns that matched no canonical alias
    """
    return build_column_map(csv_columns, aliases if aliases is not None else COLUMN_ALIASES)


def _parse_date(value: str | None) -> date | None:
    """Try a handful of common date formats; return None on failure."""
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y%m%d", "%d-%b-%Y"):
        try:
            return date.fromisoformat(raw) if fmt == "%Y-%m-%d" else __import__("datetime").datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None  # unparseable — not a rejection-worthy failure


def _cast_float(value) -> float | None:
    """Return float or None; never raises."""
    if value is None:
        return None
    try:
        f = float(str(value).strip())
        return f
    except (ValueError, TypeError):
        return None


def _validate_row(
    row_num: int,
    raw: dict,
    column_map: dict[str, str],
    dip_convention: DipConvention,
    coord_bounds: dict[str, tuple[float, float]],
) -> tuple[dict | None, dict | None]:
    """Validate a single raw row dict (keyed by canonical names).

    ``coord_bounds`` carries the easting/northing limits chosen for this
    file as a whole (see :func:`_drill_schema.detect_coordinate_mode`) — a
    per-row decision would let one file be judged against two rulers.

    Returns (record, None) on success or (None, skip_entry) on failure.
    skip_entry includes extended diagnostic fields per Sprint 2 contract:
      expected, actual, suggestion.
    """
    # --- Required field presence ---
    for req in REQUIRED_FIELDS:
        val = raw.get(req)
        if val is None or str(val).strip() == "":
            return None, {
                "row": row_num,
                "code": _CODE_MISSING_REQUIRED,
                "reason": f"row {row_num}: missing required field '{req}'",
                "raw": raw,
                "expected": f"non-empty value for '{req}'",
                "actual": None,
                "suggestion": (
                    f"Ensure the '{req}' column is present and populated, "
                    f"or add an alias to COLUMN_ALIASES."
                ),
            }

    # --- Numeric casting ---
    record: dict = {}
    for canonical, csv_col in column_map.items():
        raw_val = raw.get(canonical)
        if canonical in NUMERIC_FIELDS:
            casted = _cast_float(raw_val)
            if casted is None and canonical in REQUIRED_FIELDS:
                return None, {
                    "row": row_num,
                    "code": _CODE_NUMERIC_CAST,
                    "reason": (
                        f"row {row_num}: cannot cast required numeric field "
                        f"'{canonical}' value '{raw_val}'"
                    ),
                    "raw": raw,
                    "expected": "numeric value",
                    "actual": {"field": canonical, "value": raw_val},
                    "suggestion": (
                        "Remove text units from the value cell, "
                        "or set the column null representation."
                    ),
                }
            record[canonical] = casted
        elif canonical == "drill_date":
            record[canonical] = _parse_date(raw_val)
        else:
            record[canonical] = str(raw_val).strip() if raw_val is not None else None

    # --- Dip normalisation ---
    if record.get("dip") is not None and dip_convention == "down_positive":
        record["dip"] = normalize_dip(record["dip"], dip_convention)

    # --- Range checks ---
    for field_name, (lo, hi) in {**RANGE_CHECKS, **coord_bounds}.items():
        val = record.get(field_name)
        if val is not None and not (lo <= val <= hi):
            return None, {
                "row": row_num,
                "code": _CODE_RANGE,
                "reason": (
                    f"row {row_num}: field '{field_name}' value {val} "
                    f"out of range [{lo}, {hi}]"
                ),
                "raw": raw,
                "expected": f"{field_name} in [{lo}, {hi}]",
                "actual": {field_name: val},
                "suggestion": (
                    f"Check that '{field_name}' is in the expected unit. These "
                    f"are absurdity bounds, not a coordinate-system check — a "
                    f"value outside [{lo}, {hi}] is usually a depth, a text "
                    f"cell, or a column that mapped to the wrong field."
                ),
            }

    # --- hole_id canonicalization ---
    record["hole_id_canonical"] = canonicalize(record.get("hole_id"))

    # --- source row tracking ---
    record["_source_row"] = row_num

    return record, None


# ---------------------------------------------------------------------------
# Public parser entry point
# ---------------------------------------------------------------------------

def parse_csv_collars(
    source: Union[str, Path, IO[str]],  # noqa: UP007
    *,
    null_values: list[str] | None = None,
    vendor_aliases: dict[str, list[str]] | None = None,
) -> CollarParseResult:
    """Parse a CSV collar file and return a :class:`CollarParseResult`.

    Parameters
    ----------
    source:
        Absolute file path (str or Path) or a file-like text stream.
    null_values:
        Additional strings to treat as null (on top of the Polars defaults).
        Common survey software emits "-", "N/A", "NULL", "n/a".
    vendor_aliases:
        Extra column spellings keyed by canonical field name, merged ahead
        of COLUMN_ALIASES so they win on a tie. Two callers use this:
        a stored vendor profile (CC-02 Item 6), and a column mapping the
        USER confirmed for one file, which arrives as a single-entry list
        per field. The mapping is still matched through
        ``_header_match``, so a user who types ``Hole ID`` for a column
        headed ``hole_id`` gets what they meant.

    Returns
    -------
    CollarParseResult
        Contains validated records plus quality metrics.
    """
    global_warnings: list[dict] = []
    detected_encoding = "utf-8"

    # Resolve source path/name for provenance
    if isinstance(source, (str, Path)):
        source_file_str = str(source)
    else:
        source_file_str = "<stream>"

    all_nulls = list(set(DEFAULT_NULL_VALUES + (null_values or [])))

    # --- Encoding detection and read ---
    try:
        stream, detected_encoding, sha256_hex, _byte_count = open_csv_with_encoding(source)
        raw_content = stream.getvalue()

        if detected_encoding.lower().replace("-", "") not in ("utf8", "utf-8", "ascii"):
            global_warnings.append({
                "row": None,
                "code": _CODE_ENCODING_NON_UTF8,
                "message": (
                    f"detected encoding '{detected_encoding}' (not UTF-8) — "
                    f"decoded with replacement"
                ),
                "context": {"encoding": detected_encoding},
            })
            logger.info("csv_collar: detected encoding '%s'", detected_encoding)

        # 2026-05-23 CSV audit gap #1 — auto-detect delimiter so semicolon/
        # tab/pipe CSVs (esp. EU exports) don't silently collapse to one
        # column under Polars' default comma assumption.
        detected_delim = detect_delimiter(raw_content, default=",")
        if detected_delim != ",":
            global_warnings.append({
                "row": None,
                "code": "delimiter_non_comma",
                "message": (
                    f"detected delimiter {detected_delim!r} (non-comma) — "
                    "Polars read_csv configured accordingly"
                ),
                "context": {"delimiter": detected_delim},
            })
            logger.info("csv_collar: detected delimiter %r", detected_delim)

        df = pl.read_csv(
            StringIO(raw_content),
            separator=detected_delim,
            infer_schema=False,
            null_values=all_nulls,
            truncate_ragged_lines=True,
        )

        # 2026-05-23 CSV audit gap #2 — column-aware decimal-comma transform
        # (replaces the previous warn-only path). Per-column gate: only
        # columns whose every sampled non-null value matches the decimal-
        # comma pattern get rewritten. Hole-ID columns, text columns, and
        # mixed-format columns are left alone.
        df, transformed_cols = transform_decimal_comma(df)
        if transformed_cols:
            global_warnings.append({
                "row": None,
                "code": _CODE_DECIMAL_COMMA,
                "message": (
                    f"decimal-comma transform applied to columns: "
                    f"{transformed_cols!r}"
                ),
                "context": {
                    "encoding": detected_encoding,
                    "columns": transformed_cols,
                },
            })
            logger.info(
                "csv_collar: decimal-comma transformed %d column(s): %s",
                len(transformed_cols), transformed_cols,
            )
    except Exception as exc:
        logger.error("Failed to read CSV source: %s", exc)
        raise

    csv_columns: list[str] = df.columns
    total_rows: int = len(df)

    logger.info("CSV loaded: %d rows, %d columns: %s", total_rows, len(csv_columns), csv_columns)

    # --- Build column map ---
    effective_aliases = merge_vendor_aliases(COLUMN_ALIASES, vendor_aliases)
    column_map, unmapped = _build_column_map(csv_columns, aliases=effective_aliases)

    if unmapped:
        logger.warning(
            "CSV collar parser: %d unmapped column(s) will be ignored: %s",
            len(unmapped),
            unmapped,
        )

    mapped_canonical = set(column_map.keys())
    missing_required = REQUIRED_FIELDS - mapped_canonical
    if missing_required:
        logger.error(
            "CSV is missing required columns (no alias matched): %s. "
            "Mapped columns: %s",
            missing_required,
            column_map,
        )
        return CollarParseResult(
            records=[],
            total_rows=total_rows,
            valid_rows=0,
            skipped_rows=total_rows,
            unmapped_columns=unmapped,
            column_map=column_map,
            skipped_details=[{
                "row": None,
                "code": _CODE_MISSING_REQUIRED,
                "reason": f"file-level: missing required column mapping(s): {missing_required}",
                "raw": {},
                "expected": f"columns matching {sorted(missing_required)}",
                # What DID map, and what was left over. A user told only
                # "elevation is missing" cannot tell whether the column is
                # absent or merely spelled unusually; the two lists together
                # answer that at a glance, and they are the same lists the
                # column-mapping step offers to correct.
                "actual": {
                    "mapped": dict(sorted(column_map.items())),
                    "unmatched_columns": unmapped,
                },
                "suggestion": (
                    "Map the missing field(s) to one of this file's own columns, "
                    "or rename the header to a recognised spelling. Header "
                    "matching ignores case, spaces, underscores and unit "
                    "suffixes, so 'Hole ID', 'hole_id' and 'HOLEID' are all "
                    "understood — a field listed here has no column resembling "
                    "it at all."
                ),
            }],
            warnings=global_warnings,
            detected_encoding=detected_encoding,
        )

    # --- Coordinate columns that disagree about what they measure ---
    #
    # Refused rather than resolved. 'Easting' beside 'LATITUDE' passes every
    # per-field check — each value is in range for its own column — and
    # produces a hole 57 metres north of the equator. Nothing here can tell
    # which of the two headers is the mistake, so neither is assumed.
    conflict = coordinate_family_conflict(
        column_map.get("easting"), column_map.get("northing")
    )
    if conflict is not None:
        east_family, north_family = conflict
        return CollarParseResult(
            records=[],
            total_rows=total_rows,
            valid_rows=0,
            skipped_rows=total_rows,
            unmapped_columns=unmapped,
            column_map=column_map,
            skipped_details=[{
                "row": None,
                "code": _CODE_COORD_FAMILY_CONFLICT,
                "reason": (
                    f"file-level: '{column_map['easting']}' names a "
                    f"{east_family} coordinate but '{column_map['northing']}' "
                    f"names a {north_family} one"
                ),
                "raw": {},
                "expected": "both coordinate columns in the same system",
                "actual": {
                    "easting": {"column": column_map["easting"], "family": east_family},
                    "northing": {"column": column_map["northing"], "family": north_family},
                },
                "suggestion": (
                    "One of these two headers is wrong. Degrees and metres "
                    "cannot be paired: rename the mislabelled column, or map "
                    "each field to the column that really holds it."
                ),
            }],
            warnings=global_warnings,
            detected_encoding=detected_encoding,
        )

    # --- Rename DataFrame to canonical names for row iteration ---
    rename_map = {v: k for k, v in column_map.items()}
    df_renamed = df.rename(rename_map)

    # Only keep columns we have a canonical mapping for
    canonical_cols = [c for c in df_renamed.columns if c in column_map]
    df_trimmed = df_renamed.select(canonical_cols)

    # --- Dip convention detection (first pass over dip values) ---
    dip_convention: DipConvention = "down_negative"
    if "dip" in column_map:
        raw_dips = df_trimmed["dip"].to_list()
        numeric_dips = [_cast_float(v) for v in raw_dips]
        numeric_dips = [d for d in numeric_dips if d is not None]
        dip_convention = detect_dip_convention(numeric_dips)

        if dip_convention == "down_positive":
            global_warnings.append({
                "row": None,
                "code": _CODE_DIP_CONVENTION,
                "message": (
                    "detected down_positive dip convention — flipping sign to down_negative "
                    "(DB convention)"
                ),
                "context": {
                    "source_convention": dip_convention,
                    "sample_count": len(numeric_dips),
                },
            })
            logger.info(
                "csv_collar: down_positive dip convention detected (%d samples) — normalising",
                len(numeric_dips),
            )
        elif dip_convention == "ambiguous":
            global_warnings.append({
                "row": None,
                "code": _CODE_DIP_AMBIGUOUS,
                "message": (
                    "dip convention is ambiguous (mix of positive and negative values) — "
                    "no sign flip applied; DB CHECK may reject out-of-range rows"
                ),
                "context": {
                    "source_convention": dip_convention,
                    "sample_count": len(numeric_dips),
                },
            })
            logger.warning(
                "csv_collar: ambiguous dip convention (%d samples) — no normalisation applied",
                len(numeric_dips),
            )

    # --- Validate rows ---
    records: list[dict] = []
    skipped: list[dict] = []

    rows_as_dicts = df_trimmed.to_dicts()

    # --- Coordinate bounds, chosen once for the whole file ---
    #
    # Decided across every row before any row is judged, so one file is
    # measured against one ruler. Doing it per row would let the first
    # decimal-degree row set geographic bounds and the next UTM row fail
    # against them.
    coord_mode = detect_coordinate_mode(
        [_cast_float(r.get("easting")) for r in rows_as_dicts],
        [_cast_float(r.get("northing")) for r in rows_as_dicts],
    )
    coord_bounds = coordinate_bounds(coord_mode)
    global_warnings.append({
        "row": None,
        "code": "coordinate_mode_detected",
        "message": (
            f"coordinates read as {coord_mode} for range checking "
            f"(easting {coord_bounds['easting']}, northing {coord_bounds['northing']})"
        ),
        "context": {"mode": coord_mode},
    })

    for i, raw in enumerate(rows_as_dicts, start=2):  # row 1 = header, data starts at 2
        record, skip_entry = _validate_row(i, raw, column_map, dip_convention, coord_bounds)
        if record is not None:
            records.append(record)
        else:
            # Log the stable reason CODE, never the free-text `reason` —
            # that string interpolates raw cell values, so logging it
            # ships arbitrary spreadsheet content to the application log
            # and on to Log Analytics. The full reason (and the raw row)
            # stays in skipped_details for the ingest report.
            logger.warning(
                "Skipping collar row %s: %s",
                skip_entry.get("row"),
                skip_entry.get("code"),
            )
            skipped.append(skip_entry)

    valid_rows = len(records)
    skipped_rows = len(skipped)

    # --- hole_id collision detection ---
    # Pairs of different raw forms that canonicalize to the same value
    all_raw_hole_ids = [r["hole_id"] for r in records if r.get("hole_id")]
    collision_pairs = suggest_collisions(all_raw_hole_ids)
    for collision in collision_pairs:
        global_warnings.append({
            "row": None,
            "code": "hole_id_canonical_collision",
            "message": (
                f"{collision['a']!r} and {collision['b']!r} both canonicalize "
                f"to {collision['canonical']!r}"
            ),
            "context": {
                "raw_a": collision["a"],
                "raw_b": collision["b"],
                "canonical": collision["canonical"],
            },
        })
        logger.warning(
            "csv_collar: hole_id collision — '%s' and '%s' both → '%s'",
            collision["a"],
            collision["b"],
            collision["canonical"],
        )

    # --- Provenance ---
    provenance: dict = {
        "source_file": source_file_str,
        "source_file_sha256": sha256_hex,
        "parser_name": "csv_collar",
        "parser_version": PARSER_VERSION,
        "source_col_map": column_map,
    }

    result = CollarParseResult(
        records=records,
        total_rows=total_rows,
        valid_rows=valid_rows,
        skipped_rows=skipped_rows,
        unmapped_columns=unmapped,
        column_map=column_map,
        skipped_details=skipped,
        warnings=global_warnings,
        detected_encoding=detected_encoding,
        dip_convention=dip_convention,
        provenance=provenance,
    )

    logger.info(
        "CSV collar parse complete — total: %d, valid: %d, skipped: %d, quality: %.1f%%, "
        "unmapped cols: %d, dip_convention: %s, warnings: %d",
        total_rows,
        valid_rows,
        skipped_rows,
        result.parse_quality_pct,
        len(unmapped),
        dip_convention,
        len(global_warnings),
    )

    return result
