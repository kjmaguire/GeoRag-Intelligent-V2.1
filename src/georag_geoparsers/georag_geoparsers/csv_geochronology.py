"""Geochronology parser — radiometric age tables into ``silver.geochronology_samples``.

Accepts a CSV (path or stream) or rows already loaded from a worksheet /
dBASE / Access table, maps the columns of common lab and publication exports
(EarthChem, GEOROC, in-house DOI tables, lab certificates), validates each
row and returns records ready for ``app/services/ingest/geochronology_writer``.

What a row needs
----------------
``sample_id`` and an ``isotopic_system`` in the table's CHECK vocabulary
(U-Pb / Pb-Pb / Ar-Ar / K-Ar / Re-Os / Rb-Sr / Sm-Nd / Lu-Hf / other). The
system comes from the system column; when that is blank or absent it is read
out of the method text (``"LA-ICP-MS U-Pb"``) — only when exactly ONE system
is named there, never guessed. A row with no recognisable system, two
different ones, a negative age or an age older than the Earth is SKIPPED
with a reason; the rest of the file still lands.

Units and uncertainty — refuse rather than guess
-------------------------------------------------
* An ``Age (Ga)`` / ``Age_ka`` column is converted to Ma. A bare ``Age``
  column is read as Ma (the universal convention) and the result carries an
  ``geochron_age_unit_assumed`` warning naming that assumption.
* ``uncertainty_kind`` comes from its own column, or from the uncertainty
  column's header (``2σ (Ma)``, ``Err_2s``, ``1sd``). When neither states it
  the value is stored with ``uncertainty_kind`` NULL and the file carries a
  ``geochron_uncertainty_kind_unstated`` warning: 1σ and 2σ differ by a
  factor of two, and picking one would silently halve or double every error
  bar.

Location
--------
Longitude/latitude columns and easting/northing columns are kept apart and
returned raw. The parser does not decide a CRS — the writer does, with the
same declared / project / assumed precedence and warnings the collar path
uses. A lon/lat pair outside +/-180 / +/-90 does NOT cost the row its age:
the location is dropped, the age is kept, and the row is listed in
``location_issues``. (The previous parser discarded the whole row, so a
typo in a latitude silently deleted a U-Pb age.)

The previous parser also matched headers exactly and case-sensitively, and
aliased ``X`` / ``Y`` to longitude/latitude — so a UTM-located table had
every row thrown out as "lat/lon out of range". Matching now goes through
``_header_match.build_column_map``, the rule every drill parser uses.
"""
from __future__ import annotations

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
from georag_geoparsers._encoding import decode_warnings
from georag_geoparsers._header_match import build_column_map

logger = logging.getLogger(__name__)

PARSER_NAME = "csv_geochronology"
PARSER_VERSION = "2.0.0"

#: The oldest age a terrestrial or meteoritic sample can carry, with slack.
#: Anything older is a unit error (years or ka written under "Ma"), and is
#: refused rather than stored as a 4.6-billion-year-plus age.
MAX_AGE_MA = 4600.0

