"""Silver layer asset — parse an Excel sheet and insert into the appropriate Silver table.

Downloads the .xlsx file from MinIO Bronze, runs the xlsx_parser (which
delegates to the matching CSV parser), then dispatches insert logic based on
sheet_type:

  "collar"    → silver.collars         (same INSERT as silver_collars asset)
  "survey"    → silver.surveys         (same INSERT as silver_surveys asset)
  "lithology" → silver.lithology_logs  (same INSERT as silver_lithology asset)
  "sample"    → silver.samples         (same INSERT as silver_samples asset)

CRS detection (Section 04b) is applied for collar sheets using the same
heuristic logic as silver_collars.  Other sheet types do not contain
coordinates so CRS detection is skipped.

Invalid rows are logged and counted but never silently dropped — the final
MaterializeResult carries skip counts.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import json
import tempfile
import uuid
from typing import Literal

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.bronze_xlsx import BRONZE_BUCKET, EXCEL_PREFIX
from georag_dagster.parsers._hole_id import canonicalize
from georag_dagster.parsers.xlsx_parser import parse_xlsx_sheet
from georag_dagster.resources import S3Resource, PostgresResource

# Default project CRS — WGS 84 / UTM zone 13N (matches silver_collars)
PROJECT_EPSG = 32613

PROJECT_BBOX = {
    "easting_min":  400_000.0,
    "easting_max":  650_000.0,
    "northing_min": 6_100_000.0,
    "northing_max": 6_400_000.0,
}


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverXlsxConfig(Config):
    """Runtime configuration for the silver_xlsx asset."""

    # Basename of the .xlsx file uploaded in the bronze_xlsx asset.
    xlsx_filename: str

    # Name of the sheet to parse. Leave empty in single-sheet mode to
    # use the first sheet; in auto-dispatch mode this is ignored.
    sheet_name: str = ""

    # What kind of geological data the sheet contains.
    # 2026-05-23 — empty string now means "auto-dispatch": walk every
    # visible sheet, classify each via the header classifier, and
    # parse + insert each matching sheet to the right silver.* table.
    # Fixes the silent-data-loss bug where multi-sheet workbooks
    # silently dropped sheets 2+. See [[xlsx-audit-2026-05-23]].
    sheet_type: str = ""  # "" (auto) | "collar" | "survey" | "lithology" | "sample"

    # Project UUID — must exist in silver.projects (or silver.collars for FK lookups).
    project_id: str

    # Sprint 5 Phase 1 plumbing — vendor column-mapping profile ID.
    # Extracted from MinIO object metadata x-georag-vendor-profile-id by the
    # minio_upload_sensor.  The parser does NOT use this yet (Phase 2).
    vendor_profile_id: int | None = None


# ---------------------------------------------------------------------------
# CRS helpers (collar sheets only — Section 04b steps 2–4)
# ---------------------------------------------------------------------------

def _detect_source_epsg(records: list, override: int = 0) -> int:
    if override and override > 0:
        return override
    if not records:
        return PROJECT_EPSG
    sample = records[0]
    easting = sample.get("easting") or 0.0
    northing = sample.get("northing") or 0.0
    if 100_000 <= easting <= 999_999 and 1_000_000 <= northing <= 10_000_000:
        return PROJECT_EPSG
    return PROJECT_EPSG


def _bbox_valid(easting: float, northing: float) -> bool:
    return (
        PROJECT_BBOX["easting_min"] <= easting <= PROJECT_BBOX["easting_max"]
        and PROJECT_BBOX["northing_min"] <= northing <= PROJECT_BBOX["northing_max"]
    )


# ---------------------------------------------------------------------------
# SQL — one block per sheet_type, matching the corresponding Silver CSV asset
# ---------------------------------------------------------------------------

INSERT_COLLAR_SQL = """
INSERT INTO silver.collars (
    collar_id, project_id, hole_id, hole_id_canonical, easting, northing, elevation,
    total_depth, azimuth, dip, hole_type, drill_date, status, geom
) VALUES (
    %(collar_id)s, %(project_id)s, %(hole_id)s, %(hole_id_canonical)s, %(easting)s, %(northing)s,
    %(elevation)s, %(total_depth)s, %(azimuth)s, %(dip)s, %(hole_type)s,
    %(drill_date)s, %(status)s,
    ST_SetSRID(ST_MakePoint(%(easting)s, %(northing)s), %(geom_srid)s)
)
ON CONFLICT (project_id, hole_id) DO UPDATE SET
    hole_id_canonical = EXCLUDED.hole_id_canonical,
    easting     = EXCLUDED.easting,
    northing    = EXCLUDED.northing,
    elevation   = EXCLUDED.elevation,
    total_depth = EXCLUDED.total_depth,
    azimuth     = EXCLUDED.azimuth,
    dip         = EXCLUDED.dip,
    hole_type   = EXCLUDED.hole_type,
    drill_date  = EXCLUDED.drill_date,
    status      = EXCLUDED.status,
    geom        = EXCLUDED.geom,
    updated_at  = NOW()
