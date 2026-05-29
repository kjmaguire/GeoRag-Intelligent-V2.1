"""CSV Survey Parser — Bronze → Silver ingestion for downhole survey data.

Accepts a CSV file path or file-like object, auto-detects column name variations
across common survey software exports, validates each row, and returns a list of
validated survey dicts ready for Silver schema insertion.

Dip sign convention is auto-detected (down-negative vs down-positive) and
normalised to down-negative for consistency with the silver.surveys table.

Parse quality metrics are emitted as structured log output so the caller can
record them in Dagster materialisation metadata.
"""

import logging
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import IO, Any, Union

import polars as pl

from georag_dagster.parsers._csv_io import (
    DEFAULT_NULL_VALUES,
    _check_decimal_comma,
    detect_delimiter,
    open_csv_with_encoding,
    transform_decimal_comma,
)
from georag_dagster.parsers._dip_convention import DipConvention, detect_dip_convention, normalize_dip
from georag_dagster.parsers._hole_id import canonicalize, suggest_collisions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column name alias maps — keys are canonical names, values are accepted aliases
# Order within each list reflects preference when multiple aliases are present.
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict = {
    "hole_id":       ["HoleID", "Hole_ID", "HOLEID", "hole_id", "DH_ID"],
    "depth":         ["Depth", "DEPTH", "Depth_m", "depth"],
    "azimuth":       ["Azimuth", "AZI", "AZ", "azimuth"],
    "dip":           ["Dip", "DIP", "Inclination", "INC", "dip"],
    "survey_method": ["Method", "SurveyMethod", "Instrument", "method"],
}

# Required fields — rows missing any of these are rejected
REQUIRED_FIELDS: frozenset = frozenset({"hole_id", "depth", "azimuth", "dip"})

# Numeric fields that must be castable to float
NUMERIC_FIELDS: frozenset = frozenset({"depth", "azimuth", "dip"})

# Valid survey methods — SME-defined list (update via config if scope grows)
VALID_SURVEY_METHODS: frozenset = frozenset({"Reflex", "Gyro", "Magnetic", "Acid Test"})

# Range checks
RANGE_CHECKS: dict = {
    "depth":   (0.0,   10_000.0),
    "azimuth": (0.0,   360.0),
    "dip":     (-90.0, 0.0),
}

# Warning / skip codes
_CODE_ENCODING_NON_UTF8 = "encoding_non_utf8"
_CODE_DIP_CONVENTION = "dip_convention_normalized"
_CODE_DIP_AMBIGUOUS = "dip_convention_ambiguous"
_CODE_MISSING_REQUIRED = "missing_required"
_CODE_NUMERIC_CAST = "numeric_cast_failed"
_CODE_RANGE = "range_check_failed"
_CODE_INVALID_METHOD = "invalid_survey_method"
_CODE_DECIMAL_COMMA = "decimal_comma_detected"


# ---------------------------------------------------------------------------
# Parse result dataclass
# ---------------------------------------------------------------------------

PARSER_VERSION = "2.0.0"


@dataclass
class SurveyParseResult:
    """Container for a completed survey parse run."""
    records: list
    total_rows: int
    valid_rows: int
    skipped_rows: int
    unmapped_columns: list
    column_map: dict
    skipped_details: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
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

def _build_column_map(csv_columns: list) -> tuple:
    """Map canonical field names to the first matching CSV column alias found."""
    csv_col_set = set(csv_columns)
    column_map: dict = {}

    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in csv_col_set:
                column_map[canonical] = alias
                break

    matched_csv_cols = set(column_map.values())
    unmapped = [c for c in csv_columns if c not in matched_csv_cols]
    return column_map, unmapped