# ---------------------------------------------------------------------------
# Column aliases — canonical -> preferred-order spellings. Matched through
# _header_match.normalize_header, so case, spaces, punctuation and a
# trailing length unit do not matter.
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict[str, list[str]] = {
    "sample_id": [
        "sample_id", "SampleID", "Sample", "Sample_No", "Sample_Number",
        "SampleName", "Sample_Name", "Specimen", "Field_ID", "Lab_ID",
    ],
    "rock_type": ["rock_type", "RockType", "Lithology", "Rock", "Host", "Rock_Name"],
    "isotopic_system": [
        "isotopic_system", "IsotopicSystem", "System", "Method_System",
        "Isotope_System", "Isotopic_Method", "Dating_System", "Decay_System",
        "Chronometer",
    ],
    "mineral_dated": [
        "mineral_dated", "Mineral", "MineralDated", "Phase", "Material",
        "Material_Dated", "Dated_Mineral",
    ],
    "age_ma": [
        "age_ma", "Age(Ma)", "Age_My", "Age_Myr", "Best_Age_Ma", "Preferred_Age_Ma",
        "Weighted_Mean_Age_Ma", "Concordia_Age_Ma", "Plateau_Age_Ma",
        "Isochron_Age_Ma", "Age", "Best_Age", "Preferred_Age",
        "Weighted_Mean_Age", "Concordia_Age", "Plateau_Age", "Isochron_Age",
    ],
    "age_ga": ["age_ga", "Age(Ga)", "Best_Age_Ga", "Age_Gyr"],
    "age_ka": ["age_ka", "Age(ka)", "Best_Age_ka", "Age_kyr"],
    "age_uncertainty_ma": [
        "age_uncertainty_ma", "AgeUncertainty", "AgeUncMa", "Uncertainty_Ma",
        "Uncertainty(Ma)", "Error_Ma", "Sigma_Ma", "2sigma_Ma", "2s_Ma",
        "2σ (Ma)", "1σ (Ma)", "1sigma_Ma", "1s_Ma", "Age_Error", "Age_Err",
        "Age_2s", "Age_2sigma", "Age_1s", "Age_1sigma", "Uncertainty",
        "Error", "Err", "2sigma", "2s", "2se", "2sd", "2σ", "1sigma", "1s",
        "1se", "1sd", "1σ", "Plus_Minus", "PM",
    ],
    "uncertainty_kind": [
        "uncertainty_kind", "UncertaintyKind", "SigmaKind", "Sigma", "ErrorKind",
        "Uncertainty_Level", "Error_Level", "Sigma_Level",
    ],
    "analytical_method": [
        "analytical_method", "AnalyticalMethod", "Method", "Technique",
        "Instrument", "Analytical_Technique", "Dating_Method",
    ],
    "laboratory": ["laboratory", "Lab", "LabName", "Lab_Name", "Facility", "Institution"],
    "publication_ref": [
        "publication_ref", "Reference", "DOI", "Citation", "Publication",
        "ReportID", "Report_ID", "Source_Reference", "Ref",
    ],
    "latitude": [
        "latitude", "Lat", "Lat_DD", "Latitude_DD", "Decimal_Latitude",
        "Lat_WGS84", "Latitude_WGS84",
    ],
    "longitude": [
        "longitude", "Lon", "Long", "Lon_DD", "Longitude_DD", "Decimal_Longitude",
        "Long_WGS84", "Longitude_WGS84",
    ],
    "easting": ["easting", "East", "X", "UTM_E", "UTM_East", "X_Coord", "UTM_X"],
    "northing": ["northing", "North", "Y", "UTM_N", "UTM_North", "Y_Coord", "UTM_Y"],
}

REQUIRED_FIELDS: frozenset[str] = frozenset({"sample_id"})

VALID_ISOTOPIC_SYSTEMS: frozenset[str] = frozenset({
    "U-Pb", "Pb-Pb", "Ar-Ar", "K-Ar", "Re-Os", "Rb-Sr", "Sm-Nd", "Lu-Hf", "other",
})

VALID_UNCERTAINTY_KINDS: frozenset[str] = frozenset({"2sigma", "1sigma", "unknown"})

#: Systems recognised inside free text ("LA-ICP-MS U-Pb zircon"). Matched on
#: the lower-cased text with every dash variant folded to "-". A text naming
#: two DIFFERENT systems is ambiguous and matches nothing.
_SYSTEM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Ar-Ar", re.compile(r"40\s*ar\s*[-/]\s*39\s*ar|\bar\s*[-/]\s*ar\b|\barar\b")),
    ("K-Ar", re.compile(r"\bk\s*[-/]\s*ar\b|\bkar\b")),
    ("Pb-Pb", re.compile(r"207\s*pb\s*[-/]\s*206\s*pb|\bpb\s*[-/]\s*pb\b|\bpbpb\b")),
    ("U-Pb", re.compile(
        r"\bu\s*[-/]\s*pb\b|\bupb\b|206\s*pb\s*[-/]\s*238\s*u|207\s*pb\s*[-/]\s*235\s*u"
    )),
    ("Re-Os", re.compile(r"\bre\s*[-/]\s*os\b|\breos\b")),
    ("Rb-Sr", re.compile(r"\brb\s*[-/]\s*sr\b|\brbsr\b")),
    ("Sm-Nd", re.compile(r"\bsm\s*[-/]\s*nd\b|\bsmnd\b")),
    ("Lu-Hf", re.compile(r"\blu\s*[-/]\s*hf\b|\bluhf\b")),
)

