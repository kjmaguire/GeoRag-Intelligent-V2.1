"""CSV Sample Parser — Bronze → Silver ingestion for geochemical sample assay data.

Accepts a CSV file path or file-like object, auto-detects column name variations
across common LIMS/lab exports, validates each row, and returns a list of validated
sample dicts ready for Silver schema insertion.

Commodity assay columns are auto-detected via regex — any column matching the
pattern ^(U3O8|Au|Ag|Cu|Pb|Zn|Ni|Fe|Ti|Li)_?(ppm|pct|ppb|pct_|_pct)?$ is
collected into the `commodity_assays` dict as a JSONB payload.

Below-detection values ("<0.01", "BDL", etc.) are captured in commodity_assay_flags
rather than causing row rejection.

Parse quality metrics are emitted as structured log output so the caller can
record them in Dagster materialisation metadata.
"""

import logging
import re
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import IO, Any, Union

import polars as pl

from georag_dagster.parsers._csv_io import (
    SAMPLE_NULL_VALUES,
    detect_delimiter,
    open_csv_with_encoding,
    transform_decimal_comma,
)
from georag_dagster.parsers._hole_id import canonicalize, suggest_collisions
from georag_dagster.parsers._unit_ambiguity import (
    detect_long_format_units,
    detect_wide_format,
    merge_flags,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column name alias maps
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict = {
    "hole_id":    ["HoleID", "Hole_ID", "hole_id"],
    "from_depth": ["From", "FromDepth", "from_depth"],
    "to_depth":   ["To", "ToDepth", "to_depth"],
    "sample_type":["SampleType", "Type", "sample_type"],
    "lab_id":     ["LabID", "Lab", "LabNumber", "lab_id"],
    "qaqc_type":  ["QAQC", "QC", "qaqc_type"],
    "sample_id":  ["SampleID", "Sample_ID", "sample_id", "Sample Number", "SampleNum"],
}

# Required fields — rows missing any of these are rejected
REQUIRED_FIELDS: frozenset = frozenset({"hole_id", "from_depth", "to_depth", "sample_type"})

# Numeric fields
NUMERIC_FIELDS: frozenset = frozenset({"from_depth", "to_depth"})

# Commodity element / unit regex — matches assay column headers
# Examples: U3O8_ppm, Au_ppb, Cu_pct, Pb_ppm, U3O8ppm, Fe_pct
ASSAY_COLUMN_RE = re.compile(
    r"^(U3O8|Au|Ag|Cu|Pb|Zn|Ni|Fe|Ti|Li)_?(ppm|pct|ppb|pct_|_pct)?$",
    re.IGNORECASE,
)

# Below-detection literal tokens (case-insensitive) — treated as BDL with unknown threshold
_BDL_LITERALS: frozenset = frozenset({"lod", "bdl", "<lod", "<dl"})

# Below-detection prefix pattern: "<0.01", "< 0.001", etc.
_BELOW_DETECT_RE = re.compile(r"^<\s*(\d+(?:\.\d+)?)$")

# Categorical validation sets
VALID_SAMPLE_TYPES: frozenset = frozenset({"Core", "Chip", "Grab", "Channel", "Soil"})
VALID_QAQC_TYPES: frozenset = frozenset({"Primary", "Duplicate", "Blank", "Standard"})

# QAQC prefix sniff patterns (case-insensitive)
_QAQC_PREFIX_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(STD|STAND|OREAS|CRM|CDN|CANMET)", re.IGNORECASE), "Standard"),
    (re.compile(r"^(BLK|BLANK)", re.IGNORECASE), "Blank"),
    (re.compile(r"^(DUP|FD|CD)", re.IGNORECASE), "Duplicate"),
]

