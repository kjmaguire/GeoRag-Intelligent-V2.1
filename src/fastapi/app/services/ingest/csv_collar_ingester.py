"""CSV drillhole-collar ingester -- NO PRODUCTION CALLERS as of 2026-08-21.

.. warning::

   ``ingest_csv_collar_file`` is not called from anywhere in ``app/``. A
   repo-wide grep finds only this module's own definition, its ``__all__``
   entry, and a comment in ingest_zip_archive.py explaining the removal.
   The only exercise it gets is tests/test_csv_collar_ingester.py.

   The ``.csv`` branch of ``ingest_zip_archive._ingest_one`` used to call it
   unconditionally. That branch now routes .csv/.tsv/.xlsx to
   ``ingest_tabular`` instead, because this ingester handles COLLARS ONLY
   and requires hole_id/easting/northing: zip a hole's full dataset --
   collars.csv, survey.csv, lithology.csv, assays.csv -- and only
   collars.csv landed. The other three returned
   ``skipped_reason="missing_required_columns"``, which increments
   ``counts["skipped"]`` rather than ``counts["errors"]``, so the archive
   was still marked completed and reported four files succeeded.
   ``ingest_tabular`` classifies the header and routes to silver.collars,
   silver.surveys, silver.lithology_logs or silver.samples as appropriate,
   via the ``georag_geoparsers`` parsers -- a different code path from this
   module, so nothing here is reached on that route either.

   KEPT rather than deleted pending a decision: Laravel's
   ``UploadController`` still 422s ``category=collar``/``category=assay``
   uploads, and if that direct-upload path is restored this module is the
   obvious thing to wire it to. If that is not the plan, delete this file
   and its two test modules rather than leaving 745 lines that look live.

Original docstring follows.

CSV drillhole-collar ingester for the ``ingest_zip_archive`` workflow.

Restores a CSV ingestion path after the Dagster retirement
(2026-07-28 — see MEMORY.md "Kestra retirement" / dagster-drop era)
left plain ``.csv`` collar/assay files with nowhere to go. Laravel's
``UploadController`` hard-rejects ``category=collar``/``category=assay``
uploads with a 422 ("retired with the Dagster services"); the one live,
end-to-end wired ingestion surface remaining in the app is the ZIP
archive path (Laravel's ``DrillUploadController`` dispatches
``category=archive, ext=zip`` to ``ingest_zip_archive.py``, and
``Components/DataImportWizard.tsx`` exposes ``.zip`` in its picker).
This module gives that workflow's per-file dispatcher (``_ingest_one``)
a ``.csv`` branch, so a geologist can zip up plain CSV collar files
alongside LAS/LOG/TIFF/XLSX/PDF and have them land in
``silver.collars`` like everything else in the archive.

Provenance of the parsing logic
--------------------------------
Column-alias mapping, delimiter auto-detection, decimal-comma
normalisation, dip sign-convention detection, and hole_id
canonicalization are PORTED from the dormant
``src/dagster/georag_dagster/parsers/csv_collar.py`` (+ its
``_csv_io.py`` / ``_dip_convention.py`` / ``_hole_id.py`` helpers) —
this module does NOT import from ``src/dagster`` directly. That
package no longer imports cleanly (unrelated modules are broken since
the Dagster drop) and its two hard dependencies for this logic,
``polars`` and ``rapidfuzz``, were deliberately removed from the
FastAPI image on 2026-07-28 (see the ``polars: REMOVED`` comment in
``pyproject.toml`` — the same relocation pattern used for the PDF
parser, moving code OUT of the dead Dagster tree instead of reviving
Dagster). This module re-implements the same behaviour with the
stdlib ``csv`` module in place of Polars, and drops the rapidfuzz-only
hole_id *collision-suggestion* feature (purely informational in the
original) while keeping ``canonicalize()`` itself, which has no
rapidfuzz dependency and is required for ``hole_id_canonical``.

Expected CSV shape (document this for users / the upload UI)
--------------------------------------------------------------
Header row required. Delimiter is auto-detected: comma, semicolon,
tab, or pipe. Encoding is auto-detected via ``charset-normalizer``
(falls back to UTF-8). European decimal-comma numbers (``"1,5"``) are
auto-transformed to ``"1.5"`` on a per-column basis when every sampled
value in that column matches the pattern.

Column name matching is case-sensitive against the alias lists below
(same alias set the Dagster parser used) — e.g. any of ``HoleID`` /
``Hole_ID`` / ``HOLEID`` / ``DrillHole`` / ``DH_ID`` / ``BH_ID`` maps
to the canonical ``hole_id`` field.

    canonical      accepted header aliases                  required
    -----------    ---------------------------------------  --------
    hole_id        HoleID, Hole_ID, HOLEID, DrillHole,          yes
                   DH_ID, BH_ID
    easting        Easting, EAST, X, UTM_E                      yes
    northing       Northing, NORTH, Y, UTM_N                     yes
    elevation      Elevation, ELEV, RL, Z                        yes
    total_depth    TotalDepth, Total_Depth, DEPTH, TD,           no
                   MaxDepth
    azimuth        Azimuth, AZI, AZ                              no
    dip            Dip, DIP, Inclination                         no
    hole_type      HoleType, Type, DrillType                     no
    drill_date     Date, DrillDate, StartDate                    no
    status         Status                                        no

easting/northing are interpreted in the owning project's CRS
(``silver.projects.crs_epsg``, defaulting to EPSG:32613 / UTM Zone 13N
when unset) and transformed to EPSG:32613 for ``silver.collars.geom``
— mirrors how ``las_ingester`` / ``cameco_log_ingester`` populate the
same column. elevation must be in metres, range [-500, 8900]. dip sign
convention (down-positive vs down-negative) is auto-detected across
the file and normalised to the DB's down-negative convention
(``dip BETWEEN -90 AND 0``). total_depth is technically optional in
the CSV (many collar-only exports omit it) but ``silver.collars`` has
a ``NOT NULL`` + ``total_depth > 0`` constraint, so a missing/invalid
value is floored to 0.01 m rather than dropping the row — the same
precedent ``cameco_log_ingester.upsert_collar_from_log`` uses for
depth-less ``.log`` headers.

Rows failing required-field presence, numeric casting, or range
checks are skipped — logged + counted, never aborting the file. Rows
that pass validation but hit a genuine DB error (e.g. two raw hole_id
spellings canonicalizing to the same value, tripping the
``uq_collars_project_hole_canonical`` partial unique index) are
likewise caught per-row via a nested transaction (asyncpg emits a
``SAVEPOINT`` for a transaction opened while already inside one) so a
single bad row can't take down the rest of the file.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import asyncpg

log = logging.getLogger("georag.ingest.csv_collar")

PARSER_NAME = "csv_collar_fastapi"
PARSER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Column aliasing (ported from georag_dagster.parsers.csv_collar)
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict[str, list[str]] = {
    "hole_id":     ["HoleID", "Hole_ID", "HOLEID", "hole_id", "DrillHole", "DH_ID", "BH_ID"],
    "easting":     ["Easting", "EAST", "X", "UTM_E", "easting"],
    "northing":    ["Northing", "NORTH", "Y", "UTM_N", "northing"],
    "elevation":   ["Elevation", "ELEV", "RL", "Z", "elevation"],
    "total_depth": ["TotalDepth", "Total_Depth", "DEPTH", "TD", "MaxDepth", "total_depth"],
    "azimuth":     ["Azimuth", "AZI", "AZ", "azimuth"],
    "dip":         ["Dip", "DIP", "Inclination", "dip"],
    "hole_type":   ["HoleType", "Type", "DrillType", "hole_type"],
    "drill_date":  ["Date", "DrillDate", "StartDate", "drill_date"],
    "status":      ["Status", "status"],
}

REQUIRED_FIELDS: frozenset[str] = frozenset({"hole_id", "easting", "northing", "elevation"})
NUMERIC_FIELDS: frozenset[str] = frozenset(
    {"easting", "northing", "elevation", "total_depth", "azimuth", "dip"}
)

RANGE_CHECKS: dict[str, tuple[float, float]] = {
    "easting":     (100_000.0,  900_000.0),
    "northing":    (0.0,        10_000_000.0),
    "elevation":   (-500.0,     8_900.0),
    "total_depth": (0.0,        10_000.0),
    "azimuth":     (0.0,        360.0),
    "dip":         (-90.0,      90.0),
}

# Null-token vocabulary — ported from georag_dagster.parsers._csv_io.DEFAULT_NULL_VALUES.
_NULL_TOKENS: frozenset[str] = frozenset(
    {
        "-", "N/A", "NULL", "null", "n/a", "na", "NA", "NONE", "none", "",
        "<DL", "<LOD", "BDL", "bdl", "N.A.", "ND", "nd",
    }
)

_CODE_MISSING_REQUIRED = "missing_required"
_CODE_NUMERIC_CAST = "numeric_cast_failed"
_CODE_RANGE = "range_check_failed"

_DEFAULT_CRS_EPSG = 32613  # UTM Zone 13N — same project default as las_ingester.
# Mirrors cameco_log_ingester.upsert_collar_from_log's 0.01 m floor: better
# than dropping a collar-only CSV row entirely when total_depth is absent.
_TOTAL_DEPTH_FLOOR_M = 0.01


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
@dataclass
class CSVCollarIngestResult:
    """Outcome of ingesting one CSV file."""

    file_path: str
    total_rows: int
    valid_rows: int
    skipped_rows: int
    collar_ids: list[str] = field(default_factory=list)
    row_errors: list[dict[str, Any]] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)
    dip_convention: str = "down_negative"
    detected_delimiter: str = ","
    detected_encoding: str = "utf-8"
    skipped: bool = False
    skipped_reason: str | None = None

    @property
    def parse_quality_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return round(self.valid_rows / self.total_rows * 100, 2)


# ---------------------------------------------------------------------------
# Encoding + delimiter detection (ported from
# georag_dagster.parsers._encoding / _csv_io — stdlib + charset-normalizer
# only, no polars)
# ---------------------------------------------------------------------------
_ENCODING_CONFIDENCE_THRESHOLD = 0.5


def _detect_encoding(data: bytes) -> str:
    try:
        from charset_normalizer import from_bytes
    except ImportError:  # pragma: no cover — charset-normalizer is a
        # transitive FastAPI dependency (requests); defensive fallback only.
        return "utf-8"

    best = from_bytes(data).best()
    if best is None:
        return "utf-8"
    confidence = 1.0 - best.chaos
    if confidence < _ENCODING_CONFIDENCE_THRESHOLD:
        return "utf-8"
    return best.encoding


_DELIMITER_CANDIDATES: tuple[str, ...] = (",", ";", "\t", "|")
_DETECT_LINE_LIMIT = 5


def _detect_delimiter(content: str, default: str = ",") -> str:
    """Pick the most likely CSV delimiter — see module docstring.

    Ported verbatim (algorithm-for-algorithm) from
    ``georag_dagster.parsers._csv_io.detect_delimiter``.
    """
    lines = [ln for ln in content.splitlines() if ln.strip()][:_DETECT_LINE_LIMIT]
    if not lines:
        return default

    best_delim = default
    best_total = 0
    best_variance = float("inf")

    for delim in _DELIMITER_CANDIDATES:
        counts = [ln.count(delim) for ln in lines]
        if max(counts) == 0:
            continue
        total = sum(counts)
        mean = total / len(counts)
        variance = sum((c - mean) ** 2 for c in counts) / len(counts)

        if total > best_total or (total == best_total and variance < best_variance):
            best_delim = delim
            best_total = total
            best_variance = variance

    return best_delim


# ---------------------------------------------------------------------------
# Decimal-comma transform — row-dict variant of
# georag_dagster.parsers._csv_io.transform_decimal_comma (Polars DataFrame
# version). Same matching rules, operating on list[dict[str, str | None]]
# instead of a DataFrame since polars isn't available here.
# ---------------------------------------------------------------------------
_DECIMAL_COMMA_RE = re.compile(r"^-?\d+,\d+$")
_PLAIN_INT_RE = re.compile(r"^-?\d+$")


def _transform_decimal_comma_rows(
    rows: list[dict[str, str | None]], columns: list[str],
) -> list[str]:
    """Rewrite comma-decimal values ("1,5" -> "1.5") in-place for any
    column in *columns* where every non-null sampled value matches the
    decimal-comma or plain-integer pattern. Returns the transformed
    column names.
    """
    transformed: list[str] = []
    for col in columns:
        values = [r.get(col) for r in rows]
        non_null = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
        if not non_null:
            continue

        all_match = True
        has_comma_decimal = False
        for s in non_null:
            if "." in s:
                all_match = False
                break
            if _DECIMAL_COMMA_RE.match(s):
                has_comma_decimal = True
                continue
            if _PLAIN_INT_RE.match(s):
                continue
            all_match = False
            break

        if all_match and has_comma_decimal:
            for r in rows:
                v = r.get(col)
                if v is not None:
                    r[col] = str(v).replace(",", ".")
            transformed.append(col)

    return transformed


# ---------------------------------------------------------------------------
# Dip sign-convention detection (ported from
# georag_dagster.parsers._dip_convention — pure stdlib)
# ---------------------------------------------------------------------------
DipConvention = Literal["down_negative", "down_positive", "ambiguous"]

_DIP_MINIMUM_SAMPLES = 5
_DIP_MAJORITY_THRESHOLD = 0.80


def detect_dip_convention(dips: list[float]) -> DipConvention:
    valid = [d for d in dips if d is not None]
    if len(valid) < _DIP_MINIMUM_SAMPLES:
        return "down_negative"

    neg = sum(1 for d in valid if -90.0 <= d <= 0.0)
    pos = sum(1 for d in valid if 0.0 <= d <= 90.0)
    total = len(valid)

    if neg / total >= _DIP_MAJORITY_THRESHOLD:
        return "down_negative"
    if pos / total >= _DIP_MAJORITY_THRESHOLD:
        return "down_positive"
    return "ambiguous"


def normalize_dip(value: float, source_convention: DipConvention) -> float:
    if source_convention == "down_positive":
        return -value
    return value


# ---------------------------------------------------------------------------
# Hole ID canonicalization (ported from georag_dagster.parsers._hole_id —
# only the rapidfuzz-free `canonicalize` half; fuzzy_match/suggest_collisions
# needed rapidfuzz, which isn't installed in the FastAPI image, and are
# purely informational in the original — not required for correctness here)
# ---------------------------------------------------------------------------
_SEP_RE = re.compile(r"[ \-_./]+")


def canonicalize(hole_id: str | None) -> str | None:
    if hole_id is None:
        return None
    stripped = str(hole_id).strip()
    if not stripped:
        return None
    no_seps = _SEP_RE.sub("", stripped)
    if not no_seps:
        return None
    return no_seps.upper()


# ---------------------------------------------------------------------------
# Column mapping + row validation
# ---------------------------------------------------------------------------
def _build_column_map(csv_columns: list[str]) -> tuple[dict[str, str], list[str]]:
    csv_col_set = set(csv_columns)
    column_map: dict[str, str] = {}

    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in csv_col_set:
                column_map[canonical] = alias
                break

    matched_csv_cols = set(column_map.values())
    unmapped = [c for c in csv_columns if c not in matched_csv_cols]
    return column_map, unmapped


def _cast_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _parse_date(value: str | None) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y%m%d", "%d-%b-%Y"):
        try:
            return (
                date.fromisoformat(raw)
                if fmt == "%Y-%m-%d"
                else datetime.strptime(raw, fmt).date()
            )
        except ValueError:
            continue
    return None


def _validate_row(
    row_num: int,
    raw: dict[str, str | None],
    column_map: dict[str, str],
    dip_convention: DipConvention,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate one raw row (keyed by CSV column names).

    Returns (record, None) on success, keyed by canonical field names,
    or (None, error_entry) on failure.
    """
    canonical_raw: dict[str, str | None] = {
        canonical: raw.get(csv_col) for canonical, csv_col in column_map.items()
    }

    for req in REQUIRED_FIELDS:
        val = canonical_raw.get(req)
        if val is None or str(val).strip() == "":
            return None, {
                "row": row_num,
                "code": _CODE_MISSING_REQUIRED,
                "reason": f"row {row_num}: missing required field '{req}'",
            }

    record: dict[str, Any] = {}
    for canonical, raw_val in canonical_raw.items():
        if canonical in NUMERIC_FIELDS:
            casted = _cast_float(raw_val)
            if casted is None and canonical in REQUIRED_FIELDS:
                return None, {
                    "row": row_num,
                    "code": _CODE_NUMERIC_CAST,
                    "reason": (
                        f"row {row_num}: cannot cast required numeric field "
                        f"'{canonical}' value {raw_val!r}"
                    ),
                }
            record[canonical] = casted
        elif canonical == "drill_date":
            record[canonical] = _parse_date(raw_val)
        else:
            record[canonical] = str(raw_val).strip() if raw_val is not None else None

    if record.get("dip") is not None and dip_convention == "down_positive":
        record["dip"] = normalize_dip(record["dip"], dip_convention)

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
            }

    record["hole_id_canonical"] = canonicalize(record.get("hole_id"))
    return record, None


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------
async def _get_project_crs_epsg(conn: asyncpg.Connection, project_id: str) -> int:
    row = await conn.fetchrow(
        "SELECT crs_epsg FROM silver.projects WHERE project_id = $1::uuid",
        project_id,
    )
    if row and row["crs_epsg"]:
        return int(row["crs_epsg"])
    return _DEFAULT_CRS_EPSG


