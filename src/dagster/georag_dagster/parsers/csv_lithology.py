"""CSV Lithology Parser — Bronze → Silver ingestion for lithology log data.

Accepts a CSV file path or file-like object, auto-detects column name variations
across common geological software exports, validates each row, and returns a list
of validated lithology dicts ready for Silver schema insertion.

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
from georag_dagster.parsers._hole_id import canonicalize, suggest_collisions
from georag_dagster.parsers._vendor_aliases import merge_vendor_aliases

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column name alias maps — keys are canonical names, values are accepted aliases
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict = {
    "hole_id":              ["HoleID", "Hole_ID", "hole_id"],
    "from_depth":           ["From", "FromDepth", "From_m", "from_depth"],
    "to_depth":             ["To", "ToDepth", "To_m", "to_depth"],
    "lithology_code":       ["Lithology", "LithCode", "RockCode", "lithology_code"],
    "lithology_description":["Description", "LithDesc", "Lithology_Description", "lithology_description"],
    "grain_size":           ["GrainSize", "Grain", "grain_size"],
    "color":                ["Color", "Colour", "color"],
    "hardness":             ["Hardness", "hardness"],
    "rqd":                  ["RQD", "RockQualityDesignation", "rqd"],
    "recovery":             ["Recovery", "CoreRecovery", "recovery"],
    "weathering":           ["Weathering", "Weathered", "weathering"],
}

# Required fields — rows missing any of these are rejected
REQUIRED_FIELDS: frozenset = frozenset({"hole_id", "from_depth", "to_depth", "lithology_code"})

# Numeric fields that must be castable to float
NUMERIC_FIELDS: frozenset = frozenset({"from_depth", "to_depth", "rqd", "recovery"})

# Categorical validation sets (None/absent values are allowed)
VALID_GRAIN_SIZES: frozenset = frozenset({"Fine", "Medium", "Coarse", "Very Coarse"})
VALID_HARDNESS: frozenset = frozenset({"Soft", "Medium", "Hard", "Very Hard"})
VALID_WEATHERING: frozenset = frozenset({"Fresh", "Slight", "Moderate", "High", "Complete"})

# Range checks for numeric fields
RANGE_CHECKS: dict = {
    "from_depth": (0.0, 10_000.0),
    "to_depth":   (0.0, 10_000.0),
    "rqd":        (0.0, 100.0),
    "recovery":   (0.0, 100.0),
}

# Warning / skip codes
_CODE_ENCODING_NON_UTF8 = "encoding_non_utf8"
_CODE_MISSING_REQUIRED = "missing_required"
_CODE_NUMERIC_CAST = "numeric_cast_failed"
_CODE_DEPTH_ORDER = "depth_order_invalid"
_CODE_DEPTH_NEG = "depth_negative"
_CODE_RANGE = "range_check_failed"
_CODE_CATEGORICAL = "invalid_categorical_value"
_CODE_DECIMAL_COMMA = "decimal_comma_detected"


# ---------------------------------------------------------------------------
# Parse result dataclass
# ---------------------------------------------------------------------------

PARSER_VERSION = "2.0.0"


@dataclass
class LithologyParseResult:
    """Container for a completed lithology parse run."""
    records: list
    total_rows: int
    valid_rows: int
    skipped_rows: int
    unmapped_columns: list
    column_map: dict
    skipped_details: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    detected_encoding: str = "utf-8"
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
    csv_columns: list,
    *,
    aliases: dict | None = None,
) -> tuple:
    """Map canonical field names to the first matching CSV column alias found.

    Parameters
    ----------
    csv_columns:
        Column names present in the source CSV.
    aliases:
        Optional alias dict to use instead of the module-level
        COLUMN_ALIASES. Used by parse_csv_lithology to inject a
        vendor-profile-merged dict (CC-02 Item 6).
    """
    csv_col_set = set(csv_columns)
    column_map: dict = {}
    alias_source = aliases if aliases is not None else COLUMN_ALIASES

    for canonical, alias_list in alias_source.items():
        for alias in alias_list:
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

    # --- from_depth / to_depth ordering ---
    from_d = record.get("from_depth")
    to_d = record.get("to_depth")
    if from_d is not None and to_d is not None:
        if from_d < 0:
            return None, {
                "row": row_num,
                "code": _CODE_DEPTH_NEG,
                "reason": f"row {row_num}: from_depth {from_d} must be >= 0",
                "raw": raw,
                "expected": "from_depth >= 0",
                "actual": {"from_depth": from_d},
                "suggestion": "Negative downhole depth is likely a sign-convention error.",
            }
        if to_d <= from_d:
            return None, {
                "row": row_num,
                "code": _CODE_DEPTH_ORDER,
                "reason": (
                    f"row {row_num}: to_depth {to_d} must be > from_depth {from_d}"
                ),
                "raw": raw,
                "expected": "to_depth > from_depth",
                "actual": {"from_depth": from_d, "to_depth": to_d},
                "suggestion": "Swap the from/to columns or check data entry.",
            }

    # --- Range checks ---
    for field_name, (lo, hi) in RANGE_CHECKS.items():
        if field_name in ("from_depth", "to_depth"):
            continue  # already checked above with ordering logic
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
                "suggestion": f"Check '{field_name}' units; expected range [{lo}, {hi}].",
            }

    # --- Categorical validations (optional fields) ---
    grain = record.get("grain_size")
    if grain is not None and grain not in VALID_GRAIN_SIZES:
        return None, {
            "row": row_num,
            "code": _CODE_CATEGORICAL,
            "reason": (
                f"row {row_num}: grain_size '{grain}' not in "
                f"allowed set {sorted(VALID_GRAIN_SIZES)}"
            ),
            "raw": raw,
            "expected": f"one of {sorted(VALID_GRAIN_SIZES)}",
            "actual": {"value": grain},
            "suggestion": "Map via COLUMN_ALIASES or consult the geologist.",
        }

    hardness = record.get("hardness")
    if hardness is not None and hardness not in VALID_HARDNESS:
        return None, {
            "row": row_num,
            "code": _CODE_CATEGORICAL,
            "reason": (
                f"row {row_num}: hardness '{hardness}' not in "
                f"allowed set {sorted(VALID_HARDNESS)}"
            ),
            "raw": raw,
            "expected": f"one of {sorted(VALID_HARDNESS)}",
            "actual": {"value": hardness},
            "suggestion": "Map via COLUMN_ALIASES or consult the geologist.",
        }

    weathering = record.get("weathering")
    if weathering is not None and weathering not in VALID_WEATHERING:
        return None, {
            "row": row_num,
            "code": _CODE_CATEGORICAL,
            "reason": (
                f"row {row_num}: weathering '{weathering}' not in "
                f"allowed set {sorted(VALID_WEATHERING)}"
            ),
            "raw": raw,
            "expected": f"one of {sorted(VALID_WEATHERING)}",
            "actual": {"value": weathering},
            "suggestion": "Map via COLUMN_ALIASES or consult the geologist.",
        }

    # --- hole_id canonicalization ---
    record["hole_id_canonical"] = canonicalize(record.get("hole_id"))

    # --- source row tracking ---
    record["_source_row"] = row_num

    return record, None


# ---------------------------------------------------------------------------
# Public parser entry point
# ---------------------------------------------------------------------------

def parse_csv_lithology(
    source: Union[str, Path, IO],
    *,
    null_values: list = None,
    vendor_aliases: dict[str, list[str]] | None = None,
) -> LithologyParseResult:
    """Parse a CSV lithology log file and return a :class:`LithologyParseResult`.

    Parameters
    ----------
    source:
        Absolute file path (str or Path) or a file-like text stream.
    null_values:
        Additional strings to treat as null (on top of the Polars defaults).
    vendor_aliases:
        Optional per-vendor column aliases keyed by canonical field name,
        merged with the module-level COLUMN_ALIASES at column-resolution
        time. Vendor entries take precedence on tie. Use this to onboard
        non-standard exporters (MX Deposit, ALS, SGS, etc.) without
        modifying COLUMN_ALIASES. CC-02 Item 6.

    Returns
    -------
    LithologyParseResult
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
            logger.info("csv_lithology: detected encoding '%s'", detected_encoding)

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
            logger.info("csv_lithology: detected delimiter %r", detected_delim)

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
                "csv_lithology: decimal-comma transformed %d column(s): %s",
                len(transformed_cols), transformed_cols,
            )
    except Exception as exc:
        logger.error("Failed to read CSV source: %s", exc)
        raise

    csv_columns: list = df.columns
    total_rows: int = len(df)

    logger.info("CSV loaded: %d rows, %d columns: %s", total_rows, len(csv_columns), csv_columns)

    effective_aliases = merge_vendor_aliases(COLUMN_ALIASES, vendor_aliases)
    column_map, unmapped = _build_column_map(
        csv_columns, aliases=effective_aliases,
    )

    if unmapped:
        logger.warning(
            "CSV lithology parser: %d unmapped column(s) will be ignored: %s",
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
        return LithologyParseResult(
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

    records: list = []
    skipped: list = []

    rows_as_dicts = df_trimmed.to_dicts()
    for i, raw in enumerate(rows_as_dicts, start=2):
        record, skip_entry = _validate_row(i, raw, column_map)
        if record is not None:
            records.append(record)
        else:
            logger.warning("Skipping lithology row: %s", skip_entry.get("reason"))
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
            "csv_lithology: hole_id collision — '%s' and '%s' both → '%s'",
            collision["a"],
            collision["b"],
            collision["canonical"],
        )

    # --- Provenance ---
    provenance: dict = {
        "source_file": source_file_str,
        "source_file_sha256": sha256_hex,
        "parser_name": "csv_lithology",
        "parser_version": PARSER_VERSION,
        "source_col_map": column_map,
    }

    result = LithologyParseResult(
        records=records,
        total_rows=total_rows,
        valid_rows=valid_rows,
        skipped_rows=skipped_rows,
        unmapped_columns=unmapped,
        column_map=column_map,
        skipped_details=skipped,
        warnings=global_warnings,
        detected_encoding=detected_encoding,
        provenance=provenance,
    )

    logger.info(
        "CSV lithology parse complete — total: %d, valid: %d, skipped: %d, quality: %.1f%%, "
        "unmapped cols: %d, warnings: %d",
        total_rows,
        valid_rows,
        skipped_rows,
        result.parse_quality_pct,
        len(unmapped),
        len(global_warnings),
    )

    return result
