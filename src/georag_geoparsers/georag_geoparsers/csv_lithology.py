"""CSV Lithology Parser — Bronze → Silver ingestion for lithology log data.

Accepts a CSV file path or file-like object, auto-detects column name variations
across common geological software exports, validates each row, and returns a list
of validated lithology dicts ready for Silver schema insertion.

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

from georag_geoparsers._csv_io import (
    DEFAULT_NULL_VALUES,
    detect_delimiter,
    open_csv_with_encoding,
    transform_decimal_comma,
)
from georag_geoparsers._depth_units import convert_feet_columns
from georag_geoparsers._drill_schema import LITHOLOGY_ALIASES, LITHOLOGY_REQUIRED
from georag_geoparsers._encoding import decode_warnings, is_utf8_compatible
from georag_geoparsers._header_match import alias_skeletons, build_column_map, normalize_header
from georag_geoparsers._hole_id import canonicalize, suggest_collisions
from georag_geoparsers._optional_enum import BlankedValues, canonical_choice
from georag_geoparsers._vendor_aliases import merge_vendor_aliases

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column name alias maps — keys are canonical names, values are accepted
# aliases. Defined in _drill_schema so the sheet classifier and the FastAPI
# writers read the same vocabulary; re-exported here because tests and
# _sheet_classifier import them from this module.
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict = LITHOLOGY_ALIASES

# Required fields — rows missing any of these are rejected
REQUIRED_FIELDS: frozenset = LITHOLOGY_REQUIRED

# Numeric fields that must be castable to float
NUMERIC_FIELDS: frozenset = frozenset({"from_depth", "to_depth", "rqd", "recovery"})

# Categorical vocabularies for the OPTIONAL descriptive columns. A value outside
# its set does NOT reject the row: the field is blanked and the parse records an
# ``optional_values_blanked`` warning (see _optional_enum for the decision).
# Required fields (hole id, from/to, lithology code) still reject.
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

#: Widths of the silver.lithology_logs columns (2026_04_09_180300:
#: lithology_code varchar(20), color varchar(50); grain_size / hardness /
#: weathering are held inside varchar(20) by their vocabularies). A longer
#: value does not fit and would fail the WHOLE batch insert, taking every
#: interval of the file with it, so it is handled here, with the full text
#: kept in the description.
LITHOLOGY_CODE_MAX = 20
COLOR_MAX = 50

#: Whole-word spellings of a vocabulary value that mean exactly that value.
#: These are the same word, not a nearest-match: "Fine grained" IS "Fine", and
#: "Slightly weathered" IS the ISRM grade "Slight". Anything else is still
#: blanked, with the geologist's text kept in the description.
_GRAIN_SUFFIX = re.compile(r"[\s-]+grain(?:ed)?$", re.IGNORECASE)
_WEATHERING_SPELLINGS: dict = {
    "slightly weathered": "Slight",
    "slightly": "Slight",
    "moderately weathered": "Moderate",
    "moderately": "Moderate",
    "highly weathered": "High",
    "highly": "High",
    "completely weathered": "Complete",
    "completely": "Complete",
}

# Warning / skip codes
_CODE_ENCODING_NON_UTF8 = "encoding_non_utf8"
_CODE_MISSING_REQUIRED = "missing_required"
_CODE_NUMERIC_CAST = "numeric_cast_failed"
_CODE_DEPTH_ORDER = "depth_order_invalid"
_CODE_DEPTH_NEG = "depth_negative"
_CODE_RANGE = "range_check_failed"
_CODE_DECIMAL_COMMA = "decimal_comma_detected"


# ---------------------------------------------------------------------------
# Parse result dataclass
# ---------------------------------------------------------------------------

PARSER_VERSION = "2.1.0"


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

    Case, separators and unit suffixes are folded by ``_header_match``, so
    ``From (m)`` and ``from_m`` reach the same field as ``From``. Vendor
    aliases go through the identical rule — a profile written with one
    spelling now matches every equivalent one.
    """
    return build_column_map(
        csv_columns,
        aliases if aliases is not None else COLUMN_ALIASES,
    )


