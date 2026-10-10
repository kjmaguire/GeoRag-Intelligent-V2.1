"""CSV Sample Parser — Bronze → Silver ingestion for geochemical sample assay data.

Accepts a CSV file path or file-like object, auto-detects column name variations
across common LIMS/lab exports, validates each row, and returns a list of validated
sample dicts ready for Silver schema insertion.

Commodity assay columns are recognised by ``_assay_columns.parse_assay_header``:
any assayable element, oxide or element name, with a unit written as ppm, ppb,
g/t (gpt), %, pct or oz/t in any of the usual spellings (``Au (g/t)``,
``Ag_gpt``, ``Cu_%``, ``Pb ppm``). Values are stored under a canonical key
(``Au_ppm``, ``Cu_pct``) in the stored unit - g/t is ppm, oz/t is converted
(x34.2857). A bare element (``Mo``) is ASSUMED ppm and reported as such. The
old literal list (U3O8/Au/Ag/Cu/Pb/Zn/Ni/Fe/Ti/Li, ppm/pct/ppb only) dropped
every other element and g/t gold on the floor (audit 2026-09-29, ING-3).

Below-detection values ("<0.01", "BDL", a negated detection limit such as
"-0.005") are captured in commodity_assay_flags rather than causing row
rejection; over-limit values (">10") keep the limit as the value and carry an
over-detection flag; -9999 / -999 / -99999 are "not measured", not grades.

``sample_type`` is optional: most assay exports have no such column. When it is
absent, or holds a value outside Core/Chip/Grab/Channel/Soil and their common
synonyms (DD, HQ, RC, rock chip, ...), the row is kept, the type is left blank
and the blanking is reported (Kyle, 2026-09-29: keep the row, blank the field,
say so).

Parse quality metrics are emitted as structured log output so the caller can
record them in Dagster materialisation metadata.
"""

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Union

import polars as pl

from georag_geoparsers._assay_columns import AssaySpec, parse_assay_header
from georag_geoparsers._csv_io import (
    SAMPLE_NULL_VALUES,
    RaggedRows,
    detect_delimiter,
    open_csv_with_encoding,
    read_csv_checked,
    transform_decimal_comma,
)
from georag_geoparsers._depth_units import convert_feet_columns
from georag_geoparsers._drill_schema import SAMPLE_ALIASES, SAMPLE_REQUIRED
from georag_geoparsers._encoding import decode_warnings, is_utf8_compatible
from georag_geoparsers._header_match import build_column_map
from georag_geoparsers._hole_id import canonicalize, suggest_collisions
from georag_geoparsers._optional_enum import BlankedValues, canonical_choice
from georag_geoparsers._unit_ambiguity import (
    detect_long_format_units,
    detect_wide_format,
    merge_flags,
)
from georag_geoparsers._vendor_aliases import merge_vendor_aliases

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column name alias maps — defined in _drill_schema so the sheet classifier
# and the FastAPI writers read the same vocabulary; re-exported here because
# tests and _sheet_classifier import them from this module.
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict = SAMPLE_ALIASES

# Required fields — rows missing any of these are rejected.
#
# NOT sample_type, although _drill_schema.SAMPLE_REQUIRED (which the sheet
# classifier scores on) still lists it: most assay exports carry no
# sample-type column, and requiring one sent them to the text fallback with
# no samples and no assays (ING-4). The classifier keeps its 3-of-4 reading,
# so what classifies as a sample sheet is unchanged; the parser just stops
# refusing it.
REQUIRED_FIELDS: frozenset = frozenset(SAMPLE_REQUIRED - {"sample_type"})

# Numeric fields
NUMERIC_FIELDS: frozenset = frozenset({"from_depth", "to_depth"})