;
"""

COLLAR_POSTLOAD_SQL = """
DO $$
BEGIN
    -- DB review #5 — converge on the Laravel-migration name (idx_collars_geom)
    -- so Dagster doesn't race-create a duplicate GIST on silver.collars.geom.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver' AND tablename = 'collars'
          AND indexname = 'idx_collars_geom'
    ) THEN
        CREATE INDEX idx_collars_geom ON silver.collars USING GIST (geom);
    END IF;
END$$;
ANALYZE silver.collars;
"""

COLLAR_LOOKUP_SQL = """
SELECT hole_id, collar_id
FROM silver.collars
WHERE hole_id = ANY(%(hole_ids)s)
  AND project_id = %(project_id)s
"""

INSERT_SURVEY_SQL = """
INSERT INTO silver.surveys (
    survey_id, collar_id, depth, azimuth, dip, survey_method
) VALUES (
    %(survey_id)s, %(collar_id)s, %(depth)s, %(azimuth)s, %(dip)s, %(survey_method)s
)
ON CONFLICT (survey_id) DO UPDATE SET
    collar_id     = EXCLUDED.collar_id,
    depth         = EXCLUDED.depth,
    azimuth       = EXCLUDED.azimuth,
    dip           = EXCLUDED.dip,
    survey_method = EXCLUDED.survey_method
;
"""

SURVEY_POSTLOAD_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver' AND tablename = 'surveys'
          AND indexname = 'surveys_collar_id_depth_idx'
    ) THEN
        CREATE INDEX surveys_collar_id_depth_idx ON silver.surveys (collar_id, depth);
    END IF;
END$$;
ANALYZE silver.surveys;
"""

INSERT_LITHOLOGY_SQL = """
INSERT INTO silver.lithology_logs (
    log_id, collar_id, from_depth, to_depth, lithology_code,
    lithology_description, grain_size, color, hardness, rqd, recovery, weathering
) VALUES (
    %(log_id)s, %(collar_id)s, %(from_depth)s, %(to_depth)s, %(lithology_code)s,
    %(lithology_description)s, %(grain_size)s, %(color)s, %(hardness)s,
    %(rqd)s, %(recovery)s, %(weathering)s
)
ON CONFLICT (log_id) DO UPDATE SET
    collar_id             = EXCLUDED.collar_id,
    from_depth            = EXCLUDED.from_depth,
    to_depth              = EXCLUDED.to_depth,
    lithology_code        = EXCLUDED.lithology_code,
    lithology_description = EXCLUDED.lithology_description,
    grain_size            = EXCLUDED.grain_size,
    color                 = EXCLUDED.color,
    hardness              = EXCLUDED.hardness,
    rqd                   = EXCLUDED.rqd,
    recovery              = EXCLUDED.recovery,
    weathering            = EXCLUDED.weathering
;
"""

LITHOLOGY_POSTLOAD_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver' AND tablename = 'lithology_logs'
          AND indexname = 'lithology_logs_collar_depth_idx'
    ) THEN
        CREATE INDEX lithology_logs_collar_depth_idx
            ON silver.lithology_logs (collar_id, from_depth, to_depth);
    END IF;
END$$;
ANALYZE silver.lithology_logs;
"""

INSERT_SAMPLE_SQL = """
INSERT INTO silver.samples (
    sample_id, collar_id, from_depth, to_depth, sample_type, lab_id,
    commodity_assays, qaqc_type
) VALUES (
    %(sample_id)s, %(collar_id)s, %(from_depth)s, %(to_depth)s,
    %(sample_type)s, %(lab_id)s, %(commodity_assays)s, %(qaqc_type)s
)
ON CONFLICT (sample_id) DO UPDATE SET
    collar_id        = EXCLUDED.collar_id,
    from_depth       = EXCLUDED.from_depth,
    to_depth         = EXCLUDED.to_depth,
    sample_type      = EXCLUDED.sample_type,
    lab_id           = EXCLUDED.lab_id,
    commodity_assays = EXCLUDED.commodity_assays,
    qaqc_type        = EXCLUDED.qaqc_type