def _cast_float(value) -> float:
    """Return float or None; never raises."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


#: Optional descriptive fields and the vocabulary each is checked against.
_OPTIONAL_ENUMS: dict = {
    "grain_size": VALID_GRAIN_SIZES,
    "hardness": VALID_HARDNESS,
    "weathering": VALID_WEATHERING,
}


def _keep_in_description(record: dict, field_name: str, value: str) -> None:
    """Append ``[field: value]`` to the description, so the text is not lost."""
    kept = f"[{field_name}: {' '.join(str(value).split())[:200]}]"
    prior = (record.get("lithology_description") or "").strip()
    record["lithology_description"] = f"{prior} {kept}".strip() if prior else kept


def _spell_enum(field_name: str, value: str) -> str:
    """The vocabulary spelling of *value*, before it is checked against it."""
    text = " ".join(value.split())
    if field_name == "grain_size":
        return _GRAIN_SUFFIX.sub("", text)
    if field_name == "weathering":
        return _WEATHERING_SPELLINGS.get(text.casefold(), text)
    return text


def _validate_row(
    row_num: int,
    raw: dict,
    column_map: dict,
    blanked: BlankedValues | None = None,
    truncated: BlankedValues | None = None,
) -> tuple:
    """Validate a single raw row dict (keyed by canonical names).

    Returns (record, None) on success or (None, skip_entry) on failure.
    skip_entry includes extended diagnostic fields per Sprint 2 contract:
      expected, actual, suggestion.

    An optional enum-like field (grain_size / hardness / weathering) whose
    value is outside its vocabulary is set to None and recorded in
    *blanked*; it never rejects the row.
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
    kept_numbers: list = []
    for canonical in column_map:
        raw_val = raw.get(canonical)
        if canonical in NUMERIC_FIELDS:
            if canonical in REQUIRED_FIELDS:
                casted = _cast_float(raw_val)
            else:
                # rqd / recovery: a trailing "%" is a unit, not a defect
                # ("85%" used to be read as no value at all, silently). What
                # is still not a number is blanked, counted and kept.
                text = str(raw_val).strip() if raw_val is not None else ""
                casted = _cast_float(text[:-1] if text.endswith("%") else text)
                if casted is None and text:
                    if blanked is not None:
                        blanked.add(f"{canonical} (not a number)", text)
                    kept_numbers.append((canonical, text))
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
        if val is not None and not (lo <= val <= hi) and field_name not in REQUIRED_FIELDS:
            # rqd / recovery are OPTIONAL: an out-of-range value (RQD 140) is
            # a data-entry error in one attribute, and silver's CHECK (0-100)
            # would refuse the row anyway. Keep the interval, blank the value,
            # keep the number in the description and say so.
            record[field_name] = None
            if blanked is not None:
                blanked.add(f"{field_name} (outside {lo:g}-{hi:g})", val)
            kept_numbers.append((field_name, f"{val:g}"))
            continue
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

    # --- Categorical validations (optional fields): blank, never reject ---
    for field_name, valid in _OPTIONAL_ENUMS.items():
        value = record.get(field_name)
        if value is None:
            continue
        if not value.strip():
            record[field_name] = None
            continue
        canonical = canonical_choice(_spell_enum(field_name, value), valid)
        if canonical is None:
            record[field_name] = None
            if blanked is not None:
                blanked.add(field_name, value)
            # Keep the geologist's word on the interval itself: the field is
            # blanked because it is not in the vocabulary, not because it is
            # wrong, and silver.lithology_logs.lithology_description is the
            # free-text home for it (e.g. "... [grain_size: porphyritic]").
            _keep_in_description(record, field_name, value.strip())
        else:
            record[field_name] = canonical

    # --- text values wider than their silver column ---------------------------
    # Kept whole in the description; the column gets what fits (the code, which
    # is required) or nothing (the colour). Never silently cut.
    code = record.get("lithology_code")
    if code and len(code) > LITHOLOGY_CODE_MAX:
        _keep_in_description(record, "lithology_code", code)
        record["lithology_code"] = code[:LITHOLOGY_CODE_MAX].rstrip()
        if truncated is not None:
            truncated.add("lithology_code", code)
    colour = record.get("color")
    if colour and len(colour) > COLOR_MAX:
        _keep_in_description(record, "color", colour)
        record["color"] = None
        if truncated is not None:
            truncated.add("color", colour)

    # --- optional numbers that did not fit: keep the text --------------------
    for field_name, text in kept_numbers:
        _keep_in_description(record, field_name, text)

    # --- hole_id canonicalization ---
    record["hole_id_canonical"] = canonicalize(record.get("hole_id"))

    # --- source row tracking ---
    record["_source_row"] = row_num

    return record, None


