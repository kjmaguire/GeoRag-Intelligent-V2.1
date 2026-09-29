"""CSV Structure Parser — Bronze → Silver ingestion for structural measurements.

Accepts a CSV file path or file-like object, auto-detects column name variations
across common structural-log exports, validates each row, and returns a list of
validated structure dicts ready for ``silver.structure``.

The target is the ``silver.structure`` COLUMN set (created by
2026_05_20_060400_create_silver_geological_singulars), not the looser field
names in the architecture doc's §04e table:

    depth            NOT NULL   a single downhole depth (m)
    structure_type   NOT NULL   free text in silver; gold restricts it (below)
    alpha_angle                 angle to the core axis, 0-90
    beta_angle                  angle around the core, 0-360
    true_dip                    0-90
    true_dip_dir                0-360 (dip DIRECTION, not strike)
    roughness, infill, notes

What this parser refuses to guess
---------------------------------
* **Strike.** silver.structure has no strike column, and turning a strike
  into a dip direction needs a convention (right-hand rule, left-hand rule,
  quadrant notation) that a bare ``Strike`` header does not declare. So a
  ``Strike`` column is NOT converted: the value is kept in ``notes`` and one
  ``structure_strike_not_converted`` warning says how many rows were affected.
  Only a column that names the rule (``Strike_RHR``) is converted - that is
  unambiguous - and the conversion is itself counted in a warning.
* **Interval vs point.** A From/To pair collapses to ``depth = From`` with the
  extent kept in ``notes`` and counted in ``structure_interval_collapsed``;
  silver.structure holds one depth per row.
* **Dip direction from ``Azimuth``.** Accepted, because structural logs do
  call it that, but flagged (``structure_dip_direction_from_azimuth``): the
  same header on a survey is a hole bearing.
* **Structure type.** gold.structure_measurements_visual rejects anything
  outside a twelve-value vocabulary, and one bad value fails the whole
  promotion INSERT. Types are therefore mapped to that vocabulary by an
  explicit synonym table; anything else is stored as ``other`` with the raw
  text in ``notes`` and counted in ``structure_type_unmapped``. No fuzzy
  matching.

Rows are rejected (into ``skipped_details``) for a missing hole or depth, a
non-numeric or negative depth, or an ANGLE OUT OF RANGE (dip 0-90, dip
direction 0-360, alpha 0-90, beta 0-360). A non-numeric OPTIONAL angle ("?",
"n/a") is blanked and reported as ``optional_values_blanked``; the row is
kept.
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
from georag_geoparsers._drill_schema import (
    STRUCTURE_ALIASES,
    STRUCTURE_REQUIRED,
    STRUCTURE_SIGNAL_ALIASES,
)
from georag_geoparsers._header_match import alias_skeletons, build_column_map, normalize_header
from georag_geoparsers._hole_id import canonicalize, suggest_collisions
from georag_geoparsers._optional_enum import BlankedValues
from georag_geoparsers._vendor_aliases import merge_vendor_aliases

logger = logging.getLogger(__name__)

COLUMN_ALIASES: dict = STRUCTURE_ALIASES
REQUIRED_FIELDS: frozenset = STRUCTURE_REQUIRED

#: Every column that is read as a number. ``depth`` is required; the rest are
#: optional and are blanked (not rejected) when they are not numeric.
_REQUIRED_NUMERIC = ("depth",)
_OPTIONAL_NUMERIC = (
    "to_depth", "alpha_angle", "beta_angle", "true_dip", "true_dip_dir",
    "strike", "strike_rhr",
)

#: Inclusive range checks. An out-of-range value rejects the row: an angle of
#: 130 degrees is a unit or column mix-up, not a measurement to store.
RANGE_CHECKS: dict = {
    "depth":        (0.0, 10_000.0),
    "alpha_angle":  (0.0, 90.0),
    "beta_angle":   (0.0, 360.0),
    "true_dip":     (0.0, 90.0),
    "true_dip_dir": (0.0, 360.0),
}

#: The vocabulary gold.structure_measurements_visual.structure_type accepts
#: (its CHECK constraint). silver.structure.structure_type is free text, so
#: this parser is where the two are reconciled.
VALID_STRUCTURE_TYPES: frozenset = frozenset({
    "fault", "shear", "fracture", "joint", "vein", "foliation", "cleavage",
    "bedding", "contact", "fold_axis", "lineation", "other",
})

#: Synonym -> vocabulary value. Conservative and exact: whole cell (after
#: case/punctuation folding) first, then a single-keyword token scan. Add a
#: spelling here only when it cannot mean two things.
_TYPE_SYNONYMS: dict = {
    "fault": "fault", "faults": "fault", "flt": "fault",
    "shear": "shear", "shears": "shear", "shr": "shear", "shear zone": "shear",
    "fracture": "fracture", "fractures": "fracture", "frac": "fracture",
    "fract": "fracture",
    "joint": "joint", "joints": "joint", "jt": "joint", "jnt": "joint",
    "vein": "vein", "veins": "vein", "vn": "vein",
    "foliation": "foliation", "foliations": "foliation", "fol": "foliation",
    "foln": "foliation",
    "cleavage": "cleavage", "clv": "cleavage",
    "bedding": "bedding", "bed": "bedding", "beds": "bedding", "bdg": "bedding",
    "contact": "contact", "contacts": "contact", "ctc": "contact",
    "fold axis": "fold_axis", "foldaxis": "fold_axis", "fold_axis": "fold_axis",
    "lineation": "lineation", "lineations": "lineation", "lin": "lineation",
    "other": "other", "unknown": "other",
}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

# Warning / skip codes
_CODE_ENCODING_NON_UTF8 = "encoding_non_utf8"
_CODE_MISSING_REQUIRED = "missing_required"
_CODE_NUMERIC_CAST = "numeric_cast_failed"
_CODE_RANGE = "range_check_failed"
_CODE_DECIMAL_COMMA = "decimal_comma_detected"

# Row-level counters -> parse-level warning codes.
CODE_STRIKE_NOT_CONVERTED = "structure_strike_not_converted"
CODE_STRIKE_CONVERTED = "structure_strike_converted"
CODE_INTERVAL_COLLAPSED = "structure_interval_collapsed"
CODE_TYPE_UNMAPPED = "structure_type_unmapped"
CODE_NO_ORIENTATION = "structure_no_orientation"
CODE_DIP_DIR_FROM_AZIMUTH = "structure_dip_direction_from_azimuth"

PARSER_VERSION = "1.0.0"


@dataclass
class StructureParseResult:
    """Container for a completed structure parse run."""
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

def _cast_float(value) -> float | None:
    """Return float or None; never raises."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        # Not an error: the caller decides whether a non-numeric cell rejects
        # the row (required fields) or is blanked and reported (optional).
        logger.debug("csv_structure: non-numeric cell")
        return None