# ---------------------------------------------------------------------------
# Warning / skip codes
# ---------------------------------------------------------------------------
_CODE_ENCODING_NON_UTF8 = "encoding_non_utf8"
_CODE_QAQC_DETECTED = "qaqc_detected_by_prefix"
_CODE_ASSAY_BDL = "assay_below_detection"
_CODE_ASSAY_UNPARSEABLE = "assay_unparseable"
_CODE_MISSING_REQUIRED = "missing_required"
_CODE_NUMERIC_CAST = "numeric_cast_failed"
_CODE_DEPTH_ORDER = "depth_order_invalid"
_CODE_DEPTH_NEG = "depth_negative"
_CODE_INVALID_SAMPLE_TYPE = "invalid_sample_type"
_CODE_INVALID_QAQC = "invalid_qaqc_type"
_CODE_DECIMAL_COMMA = "decimal_comma_detected"


# ---------------------------------------------------------------------------
# Parse result dataclass
# ---------------------------------------------------------------------------

PARSER_VERSION = "2.0.0"

# ---------------------------------------------------------------------------
# Long-format detection patterns
# ---------------------------------------------------------------------------

# Column name patterns for long-format detection (element, value, unit)
_LONG_ELEMENT_COLS = {"element", "Element", "ELEMENT"}
_LONG_VALUE_COLS = {"value", "Value", "VALUE"}
_LONG_UNIT_COLS = {"unit", "Unit", "UNIT"}
_LONG_DL_COLS = {"detection_limit", "DetectionLimit", "DL", "dl"}

# Unit normalization map — raw → canonical
_UNIT_NORMALIZE: dict[str, str] = {
    "%": "pct",
    "percent": "pct",
    "pct": "pct",
    "g/t": "gpt",
    "gpt": "gpt",
    "grams_per_tonne": "gpt",
    "g_t": "gpt",
    "grams/tonne": "gpt",
    "ppb": "ppb",
    "ppm": "ppm",
}

# Long-format grouping columns — hole_id, from_depth, to_depth, sample_type are required;
# others are optional but included in the group key when present.
_LONG_REQUIRED_GROUPING = {"hole_id", "from_depth", "to_depth", "sample_type"}
_LONG_OPTIONAL_GROUPING = {"sample_id", "lab_id", "qaqc_type"}


@dataclass
class SampleParseResult:
    """Container for a completed sample parse run."""
    records: list
    total_rows: int
    valid_rows: int
    skipped_rows: int
    unmapped_columns: list
    column_map: dict
    assay_columns: list
    skipped_details: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    detected_encoding: str = "utf-8"
    provenance: dict[str, Any] = field(default_factory=dict)
    # CC-01 Item 1 Slice 2 — per-record outlier flags shaped for direct
    # insertion into silver.review_queue.outlier_flags. Aligned 1:1 with
    # ``records``; element i is ``{"unit_ambiguity": ["Au column", ...]}``
    # or an empty dict when the row is clean. Consumers MUST treat empty
    # dicts as "no flag" — do NOT enqueue clean rows for review.
    outlier_flags: list[dict[str, list[str]]] = field(default_factory=list)

    @property
    def parse_quality_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return round(self.valid_rows / self.total_rows * 100, 2)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_unit(raw_unit: str | None) -> tuple[str, bool]:
    """Normalize a raw unit string to canonical form.

    Returns (canonical_unit, was_changed) where was_changed=True means the
    unit was not already in its canonical form and a warning should be emitted.
    """
    if not raw_unit:
        return "ppm", True  # assume ppm as safe fallback
    stripped = raw_unit.strip().lower()
    canonical = _UNIT_NORMALIZE.get(stripped)
    if canonical is None:
        # Unknown unit — keep as-is
        return stripped, False
    changed = canonical != stripped
    return canonical, changed


def _detect_long_format(csv_columns: list) -> bool:
    """Return True if the CSV looks like a long-format assay file.

    Criteria: at least 2 of {element, value, unit} column name variants are
    present AND no ASSAY_COLUMN_RE columns exist.
    """
    col_set = set(csv_columns)

    has_element = bool(col_set & _LONG_ELEMENT_COLS)
    has_value = bool(col_set & _LONG_VALUE_COLS)
    has_unit = bool(col_set & _LONG_UNIT_COLS)

    long_indicators = sum([has_element, has_value, has_unit])
    if long_indicators < 2:
        return False

    # Check that no wide-format assay columns are present
    has_assay_cols = any(ASSAY_COLUMN_RE.match(c) for c in csv_columns)
    return not has_assay_cols