# LEGACY. The ten-element vocabulary assay detection used before 2026-09-29;
# kept only because it is a public name. Detection is parse_assay_header.
ASSAY_COLUMN_RE = re.compile(
    r"^(U3O8|Au|Ag|Cu|Pb|Zn|Ni|Fe|Ti|Li)_?(ppm|pct|ppb|pct_|_pct)?$",
    re.IGNORECASE,
)

# Below-detection literal tokens (case-insensitive) — treated as BDL with unknown threshold
_BDL_LITERALS: frozenset = frozenset({"lod", "bdl", "<lod", "<dl"})

# Below-detection prefix pattern: "<0.01", "< 0.001", etc.
_BELOW_DETECT_RE = re.compile(r"^<\s*(\d+(?:\.\d+)?)$")

# Over-limit prefix pattern: ">10", "> 10000" (ING-9). The value above the
# upper detection limit is not known; the limit is.
_OVER_DETECT_RE = re.compile(r"^>\s*(\d+(?:\.\d+)?)$")

#: Numeric codes that mean "not measured", not a grade (ING-10). Stored as
#: absent. Other negative values are negated detection limits (below).
MISSING_SENTINELS: frozenset[float] = frozenset({-9999.0, -999.0, -99999.0})

# Categorical validation sets
VALID_SAMPLE_TYPES: frozenset = frozenset({"Core", "Chip", "Grab", "Channel", "Soil"})

#: Spellings that ARE one of the allowed types (ING-4). Matched after
#: case-folding and collapsing whitespace/punctuation. Nothing is added to
#: the enum: an RC chip sample is a Chip, a half-core HQ sample is a Core.
#:
#: §04e, SME-approved (Kyle, 2026-09-29, "use best recommendation"):
#: a TRENCH sample is a continuous cut across the exposure — a Channel — and
#: RAB (rotary air blast) and AIRCORE (AC) return drill cuttings — Chips,
#: like RC. Anything still not here (Pulp, Reject, ...) is left blank and
#: reported rather than guessed at.
SAMPLE_TYPE_SYNONYMS: dict[str, str] = {
    **{k: "Core" for k in (
        "core", "dd", "ddh", "diamond", "diamond drill", "diamond core",
        "half core", "quarter core", "whole core", "full core", "split core",
        "hq", "nq", "pq", "bq", "hq3", "nq2", "nq3", "pq3",
    )},
    **{k: "Chip" for k in (
        "chip", "chips", "rc", "rc chip", "rc chips", "reverse circulation",
        "reverse circ", "percussion", "rock chip", "rock chips",
        # §04e 2026-09-29: RAB and aircore cuttings.
        "rab", "rab chip", "rab chips", "rotary air blast", "rotary airblast",
        "aircore", "air core", "ac", "ac chip", "ac chips", "aircore chip",
        "aircore chips",
    )},
    **{k: "Grab" for k in ("grab", "grab sample", "grabs")},
    **{k: "Channel" for k in (
        "channel", "channel sample", "channels",
        # §04e 2026-09-29: trench samples.
        "trench", "trenches", "trench sample", "trench channel",
        "trench channel sample", "channel trench",
    )},
    **{k: "Soil" for k in ("soil", "soils", "soil sample")},
}
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
_CODE_SAMPLE_TYPE_MISSING = "sample_type_column_missing"
_CODE_ASSAY_OVER_LIMIT = "assay_over_detection"
_CODE_ASSAY_SENTINEL = "assay_missing_sentinel"
_CODE_ASSAY_UNIT_ASSUMED = "assay_unit_assumed"
_CODE_ASSAY_UNIT_CONVERTED = "assay_unit_converted"
_CODE_ASSAY_COLUMNS_MERGED = "assay_columns_merged"
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
_LONG_REQUIRED_GROUPING = {"hole_id", "from_depth", "to_depth"}
_LONG_OPTIONAL_GROUPING = {"sample_type", "sample_id", "lab_id", "qaqc_type"}


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
    has_assay_cols = any(parse_assay_header(c) for c in csv_columns)
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

    # Elements whose unit the file never stated and that were therefore read as
    # ppm. Reported once below as ``assay_unit_assumed`` (the wide-format
    # counterpart is _assay_column_warnings): Cu in % read as ppm is a 10,000x
    # error, so a default here is never silent.
    assumed_unit_elements: list[str] = []

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
            if not unit and elem and elem not in assumed_unit_elements:
                # A blank unit CELL is the same assumption as a missing
                # unit column: _normalize_unit falls back to ppm.
                assumed_unit_elements.append(elem)
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
        assumed_unit_elements = sorted(element_unit_map)

    if assumed_unit_elements:
        shown = ", ".join(repr(e) for e in assumed_unit_elements[:12])
        if len(assumed_unit_elements) > 12:
            shown += f" and {len(assumed_unit_elements) - 12} more"
        global_warnings.append({
            "row": None,
            "code": _CODE_ASSAY_UNIT_ASSUMED,
            "message": (
                f"{len(assumed_unit_elements)} assayed element(s) name no unit "
                f"and were read as ppm"
            ),
            "detail": (
                f"This long-format file has no usable unit for these elements, "
                f"so their values were stored as ppm: {shown}. That is an "
                f"assumption, not something the file says - if an element is "
                f"in %, g/t or ppb, add a unit column (or use a header such as "
                f"'Cu_pct', 'Au_ppb') and re-upload."
            )[:900],
            "context": {"elements": assumed_unit_elements[:50]},
        })

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

    # Built column by column from the full column list, NOT from the list of
    # row dicts: pl.DataFrame(list_of_dicts) infers its columns from the first
    # 100 dicts, so an element first seen in the 101st sample group
    # ("Au" for 100 samples, then the first "Mo") had its whole column dropped
    # with no warning (audit finding 8).
    wide_columns = [*group_key_cols, *assay_col_names]
    wide_df = pl.DataFrame({
        column: [record.get(column) for record in group_records.values()]
        for column in wide_columns
    })
    lost = [column for column in assay_col_names if column not in wide_df.columns]
    if lost:  # unreachable by construction; a silent drop is the failure to prevent
        raise RuntimeError(f"long-format pivot lost assay column(s): {lost}")
    # Align flags list with wide_df row order (dict preserves insertion order).
    pivoted_flags = list(group_flags.values())
    pivoted_unit_ambiguity = list(group_unit_ambiguity.values())
    return wide_df, assay_col_names, pivoted_flags, pivoted_unit_ambiguity