def map_structure_type(raw: str | None) -> tuple[str, bool]:
    """``(vocabulary value, mapped)`` for a raw structure-type cell.

    ``mapped`` is False when the cell was blank or matched nothing; the value
    is then ``"other"`` and the caller records the raw text. A cell naming
    two different types ("shear vein") is NOT mapped - picking one would be
    a guess.
    """
    if raw is None:
        return "other", False
    text = " ".join(_TOKEN_SPLIT.split(str(raw).strip().casefold())).strip()
    if not text:
        return "other", False
    if text in _TYPE_SYNONYMS:
        return _TYPE_SYNONYMS[text], text not in ("unknown",)
    found = {
        _TYPE_SYNONYMS[token]
        for token in text.split()
        if token in _TYPE_SYNONYMS and _TYPE_SYNONYMS[token] != "other"
    }
    if len(found) == 1:
        return next(iter(found)), True
    return "other", False


class _Counters:
    """What one parse converted, collapsed or could not read."""

    def __init__(self) -> None:
        self.strike_not_converted = 0
        self.strike_converted = 0
        self.interval_collapsed = 0
        self.no_orientation = 0
        self.type_unmapped = BlankedValues()
        self.blanked = BlankedValues()


def _reject(row_num: int, raw: dict, code: str, reason: str,
            expected: str, actual: Any, suggestion: str) -> tuple:
    return None, {
        "row": row_num,
        "code": code,
        "reason": f"row {row_num}: {reason}",
        "raw": raw,
        "expected": expected,
        "actual": actual,
        "suggestion": suggestion,
    }


