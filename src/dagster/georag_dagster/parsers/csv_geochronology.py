"""CSV Geochronology Parser — Bronze → Silver ingestion for radiometric age samples.

Accepts a CSV file path or file-like object, auto-detects column aliases across
common academic / lab exports (e.g. EarthChem, GEOROC, in-house DOI tables),
validates each row, and returns a list of validated geochronology dicts ready
for ``silver.geochronology_samples`` insertion.

Per the CC-03 Item 3 spec field list:
  - ``isotopic_system`` is required + enum-restricted
  - ``age_ma`` is numeric, optional (some entries record only ratios)
  - ``uncertainty_kind`` is enum-restricted (``2sigma``/``1sigma``/``unknown``);
    rows with a non-vocabulary value are REJECTED (per the test contract).
  - latitude/longitude are folded into a WKT ``POINT(lon lat)`` in ``geom_wkt``
    so the Dagster asset can hand it straight to ``ST_GeomFromText(..., 4326)``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import IO, Any, Union

import polars as pl

from georag_dagster.parsers._csv_io import (
    DEFAULT_NULL_VALUES,
    detect_delimiter,
    open_csv_with_encoding,
    transform_decimal_comma,
)

logger = logging.getLogger(__name__)

PARSER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Column alias map — canonical → preferred-order list of CSV header variants
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict[str, list[str]] = {
    "sample_id":          ["sample_id", "SampleID", "Sample_ID", "Sample", "SampleName"],
    "project_id":         ["project_id", "ProjectID", "Project"],
    "rock_type":          ["rock_type", "RockType", "Lithology", "Rock", "Host"],
    "isotopic_system":    ["isotopic_system", "IsotopicSystem", "System", "Method_System"],
    "mineral_dated":      ["mineral_dated", "Mineral", "MineralDated", "Phase"],
    "age_ma":             ["age_ma", "AgeMa", "Age_Ma", "Age(Ma)", "Age"],
    "age_uncertainty_ma": ["age_uncertainty_ma", "AgeUncertainty", "AgeUncMa",
                           "Uncertainty_Ma", "Error_Ma", "Sigma_Ma", "2sigma_Ma",
                           "Uncertainty(Ma)"],
    "uncertainty_kind":   ["uncertainty_kind", "UncertaintyKind", "SigmaKind",
                           "Sigma", "ErrorKind"],
    "analytical_method":  ["analytical_method", "AnalyticalMethod", "Method",
                           "Technique", "Instrument"],
    "laboratory":         ["laboratory", "Lab", "LabName", "Facility"],
    "publication_ref":    ["publication_ref", "Reference", "DOI", "Citation",
                           "Publication", "ReportID"],
    "latitude":           ["latitude", "Latitude", "Lat", "LAT", "Y"],
    "longitude":          ["longitude", "Longitude", "Lon", "LON", "Long", "X"],
}

REQUIRED_FIELDS: frozenset[str] = frozenset({"sample_id", "isotopic_system"})

NUMERIC_FIELDS: frozenset[str] = frozenset({
    "age_ma", "age_uncertainty_ma", "latitude", "longitude",
})

VALID_ISOTOPIC_SYSTEMS: frozenset[str] = frozenset({
    "U-Pb", "Pb-Pb", "Ar-Ar", "K-Ar", "Re-Os", "Rb-Sr", "Sm-Nd", "Lu-Hf", "other",
})

VALID_UNCERTAINTY_KINDS: frozenset[str] = frozenset({"2sigma", "1sigma", "unknown"})

# Lower-case aliases for the isotopic-system enum so a slightly off-cased
# value ("u-pb", "ar/ar") still matches its canonical form.
_ISOTOPIC_SYSTEM_ALIASES: dict[str, str] = {
    "u-pb": "U-Pb", "upb": "U-Pb", "u/pb": "U-Pb",
    "pb-pb": "Pb-Pb", "pbpb": "Pb-Pb",
    "ar-ar": "Ar-Ar", "arar": "Ar-Ar", "ar/ar": "Ar-Ar", "40ar/39ar": "Ar-Ar",
    "k-ar": "K-Ar", "kar": "K-Ar",
    "re-os": "Re-Os", "reos": "Re-Os",
    "rb-sr": "Rb-Sr", "rbsr": "Rb-Sr",
    "sm-nd": "Sm-Nd", "smnd": "Sm-Nd",
    "lu-hf": "Lu-Hf", "luhf": "Lu-Hf",
    "other": "other",
}

# Uncertainty-kind aliases — the column may carry "2σ", "2sd", etc.
_UNCERTAINTY_KIND_ALIASES: dict[str, str] = {
    "2sigma": "2sigma", "2σ": "2sigma", "2s": "2sigma", "2sd": "2sigma",
    "1sigma": "1sigma", "1σ": "1sigma", "1s": "1sigma", "1sd": "1sigma",
    "unknown": "unknown", "?": "unknown", "n/a": "unknown",
}

# Warning / skip codes
_CODE_ENCODING_NON_UTF8 = "encoding_non_utf8"
_CODE_MISSING_REQUIRED = "missing_required"
_CODE_INVALID_ISOTOPIC = "invalid_isotopic_system"
_CODE_INVALID_UNCERTAINTY = "invalid_uncertainty_kind"
_CODE_NUMERIC_CAST = "numeric_cast_failed"
_CODE_LATLON_OUT_OF_RANGE = "latlon_out_of_range"
_CODE_DECIMAL_COMMA = "decimal_comma_detected"


@dataclass
class GeochronParseResult:
    """Container for a completed geochronology parse run."""
    records: list[dict]
    total_rows: int
    valid_rows: int
    skipped_rows: int
    unmapped_columns: list[str]
    column_map: dict[str, str]
    skipped_details: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    detected_encoding: str = "utf-8"
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def parse_quality_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return round(self.valid_rows / self.total_rows * 100, 2)


def _build_column_map(csv_columns: list[str]) -> tuple[dict[str, str], list[str]]:
    """Map canonical → first matching CSV column alias found."""
    csv_set = set(csv_columns)
    column_map: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in csv_set:
                column_map[canonical] = alias
                break
    matched = set(column_map.values())
    unmapped = [c for c in csv_columns if c not in matched]
    return column_map, unmapped


def _cast_float(value) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _canonical_isotopic_system(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = str(raw).strip()
    if key == "":
        return None
    if key in VALID_ISOTOPIC_SYSTEMS:
        return key
    return _ISOTOPIC_SYSTEM_ALIASES.get(key.lower())


def _canonical_uncertainty_kind(raw: str | None) -> str | None:
    """Map a raw uncertainty kind to its enum value.

    Returns ``None`` if the input is empty/missing — distinct from an
    *invalid* value, which is signalled by callers checking that the
    canonical form is one of VALID_UNCERTAINTY_KINDS.
    """
    if raw is None:
        return None
    key = str(raw).strip()
    if key == "":
        return None
    if key in VALID_UNCERTAINTY_KINDS:
        return key
    return _UNCERTAINTY_KIND_ALIASES.get(key.lower())


def _validate_row(row_num: int, raw: dict) -> tuple[dict | None, dict | None]:
    """Validate a single row dict (keys already in canonical form).

    Returns (record, None) on success or (None, skip_entry) on failure.
    """
    # Required fields
    for req in REQUIRED_FIELDS:
        val = raw.get(req)
        if val is None or str(val).strip() == "":
            return None, {
                "row": row_num,
                "code": _CODE_MISSING_REQUIRED,
                "reason": f"row {row_num}: missing required field '{req}'",
                "raw": raw,
            }

    # Isotopic system — required + enum
    isotopic = _canonical_isotopic_system(raw.get("isotopic_system"))
    if isotopic is None:
        return None, {
            "row": row_num,
            "code": _CODE_INVALID_ISOTOPIC,
            "reason": (
                f"row {row_num}: isotopic_system "
                f"{raw.get('isotopic_system')!r} is not in the allowed enum"
            ),
            "raw": raw,
        }

    # Uncertainty kind — optional but, if provided, must canonicalise to enum.
    uncertainty_kind_canonical: str | None = None
    raw_uk = raw.get("uncertainty_kind")
    if raw_uk is not None and str(raw_uk).strip() != "":
        uncertainty_kind_canonical = _canonical_uncertainty_kind(raw_uk)
        if uncertainty_kind_canonical is None:
            return None, {
                "row": row_num,
                "code": _CODE_INVALID_UNCERTAINTY,
                "reason": (
                    f"row {row_num}: uncertainty_kind {raw_uk!r} not in "
                    f"{sorted(VALID_UNCERTAINTY_KINDS)}"
                ),
                "raw": raw,
            }

    record: dict[str, Any] = {
        "sample_id":         str(raw["sample_id"]).strip(),
        "project_id":        (str(raw["project_id"]).strip()
                              if raw.get("project_id") else None),
        "rock_type":         (str(raw["rock_type"]).strip()
                              if raw.get("rock_type") else None),
        "isotopic_system":   isotopic,
        "mineral_dated":     (str(raw["mineral_dated"]).strip()
                              if raw.get("mineral_dated") else None),
        "analytical_method": (str(raw["analytical_method"]).strip()
                              if raw.get("analytical_method") else None),
        "laboratory":        (str(raw["laboratory"]).strip()
                              if raw.get("laboratory") else None),
        "publication_ref":   (str(raw["publication_ref"]).strip()
                              if raw.get("publication_ref") else None),
        "uncertainty_kind":  uncertainty_kind_canonical,
    }

    # Numerics
    for field_name in ("age_ma", "age_uncertainty_ma", "latitude", "longitude"):
        record[field_name] = _cast_float(raw.get(field_name))

    # Age non-negative sanity (CHECK constraint mirror).
    if record["age_ma"] is not None and record["age_ma"] < 0:
        return None, {
            "row": row_num,
            "code": _CODE_NUMERIC_CAST,
            "reason": f"row {row_num}: age_ma {record['age_ma']} is negative",
            "raw": raw,
        }
    if record["age_uncertainty_ma"] is not None and record["age_uncertainty_ma"] < 0:
        return None, {
            "row": row_num,
            "code": _CODE_NUMERIC_CAST,
            "reason": (
                f"row {row_num}: age_uncertainty_ma "
                f"{record['age_uncertainty_ma']} is negative"
            ),
            "raw": raw,
        }

    # lat/lon → geom_wkt (POINT in EPSG:4326). Both must be present and in
    # range; otherwise the row carries no geom (NULL is fine for academic
    # records lacking spatial metadata).
    lat = record["latitude"]
    lon = record["longitude"]
    geom_wkt: str | None = None
    if lat is not None and lon is not None:
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            return None, {
                "row": row_num,
                "code": _CODE_LATLON_OUT_OF_RANGE,
                "reason": (
                    f"row {row_num}: lat/lon out of range "
                    f"(lat={lat}, lon={lon})"
                ),
                "raw": raw,
            }
        geom_wkt = f"POINT({lon} {lat})"
    record["geom_wkt"] = geom_wkt

    record["_source_row"] = row_num
    return record, None


def parse_csv_geochronology(
    source: Union[str, Path, IO[str]],
    *,
    null_values: list[str] | None = None,
) -> GeochronParseResult:
    """Parse a CSV geochronology file and return a :class:`GeochronParseResult`."""
    global_warnings: list[dict] = []
    detected_encoding = "utf-8"
    source_file_str = str(source) if isinstance(source, (str, Path)) else "<stream>"

    all_nulls = list(set(DEFAULT_NULL_VALUES + (null_values or [])))

    stream, detected_encoding, sha256_hex, _ = open_csv_with_encoding(source)
    raw_content = stream.getvalue()

    if detected_encoding.lower().replace("-", "") not in ("utf8", "utf-8", "ascii"):
        global_warnings.append({
            "row": None,
            "code": _CODE_ENCODING_NON_UTF8,
            "message": f"detected encoding {detected_encoding!r} (not UTF-8)",
            "context": {"encoding": detected_encoding},
        })

    detected_delim = detect_delimiter(raw_content, default=",")
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
            "message": f"decimal-comma transform applied to: {transformed_cols!r}",
            "context": {"columns": transformed_cols},
        })

    total_rows = len(df)
    column_map, unmapped = _build_column_map(df.columns)

    missing_required = REQUIRED_FIELDS - set(column_map.keys())
    if missing_required:
        return GeochronParseResult(
            records=[],
            total_rows=total_rows,
            valid_rows=0,
            skipped_rows=total_rows,
            unmapped_columns=unmapped,
            column_map=column_map,
            skipped_details=[{
                "row": None,
                "code": _CODE_MISSING_REQUIRED,
                "reason": (
                    f"file-level: missing required column mapping(s): "
                    f"{missing_required}"
                ),
                "raw": {},
            }],
            warnings=global_warnings,
            detected_encoding=detected_encoding,
            provenance={
                "source_file": source_file_str,
                "source_file_sha256": sha256_hex,
                "parser_name": "csv_geochronology",
                "parser_version": PARSER_VERSION,
                "source_col_map": column_map,
            },
        )

    rename_map = {v: k for k, v in column_map.items()}
    df_renamed = df.rename(rename_map)
    canonical_cols = [c for c in df_renamed.columns if c in column_map]
    df_trimmed = df_renamed.select(canonical_cols)

    records: list[dict] = []
    skipped: list[dict] = []
    for i, raw in enumerate(df_trimmed.to_dicts(), start=2):
        record, skip_entry = _validate_row(i, raw)
        if record is not None:
            records.append(record)
        else:
            logger.warning("Skipping geochron row: %s", skip_entry.get("reason"))
            skipped.append(skip_entry)

    result = GeochronParseResult(
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
            "parser_name": "csv_geochronology",
            "parser_version": PARSER_VERSION,
            "source_col_map": column_map,
        },
    )

    logger.info(
        "CSV geochronology parse complete — total: %d, valid: %d, skipped: %d, "
        "quality: %.1f%%, unmapped cols: %d",
        result.total_rows, result.valid_rows, result.skipped_rows,
        result.parse_quality_pct, len(unmapped),
    )
    return result


# Lookup table used by the Dagster asset to write silver.document_domain_tag
# rows with the right sub_type_id per row's isotopic_system. IDs come from
# migration 2026_05_24_010100_extend_data_sub_type_geochronology.
ISOTOPIC_SYSTEM_SUB_TYPE_ID: dict[str, int] = {
    "U-Pb":  211,
    "Pb-Pb": 211,
    "Ar-Ar": 212,
    "K-Ar":  212,
    "Re-Os": 213,
    "Rb-Sr": 214,
    "Sm-Nd": 215,
    "Lu-Hf": 216,
    "other": 217,
}
