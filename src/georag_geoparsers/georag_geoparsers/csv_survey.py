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
from pathlib import Path
from typing import IO, Any, Union

from georag_geoparsers._azimuth_reference import canonical_azimuth_reference
from georag_geoparsers._csv_io import (
    DEFAULT_NULL_VALUES,
    detect_delimiter,
    open_csv_with_encoding,
    read_csv_checked,
    transform_decimal_comma,
)
from georag_geoparsers._depth_units import convert_feet_columns
from georag_geoparsers._dip_convention import DipConvention, normalize_dip, resolve_dip_convention
from georag_geoparsers._drill_schema import SURVEY_ALIASES, SURVEY_REQUIRED
from georag_geoparsers._encoding import decode_warnings, is_utf8_compatible
from georag_geoparsers._header_match import build_column_map
from georag_geoparsers._hole_id import canonicalize, suggest_collisions
from georag_geoparsers._optional_enum import BlankedValues, canonical_choice
from georag_geoparsers._vendor_aliases import merge_vendor_aliases

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column name alias maps — keys are canonical names, values are accepted
# aliases. Order within each list reflects preference when multiple aliases
# are present. Defined in _drill_schema so the sheet classifier and the
# FastAPI writers read the same vocabulary; re-exported here because tests
# and _sheet_classifier import them from this module.
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict = SURVEY_ALIASES

# Required fields — rows missing any of these are rejected
REQUIRED_FIELDS: frozenset = SURVEY_REQUIRED

# Numeric fields that must be castable to float
NUMERIC_FIELDS: frozenset = frozenset({"depth", "azimuth", "dip"})

# Valid survey methods — SME-defined list (update via config if scope grows).
# survey_method is OPTIONAL: a value outside this list is blanked (the writer
# then records "unknown") and reported as ``optional_values_blanked``; it does
# not reject the station.
VALID_SURVEY_METHODS: frozenset = frozenset({"Reflex", "Gyro", "Magnetic", "Acid Test"})

# Range checks
RANGE_CHECKS: dict = {
    "depth":   (0.0,   10_000.0),
    "azimuth": (0.0,   360.0),
    "dip":     (-90.0, 90.0),   # §04e 2026-09-29: a positive dip is an up-hole
}

# Warning / skip codes
_CODE_ENCODING_NON_UTF8 = "encoding_non_utf8"
_CODE_DIP_CONVENTION = "dip_convention_normalized"
_CODE_DIP_AMBIGUOUS = "dip_convention_ambiguous"
_CODE_MISSING_REQUIRED = "missing_required"
_CODE_NUMERIC_CAST = "numeric_cast_failed"
_CODE_RANGE = "range_check_failed"
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