_DASHES = re.compile(r"[‐-―−]")

# Uncertainty-kind aliases — the column may carry "2σ", "2sd", etc.
_UNCERTAINTY_KIND_ALIASES: dict[str, str] = {
    "2sigma": "2sigma", "2σ": "2sigma", "2s": "2sigma", "2sd": "2sigma",
    "2se": "2sigma", "95%": "2sigma", "2 sigma": "2sigma",
    "1sigma": "1sigma", "1σ": "1sigma", "1s": "1sigma", "1sd": "1sigma",
    "1se": "1sigma", "68%": "1sigma", "1 sigma": "1sigma",
    "unknown": "unknown", "?": "unknown", "n/a": "unknown",
}

_HEADER_2SIGMA = re.compile(r"(?<![0-9])2\s*(σ|sigma|s\b|sd\b|se\b|s[_ )])|95\s*%", re.I)
_HEADER_1SIGMA = re.compile(r"(?<![0-9])1\s*(σ|sigma|s\b|sd\b|se\b|s[_ )])|68\s*%", re.I)

# Row / file codes
CODE_ENCODING_NON_UTF8 = "encoding_non_utf8"
CODE_MISSING_REQUIRED = "missing_required"
CODE_INVALID_ISOTOPIC = "invalid_isotopic_system"
CODE_INVALID_UNCERTAINTY = "invalid_uncertainty_kind"
CODE_NUMERIC_CAST = "numeric_cast_failed"
CODE_AGE_IMPLAUSIBLE = "age_implausible"
CODE_LATLON_OUT_OF_RANGE = "latlon_out_of_range"
CODE_DECIMAL_COMMA = "decimal_comma_detected"
CODE_AGE_UNIT_ASSUMED = "geochron_age_unit_assumed"
CODE_UNCERTAINTY_KIND_UNSTATED = "geochron_uncertainty_kind_unstated"
CODE_SYSTEM_FROM_METHOD = "geochron_system_from_method"


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
    #: Rows kept WITHOUT a location, and why (their age still landed).
    location_issues: list[dict] = field(default_factory=list)
    detected_encoding: str = "utf-8"
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def parse_quality_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return round(self.valid_rows / self.total_rows * 100, 2)


# ---------------------------------------------------------------------------
# Detection — used by ingest_tabular to route a table here
# ---------------------------------------------------------------------------