def _build_column_map(csv_columns: list, *, aliases: dict | None = None) -> tuple:
    """Map canonical field names to the first matching CSV column alias found.

    Also identifies assay columns (those matching ASSAY_COLUMN_RE) and returns
    them separately for commodity_assays dict assembly.

    Case, separators and unit suffixes are folded by ``_header_match``, so
    ``Sample ID`` and ``sample_id`` reach the same field. Assay detection
    still runs against the column's ORIGINAL spelling: ASSAY_COLUMN_RE reads
    element symbols and unit suffixes (``Au_ppm``, ``Ag_gpt``), and the two
    things normalisation discards are exactly the two things it needs.

    Returns:
        column_map   — {canonical_name: csv_column_name}
        assay_cols   — list of CSV column names that are commodity assay columns
        unmapped     — CSV columns that matched no canonical alias and no assay pattern
    """
    column_map, unmatched = build_column_map(
        csv_columns, aliases if aliases is not None else COLUMN_ALIASES,
    )
    matched_csv_cols = set(column_map.values())

    # Identify assay columns
    assay_cols = [c for c in unmatched if parse_assay_header(c) is not None]

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
      "-0.01"              → (0.005, {...})         — a NEGATED detection limit
                                                     is "<0.01" (ING-10)
      "-9999", "-999"      → (None, {"missing_sentinel": True, ...})
      ">10"                → (10.0, {"od_flag": True, "od_threshold": 10.0})
                                                     — above the upper limit
                                                     (ING-9)
      "BDL", "<LOD", ...   → (None, {...})          — BDL unknown threshold
      "NS", "NR", ...      → (None, {"unparseable": True, ...})

    A concentration cannot be negative, so a negative cell is never stored as
    a grade: the sentinels are "not measured", and any other negative is the
    negated detection limit, the same convention ingest_tabular's surface
    geochemistry path documents (``_is_below_detection``).
    """
    if raw is None:
        return None, None
    stripped = str(raw).strip()
    if stripped == "":
        return None, None

    # Plain numeric
    try:
        number = float(stripped)
    except ValueError:
        number = None
    if number is not None and math.isfinite(number):
        if number in MISSING_SENTINELS:
            return None, {"missing_sentinel": True, "original": stripped}
        if number >= 0:
            return number, None
        limit = abs(number)
        return (
            limit / 2.0,
            {
                "dl_flag": True,
                "dl_threshold": limit,
                "original": stripped,
                "substitution": "half_dl",
                "negated_limit": True,
            },
        )
    if number is not None:
        # NaN / infinity: not a measurement.
        return None, {"unparseable": True, "original": stripped}

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

    # Above the upper detection limit: ">10". The limit is the only number
    # the lab reported, so it is the value, flagged - dropping the cell
    # removed exactly the highest-grade intervals (ING-9).
    m = _OVER_DETECT_RE.match(stripped)
    if m:
        threshold = float(m.group(1))
        return (
            threshold,
            {
                "od_flag": True,
                "od_threshold": threshold,
                "original": stripped,
                "substitution": "limit",
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


def canonical_sample_type(value: str | None) -> str | None:
    """The allowed sample type *value* denotes, or None (ING-4).

    Case, whitespace and punctuation are folded ("CORE", "Half-Core",
    "R.C."), then the value is looked up among the allowed types and their
    synonyms. None means "not a type we can name" - the caller blanks it.
    """
    if value is None:
        return None
    folded = " ".join(re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split())
    if not folded:
        return None
    if folded.replace(" ", "") == "rc":
        return "Chip"
    for allowed in VALID_SAMPLE_TYPES:
        if allowed.casefold() == folded:
            return allowed
    # Dotted abbreviations fold to spaced letters ("A.C." -> "a c",
    # "R.A.B." -> "r a b"); the compact form is the abbreviation.
    return SAMPLE_TYPE_SYNONYMS.get(folded) or SAMPLE_TYPE_SYNONYMS.get(folded.replace(" ", ""))


def _assay_specs(assay_cols: list) -> dict[str, AssaySpec]:
    """``{column: AssaySpec}`` for the recognised assay columns."""
    specs: dict[str, AssaySpec] = {}
    for col in assay_cols:
        spec = parse_assay_header(col)
        if spec is not None:
            specs[col] = spec
    return specs


def _assay_column_warnings(specs: dict[str, AssaySpec]) -> list[dict]:
    """File-level notes: units assumed, units converted, columns merged."""
    out: list[dict] = []
    assumed = [c for c, s in specs.items() if s.unit_assumed]
    if assumed:
        shown = ", ".join(repr(c) for c in assumed[:12])
        out.append({
            "row": None,
            "code": _CODE_ASSAY_UNIT_ASSUMED,
            "message": (
                f"{len(assumed)} assay column(s) name no unit and were read as ppm"
            ),
            "detail": (
                f"These assay columns name an element but no unit, so their "
                f"values were stored as ppm: {shown}. That is an assumption, "
                f"not something the file says - if a column is in %, g/t or "
                f"ppb, rename it (e.g. 'Cu_pct', 'Au_ppb') and re-upload."
            )[:900],
            "context": {"columns": assumed[:50]},
        })
    converted = [(c, s) for c, s in specs.items() if s.converted]
    if converted:
        shown = ", ".join(
            f"{c!r} ({s.source_unit} -> {s.unit}"
            + (f" x{s.factor:g}" if s.factor != 1.0 else "") + ")"
            for c, s in converted[:12]
        )
        out.append({
            "row": None,
            "code": _CODE_ASSAY_UNIT_CONVERTED,
            "message": f"{len(converted)} assay column(s) stored in a standard unit",
            "detail": (
                f"Assays are stored as ppm, ppb or %; these columns were "
                f"stored in the equivalent unit: {shown}."
            )[:900],
            "context": {
                "columns": {c: [s.source_unit, s.unit, s.factor] for c, s in converted[:50]},
            },
        })
    by_key: dict[str, list[str]] = {}
    for col, spec in specs.items():
        by_key.setdefault(spec.key, []).append(col)
    merged = {k: cols for k, cols in by_key.items() if len(cols) > 1}
    if merged:
        shown = "; ".join(f"{k}: {', '.join(repr(c) for c in cols)}" for k, cols in merged.items())
        out.append({
            "row": None,
            "code": _CODE_ASSAY_COLUMNS_MERGED,
            "message": (
                f"{len(merged)} element(s) are reported in more than one column"
            ),
            "detail": (
                f"More than one column holds the same element in the same unit "
                f"({shown}). For each sample the first column with a measured "
                f"value is kept; a later column only fills a cell the earlier "
                f"one left empty or reported as below/above detection."
            )[:900],
            "context": {"merged": merged},
        })
    return out


def _scaled_flags(flags: dict, factor: float) -> dict:
    """*flags* with their thresholds converted to the stored unit."""
    if factor == 1.0:
        return flags
    out = dict(flags)
    for key in ("dl_threshold", "od_threshold"):
        if isinstance(out.get(key), (int, float)):
            out[key] = out[key] * factor
    out["source_factor"] = factor
    return out


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
    blanked: BlankedValues | None = None,
    assay_specs: dict[str, AssaySpec] | None = None,
) -> tuple:
    """Validate a single raw row dict (keyed by canonical names + original assay col names).

    Returns (record, None) on success or (None, skip_entry) on failure.
    *row_warnings* is mutated in-place with per-row soft warnings.

    An unrecognised OPTIONAL ``qaqc_type`` is set to None and recorded in
    *blanked*; it does not reject the row and it is NOT then re-inferred as
    "Primary" - a value the lab wrote that we cannot read must not quietly
    become a plain primary sample (a "CRM" treated as primary would skew
    every assay statistic). ``sample_type`` is treated the same way since
    2026-09-29 (ING-4): canonicalised through its synonyms, and blanked -
    never rejecting the row and its assays - when it names nothing we know.
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

    # --- sample_type: canonicalise, or blank and say so (ING-4) ---
    sample_type = record.get("sample_type")
    if sample_type is not None:
        if not str(sample_type).strip():
            record["sample_type"] = None
        else:
            canonical_type = canonical_sample_type(sample_type)
            if canonical_type is None and blanked is not None:
                blanked.add("sample_type", sample_type)
            record["sample_type"] = canonical_type

    # --- qaqc_type: validate explicit value or detect by prefix ---
    qaqc = record.get("qaqc_type")
    qaqc_blanked = False
    if qaqc is not None:
        if not qaqc.strip():
            qaqc = None
            record["qaqc_type"] = None
        else:
            canonical_qaqc = canonical_choice(qaqc, VALID_QAQC_TYPES)
            if canonical_qaqc is None:
                if blanked is not None:
                    blanked.add("qaqc_type", qaqc)
                qaqc = None
                qaqc_blanked = True
                record["qaqc_type"] = None
            else:
                qaqc = canonical_qaqc
                record["qaqc_type"] = canonical_qaqc

    if not qaqc_blanked and (not qaqc_col_present or qaqc is None):
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
    specs = assay_specs if assay_specs is not None else _assay_specs(assay_cols)
    #: key -> how good the kept reading is: 2 a clean measurement, 1 a
    #: flagged value (below/above detection), 0 no value. A later column for
    #: the same element and unit replaces it only with a strictly better one.
    rank_by_key: dict[str, int] = {}

    for col in assay_cols:
        raw_val = raw.get(col)
        if raw_val is None:
            continue  # absent assay value is fine — skip entirely
        spec = specs.get(col)
        if spec is None:
            continue

        value, flags = _parse_assay_value(raw_val)
        if value is not None:
            value = value * spec.factor
        if flags is not None:
            flags = _scaled_flags(flags, spec.factor)
        key = spec.key
        rank = 2 if value is not None and flags is None else int(value is not None)
        if key in rank_by_key and rank <= rank_by_key[key]:
            continue      # an earlier column already holds as good a reading
        commodity_assays.pop(key, None)
        commodity_assay_flags.pop(key, None)
        rank_by_key[key] = rank

        if flags is not None:
            if flags.get("od_flag"):
                row_warnings.append({
                    "row": row_num,
                    "code": _CODE_ASSAY_OVER_LIMIT,
                    "message": (
                        f"assay '{col}' above the upper detection limit: "
                        f"'{flags['original']}'; stored as the limit and flagged"
                    ),
                    "context": {"column": col, "key": key, **flags},
                })
            elif flags.get("missing_sentinel"):
                row_warnings.append({
                    "row": row_num,
                    "code": _CODE_ASSAY_SENTINEL,
                    "message": (
                        f"assay '{col}' value '{flags['original']}' is a "
                        f"'not measured' code — stored as absent, not as a grade"
                    ),
                    "context": {"column": col, "key": key, **flags},
                })
            elif flags.get("dl_flag"):
                row_warnings.append({
                    "row": row_num,
                    "code": _CODE_ASSAY_BDL,
                    "message": (
                        f"assay '{col}' below detection: '{flags['original']}'; "
                        f"substitution='{flags['substitution']}'"
                    ),
                    "context": {"column": col, "key": key, **flags},
                })
            elif flags.get("unparseable"):
                row_warnings.append({
                    "row": row_num,
                    "code": _CODE_ASSAY_UNPARSEABLE,
                    "message": (
                        f"assay '{col}' value '{flags['original']}' is not numeric — "
                        f"omitting from commodity_assays"
                    ),
                    "context": {"column": col, "key": key, **flags},
                })
            commodity_assay_flags[key] = flags

        if value is not None:
            commodity_assays[key] = value
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
    vendor_aliases: dict[str, list[str]] | None = None,
) -> SampleParseResult:
    """Parse a CSV geochemical sample file and return a :class:`SampleParseResult`.

    Parameters
    ----------
    source:
        Absolute file path (str or Path) or a file-like text stream.
    null_values:
        Additional strings to treat as null (on top of the Polars defaults).
    vendor_aliases:
        Extra column spellings keyed by canonical field name, merged ahead
        of COLUMN_ALIASES so they win on a tie. Two callers use this: a
        stored vendor profile (CC-02 Item 6), and a column mapping the USER
        confirmed for one file, which arrives as a single-entry list per
        field. The mapping is still matched through ``_header_match``, so a
        spelling that differs only in case or separators still lands.

        A mapped column stops being an assay column: assay detection runs
        over what alias matching did NOT claim, so naming ``Au_ppm`` as the
        sample_id would remove it from ``commodity_assays``. That is the
        correct reading of an explicit instruction, but it is worth knowing
        before mapping a column that looks like a grade.

    Returns
    -------
    SampleParseResult
        Contains validated records plus quality metrics.
    """
    global_warnings: list = []
    detected_encoding = "utf-8"

    if isinstance(source, (str, Path)):
        source_file_str = str(source)
    else:
        source_file_str = "<stream>"

    all_nulls = list(set(SAMPLE_NULL_VALUES + (null_values or [])))

    try:
        stream, detected_encoding, sha256_hex, _byte_count = open_csv_with_encoding(source)
        raw_content = stream.getvalue()

        global_warnings.extend(decode_warnings(detected_encoding, raw_content))
        if not is_utf8_compatible(detected_encoding):
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

        df, ragged = read_csv_checked(
            raw_content, separator=detected_delim, null_values=all_nulls,
        )
        global_warnings.extend(ragged.warnings())

        # 2026-05-23 — column-aware decimal-comma transform (CSV audit gap #2).
        # An assay column in decimal commas that also holds censored cells
        # ("<0,005", "> 10,5", "BDL", "NS") IS transformed, cell by cell
        # (audit finding 10): this used to disqualify the column, leaving every
        # "0,52" as text that _parse_assay_value cannot read. A column that
        # mixes a point and a comma (a "<0.01" beside a "0,52") still fails
        # the gate and keeps its raw strings, and one whose every comma group
        # is three digits ("1,250") is left alone and warned about
        # (decimal_comma_ambiguous).
        df, transformed_cols = transform_decimal_comma(df)
        global_warnings.extend(transformed_cols.ambiguity_warnings())
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

    effective_aliases = merge_vendor_aliases(COLUMN_ALIASES, vendor_aliases)
    column_map, assay_cols, unmapped = _build_column_map(
        csv_columns, aliases=effective_aliases,
    )

    # ---------------------------------------------------------------------------
    # Long-format detection and pivot — must run before unmapped column logging
    # so we don't spuriously log element/value/unit as unmapped in long format.
    # ---------------------------------------------------------------------------
    is_long_format = _detect_long_format(csv_columns)
    pivoted_flags: list[dict] = []  # empty for wide-format; populated for long-format
    pivoted_unit_ambiguity: list[list[str]] = []  # CC-01 Item 1 Slice 2
    #: Rows wider than the header (audit finding 9). In a wide file they are
    #: skipped where they sit; in a long file they were emptied before the
    #: pivot (so they joined no group) and are reported up front, because the
    #: validation loop below walks pivoted groups, not file rows.
    ragged_in_loop = ragged
    ragged_skips: list[dict] = []

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
        column_map, pivoted_assay_from_map, unmapped = _build_column_map(
            csv_columns, aliases=effective_aliases,
        )
        assay_cols = pivoted_assay_cols
        ragged_skips = [entry.skip_entry() for _row, entry in sorted(ragged.rows.items())]
        ragged_in_loop = RaggedRows()
        total_rows = len(df) + len(ragged_skips)

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

    specs = _assay_specs(assay_cols)
    global_warnings.extend(_assay_column_warnings(specs))

    if "sample_type" not in column_map:
        if not specs and "sample_id" not in column_map:
            # No type, no assays, no sample number: nothing says these
            # intervals are samples, and writing them as such would fill
            # silver.samples with a geotech or density table's rows.
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
                    "reason": (
                        "file-level: missing required column mapping(s): "
                        "frozenset({'sample_type'}) - and no assay or sample "
                        "number column either, so nothing marks these rows "
                        "as samples"
                    ),
                    "raw": {},
                    "expected": "a sample_type, sample number or assay column",
                    "actual": None,
                    "suggestion": (
                        "Add a sample number or assay column, or upload the "
                        "table under the category it belongs to."
                    ),
                }],
                warnings=global_warnings,
                detected_encoding=detected_encoding,
            )
        global_warnings.append({
            "row": None,
            "code": _CODE_SAMPLE_TYPE_MISSING,
            "message": (
                "the file has no sample-type column; samples were kept with "
                "their type left blank"
            ),
            "detail": (
                "No column in this file says what kind of sample each row is "
                "(core, chip, grab, channel, soil), so the samples and their "
                "assays were stored with the type left blank rather than "
                "guessed. Add a Sample_Type column and re-upload if the type "
                "matters for your analysis."
            ),
        })

    qaqc_col_present = "qaqc_type" in column_map

    # Rename canonical columns; keep assay columns under their original names
    rename_map = {v: k for k, v in column_map.items()}
    df_renamed = df.rename(rename_map)
    # Select canonical mapped cols + all assay cols
    keep_cols = [c for c in df_renamed.columns if c in column_map] + [
        c for c in assay_cols if c in df_renamed.columns
    ]
    df_trimmed = df_renamed.select(keep_cols)
    # "From_ft" / "To_ft" -> metres (GIS-3); the header's unit is honoured.
    df_trimmed, unit_warning = convert_feet_columns(
        df_trimmed,
        columns={"from_depth": "from_depth", "to_depth": "to_depth"},
        headers=column_map,
        fields=("from_depth", "to_depth"),
        parser="csv_sample",
    )
    if unit_warning is not None:
        global_warnings.append(unit_warning)

    records: list = []
    skipped: list = list(ragged_skips)
    # CC-01 Item 1 Slice 2 — track the source pivot_idx for each kept
    # record so long-format unit-ambiguity flags can be re-aligned after
    # validation drops invalid rows.
    pivot_indices_kept: list[int] = []
    blanked = BlankedValues()

    rows_as_dicts = df_trimmed.to_dicts()
    for pivot_idx, raw in enumerate(rows_as_dicts):
        i = pivot_idx + 2  # 1-based CSV line (header is line 1)
        if ragged_in_loop.skip(i, skipped):
            continue
        row_warnings: list = []
        record, skip_entry = _validate_row(
            i, raw, column_map, assay_cols, qaqc_col_present, row_warnings,
            blanked, specs,
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
                    # Pivot flags are keyed by the pivot's column name
                    # ("Au_gpt"); the record is keyed canonically ("Au_ppm")
                    # with thresholds in the stored unit.
                    rekeyed = {}
                    for col_name, flag in extra_flags.items():
                        spec = specs.get(col_name)
                        if spec is None:
                            rekeyed[col_name] = flag
                        else:
                            rekeyed[spec.key] = _scaled_flags(flag, spec.factor)
                    merged = {**existing, **rekeyed}
                    record["commodity_assay_flags"] = merged if merged else None
            records.append(record)
            pivot_indices_kept.append(pivot_idx)
        else:
            # Log the stable reason CODE, never the free-text `reason` —
            # that string interpolates raw cell values, so logging it
            # ships arbitrary spreadsheet content to the application log
            # and on to Log Analytics. The full reason (and the raw row)
            # stays in skipped_details for the ingest report.
            logger.warning(
                "Skipping sample row %s: %s",
                skip_entry.get("row"),
                skip_entry.get("code"),
            )
            skipped.append(skip_entry)

    valid_rows = len(records)
    skipped_rows = len(skipped)

    blanked_warning = blanked.as_warning(parser="csv_sample")
    if blanked_warning is not None:
        global_warnings.append(blanked_warning)
        logger.warning(
            "csv_sample: %d optional value(s) blanked (%s)",
            blanked.total, ", ".join(blanked_warning["fields"]),
        )

    # CC-01 Item 1 Slice 2 — compute per-record unit ambiguity. Wide-format
    # detector inspects each record's commodity_assays + assay column names;
    # long-format additionally contributes flags collected during pivot.
    # The detector reasons about the COLUMN a value came from ("Au" with no
    # unit), so it is handed the values under their original column names.
    by_column = [
        {"commodity_assays": {
            col: (r.get("commodity_assays") or {}).get(spec.key)
            for col, spec in specs.items()
            if spec.key in (r.get("commodity_assays") or {})
        }}
        for r in records
    ]
    wide_unit_flags = detect_wide_format(assay_cols, by_column)
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