def _pivot_long_to_wide(
    df: pl.DataFrame,
    csv_columns: list,
    global_warnings: list,
) -> tuple[pl.DataFrame, list[str], list[dict], list[list[str]]] | None:
    """Pivot a long-format assay DataFrame to wide format.

    Returns ``(wide_df, assay_col_names, pivoted_flags)`` on success or ``None``
    on fatal error (caller emits skipped_details entry and returns empty result).

    ``pivoted_flags`` is a list aligned 1:1 with ``wide_df`` rows: element i is
    the ``{col_name: flag_dict}`` mapping that belongs to wide row i. Flags live
    outside the DataFrame so the second ``_build_column_map`` pass in
    ``parse_csv_samples`` cannot strip them (the Sprint 2 xfail bug).

    Grouping key = required columns + any optional grouping columns present.
    Pivot column = element  |  value column = value
    Output column name = {element}_{normalized_unit}

    Mutates *global_warnings* with unit_normalized and long_format_detected warnings.
    """
    # Identify column name variants in the actual DataFrame
    col_set = set(csv_columns)

    element_col = next((c for c in _LONG_ELEMENT_COLS if c in col_set), None)
    value_col = next((c for c in _LONG_VALUE_COLS if c in col_set), None)
    unit_col = next((c for c in _LONG_UNIT_COLS if c in col_set), None)
    dl_col = next((c for c in _LONG_DL_COLS if c in col_set), None)

    if element_col is None or value_col is None:
        return None  # caller handles error

    # Build canonical-to-csv-column map for grouping fields using COLUMN_ALIASES
    group_key_cols: list[str] = []
    for canonical_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in col_set:
                # Check if this is a required or optional grouping col
                if canonical_name in (_LONG_REQUIRED_GROUPING | _LONG_OPTIONAL_GROUPING):
                    group_key_cols.append(alias)
                    break

    # Verify required grouping columns are present
    for canonical_name in _LONG_REQUIRED_GROUPING:
        found = any(alias in col_set for alias in COLUMN_ALIASES.get(canonical_name, []))
        if not found:
            return None  # caller emits error with code long_format_missing_grouping_column

    if not group_key_cols:
        return None

    # Unit normalization: add a synthetic column with canonical unit names
    # and build element→unit mapping for column naming
    if unit_col:
        # Collect all (element, unit) pairs to build output column names
        pairs = (
            df.select([element_col, unit_col])
            .unique()
            .to_dicts()
        )
        # Map: element → canonical_unit (last wins if multiple units per element)
        element_unit_map: dict[str, str] = {}
        for pair in pairs:
            elem = str(pair.get(element_col, "") or "").strip()
            unit = str(pair.get(unit_col, "") or "").strip()
            canonical_unit, changed = _normalize_unit(unit)
            element_unit_map[elem] = canonical_unit
            if changed:
                global_warnings.append({
                    "row": None,
                    "code": "unit_normalized",
                    "message": f"unit '{unit}' normalized to '{canonical_unit}'",
                    "context": {"raw": unit, "normalized": canonical_unit},
                })
    else:
        # No unit column — default all to ppm
        elements = df[element_col].unique().to_list()
        element_unit_map = {str(e): "ppm" for e in elements if e is not None}

    # Build wide-format records via groupby + manual pivot.
    # Flags live in a parallel dict (same key) so they stay out of the DataFrame
    # columns — see the return-shape comment at the top of this function.
    group_records: dict[tuple, dict] = {}
    group_flags: dict[tuple, dict] = {}
    # CC-01 Item 1 Slice 2 — unit-ambiguity flags per group_key. Each long
    # row that triggers a flag contributes its strings; the wide row gets
    # the union (dedup preserved by merge_flags).
    group_unit_ambiguity: dict[tuple, list[str]] = {}

    rows = df.to_dicts()

    # Pre-compute per-raw-row unit ambiguity once so the grouping loop
    # below stays O(n).
    raw_row_unit_flags = detect_long_format_units(rows, element_col, unit_col)

    for row_idx, row in enumerate(rows):
        # Build group key tuple
        key_vals = tuple(row.get(c) for c in group_key_cols)

        element = str(row.get(element_col, "") or "").strip()
        raw_value = row.get(value_col)
        dl_raw = row.get(dl_col) if dl_col else None

        if not element:
            continue

        canon_unit = element_unit_map.get(element, "ppm")
        col_name = f"{element}_{canon_unit}"

        if key_vals not in group_records:
            # Copy grouping column values
            group_records[key_vals] = {c: row.get(c) for c in group_key_cols}
            group_flags[key_vals] = {}
            group_unit_ambiguity[key_vals] = []

        # Carry this raw row's ambiguity strings forward into the group.
        if row_idx < len(raw_row_unit_flags) and raw_row_unit_flags[row_idx]:
            group_unit_ambiguity[key_vals] = merge_flags(
                group_unit_ambiguity[key_vals],
                raw_row_unit_flags[row_idx],
            )

        # Parse the assay value (reuse existing helper)
        value, flags = _parse_assay_value(str(raw_value) if raw_value is not None else None)

        # Handle detection_limit from long format
        if dl_raw is not None and flags is None:
            try:
                dl_float = float(str(dl_raw).strip())
                if value is not None and value < dl_float:
                    flags = {
                        "dl_flag": True,
                        "dl_threshold": dl_float,
                        "original": str(raw_value),
                        "substitution": "half_dl",
                    }
                    value = dl_float / 2.0
            except (ValueError, TypeError):
                pass

        if value is not None:
            group_records[key_vals][col_name] = value
        if flags:
            # First-writer-wins, matching the previous setdefault() semantics.
            group_flags[key_vals].setdefault(col_name, flags)

    if not group_records:
        return pl.DataFrame(), [], [], []

    # Determine all assay column names (preserving insertion order)
    seen_cols: dict[str, None] = {}
    for rec in group_records.values():
        for k in rec:
            if k not in group_key_cols:
                seen_cols[k] = None
    assay_col_names = list(seen_cols.keys())

    # Emit long_format_detected warning
    n_elements = len(element_unit_map)
    n_samples = len(group_records)
    global_warnings.append({
        "row": None,
        "code": "long_format_detected",
        "message": "input pivoted long→wide for ingestion",
        "context": {"n_elements": n_elements, "n_samples_after_pivot": n_samples},
    })
    logger.info(
        "csv_sample: long format detected — %d elements, %d samples after pivot",
        n_elements,
        n_samples,
    )

    wide_df = pl.DataFrame(list(group_records.values()))
    # Align flags list with wide_df row order (dict preserves insertion order).
    pivoted_flags = list(group_flags.values())
    pivoted_unit_ambiguity = list(group_unit_ambiguity.values())
    return wide_df, assay_col_names, pivoted_flags, pivoted_unit_ambiguity