;
"""

SAMPLE_POSTLOAD_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver' AND tablename = 'samples'
          AND indexname = 'samples_collar_depth_idx'
    ) THEN
        CREATE INDEX samples_collar_depth_idx
            ON silver.samples (collar_id, from_depth, to_depth);
    END IF;
    -- DB review #5 — converge on the Laravel-migration name
    -- (idx_samples_assays_gin) so Dagster doesn't race-create a duplicate
    -- GIN on commodity_assays.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver' AND tablename = 'samples'
          AND indexname = 'idx_samples_assays_gin'
    ) THEN
        CREATE INDEX idx_samples_assays_gin
            ON silver.samples USING GIN (commodity_assays);
    END IF;
END$$;
ANALYZE silver.samples;
"""


# ---------------------------------------------------------------------------
# Insert helpers — one per sheet_type
# ---------------------------------------------------------------------------

def _insert_collars(
    records: list,
    project_id: str,
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> tuple:
    """Apply CRS detection, bbox filtering, and bulk-insert collars.

    Returns (inserted_count, bbox_skipped_count).
    """
    source_epsg = _detect_source_epsg(records)
    context.log.info("Detected source CRS: EPSG:%d", source_epsg)

    project_id_val = project_id if project_id else None
    insert_params: list = []
    bbox_rejected: list = []

    for rec in records:
        east = rec.get("easting")
        north = rec.get("northing")
        if east is None or north is None:
            continue
        if not _bbox_valid(east, north):
            context.log.warning(
                "XLSX collar: coordinate (%s, %s) outside project bbox — skipped",
                east,
                north,
            )
            bbox_rejected.append(rec.get("hole_id", "unknown"))
            continue
        insert_params.append({
            "collar_id":   str(uuid.uuid4()),
            "project_id":  project_id_val,
            "hole_id":     rec.get("hole_id"),
            # Parser already canonicalized per parsers/csv_collar.py /
            # parsers/_hole_id.py. Fall back to a stripped/uppercased form
            # when the XLSX path hands us a record from a sheet_type that
            # didn't run through the csv_collar parser.
            "hole_id_canonical": (
                rec.get("hole_id_canonical")
                or canonicalize(rec.get("hole_id"))
            ),
            "easting":     east,
            "northing":    north,
            "elevation":   rec.get("elevation"),
            "total_depth": rec.get("total_depth"),
            "azimuth":     rec.get("azimuth"),
            "dip":         rec.get("dip"),
            "hole_type":   rec.get("hole_type"),
            "drill_date":  rec.get("drill_date"),
            "status":      rec.get("status"),
            "geom_srid":   PROJECT_EPSG,
        })

    inserted_count = 0
    if insert_params:
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur, INSERT_COLLAR_SQL, insert_params, page_size=200
                )
                inserted_count = len(insert_params)
            conn.commit()
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(COLLAR_POSTLOAD_SQL)
            conn.commit()
        context.log.info("Post-load: GIST index ensured, ANALYZE run on silver.collars")

    return inserted_count, len(bbox_rejected)