def _validate_row(
    row_num: int,
    raw: dict,
    column_map: dict,
    counters: _Counters | None = None,
) -> tuple:
    """Validate one raw row (keyed by canonical names).

    Returns (record, None) on success or (None, skip_entry) on failure.
    """
    counters = counters if counters is not None else _Counters()

    for req in REQUIRED_FIELDS:
        val = raw.get(req)
        if val is None or str(val).strip() == "":
            return _reject(
                row_num, raw, _CODE_MISSING_REQUIRED,
                f"missing required field '{req}'",
                f"non-empty value for '{req}'", None,
                f"Ensure the '{req}' column is present and populated, "
                f"or map it explicitly.",
            )

    record: dict = {}
    for canonical in column_map:
        raw_val = raw.get(canonical)
        if canonical in _REQUIRED_NUMERIC:
            casted = _cast_float(raw_val)
            if casted is None:
                return _reject(
                    row_num, raw, _CODE_NUMERIC_CAST,
                    f"cannot cast required numeric field '{canonical}' "
                    f"value '{raw_val}'",
                    "numeric value", {"field": canonical, "value": raw_val},
                    "Remove text units from the value cell.",
                )
            record[canonical] = casted
        elif canonical in _OPTIONAL_NUMERIC:
            casted = _cast_float(raw_val)
            if casted is None and raw_val is not None and str(raw_val).strip():
                # A text cell in an OPTIONAL numeric column: keep the row.
                counters.blanked.add(canonical, raw_val)
            record[canonical] = casted
        else:
            text = str(raw_val).strip() if raw_val is not None else None
            record[canonical] = text or None

    for field_name, (lo, hi) in RANGE_CHECKS.items():
        val = record.get(field_name)
        if val is not None and not (lo <= val <= hi):
            return _reject(
                row_num, raw, _CODE_RANGE,
                f"field '{field_name}' value {val} out of range [{lo}, {hi}]",
                f"{field_name} in [{lo}, {hi}]", {field_name: val},
                f"Check '{field_name}' is in degrees (dip 0-90, dip direction "
                f"0-360, alpha 0-90, beta 0-360) and in the right column.",
            )

    notes: list[str] = []
    if record.get("notes"):
        notes.append(record["notes"])

    # -- interval -> single depth -------------------------------------------
    to_depth = record.pop("to_depth", None)
    if to_depth is not None and to_depth > record["depth"]:
        counters.interval_collapsed += 1
        notes.append(f"logged over {record['depth']:g}-{to_depth:g} m")

    # -- strike: convert only when the column names the rule ------------------
    strike = record.pop("strike", None)
    strike_rhr = record.pop("strike_rhr", None)
    if strike_rhr is not None and not (0.0 <= strike_rhr <= 360.0):
        counters.blanked.add("strike_rhr", strike_rhr)
        strike_rhr = None
    if strike is not None and not (0.0 <= strike <= 360.0):
        counters.blanked.add("strike", strike)
        strike = None
    if record.get("true_dip_dir") is None:
        if strike_rhr is not None:
            record["true_dip_dir"] = (strike_rhr + 90.0) % 360.0
            counters.strike_converted += 1
            notes.append(f"strike {strike_rhr:g} (RHR)")
        elif strike is not None:
            counters.strike_not_converted += 1
            notes.append(f"strike {strike:g} (convention not declared)")

    # -- structure type -------------------------------------------------------
    raw_type = record.get("structure_type")
    mapped_type, mapped = map_structure_type(raw_type)
    record["structure_type"] = mapped_type
    if not mapped and mapped_type == "other":
        # 'other' is the vocabulary's own bucket, so a cell that literally
        # says "other" is fine; blank or unrecognised text is reported.
        if raw_type and " ".join(str(raw_type).split()).casefold() != "other":
            counters.type_unmapped.add("structure_type", raw_type)
            notes.append(f"type: {raw_type}")
        elif not raw_type:
            counters.type_unmapped.add("structure_type", "(blank)")

    orientation = (
        record.get("true_dip"), record.get("true_dip_dir"),
        record.get("alpha_angle"), record.get("beta_angle"),
    )
    if all(v is None for v in orientation):
        counters.no_orientation += 1

    record["notes"] = "; ".join(notes) if notes else None
    # A stable record shape: every silver.structure column is present, None
    # where the file had no such column.
    for column in (
        "alpha_angle", "beta_angle", "true_dip", "true_dip_dir",
        "roughness", "infill",
    ):
        record.setdefault(column, None)

    record["hole_id_canonical"] = canonicalize(record.get("hole_id"))
    record["_source_row"] = row_num
    return record, None