def _cast_float(value) -> float:
    """Return float or None; never raises."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _validate_row(
    row_num: int,
    raw: dict,
    column_map: dict,
    dip_convention: DipConvention,
) -> tuple:
    """Validate a single raw row dict (keyed by canonical names).

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
    for canonical in column_map:
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
        else:
            record[canonical] = str(raw_val).strip() if raw_val is not None else None

    # --- Dip normalisation ---
    if record.get("dip") is not None and dip_convention == "down_positive":
        record["dip"] = normalize_dip(record["dip"], dip_convention)

    # --- Range checks ---
    for field_name, (lo, hi) in RANGE_CHECKS.items():
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
                    f"Check that '{field_name}' is in the expected unit. "
                    f"Depth range [{lo}, {hi}] m; azimuth 0–360; dip -90–0."
                ),
            }

    # --- Survey method validation ---
    method = record.get("survey_method")
    if method is not None and method not in VALID_SURVEY_METHODS:
        return None, {
            "row": row_num,
            "code": _CODE_INVALID_METHOD,
            "reason": (
                f"row {row_num}: survey_method '{method}' not in "
                f"allowed set {sorted(VALID_SURVEY_METHODS)}"
            ),
            "raw": raw,
            "expected": f"one of {sorted(VALID_SURVEY_METHODS)}",
            "actual": {"value": method},
            "suggestion": (
                "Map via COLUMN_ALIASES or consult the survey instrument vendor."
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

def parse_csv_surveys(
    source: Union[str, Path, IO],
    *,
    null_values: list = None,
) -> SurveyParseResult:
    """Parse a CSV downhole survey file and return a :class:`SurveyParseResult`.

    Parameters
    ----------
    source:
        Absolute file path (str or Path) or a file-like text stream.
    null_values:
        Additional strings to treat as null (on top of the Polars defaults).

    Returns
    -------
    SurveyParseResult
        Contains validated records plus quality metrics.
    """
    global_warnings: list = []
    detected_encoding = "utf-8"

    if isinstance(source, (str, Path)):
        source_file_str = str(source)
    else:
        source_file_str = "<stream>"

    all_nulls = list(set(DEFAULT_NULL_VALUES + (null_values or [])))

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
            logger.info("csv_survey: detected encoding '%s'", detected_encoding)

        # 2026-05-23 — delimiter auto-detection (CSV audit gap #1).
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
            logger.info("csv_survey: detected delimiter %r", detected_delim)

        df = pl.read_csv(
            StringIO(raw_content),
            separator=detected_delim,
            infer_schema=False,
            null_values=all_nulls,
            truncate_ragged_lines=True,
        )

        # 2026-05-23 — column-aware decimal-comma transform (CSV audit gap #2).
        df, transformed_cols = transform_decimal_comma(df)
        if transformed_cols:
            global_warnings.append({
                "row": None,
                "code": _CODE_DECIMAL_COMMA,
                "message": (
                    f"decimal-comma transform applied to columns: {transformed_cols!r}"
                ),
                "context": {
                    "encoding": detected_encoding,
                    "columns": transformed_cols,
                },
            })
            logger.info(
                "csv_survey: decimal-comma transformed %d column(s): %s",
                len(transformed_cols), transformed_cols,
            )
    except Exception as exc:
        logger.error("Failed to read CSV source: %s", exc)
        raise

    csv_columns: list = df.columns
    total_rows: int = len(df)

    logger.info("CSV loaded: %d rows, %d columns: %s", total_rows, len(csv_columns), csv_columns)

    column_map, unmapped = _build_column_map(csv_columns)

    if unmapped:
        logger.warning(
            "CSV survey parser: %d unmapped column(s) will be ignored: %s",
            len(unmapped),
            unmapped,
        )

    mapped_canonical = set(column_map.keys())
    missing_required = REQUIRED_FIELDS - mapped_canonical
    if missing_required:
        logger.error(
            "CSV is missing required columns (no alias matched): %s. Mapped columns: %s",
            missing_required,
            column_map,
        )
        return SurveyParseResult(
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
                "expected": f"columns matching {missing_required} in COLUMN_ALIASES",
                "actual": None,
                "suggestion": (
                    "Add aliases for the missing columns to COLUMN_ALIASES or rename "
                    "the CSV headers to a recognised alias."
                ),
            }],
            warnings=global_warnings,
            detected_encoding=detected_encoding,
        )

    rename_map = {v: k for k, v in column_map.items()}
    df_renamed = df.rename(rename_map)
    canonical_cols = [c for c in df_renamed.columns if c in column_map]
    df_trimmed = df_renamed.select(canonical_cols)

    # --- Dip convention detection (first pass) ---
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
                    "detected down_positive dip convention — flipping sign to down_negative"
                ),
                "context": {
                    "source_convention": dip_convention,
                    "sample_count": len(numeric_dips),
                },
            })
            logger.info(
                "csv_survey: down_positive dip convention detected (%d samples) — normalising",
                len(numeric_dips),
            )
        elif dip_convention == "ambiguous":
            global_warnings.append({
                "row": None,
                "code": _CODE_DIP_AMBIGUOUS,
                "message": (
                    "dip convention is ambiguous — no sign flip applied"
                ),
                "context": {
                    "source_convention": dip_convention,
                    "sample_count": len(numeric_dips),
                },
            })
            logger.warning(
                "csv_survey: ambiguous dip convention (%d samples) — no normalisation",
                len(numeric_dips),
            )

    records: list = []
    skipped: list = []

    rows_as_dicts = df_trimmed.to_dicts()
    for i, raw in enumerate(rows_as_dicts, start=2):
        record, skip_entry = _validate_row(i, raw, column_map, dip_convention)
        if record is not None:
            records.append(record)
        else:
            logger.warning("Skipping survey row: %s", skip_entry.get("reason"))
            skipped.append(skip_entry)

    valid_rows = len(records)
    skipped_rows = len(skipped)

    # --- hole_id collision detection ---
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
            "csv_survey: hole_id collision — '%s' and '%s' both → '%s'",
            collision["a"],
            collision["b"],
            collision["canonical"],
        )

    # --- Provenance ---
    provenance: dict = {
        "source_file": source_file_str,
        "source_file_sha256": sha256_hex,
        "parser_name": "csv_survey",
        "parser_version": PARSER_VERSION,
        "source_col_map": column_map,
    }

    result = SurveyParseResult(
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
        "CSV survey parse complete — total: %d, valid: %d, skipped: %d, quality: %.1f%%, "
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