async def _upsert_collar(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    workspace_id: str,
    crs_epsg: int,
    record: dict[str, Any],
) -> str:
    """Insert-or-update one `silver.collars` row from a validated CSV record.

    Conflict target mirrors las_ingester / cameco_log_ingester's
    (project_id, hole_id) unique constraint. georef_method='declared'
    lets the `trg_derive_collar_spatial_uncertainty` trigger (Strategy B,
    2026-07-02) auto-populate spatial_uncertainty_m — a CSV-declared
    coordinate is exactly the 'declared' case the trigger models.
    """
    hole_id = record["hole_id"]
    hole_id_canonical = record.get("hole_id_canonical") or canonicalize(hole_id)
    total_depth = record.get("total_depth")
    if not total_depth or total_depth <= 0:
        total_depth = _TOTAL_DEPTH_FLOOR_M

    row = await conn.fetchrow(
        """
        INSERT INTO silver.collars
            (collar_id, hole_id, hole_id_canonical, project_id, workspace_id,
             easting, northing, elevation, total_depth, hole_type, status,
             azimuth, dip, drill_date, georef_method,
             geom, geom_4326, created_at, updated_at)
        VALUES (
            gen_random_uuid(), $1, $2, $3::uuid, $4::uuid,
            $5, $6, $7, $8, $9, $10,
            $11, $12, $13, 'declared',
            ST_Transform(ST_SetSRID(ST_MakePoint($5, $6), $14::int), 32613),
            ST_Transform(ST_SetSRID(ST_MakePoint($5, $6), $14::int), 4326),
            NOW(), NOW()
        )
        ON CONFLICT (project_id, hole_id) DO UPDATE SET
            hole_id_canonical = EXCLUDED.hole_id_canonical,
            easting            = EXCLUDED.easting,
            northing           = EXCLUDED.northing,
            elevation          = EXCLUDED.elevation,
            total_depth        = EXCLUDED.total_depth,
            hole_type          = EXCLUDED.hole_type,
            status             = EXCLUDED.status,
            azimuth            = EXCLUDED.azimuth,
            dip                = EXCLUDED.dip,
            drill_date         = EXCLUDED.drill_date,
            georef_method      = EXCLUDED.georef_method,
            geom               = EXCLUDED.geom,
            geom_4326          = EXCLUDED.geom_4326,
            updated_at         = NOW()
        RETURNING collar_id::text AS collar_id
        """,
        hole_id, hole_id_canonical, project_id, workspace_id,
        record["easting"], record["northing"], record.get("elevation"),
        total_depth, record.get("hole_type") or "exploration", record.get("status") or "active",
        record.get("azimuth"), record.get("dip"), record.get("drill_date"),
        crs_epsg,
    )
    return row["collar_id"]