def _build_column_map(csv_columns: list) -> tuple:
    """Map canonical field names to the first matching CSV column alias found.

    Also identifies assay columns (those matching ASSAY_COLUMN_RE) and returns
    them separately for commodity_assays dict assembly.

    Returns:
        column_map   — {canonical_name: csv_column_name}
        assay_cols   — list of CSV column names that are commodity assay columns
        unmapped     — CSV columns that matched no canonical alias and no assay pattern
    """
    csv_col_set = set(csv_columns)
    column_map: dict = {}

    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in csv_col_set:
                column_map[canonical] = alias
                break

    matched_csv_cols = set(column_map.values())

    # Identify assay columns
    assay_cols = []
    for col in csv_columns:
        if col not in matched_csv_cols and ASSAY_COLUMN_RE.match(col):
            assay_cols.append(col)

    all_accounted = matched_csv_cols | set(assay_cols)
    unmapped = [c for c in csv_columns if c not in all_accounted]
    return column_map, assay_cols, unmapped


def _cast_float(value) -> float | None:
    """Return float or None; never raises."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _parse_assay_value(
    raw: str | None,
) -> tuple[float | None, dict | None]:
    """Parse a raw assay cell into (value, flags).

    Returns
    -------
    (value, flags) where:
      - value is the numeric float, or None if absent/BDL/unparseable
      - flags is a dict of metadata, or None for a clean numeric read

    Cases handled:
      None / ""            → (None, None)         — absent, skip
      "0.42"               → (0.42, None)          — normal
      "<0.01"              → (0.005, {...})         — half-detection-limit
      "BDL", "<LOD", ...   → (None, {...})          — BDL unknown threshold
      "NS", "NR", ...      → (None, {"unparseable": True, ...})
    """
    if raw is None:
        return None, None
    stripped = str(raw).strip()
    if stripped == "":
        return None, None

    # Plain numeric
    try:
        return float(stripped), None
    except ValueError:
        pass

    # Below-detection with numeric threshold: "<0.01", "< 0.001"
    m = _BELOW_DETECT_RE.match(stripped)
    if m:
        threshold = float(m.group(1))
        half_dl = threshold / 2.0
        return (
            half_dl,
            {
                "dl_flag": True,
                "dl_threshold": threshold,
                "original": stripped,
                "substitution": "half_dl",
            },
        )

    # BDL literals
    if stripped.lower() in _BDL_LITERALS:
        return (
            None,
            {
                "dl_flag": True,
                "dl_threshold": None,
                "original": stripped,
                "substitution": "null",
            },
        )

    # Unparseable
    return None, {"unparseable": True, "original": stripped}


def _detect_qaqc_type(
    sample_id: str | None,
    existing: str | None,
) -> str | None:
    """Detect QA/QC type from a sample_id prefix when no explicit column exists.

    Returns the detected QAQC type string, or ``None`` if no prefix matched
    (caller should default to ``"Primary"``).

    Does NOT overwrite an explicitly provided *existing* value.
    """
    if existing is not None:
        return existing
    if not sample_id:
        return None
    sid = str(sample_id).strip()
    for pattern, qaqc_type in _QAQC_PREFIX_MAP:
        if pattern.match(sid):
            return qaqc_type
    return None


def _validate_row(
    row_num: int,
    raw: dict,
    column_map: dict,
    assay_cols: list,
    qaqc_col_present: bool,
    row_warnings: list,
) -> tuple:
    """Validate a single raw row dict (keyed by canonical names + original assay col names).

    Returns (record, None) on success or (None, skip_entry) on failure.
    *row_warnings* is mutated in-place with per-row soft warnings.
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

    # --- Numeric casting for core fields ---
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

    # --- Depth ordering ---
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

    # --- sample_type validation ---
    sample_type = record.get("sample_type")
    if sample_type is not None and sample_type not in VALID_SAMPLE_TYPES:
        return None, {
            "row": row_num,
            "code": _CODE_INVALID_SAMPLE_TYPE,
            "reason": (
                f"row {row_num}: sample_type '{sample_type}' not in "
                f"allowed set {sorted(VALID_SAMPLE_TYPES)}"
            ),
            "raw": raw,
            "expected": f"one of {sorted(VALID_SAMPLE_TYPES)}",
            "actual": {"value": sample_type},
            "suggestion": (
                "Map via COLUMN_ALIASES or consult the lab."
            ),
        }

    # --- qaqc_type: validate explicit value or detect by prefix ---
    qaqc = record.get("qaqc_type")
    if qaqc is not None and qaqc not in VALID_QAQC_TYPES:
        return None, {
            "row": row_num,
            "code": _CODE_INVALID_QAQC,
            "reason": (
                f"row {row_num}: qaqc_type '{qaqc}' not in "
                f"allowed set {sorted(VALID_QAQC_TYPES)}"
            ),
            "raw": raw,
            "expected": f"one of {sorted(VALID_QAQC_TYPES)}",
            "actual": {"value": qaqc},
            "suggestion": (
                "Map via COLUMN_ALIASES or consult the lab."
            ),
        }

    if not qaqc_col_present or qaqc is None:
        # Attempt prefix-based detection from sample_id or hole_id
        probe_id = record.get("sample_id") or record.get("hole_id")
        detected = _detect_qaqc_type(probe_id, qaqc)
        if detected is None:
            detected = "Primary"
        if detected != qaqc:
            row_warnings.append({
                "row": row_num,
                "code": _CODE_QAQC_DETECTED,
                "message": (
                    f"qaqc_type inferred as '{detected}' from sample_id/hole_id prefix"
                ),
                "context": {"probe_id": probe_id, "inferred": detected},
            })
        record["qaqc_type"] = detected

    # --- Commodity assays ---
    commodity_assays: dict = {}
    commodity_assay_flags: dict = {}

    for col in assay_cols:
        raw_val = raw.get(col)
        if raw_val is None:
            continue  # absent assay value is fine — skip entirely

        value, flags = _parse_assay_value(raw_val)

        if flags is not None:
            if flags.get("dl_flag"):
                row_warnings.append({
                    "row": row_num,
                    "code": _CODE_ASSAY_BDL,
                    "message": (
                        f"assay '{col}' below detection: '{flags['original']}'; "
                        f"substitution='{flags['substitution']}'"
                    ),
                    "context": {"column": col, **flags},
                })
            elif flags.get("unparseable"):
                row_warnings.append({
                    "row": row_num,
                    "code": _CODE_ASSAY_UNPARSEABLE,
                    "message": (
                        f"assay '{col}' value '{flags['original']}' is not numeric — "
                        f"omitting from commodity_assays"
                    ),
                    "context": {"column": col, **flags},
                })
            commodity_assay_flags[col] = flags

        if value is not None:
            commodity_assays[col] = value
        # If value is None (BDL unknown / unparseable), key is omitted — row is NOT rejected

    record["commodity_assays"] = commodity_assays
    record["commodity_assay_flags"] = commodity_assay_flags if commodity_assay_flags else None

    # --- hole_id canonicalization ---
    record["hole_id_canonical"] = canonicalize(record.get("hole_id"))

    # --- source row tracking ---
    record["_source_row"] = row_num

    return record, None