def _row_warnings(
    counters: _Counters, *, mapped_dip_dir_from_azimuth: bool,
    total_kept: int,
) -> list:
    """Parse-level warnings for what the rows above converted or could not read."""
    out: list = []

    def _add(code: str, message: str, detail: str) -> None:
        out.append({
            "row": None, "code": code,
            "message": message, "detail": detail[:900],
        })

    if counters.strike_not_converted:
        n = counters.strike_not_converted
        _add(
            CODE_STRIKE_NOT_CONVERTED,
            f"{n} structure row(s) gave a strike but no dip direction; "
            f"strike was not converted",
            f"{n} row(s) carry a Strike value and no dip direction. Turning "
            f"strike into dip direction needs the convention (right-hand "
            f"rule, left-hand rule or quadrant notation), which a plain "
            f"'Strike' column does not state, so the dip direction was left "
            f"empty and the strike was kept in the row's notes. Re-export "
            f"with a dip direction column (or a 'Strike_RHR' column if the "
            f"strikes are right-hand-rule) to get these rows on the "
            f"stereonet.",
        )
    if counters.strike_converted:
        n = counters.strike_converted
        _add(
            CODE_STRIKE_CONVERTED,
            f"{n} structure row(s) converted from right-hand-rule strike",
            f"{n} row(s) had no dip direction but a strike in a column "
            f"named as right-hand-rule; dip direction was set to strike + "
            f"90 (mod 360).",
        )
    if counters.interval_collapsed:
        n = counters.interval_collapsed
        _add(
            CODE_INTERVAL_COLLAPSED,
            f"{n} structure row(s) were logged over an interval",
            f"{n} row(s) carry a From/To pair. silver.structure stores one "
            f"depth per measurement, so the depth is the From (top) value "
            f"and the extent is kept in the row's notes.",
        )
    if counters.no_orientation:
        n = counters.no_orientation
        _add(
            CODE_NO_ORIENTATION,
            f"{n} of {total_kept} structure row(s) have no orientation",
            f"{n} row(s) have no dip, dip direction, alpha or beta, so they "
            f"are recorded as features at a depth but cannot be plotted on a "
            f"stereonet.",
        )
    info = counters.type_unmapped.as_warning(parser="csv_structure")
    if info is not None:
        fld = info["fields"]["structure_type"]
        _add(
            CODE_TYPE_UNMAPPED,
            f"{fld['count']} structure row(s) have a blank or unrecognised type",
            f"{fld['count']} row(s) have a structure type outside the "
            f"platform's list (fault, shear, fracture, joint, vein, "
            f"foliation, cleavage, bedding, contact, fold_axis, lineation, "
            f"other) - e.g. {', '.join(repr(e) for e in fld['examples'])}. "
            f"They were stored as 'other' with the original text in the "
            f"row's notes.",
        )
        out[-1]["fields"] = info["fields"]
    if mapped_dip_dir_from_azimuth:
        _add(
            CODE_DIP_DIR_FROM_AZIMUTH,
            "the 'azimuth' column was read as the dip direction",
            "This file has no dip-direction column, so its Azimuth/AZI column "
            "was used as the dip direction of each structure. If it is in "
            "fact the strike or the hole bearing, the stereonet is wrong - "
            "re-export with a 'DipDir' column or map the column explicitly.",
        )
    blanked = counters.blanked.as_warning(parser="csv_structure")
    if blanked is not None:
        out.append(blanked)
    return out


# ---------------------------------------------------------------------------
# Public parser entry point
# ---------------------------------------------------------------------------