async def _emit_provenance(
    conn: asyncpg.Connection,
    *,
    target_id: str,
    source_file: str,
    source_sha256: str,
    ingest_run_id: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO bronze.provenance
            (provenance_id, target_schema, target_table, target_id,
             source_file, source_file_sha256,
             parser_name, parser_version, ingested_at, ingest_run_id)
        VALUES (gen_random_uuid(), 'silver', 'collars', $1::uuid,
                $2, $3, $4, $5, NOW(), $6)
        """,
        target_id, source_file, source_sha256,
        PARSER_NAME, PARSER_VERSION,
        asyncpg.pgproto.types.UUID(ingest_run_id) if ingest_run_id else None,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def ingest_csv_collar_file(
    conn: asyncpg.Connection,
    csv_path: str,
    *,
    workspace_id: str,
    project_id: str,
    ingest_run_id: str | None = None,
) -> CSVCollarIngestResult:
    """Ingest one CSV collar file into `silver.collars` + `bronze.provenance`.

    Args:
        conn: asyncpg connection. Caller (``ingest_zip_archive._ingest_one``)
            wraps the call in ``async with conn.transaction():`` exactly like
            the LAS/XLSX branches — per-row DB writes below open a *nested*
            transaction each, which asyncpg implements as a SAVEPOINT when
            already inside a transaction, so one bad row rolls back only
            itself.
        csv_path: path to the CSV file on disk (already extracted from the
            ZIP archive).
        workspace_id: silver.workspaces UUID for RLS/tenancy scoping —
            written directly onto every row, same as las_ingester /
            cameco_log_ingester.
        project_id: silver.projects UUID. Always supplied by the caller
            (``IngestZipArchiveInput.project_id``) — unlike las_ingester
            there is no get-or-create-by-name path here, matching how the
            .log/.tif/.xlsx/.pdf branches in ``_ingest_one`` already use
            ``input.project_id`` directly.
        ingest_run_id: optional bronze.provenance.ingest_run_id link.

    Returns:
        CSVCollarIngestResult. ``skipped=True`` means the file produced
        zero usable rows (unreadable, no header, or missing required
        column mappings) — the caller should treat that like the LAS/XLSX
        branches' ``result.skipped`` (bump counts["skipped"], not
        counts["csv"]).
    """
    p = Path(csv_path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        return CSVCollarIngestResult(
            file_path=csv_path, total_rows=0, valid_rows=0, skipped_rows=0,
            skipped=True, skipped_reason=f"read_failed:{type(e).__name__}",
        )

    sha = hashlib.sha256(raw).hexdigest()
    encoding = _detect_encoding(raw)
    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
        encoding = "utf-8"

    delimiter = _detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        return CSVCollarIngestResult(
            file_path=csv_path, total_rows=0, valid_rows=0, skipped_rows=0,
            skipped=True, skipped_reason="empty_or_no_header",
            detected_delimiter=delimiter, detected_encoding=encoding,
        )

    # Strip a leading BOM / stray whitespace from header names so "﻿HoleID"
    # (common from Excel "CSV UTF-8" exports) still matches the alias list.
    csv_columns = [(c or "").strip().lstrip("﻿") for c in reader.fieldnames]
    rows_raw: list[dict[str, str | None]] = []
    for row in reader:
        normalized = {
            (k or "").strip().lstrip("﻿"): v for k, v in row.items() if k is not None
        }
        rows_raw.append(normalized)
    total_rows = len(rows_raw)

    column_map, unmapped = _build_column_map(csv_columns)
    missing_required = REQUIRED_FIELDS - set(column_map.keys())
    if missing_required:
        log.warning(
            "csv_collar_ingester: %s missing required column mapping(s) %s "
            "(mapped: %s)",
            csv_path, sorted(missing_required), column_map,
        )
        return CSVCollarIngestResult(
            file_path=csv_path, total_rows=total_rows, valid_rows=0,
            skipped_rows=total_rows, skipped=True,
            skipped_reason="missing_required_columns",
            unmapped_columns=unmapped,
            detected_delimiter=delimiter, detected_encoding=encoding,
            row_errors=[{
                "row": None,
                "code": _CODE_MISSING_REQUIRED,
                "reason": f"missing required column mapping(s): {sorted(missing_required)}",
            }],
        )

    # Null-token normalization (DEFAULT_NULL_VALUES-equivalent).
    for r in rows_raw:
        for k, v in list(r.items()):
            if v is not None and str(v).strip() in _NULL_TOKENS:
                r[k] = None

    # Decimal-comma transform — only on columns we actually map to a
    # numeric canonical field, so hole_id / status / free-text columns are
    # never touched.
    numeric_csv_cols = [column_map[c] for c in NUMERIC_FIELDS if c in column_map]
    transformed_cols = _transform_decimal_comma_rows(rows_raw, numeric_csv_cols)
    if transformed_cols:
        log.info(
            "csv_collar_ingester: %s decimal-comma transform applied to %s",
            csv_path, transformed_cols,
        )

    dip_convention: DipConvention = "down_negative"
    if "dip" in column_map:
        dip_col = column_map["dip"]
        numeric_dips = [_cast_float(r.get(dip_col)) for r in rows_raw]
        numeric_dips = [d for d in numeric_dips if d is not None]
        dip_convention = detect_dip_convention(numeric_dips)
        if dip_convention != "down_negative":
            log.info(
                "csv_collar_ingester: %s dip convention detected as %s",
                csv_path, dip_convention,
            )

    crs_epsg = await _get_project_crs_epsg(conn, project_id)

    collar_ids: list[str] = []
    row_errors: list[dict[str, Any]] = []
    valid_rows = 0
    skipped_rows = 0

    for i, raw_row in enumerate(rows_raw, start=2):  # row 1 = header
        record, err = _validate_row(i, raw_row, column_map, dip_convention)
        if record is None:
            skipped_rows += 1
            row_errors.append(err)
            log.debug("csv_collar_ingester: %s row %d skipped — %s", csv_path, i, err["reason"])
            continue

        try:
            async with conn.transaction():  # nested -> SAVEPOINT
                collar_id = await _upsert_collar(
                    conn,
                    project_id=project_id,
                    workspace_id=workspace_id,
                    crs_epsg=crs_epsg,
                    record=record,
                )
        except Exception as exc:  # noqa: BLE001 — per-row containment is the point
            skipped_rows += 1
            row_errors.append({
                "row": i,
                "code": "db_write_failed",
                "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
            })
            log.warning(
                "csv_collar_ingester: %s row %d DB write failed — %s (continuing)",
                csv_path, i, exc,
            )
            continue

        collar_ids.append(collar_id)
        valid_rows += 1
        try:
            await _emit_provenance(
                conn,
                target_id=collar_id,
                source_file=str(p)[:1000],
                source_sha256=sha,
                ingest_run_id=ingest_run_id,
            )
        except Exception as exc:  # noqa: BLE001 — provenance is best-effort
            log.warning("csv_collar_ingester.provenance_emit_failed err=%s", exc)

    if row_errors:
        log.info(
            "csv_collar_ingester: %s complete — total=%d valid=%d skipped=%d",
            csv_path, total_rows, valid_rows, skipped_rows,
        )

    return CSVCollarIngestResult(
        file_path=csv_path,
        total_rows=total_rows,
        valid_rows=valid_rows,
        skipped_rows=skipped_rows,
        collar_ids=collar_ids,
        row_errors=row_errors,
        unmapped_columns=unmapped,
        dip_convention=dip_convention,
        detected_delimiter=delimiter,
        detected_encoding=encoding,
        skipped=(valid_rows == 0),
        skipped_reason=None if valid_rows > 0 else "no_valid_rows",
    )


__all__ = [
    "ingest_csv_collar_file",
    "CSVCollarIngestResult",
    "canonicalize",
    "detect_dip_convention",
    "normalize_dip",
    "COLUMN_ALIASES",
    "REQUIRED_FIELDS",
]