def _build_column_map(csv_columns: list, *, aliases: dict | None = None) -> tuple:
    """Map canonical field names to the first matching CSV column alias found.

    Case, separators and unit suffixes are folded by ``_header_match``, so
    ``Depth (m)`` and ``depth_m`` reach the same field as ``Depth``.
    """
    return build_column_map(csv_columns, aliases if aliases is not None else COLUMN_ALIASES)


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
    blanked: BlankedValues | None = None,
) -> tuple:
    """Validate a single raw row dict (keyed by canonical names).

    Returns (record, None) on success or (None, skip_entry) on failure.
    skip_entry includes extended diagnostic fields per Sprint 2 contract:
      expected, actual, suggestion.

    An unrecognised optional ``survey_method`` or ``azimuth_reference`` is set
    to None and recorded in *blanked*; it never rejects the station.
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
    if record.get("dip") is not None:
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
                    f"Depth range [{lo}, {hi}] m; azimuth 0–360; dip -90–90 (negative = down)."
                ),
            }

    # --- Survey method (optional): blank, never reject ---
    method = record.get("survey_method")
    if method is not None:
        if not method.strip():
            record["survey_method"] = None
        else:
            canonical = canonical_choice(method, VALID_SURVEY_METHODS)
            if canonical is None:
                record["survey_method"] = None
                if blanked is not None:
                    blanked.add("survey_method", method)
            else:
                record["survey_method"] = canonical

    # --- Azimuth reference (optional): canonicalise, blank, never reject ---
    # true / magnetic / grid, or None. An unreadable value is blanked and
    # reported rather than read as grid: grid is the no-correction default,
    # so guessing it would hide a declaration the file did make.
    reference = record.get("azimuth_reference")
    if reference is not None:
        if not reference.strip():
            record["azimuth_reference"] = None
        else:
            canonical_ref = canonical_azimuth_reference(reference)
            record["azimuth_reference"] = canonical_ref
            if canonical_ref is None and blanked is not None:
                blanked.add("azimuth_reference", reference)

    # --- hole_id canonicalization ---
    record["hole_id_canonical"] = canonicalize(record.get("hole_id"))

    # --- source row tracking ---
    record["_source_row"] = row_num

    return record, None


# ---------------------------------------------------------------------------
# Public parser entry point
# ---------------------------------------------------------------------------

def parse_csv_surveys(
    source: Union[str, Path, IO],  # noqa: UP007
    *,
    null_values: list = None,
    vendor_aliases: dict[str, list[str]] | None = None,
) -> SurveyParseResult:
    """Parse a CSV downhole survey file and return a :class:`SurveyParseResult`.

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

        global_warnings.extend(decode_warnings(detected_encoding, raw_content))
        if not is_utf8_compatible(detected_encoding):
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

        df, ragged = read_csv_checked(
            raw_content, separator=detected_delim, null_values=all_nulls,
        )
        global_warnings.extend(ragged.warnings())

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

    column_map, unmapped = _build_column_map(
        csv_columns, aliases=merge_vendor_aliases(COLUMN_ALIASES, vendor_aliases),
    )

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

    # --- Length units named in the header (GIS-3): "Depth_ft" -> metres ---
    df_trimmed, unit_warning = convert_feet_columns(
        df_trimmed,
        columns={"depth": "depth"},
        headers=column_map,
        fields=("depth",),
        parser="csv_survey",
    )
    if unit_warning is not None:
        global_warnings.append(unit_warning)

    # --- Dip convention detection (first pass) ---
    dip_convention: DipConvention = "down_negative"
    if "dip" in column_map:
        raw_dips = df_trimmed["dip"].to_list()
        numeric_dips = [_cast_float(v) for v in raw_dips]
        numeric_dips = [d for d in numeric_dips if d is not None]
        dip_resolution = resolve_dip_convention(
            numeric_dips, header=column_map["dip"], parser="csv_survey",
        )
        dip_convention = dip_resolution.convention
        global_warnings.extend(dip_resolution.warnings)
        if dip_convention != "down_negative":
            logger.info(
                "csv_survey: dip convention %s (%d samples)",
                dip_convention, len(numeric_dips),
            )

    records: list = []
    skipped: list = []
    blanked = BlankedValues()

    rows_as_dicts = df_trimmed.to_dicts()
    for i, raw in enumerate(rows_as_dicts, start=2):
        if ragged.skip(i, skipped):
            continue
        record, skip_entry = _validate_row(i, raw, column_map, dip_convention, blanked)
        if record is not None:
            records.append(record)
        else:
            # Log the stable reason CODE, never the free-text `reason` —
            # that string interpolates raw cell values, so logging it
            # ships arbitrary spreadsheet content to the application log
            # and on to Log Analytics. The full reason (and the raw row)
            # stays in skipped_details for the ingest report.
            logger.warning(
                "Skipping survey row %s: %s",
                skip_entry.get("row"),
                skip_entry.get("code"),
            )
            skipped.append(skip_entry)

    valid_rows = len(records)
    skipped_rows = len(skipped)

    blanked_warning = blanked.as_warning(parser="csv_survey")
    if blanked_warning is not None:
        global_warnings.append(blanked_warning)
        logger.warning(
            "csv_survey: %d optional value(s) blanked (%s)",
            blanked.total, ", ".join(blanked_warning["fields"]),
        )

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