# ---------------------------------------------------------------------------
# Public parser entry point
# ---------------------------------------------------------------------------

def parse_csv_samples(
    source: Union[str, Path, IO],  # noqa: UP007
    *,
    null_values: list = None,
) -> SampleParseResult:
    """Parse a CSV geochemical sample file and return a :class:`SampleParseResult`.

    Parameters
    ----------
    source:
        Absolute file path (str or Path) or a file-like text stream.
    null_values:
        Additional strings to treat as null (on top of the Polars defaults).

    Returns
    -------
    SampleParseResult
        Contains validated records plus quality metrics.
    """
    global_warnings: list = []
    detected_encoding = "utf-8"

    if isinstance(source, (str, Path)):  # noqa: SIM108
        source_file_str = str(source)
    else:
        source_file_str = "<stream>"

    all_nulls = list(set(SAMPLE_NULL_VALUES + (null_values or [])))

    try:
        stream, detected_encoding, sha256_hex, _byte_count = open_csv_with_encoding(source)
        raw_content = stream.getvalue()

        if detected_encoding.lower().replace("-", "") not in ("utf8", "utf-8", "ascii"):
            global_warnings.append({
                "row": None,
                "code": _CODE_ENCODING_NON_UTF8,
                "message": f"detected encoding '{detected_encoding}' (not UTF-8) — decoded with replacement",
                "context": {"encoding": detected_encoding},
            })
            logger.info("csv_sample: detected encoding '%s'", detected_encoding)

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
            logger.info("csv_sample: detected delimiter %r", detected_delim)

        df = pl.read_csv(
            StringIO(raw_content),
            separator=detected_delim,
            infer_schema=False,
            null_values=all_nulls,
            truncate_ragged_lines=True,
        )

        # 2026-05-23 — column-aware decimal-comma transform (CSV audit gap #2).
        # Note: for sample CSVs, columns with mixed BDL tokens ("<0.01",
        # "BDL", etc.) will fail the all-match gate and NOT be transformed.
        # That's intentional v1 behaviour: depth columns (from/to) get
        # transformed cleanly; assay columns with BDL keep their raw
        # strings for downstream _parse_assay_value to handle.
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
                "csv_sample: decimal-comma transformed %d column(s): %s",
                len(transformed_cols), transformed_cols,
            )
    except Exception as exc:
        logger.error("Failed to read CSV source: %s", exc)
        raise

    csv_columns: list = df.columns
    total_rows: int = len(df)

    logger.info("CSV loaded: %d rows, %d columns: %s", total_rows, len(csv_columns), csv_columns)

    column_map, assay_cols, unmapped = _build_column_map(csv_columns)

    # ---------------------------------------------------------------------------
    # Long-format detection and pivot — must run before unmapped column logging
    # so we don't spuriously log element/value/unit as unmapped in long format.
    # ---------------------------------------------------------------------------
    is_long_format = _detect_long_format(csv_columns)
    pivoted_flags: list[dict] = []  # empty for wide-format; populated for long-format
    pivoted_unit_ambiguity: list[list[str]] = []  # CC-01 Item 1 Slice 2

    if is_long_format:
        logger.info("csv_sample: long format detected — pivoting to wide")
        pivot_result = _pivot_long_to_wide(df, csv_columns, global_warnings)

        if pivot_result is None:
            # Fatal: missing required grouping column
            logger.error(
                "csv_sample: long format detected but required grouping columns missing"
            )
            return SampleParseResult(
                records=[],
                total_rows=total_rows,
                valid_rows=0,
                skipped_rows=total_rows,
                unmapped_columns=unmapped,
                column_map=column_map,
                assay_columns=[],
                skipped_details=[{
                    "row": None,
                    "code": "long_format_missing_grouping_column",
                    "reason": (
                        "file-level: long format detected but one or more required grouping "
                        "columns (hole_id, from_depth, to_depth, sample_type) are missing"
                    ),
                    "raw": {},
                    "expected": "columns for hole_id, from_depth, to_depth, sample_type",
                    "actual": None,
                    "suggestion": (
                        "Ensure the long-format file has all required grouping columns, "
                        "or add aliases to COLUMN_ALIASES."
                    ),
                }],
                warnings=global_warnings,
                detected_encoding=detected_encoding,
            )

        wide_df, pivoted_assay_cols, pivoted_flags, pivoted_unit_ambiguity = pivot_result
        if wide_df is None or len(wide_df) == 0:
            logger.warning("csv_sample: long-format pivot produced 0 rows")
            return SampleParseResult(
                records=[],
                total_rows=total_rows,
                valid_rows=0,
                skipped_rows=total_rows,
                unmapped_columns=unmapped,
                column_map=column_map,
                assay_columns=pivoted_assay_cols if pivot_result else [],
                skipped_details=[],
                warnings=global_warnings,
                detected_encoding=detected_encoding,
            )

        # Replace df and derived variables with the pivoted wide version
        df = wide_df
        csv_columns = df.columns
        column_map, pivoted_assay_from_map, unmapped = _build_column_map(csv_columns)
        assay_cols = pivoted_assay_cols
        total_rows = len(df)

    # Log assay and unmapped columns after long-format pivot (if any) is complete
    if assay_cols:
        logger.info(
            "CSV sample parser: detected %d assay column(s): %s",
            len(assay_cols),
            assay_cols,
        )

    if unmapped:
        logger.warning(
            "CSV sample parser: %d unmapped column(s) will be ignored: %s",
            len(unmapped),
            unmapped,
        )

    # ---------------------------------------------------------------------------
    # Required columns check (applies to both wide and post-pivot long)
    # ---------------------------------------------------------------------------
    mapped_canonical = set(column_map.keys())
    missing_required = REQUIRED_FIELDS - mapped_canonical
    if missing_required:
        logger.error(
            "CSV is missing required columns (no alias matched): %s. Mapped columns: %s",
            missing_required,
            column_map,
        )
        return SampleParseResult(
            records=[],
            total_rows=total_rows,
            valid_rows=0,
            skipped_rows=total_rows,
            unmapped_columns=unmapped,
            column_map=column_map,
            assay_columns=assay_cols,
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

    qaqc_col_present = "qaqc_type" in column_map

    # Rename canonical columns; keep assay columns under their original names
    rename_map = {v: k for k, v in column_map.items()}
    df_renamed = df.rename(rename_map)
    # Select canonical mapped cols + all assay cols
    keep_cols = [c for c in df_renamed.columns if c in column_map] + [
        c for c in assay_cols if c in df_renamed.columns
    ]
    df_trimmed = df_renamed.select(keep_cols)

    records: list = []
    skipped: list = []
    # CC-01 Item 1 Slice 2 — track the source pivot_idx for each kept
    # record so long-format unit-ambiguity flags can be re-aligned after
    # validation drops invalid rows.
    pivot_indices_kept: list[int] = []

    rows_as_dicts = df_trimmed.to_dicts()
    for pivot_idx, raw in enumerate(rows_as_dicts):
        i = pivot_idx + 2  # 1-based CSV line (header is line 1)
        row_warnings: list = []
        record, skip_entry = _validate_row(
            i, raw, column_map, assay_cols, qaqc_col_present, row_warnings
        )
        global_warnings.extend(row_warnings)
        if record is not None:
            # Merge long-format DL flags (captured in _pivot_long_to_wide and
            # routed outside the DataFrame — see the flags-return-shape doc
            # on that function). No-op for the wide-format path.
            if pivot_idx < len(pivoted_flags):
                extra_flags = pivoted_flags[pivot_idx]
                if extra_flags:
                    existing = record.get("commodity_assay_flags") or {}
                    merged = {**existing, **extra_flags}
                    record["commodity_assay_flags"] = merged if merged else None
            records.append(record)
            pivot_indices_kept.append(pivot_idx)
        else:
            logger.warning("Skipping sample row: %s", skip_entry.get("reason"))
            skipped.append(skip_entry)

    valid_rows = len(records)
    skipped_rows = len(skipped)

    # CC-01 Item 1 Slice 2 — compute per-record unit ambiguity. Wide-format
    # detector inspects each record's commodity_assays + assay column names;
    # long-format additionally contributes flags collected during pivot.
    wide_unit_flags = detect_wide_format(assay_cols, records)
    outlier_flags: list[dict[str, list[str]]] = []
    for idx in range(len(records)):
        merged_strs = list(wide_unit_flags[idx]) if idx < len(wide_unit_flags) else []
        if pivoted_unit_ambiguity and idx < len(pivot_indices_kept):
            pivot_idx = pivot_indices_kept[idx]
            if pivot_idx < len(pivoted_unit_ambiguity):
                merged_strs = merge_flags(merged_strs, pivoted_unit_ambiguity[pivot_idx])
        outlier_flags.append({"unit_ambiguity": merged_strs} if merged_strs else {})

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
            "csv_sample: hole_id collision — '%s' and '%s' both → '%s'",
            collision["a"],
            collision["b"],
            collision["canonical"],
        )

    # --- Provenance ---
    provenance: dict = {
        "source_file": source_file_str,
        "source_file_sha256": sha256_hex,
        "parser_name": "csv_sample",
        "parser_version": PARSER_VERSION,
        "source_col_map": column_map,
    }

    result = SampleParseResult(
        records=records,
        total_rows=total_rows,
        valid_rows=valid_rows,
        skipped_rows=skipped_rows,
        unmapped_columns=unmapped,
        column_map=column_map,
        assay_columns=assay_cols,
        skipped_details=skipped,
        warnings=global_warnings,
        detected_encoding=detected_encoding,
        provenance=provenance,
        outlier_flags=outlier_flags,
    )

    logger.info(
        "CSV sample parse complete — total: %d, valid: %d, skipped: %d, quality: %.1f%%, "
        "assay cols: %d, unmapped cols: %d, warnings: %d",
        total_rows,
        valid_rows,
        skipped_rows,
        result.parse_quality_pct,
        len(assay_cols),
        len(unmapped),
        len(global_warnings),
    )

    return result