# ---------------------------------------------------------------------------
# Public parser entry point
# ---------------------------------------------------------------------------

def parse_csv_lithology(
    source: Union[str, Path, IO],  # noqa: UP007
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

        global_warnings.extend(decode_warnings(detected_encoding, raw_content))
        if not is_utf8_compatible(detected_encoding):
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

    # A SECOND description column ("Comments" beside "Lith_Desc") is the same
    # kind of text the first one holds. Only one column can be THE description,
    # and the other used to be dropped unread; its text is kept in the
    # description instead, labelled with the column it came from.
    description_skeletons = alias_skeletons(
        "lithology_description", effective_aliases.get("lithology_description", []),
    )
    extra_description_cols = [
        c for c in unmapped
        if "lithology_description" in column_map
        and normalize_header(c) in description_skeletons
    ]
    unmapped = [c for c in unmapped if c not in extra_description_cols]

    rename_map = {v: k for k, v in column_map.items()}
    df_renamed = df.rename(rename_map)
    canonical_cols = [c for c in df_renamed.columns if c in column_map]
    df_trimmed = df_renamed.select(canonical_cols)
    # "From_ft" / "To_ft" -> metres (GIS-3); the header's unit is honoured.
    df_trimmed, unit_warning = convert_feet_columns(
        df_trimmed,
        columns={"from_depth": "from_depth", "to_depth": "to_depth"},
        headers=column_map,
        fields=("from_depth", "to_depth"),
        parser="csv_lithology",
    )
    if unit_warning is not None:
        global_warnings.append(unit_warning)
    extra_description_rows = (
        df.select(extra_description_cols).to_dicts() if extra_description_cols else []
    )

    records: list = []
    skipped: list = []
    blanked = BlankedValues()
    truncated = BlankedValues()

    rows_as_dicts = df_trimmed.to_dicts()
    for i, raw in enumerate(rows_as_dicts, start=2):
        if extra_description_rows:
            for col, text in extra_description_rows[i - 2].items():
                text = " ".join(str(text).split()) if text is not None else ""
                if text:
                    kept = f"[{col}: {text}]"
                    prior = (raw.get("lithology_description") or "").strip()
                    raw["lithology_description"] = f"{prior} {kept}".strip() if prior else kept
        record, skip_entry = _validate_row(i, raw, column_map, blanked, truncated)
        if record is not None:
            records.append(record)
        else:
            # Log the stable reason CODE, never the free-text `reason` —
            # that string interpolates raw cell values, so logging it
            # ships arbitrary spreadsheet content to the application log
            # and on to Log Analytics. The full reason (and the raw row)
            # stays in skipped_details for the ingest report.
            logger.warning(
                "Skipping lithology row %s: %s",
                skip_entry.get("row"),
                skip_entry.get("code"),
            )
            skipped.append(skip_entry)

    valid_rows = len(records)
    skipped_rows = len(skipped)

    blanked_warning = blanked.as_warning(parser="csv_lithology")
    if blanked_warning is not None:
        global_warnings.append(blanked_warning)
        logger.warning(
            "csv_lithology: %d optional value(s) blanked (%s)",
            blanked.total, ", ".join(blanked_warning["fields"]),
        )

    truncated_warning = truncated.as_warning(parser="csv_lithology")
    if truncated_warning is not None:
        truncated_warning["code"] = "lithology_values_too_long"
        truncated_warning["message"] = (
            f"{truncated.total} lithology value(s) are longer than their column "
            f"({', '.join(truncated_warning['fields'])}); the full text was kept "
            f"in the description"
        )
        truncated_warning["detail"] = (
            "silver.lithology_logs holds a lithology code in 20 characters and "
            "a colour in 50. Longer values were not cut silently: the full text "
            "is in the interval's description ('[lithology_code: ...]'), the "
            "code column holds its first 20 characters, and a too-long colour "
            "is left empty. This usually means a description was mapped as the "
            "code - map the column explicitly if so."
        )
        global_warnings.append(truncated_warning)

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
    if extra_description_cols:
        provenance["extra_description_columns"] = extra_description_cols

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