def parse_csv_structures(
    source: Union[str, Path, IO],  # noqa: UP007
    *,
    null_values: list = None,
    vendor_aliases: dict[str, list[str]] | None = None,
) -> StructureParseResult:
    """Parse a CSV structural-measurement file into a :class:`StructureParseResult`.

    Parameters
    ----------
    source:
        Absolute file path (str or Path) or a file-like text stream.
    null_values:
        Additional strings to treat as null (on top of the Polars defaults).
    vendor_aliases:
        Extra column spellings keyed by canonical field name, merged ahead of
        the built-in aliases (a stored vendor profile, or a mapping the user
        confirmed for this file).
    """
    global_warnings: list = []
    detected_encoding = "utf-8"

    source_file_str = str(source) if isinstance(source, (str, Path)) else "<stream>"
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

        df = pl.read_csv(
            StringIO(raw_content),
            separator=detected_delim,
            infer_schema=False,
            null_values=all_nulls,
            truncate_ragged_lines=True,
        )

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
    except Exception as exc:
        logger.error("Failed to read CSV source: %s", exc)
        raise

    csv_columns: list = df.columns
    total_rows: int = len(df)

    effective_aliases = merge_vendor_aliases(COLUMN_ALIASES, vendor_aliases)
    column_map, unmapped = build_column_map(csv_columns, effective_aliases)

    missing_required = REQUIRED_FIELDS - set(column_map)
    if missing_required:
        logger.error(
            "Structure CSV is missing required columns (no alias matched): %s",
            missing_required,
        )
        return StructureParseResult(
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
                    "Rename the CSV headers to a recognised alias or map the "
                    "columns explicitly."
                ),
            }],
            warnings=global_warnings,
            detected_encoding=detected_encoding,
        )

    # The dip direction came from a weak spelling (Azimuth/AZI) when the
    # mapped column is not one of the explicit dip-direction spellings.
    dip_dir_from_azimuth = False
    dip_dir_col = column_map.get("true_dip_dir")
    if dip_dir_col is not None:
        explicit = alias_skeletons(
            "true_dip_dir", STRUCTURE_SIGNAL_ALIASES["true_dip_dir"],
        )
        user_named = {
            normalize_header(a)
            for a in (vendor_aliases or {}).get("true_dip_dir", [])
        }
        dip_dir_from_azimuth = normalize_header(dip_dir_col) not in (explicit | user_named)

    rename_map = {v: k for k, v in column_map.items()}
    df_trimmed = df.rename(rename_map).select(
        [c for c in df.rename(rename_map).columns if c in column_map],
    )

    records: list = []
    skipped: list = []
    counters = _Counters()

    for i, raw in enumerate(df_trimmed.to_dicts(), start=2):
        record, skip_entry = _validate_row(i, raw, column_map, counters)
        if record is not None:
            records.append(record)
        else:
            # Log the stable CODE, never the free-text reason (it carries
            # raw cell values).
            logger.warning(
                "Skipping structure row %s: %s",
                skip_entry.get("row"), skip_entry.get("code"),
            )
            skipped.append(skip_entry)

    global_warnings.extend(_row_warnings(
        counters,
        mapped_dip_dir_from_azimuth=(
            dip_dir_from_azimuth
            and any(r.get("true_dip_dir") is not None for r in records)
        ),
        total_kept=len(records),
    ))

    all_raw_hole_ids = [r["hole_id"] for r in records if r.get("hole_id")]
    for collision in suggest_collisions(all_raw_hole_ids):
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

    result = StructureParseResult(
        records=records,
        total_rows=total_rows,
        valid_rows=len(records),
        skipped_rows=len(skipped),
        unmapped_columns=unmapped,
        column_map=column_map,
        skipped_details=skipped,
        warnings=global_warnings,
        detected_encoding=detected_encoding,
        provenance={
            "source_file": source_file_str,
            "source_file_sha256": sha256_hex,
            "parser_name": "csv_structure",
            "parser_version": PARSER_VERSION,
            "source_col_map": column_map,
        },
    )

    logger.info(
        "CSV structure parse complete — total: %d, valid: %d, skipped: %d, "
        "quality: %.1f%%, unmapped cols: %d, warnings: %d",
        total_rows, result.valid_rows, result.skipped_rows,
        result.parse_quality_pct, len(unmapped), len(global_warnings),
    )
    return result