def geochronology_signal(headers: list[str]) -> str | None:
    """How strongly a header row says "radiometric age table".

    ``"strong"``: a sample id, an age AND an isotopic-system column — no
    drill table carries that combination, so it wins even over a sample
    layout (a drill-core dating table has hole_id and depths too).
    ``"weak"``: a sample id and an age, with a method column the system may
    be read from; used only when nothing else claims the table.
    ``None``: not a geochronology table.
    """
    column_map, _ = build_column_map([str(h) for h in headers if h], COLUMN_ALIASES)
    if "sample_id" not in column_map:
        return None
    if not any(k in column_map for k in ("age_ma", "age_ga", "age_ka")):
        return None
    if "isotopic_system" in column_map:
        return "strong"
    if "analytical_method" in column_map:
        return "weak"
    return None


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _cast_float(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "":
        return None
    try:
        f = float(raw)
    except (ValueError, TypeError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _systems_in_text(text: str) -> set[str]:
    folded = _DASHES.sub("-", text.lower())
    return {name for name, pattern in _SYSTEM_PATTERNS if pattern.search(folded)}


def canonical_isotopic_system(raw: Any) -> str | None:
    """The enum value *raw* names, or None when it names none (or two)."""
    key = _text(raw)
    if key is None:
        return None
    if key in VALID_ISOTOPIC_SYSTEMS:
        return key
    if key.lower() == "other":
        return "other"
    found = _systems_in_text(key)
    return found.pop() if len(found) == 1 else None


def _canonical_uncertainty_kind(raw: Any) -> str | None:
    key = _text(raw)
    if key is None:
        return None
    if key in VALID_UNCERTAINTY_KINDS:
        return key
    return _UNCERTAINTY_KIND_ALIASES.get(key.lower())


def uncertainty_kind_from_header(header: str | None) -> str | None:
    """'2sigma' / '1sigma' when the uncertainty column's own name says so."""
    if not header:
        return None
    if _HEADER_2SIGMA.search(header):
        return "2sigma"
    if _HEADER_1SIGMA.search(header):
        return "1sigma"
    return None


def _age_header_has_unit(header: str) -> bool:
    return bool(re.search(r"(ma|my|myr|ga|gyr|ka|kyr)\W*$", header.strip(), re.I))


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------

def _skip(row_num: int, code: str, reason: str, raw: dict) -> tuple[None, dict]:
    return None, {"row": row_num, "code": code, "reason": reason, "raw": raw}


def _validate_row(
    row_num: int,
    raw: dict,
    *,
    header_uncertainty_kind: str | None,
) -> tuple[dict | None, dict | None]:
    """Validate one row (keys already canonical). ``(record, None)`` or ``(None, skip)``."""
    sample_id = _text(raw.get("sample_id"))
    if sample_id is None:
        return _skip(row_num, CODE_MISSING_REQUIRED,
                     f"row {row_num}: missing required field 'sample_id'", raw)

    notes: list[str] = []
    isotopic = canonical_isotopic_system(raw.get("isotopic_system"))
    if isotopic is None and _text(raw.get("isotopic_system")) is None:
        method_text = " ".join(
            t for t in (_text(raw.get("analytical_method")), _text(raw.get("mineral_dated"))) if t
        )
        found = _systems_in_text(method_text) if method_text else set()
        if len(found) == 1:
            isotopic = found.pop()
            notes.append(CODE_SYSTEM_FROM_METHOD)
    if isotopic is None:
        return _skip(
            row_num, CODE_INVALID_ISOTOPIC,
            f"row {row_num}: isotopic system {raw.get('isotopic_system')!r} is not one "
            f"of {sorted(VALID_ISOTOPIC_SYSTEMS)} and none could be read from the "
            f"method ({raw.get('analytical_method')!r})",
            raw,
        )

    uncertainty_kind: str | None = None
    raw_uk = _text(raw.get("uncertainty_kind"))
    if raw_uk is not None:
        uncertainty_kind = _canonical_uncertainty_kind(raw_uk)
        if uncertainty_kind is None:
            return _skip(
                row_num, CODE_INVALID_UNCERTAINTY,
                f"row {row_num}: uncertainty_kind {raw_uk!r} not in "
                f"{sorted(VALID_UNCERTAINTY_KINDS)}",
                raw,
            )

    # Age, in Ma. Ma column first; Ga / ka converted when that is all there is.
    age_ma = _cast_float(raw.get("age_ma"))
    if age_ma is None:
        ga = _cast_float(raw.get("age_ga"))
        ka = _cast_float(raw.get("age_ka"))
        age_ma = ga * 1000.0 if ga is not None else (ka / 1000.0 if ka is not None else None)
    for name in ("age_ma", "age_ga", "age_ka", "age_uncertainty_ma"):
        if _text(raw.get(name)) is not None and _cast_float(raw.get(name)) is None:
            return _skip(row_num, CODE_NUMERIC_CAST,
                         f"row {row_num}: {name} {raw.get(name)!r} is not a number", raw)
    if age_ma is not None and age_ma < 0:
        return _skip(row_num, CODE_NUMERIC_CAST,
                     f"row {row_num}: age {age_ma} Ma is negative", raw)
    if age_ma is not None and age_ma > MAX_AGE_MA:
        return _skip(
            row_num, CODE_AGE_IMPLAUSIBLE,
            f"row {row_num}: age {age_ma} Ma is older than the Earth — almost "
            f"certainly a unit error (years or ka under an Ma header); not stored",
            raw,
        )
    unc = _cast_float(raw.get("age_uncertainty_ma"))
    if unc is not None and unc < 0:
        return _skip(row_num, CODE_NUMERIC_CAST,
                     f"row {row_num}: age uncertainty {unc} is negative", raw)
    if unc is not None and uncertainty_kind is None:
        uncertainty_kind = header_uncertainty_kind

    record: dict[str, Any] = {
        "sample_id": sample_id,
        "rock_type": _text(raw.get("rock_type")),
        "isotopic_system": isotopic,
        "mineral_dated": _text(raw.get("mineral_dated")),
        "age_ma": age_ma,
        "age_uncertainty_ma": unc,
        "uncertainty_kind": uncertainty_kind,
        "analytical_method": _text(raw.get("analytical_method")),
        "laboratory": _text(raw.get("laboratory")),
        "publication_ref": _text(raw.get("publication_ref")),
        "latitude": _cast_float(raw.get("latitude")),
        "longitude": _cast_float(raw.get("longitude")),
        "easting": _cast_float(raw.get("easting")),
        "northing": _cast_float(raw.get("northing")),
        "geom_wkt": None,
        "location_issue": None,
        "notes": notes,
        "_source_row": row_num,
    }

    lat, lon = record["latitude"], record["longitude"]
    if lat is not None and lon is not None:
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            record["geom_wkt"] = f"POINT({lon} {lat})"
        else:
            record["latitude"] = record["longitude"] = None
            record["location_issue"] = (
                f"row {row_num}: latitude/longitude ({lat}, {lon}) is out of range; "
                f"the age was kept without a location"
            )
    return record, None


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def parse_geochronology_rows(
    rows: list[dict[str, Any]],
    *,
    columns: list[str] | None = None,
    source_label: str = "<rows>",
    first_row_number: int = 2,
    warnings: list[dict] | None = None,
    provenance: dict[str, Any] | None = None,
    detected_encoding: str = "utf-8",
) -> GeochronParseResult:
    """Validate rows already loaded from a table (header row = 1).

    ``columns`` is the header in file order; defaults to the first row's keys.
    """
    global_warnings = list(warnings or [])
    if columns is None:
        columns = list(rows[0].keys()) if rows else []
    column_map, unmapped = build_column_map([str(c) for c in columns], COLUMN_ALIASES)
    prov = {
        "source_file": source_label,
        "parser_name": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "source_col_map": column_map,
        **(provenance or {}),
    }

    missing = REQUIRED_FIELDS - set(column_map)
    if missing or not any(k in column_map for k in ("isotopic_system", "analytical_method")):
        need = sorted(missing) or ["isotopic_system (or a method column naming it)"]
        return GeochronParseResult(
            records=[], total_rows=len(rows), valid_rows=0, skipped_rows=len(rows),
            unmapped_columns=unmapped, column_map=column_map,
            skipped_details=[{
                "row": None, "code": CODE_MISSING_REQUIRED,
                "reason": f"file-level: missing required column(s): {need}",
                "raw": {},
            }],
            warnings=global_warnings, detected_encoding=detected_encoding,
            provenance=prov,
        )

    age_col = column_map.get("age_ma")
    if age_col is not None and not _age_header_has_unit(str(age_col)):
        global_warnings.append({
            "row": None,
            "code": CODE_AGE_UNIT_ASSUMED,
            "message": f"{source_label}: ages in column '{age_col}' were read as Ma",
            "detail": (
                f"The age column '{age_col}' names no unit, so its values were "
                f"stored as millions of years (Ma), the usual convention. If they "
                f"are Ga or ka, rename the column (e.g. 'Age_Ga') and re-upload — "
                f"re-uploading replaces these rows."
            ),
            "context": {"column": age_col},
        })

    unc_col = column_map.get("age_uncertainty_ma")
    header_kind = uncertainty_kind_from_header(unc_col)

    records: list[dict] = []
    skipped: list[dict] = []
    location_issues: list[dict] = []
    for i, source_row in enumerate(rows, start=first_row_number):
        raw = {canonical: source_row.get(col) for canonical, col in column_map.items()}
        record, skip_entry = _validate_row(i, raw, header_uncertainty_kind=header_kind)
        if record is not None:
            if record["location_issue"]:
                location_issues.append({
                    "row": i, "code": CODE_LATLON_OUT_OF_RANGE,
                    "reason": record["location_issue"],
                })
            records.append(record)
        elif skip_entry is not None:
            # The stable CODE only — the free-text reason interpolates raw
            # cell values, which must not reach the application log.
            logger.warning("Skipping geochron row %s: %s",
                           skip_entry.get("row"), skip_entry.get("code"))
            skipped.append(skip_entry)

    unstated = [
        r["_source_row"] for r in records
        if r["age_uncertainty_ma"] is not None and r["uncertainty_kind"] is None
    ]
    if unstated:
        global_warnings.append({
            "row": None,
            "code": CODE_UNCERTAINTY_KIND_UNSTATED,
            "message": (
                f"{source_label}: {len(unstated)} age uncertainty value(s) do not "
                f"say whether they are 1σ or 2σ"
            ),
            "detail": (
                f"Neither a sigma-level column nor the uncertainty column's name "
                f"('{unc_col}') says whether the errors are 1σ or 2σ, so they were "
                f"stored with the level UNSTATED rather than guessed — the two "
                f"differ by a factor of two. Add a column such as 'Sigma' (1s/2s) "
                f"or name the column 'Error_2s' and re-upload."
            ),
            "context": {"column": unc_col, "rows": unstated[:20]},
        })
    from_method = sum(1 for r in records if CODE_SYSTEM_FROM_METHOD in r["notes"])
    if from_method:
        global_warnings.append({
            "row": None,
            "code": CODE_SYSTEM_FROM_METHOD,
            "message": (
                f"{source_label}: the isotopic system of {from_method} row(s) was "
                f"read from the method text"
            ),
            "detail": (
                "These rows had no isotopic-system value; exactly one system was "
                "named in their method/mineral text (e.g. 'LA-ICP-MS U-Pb') and "
                "that was used. Rows naming none, or two, were skipped."
            ),
        })

    result = GeochronParseResult(
        records=records,
        total_rows=len(rows),
        valid_rows=len(records),
        skipped_rows=len(skipped),
        unmapped_columns=unmapped,
        column_map=column_map,
        skipped_details=skipped,
        warnings=global_warnings,
        location_issues=location_issues,
        detected_encoding=detected_encoding,
        provenance=prov,
    )
    logger.info(
        "Geochronology parse complete — total: %d, valid: %d, skipped: %d, "
        "unmapped cols: %d",
        result.total_rows, result.valid_rows, result.skipped_rows, len(unmapped),
    )
    return result


def parse_csv_geochronology(
    source: Union[str, Path, IO[str]],  # noqa: UP007
    *,
    null_values: list[str] | None = None,
    source_label: str | None = None,
) -> GeochronParseResult:
    """Parse a CSV geochronology file and return a :class:`GeochronParseResult`.

    ``source_label`` names the file in warnings (default: the path) — the
    workflow passes the upload's name rather than its temp-dir path.
    """
    global_warnings: list[dict] = []
    source_file_str = source_label or (
        str(source) if isinstance(source, (str, Path)) else "<stream>"
    )
    all_nulls = list(set(DEFAULT_NULL_VALUES + (null_values or [])))

    stream, detected_encoding, sha256_hex, _ = open_csv_with_encoding(source)
    raw_content = stream.getvalue()

    global_warnings.extend(decode_warnings(detected_encoding, raw_content))

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
            "code": CODE_DECIMAL_COMMA,
            "message": f"decimal-comma transform applied to: {transformed_cols!r}",
            "context": {"columns": transformed_cols},
        })

    return parse_geochronology_rows(
        df.to_dicts(),
        columns=list(df.columns),
        source_label=source_file_str,
        warnings=global_warnings,
        detected_encoding=detected_encoding,
        provenance={"source_file_sha256": sha256_hex},
    )


# Lookup table for silver.document_domain_tag sub_type ids per isotopic
# system. IDs come from migration 2026_05_24_010100_extend_data_sub_type_
# geochronology. Not written by the ingest path today (see the writer).
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