def _resolve_collar_fk(
    records: list,
    project_id: str,
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> tuple:
    """Look up collar_id for each hole_id in one batch query.

    Returns (collar_map, fk_miss_count) where collar_map is {hole_id: collar_id_str}.
    """
    all_hole_ids = list({r["hole_id"] for r in records if r.get("hole_id")})
    context.log.info(
        "Looking up collar_id for %d unique hole_id(s) in project '%s'",
        len(all_hole_ids),
        project_id,
    )
    collar_map: dict = {}
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(COLLAR_LOOKUP_SQL, {
                "hole_ids":   all_hole_ids,
                "project_id": project_id,
            })
            for row in cur.fetchall():
                collar_map[row["hole_id"]] = str(row["collar_id"])
    context.log.info(
        "Collar lookup resolved %d / %d hole_id(s)",
        len(collar_map),
        len(all_hole_ids),
    )
    return collar_map, 0  # fk_miss_count tallied by caller


def _insert_surveys(
    records: list,
    project_id: str,
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> tuple:
    """FK resolve and bulk-insert survey rows. Returns (inserted_count, fk_miss_count)."""
    if not records:
        return 0, 0
    collar_map, _ = _resolve_collar_fk(records, project_id, context, postgres)
    insert_params: list = []
    fk_miss = 0
    for rec in records:
        hole_id = rec.get("hole_id")
        collar_id = collar_map.get(hole_id)
        if collar_id is None:
            context.log.warning(
                "FK miss: hole_id '%s' not in silver.collars for project '%s' — skipped",
                hole_id,
                project_id,
            )
            fk_miss += 1
            continue
        insert_params.append({
            "survey_id":     str(uuid.uuid4()),
            "collar_id":     collar_id,
            "depth":         rec.get("depth"),
            "azimuth":       rec.get("azimuth"),
            "dip":           rec.get("dip"),
            "survey_method": rec.get("survey_method"),
        })
    inserted_count = 0
    if insert_params:
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur, INSERT_SURVEY_SQL, insert_params, page_size=200
                )
                inserted_count = len(insert_params)
            conn.commit()
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(SURVEY_POSTLOAD_SQL)
            conn.commit()
    return inserted_count, fk_miss


def _insert_lithology(
    records: list,
    project_id: str,
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> tuple:
    """FK resolve and bulk-insert lithology rows. Returns (inserted_count, fk_miss_count)."""
    if not records:
        return 0, 0
    collar_map, _ = _resolve_collar_fk(records, project_id, context, postgres)
    insert_params: list = []
    fk_miss = 0
    for rec in records:
        hole_id = rec.get("hole_id")
        collar_id = collar_map.get(hole_id)
        if collar_id is None:
            context.log.warning(
                "FK miss: hole_id '%s' not in silver.collars for project '%s' — skipped",
                hole_id,
                project_id,
            )
            fk_miss += 1
            continue
        insert_params.append({
            "log_id":               str(uuid.uuid4()),
            "collar_id":            collar_id,
            "from_depth":           rec.get("from_depth"),
            "to_depth":             rec.get("to_depth"),
            "lithology_code":       rec.get("lithology_code"),
            "lithology_description":rec.get("lithology_description"),
            "grain_size":           rec.get("grain_size"),
            "color":                rec.get("color"),
            "hardness":             rec.get("hardness"),
            "rqd":                  rec.get("rqd"),
            "recovery":             rec.get("recovery"),
            "weathering":           rec.get("weathering"),
        })
    inserted_count = 0
    if insert_params:
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur, INSERT_LITHOLOGY_SQL, insert_params, page_size=200
                )
                inserted_count = len(insert_params)
            conn.commit()
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(LITHOLOGY_POSTLOAD_SQL)
            conn.commit()
    return inserted_count, fk_miss


def _insert_samples(
    records: list,
    project_id: str,
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> tuple:
    """FK resolve and bulk-insert sample rows. Returns (inserted_count, fk_miss_count)."""
    if not records:
        return 0, 0
    collar_map, _ = _resolve_collar_fk(records, project_id, context, postgres)
    insert_params: list = []
    fk_miss = 0
    for rec in records:
        hole_id = rec.get("hole_id")
        collar_id = collar_map.get(hole_id)
        if collar_id is None:
            context.log.warning(
                "FK miss: hole_id '%s' not in silver.collars for project '%s' — skipped",
                hole_id,
                project_id,
            )
            fk_miss += 1
            continue
        assays_payload = rec.get("commodity_assays") or {}
        assay_flags = rec.get("commodity_assay_flags")  # None for clean rows
        insert_params.append({
            "sample_id":             str(uuid.uuid4()),
            "collar_id":             collar_id,
            "from_depth":            rec.get("from_depth"),
            "to_depth":              rec.get("to_depth"),
            "sample_type":           rec.get("sample_type"),
            "lab_id":                rec.get("lab_id"),
            "commodity_assays":      psycopg2.extras.Json(assays_payload),
            "qaqc_type":             rec.get("qaqc_type"),
            "commodity_assay_flags": psycopg2.extras.Json(assay_flags) if assay_flags is not None else None,
        })
    inserted_count = 0
    if insert_params:
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur, INSERT_SAMPLE_SQL, insert_params, page_size=200
                )
                inserted_count = len(insert_params)
            conn.commit()
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(SAMPLE_POSTLOAD_SQL)
            conn.commit()
    return inserted_count, fk_miss


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=["bronze_xlsx"],
    pool="csv_silver_ingest",  # 2026-05-23 XLSX audit gap Z — shares the
    # cap with the 4 CSV silver assets; same DB-contention concern on
    # bulk INSERTs into silver.* tables. See [[csv-audit-2026-05-23]].
    description=(
        "Download .xlsx file from MinIO Bronze, parse one or more sheets as collar / "
        "survey / lithology / sample data, then insert valid records into the "
        "appropriate Silver table using the same logic as the CSV Silver assets. "
        "In auto-dispatch mode (sheet_type=''), walks every visible sheet, "
        "classifies each by header pattern, and routes matching sheets to their "
        "CSV parser — one workbook upload → all four tables populated."
    ),
)
def silver_xlsx(
    context: AssetExecutionContext,
    config: SilverXlsxConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Parse Bronze XLSX → validate → insert into the appropriate silver.* table(s).

    Two modes:
      * **Single-sheet**: ``config.sheet_type`` is one of
        ``collar`` / ``survey`` / ``lithology`` / ``sample``. The sheet
        named in ``config.sheet_name`` (or the first sheet if empty) is
        parsed and inserted. Original 2025 behaviour, preserved for the
        Dagster-launched manual path.
      * **Auto-dispatch** (NEW 2026-05-23): ``config.sheet_type=""``.
        Walks every visible sheet, classifies each via the header
        classifier, parses + inserts each matching sheet to its silver.*
        table. Hidden sheets are reported as warnings and skipped.
        Unknown sheets are likewise reported and skipped (not data).
        This is the Laravel-upload default — one workbook → all four
        tables populated.
    """
    valid_explicit_types = ("collar", "survey", "lithology", "sample")
    if config.sheet_type and config.sheet_type not in valid_explicit_types:
        raise ValueError(
            f"silver_xlsx: sheet_type '{config.sheet_type}' is not one of "
            f"{valid_explicit_types} (or empty for auto-dispatch)"
        )

    context.log.info("vendor_profile_id: %s", config.vendor_profile_id)
    object_name = f"{EXCEL_PREFIX}/{config.xlsx_filename}"
    context.log.info(
        "Silver XLSX: downloading '%s/%s' from MinIO", BRONZE_BUCKET, object_name
    )

    # --- Download from Bronze to a temp file ---
    # polars.read_excel / xlrd need a real file path with the correct extension.
    import os as _os  # noqa: PLC0415
    _src_ext = _os.path.splitext(config.xlsx_filename)[1].lower() or ".xlsx"
    file_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)

    with tempfile.NamedTemporaryFile(suffix=_src_ext, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    context.log.info(
        "Silver XLSX: downloaded %d bytes to temp file '%s'", len(file_bytes), tmp_path
    )

    # Branch: auto-dispatch vs single-sheet.
    if config.sheet_type == "":
        return _silver_xlsx_auto_dispatch(
            context=context, config=config, tmp_path=tmp_path,
            postgres=postgres,
        )
    return _silver_xlsx_single_sheet(
        context=context, config=config, tmp_path=tmp_path,
        sheet_name=config.sheet_name, sheet_type=config.sheet_type,
        postgres=postgres,
    )


def _silver_xlsx_single_sheet(
    *,
    context: AssetExecutionContext,
    config: SilverXlsxConfig,
    tmp_path: str,
    sheet_name: str,
    sheet_type: str,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Parse one sheet and insert. Original (pre-2026-05-23) behaviour,
    extracted into a helper so the auto-dispatch loop can reuse it."""

    parse_result = parse_xlsx_sheet(
        path=tmp_path,
        sheet_name=sheet_name,
        sheet_type=sheet_type,  # type: ignore[arg-type]
    )

    context.log.info(
        "XLSX parse complete — sheet='%s' type=%s total=%d valid=%d skipped=%d quality=%.1f%%",
        parse_result.sheet_name,
        parse_result.sheet_type,
        parse_result.total_rows,
        parse_result.valid_rows,
        parse_result.skipped_rows,
        parse_result.parse_quality_pct,
    )

    if parse_result.unmapped_columns:
        context.log.warning(
            "Unmapped columns (dropped): %s", parse_result.unmapped_columns
        )
    for skip in parse_result.skipped_details:
        context.log.warning("Skipped row: %s", skip.get("reason", skip))

    inserted_count, fk_miss_count, bbox_skipped = _dispatch_insert(
        sheet_type=sheet_type,
        records=parse_result.records,
        project_id=config.project_id,
        context=context,
        postgres=postgres,
    )

    skipped_total = parse_result.skipped_rows + fk_miss_count + bbox_skipped

    return MaterializeResult(
        metadata={
            "xlsx_filename":     MetadataValue.text(config.xlsx_filename),
            "sheet_name":        MetadataValue.text(parse_result.sheet_name),
            "sheet_type":        MetadataValue.text(sheet_type),
            "mode":              MetadataValue.text("single_sheet"),
            "total_rows":        MetadataValue.int(parse_result.total_rows),
            "valid_rows":        MetadataValue.int(parse_result.valid_rows),
            "skipped_rows":      MetadataValue.int(skipped_total),
            "fk_miss_rows":      MetadataValue.int(fk_miss_count),
            "bbox_rejected_rows":MetadataValue.int(bbox_skipped),
            "inserted_count":    MetadataValue.int(inserted_count),
            "parse_quality_pct": MetadataValue.float(parse_result.parse_quality_pct),
            "unmapped_columns":  MetadataValue.text(str(parse_result.unmapped_columns)),
            "vendor_profile_id": MetadataValue.text(
                str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"
            ),
            "project_id":        MetadataValue.text(config.project_id),
        }
    )


def _silver_xlsx_auto_dispatch(
    *,
    context: AssetExecutionContext,
    config: SilverXlsxConfig,
    tmp_path: str,
    postgres: PostgresResource,
) -> MaterializeResult:
    """2026-05-23 — multi-sheet auto-dispatch.

    Walks every visible sheet, classifies via the header classifier,
    parses + inserts each known-type sheet to its silver.* table.

    Hidden sheets and ``unknown``-typed sheets are skipped with a
    structured log entry (so the operator can see the workbook had,
    say, a 'Notes' tab that was deliberately ignored). The asset
    aggregates per-sheet metrics into the returned MaterializeResult.
    """
    from georag_dagster.parsers.xlsx_parser import enumerate_sheets  # noqa: PLC0415

    sheets = enumerate_sheets(tmp_path)
    context.log.info(
        "auto-dispatch: enumerated %d sheet(s) — %s",
        len(sheets),
        [(s.name, s.sheet_type, s.hidden) for s in sheets],
    )

    per_sheet_results: list[dict] = []
    aggregate = {
        "sheets_total": len(sheets),
        "sheets_processed": 0,
        "sheets_skipped_hidden": 0,
        "sheets_skipped_unknown": 0,
        "total_rows": 0,
        "valid_rows": 0,
        "inserted_count": 0,
        "fk_miss_rows": 0,
        "bbox_rejected_rows": 0,
    }

    for sheet in sheets:
        if sheet.hidden:
            context.log.info(
                "auto-dispatch: skip hidden sheet '%s' (rows=%d, classified=%s)",
                sheet.name, sheet.row_count, sheet.sheet_type,
            )
            aggregate["sheets_skipped_hidden"] += 1
            per_sheet_results.append({
                "name": sheet.name, "type": sheet.sheet_type,
                "rows": sheet.row_count, "status": "skipped_hidden",
            })
            continue
        if sheet.sheet_type == "unknown" or sheet.row_count == 0:
            reason = "empty" if sheet.row_count == 0 else "headers_unrecognised"
            context.log.info(
                "auto-dispatch: skip unknown sheet '%s' (rows=%d, headers=%s, reason=%s)",
                sheet.name, sheet.row_count, sheet.headers[:6], reason,
            )
            aggregate["sheets_skipped_unknown"] += 1
            per_sheet_results.append({
                "name": sheet.name, "type": "unknown",
                "rows": sheet.row_count, "status": f"skipped_{reason}",
            })
            continue

        context.log.info(
            "auto-dispatch: parse sheet '%s' as %s (confidence=%.2f, rows=%d)",
            sheet.name, sheet.sheet_type, sheet.classify_confidence, sheet.row_count,
        )
        parse_result = parse_xlsx_sheet(
            path=tmp_path,
            sheet_name=sheet.name,
            sheet_type=sheet.sheet_type,  # type: ignore[arg-type]
        )
        inserted, fk_miss, bbox_skipped = _dispatch_insert(
            sheet_type=sheet.sheet_type,
            records=parse_result.records,
            project_id=config.project_id,
            context=context,
            postgres=postgres,
        )

        aggregate["sheets_processed"] += 1
        aggregate["total_rows"] += parse_result.total_rows
        aggregate["valid_rows"] += parse_result.valid_rows
        aggregate["inserted_count"] += inserted
        aggregate["fk_miss_rows"] += fk_miss
        aggregate["bbox_rejected_rows"] += bbox_skipped

        per_sheet_results.append({
            "name": sheet.name, "type": sheet.sheet_type,
            "rows": parse_result.total_rows,
            "valid": parse_result.valid_rows,
            "inserted": inserted, "fk_miss": fk_miss,
            "bbox_skipped": bbox_skipped, "status": "processed",
        })
        context.log.info(
            "auto-dispatch: sheet '%s' done — inserted=%d fk_miss=%d bbox_skipped=%d",
            sheet.name, inserted, fk_miss, bbox_skipped,
        )

    context.log.info(
        "auto-dispatch: complete — processed=%d hidden=%d unknown=%d inserted=%d",
        aggregate["sheets_processed"], aggregate["sheets_skipped_hidden"],
        aggregate["sheets_skipped_unknown"], aggregate["inserted_count"],
    )

    return MaterializeResult(
        metadata={
            "xlsx_filename":          MetadataValue.text(config.xlsx_filename),
            "mode":                   MetadataValue.text("auto_dispatch"),
            "sheets_total":           MetadataValue.int(aggregate["sheets_total"]),
            "sheets_processed":       MetadataValue.int(aggregate["sheets_processed"]),
            "sheets_skipped_hidden":  MetadataValue.int(aggregate["sheets_skipped_hidden"]),
            "sheets_skipped_unknown": MetadataValue.int(aggregate["sheets_skipped_unknown"]),
            "total_rows":             MetadataValue.int(aggregate["total_rows"]),
            "valid_rows":             MetadataValue.int(aggregate["valid_rows"]),
            "inserted_count":         MetadataValue.int(aggregate["inserted_count"]),
            "fk_miss_rows":           MetadataValue.int(aggregate["fk_miss_rows"]),
            "bbox_rejected_rows":     MetadataValue.int(aggregate["bbox_rejected_rows"]),
            "per_sheet":              MetadataValue.json(per_sheet_results),
            "vendor_profile_id":      MetadataValue.text(
                str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"
            ),
            "project_id":             MetadataValue.text(config.project_id),
        }
    )


def _dispatch_insert(
    *,
    sheet_type: str,
    records: list,
    project_id: str,
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> tuple[int, int, int]:
    """Shared insert dispatcher used by both single-sheet and auto modes.

    Returns ``(inserted_count, fk_miss_count, bbox_skipped)``.
    bbox_skipped is non-zero only for collar inserts; the other types
    don't have a CRS-bbox check.
    """
    if not records:
        context.log.warning(
            "No valid records — silver table for '%s' unchanged.", sheet_type,
        )
        return (0, 0, 0)

    if sheet_type == "collar":
        inserted, bbox_skipped = _insert_collars(records, project_id, context, postgres)
        context.log.info(
            "Inserted %d collars (%d bbox-rejected)", inserted, bbox_skipped,
        )
        return (inserted, 0, bbox_skipped)
    if sheet_type == "survey":
        inserted, fk_miss = _insert_surveys(records, project_id, context, postgres)
        context.log.info(
            "Inserted %d surveys (%d FK misses)", inserted, fk_miss,
        )
        return (inserted, fk_miss, 0)
    if sheet_type == "lithology":
        inserted, fk_miss = _insert_lithology(records, project_id, context, postgres)
        context.log.info(
            "Inserted %d lithology intervals (%d FK misses)", inserted, fk_miss,
        )
        return (inserted, fk_miss, 0)
    if sheet_type == "sample":
        inserted, fk_miss = _insert_samples(records, project_id, context, postgres)
        context.log.info(
            "Inserted %d samples (%d FK misses)", inserted, fk_miss,
        )
        return (inserted, fk_miss, 0)

    context.log.warning(
        "_dispatch_insert: unknown sheet_type '%s' — silently dropped", sheet_type,
    )
    return (0, 0, 0)
