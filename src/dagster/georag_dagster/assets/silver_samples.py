"""Silver layer asset — parse, validate, and insert geochemical sample data into PostGIS.

Reads the raw CSV from the MinIO Bronze bucket, runs it through the CSV sample
parser, resolves hole_id → collar_id FK via silver.collars lookup, and bulk-
inserts valid records into silver.samples. The commodity_assays dict is stored as
JSONB using psycopg2.extras.Json for proper serialisation.

Invalid rows and FK misses are logged and counted but never silently dropped —
the final MaterializeResult carries skip counts.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import uuid
from io import StringIO

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.bronze_samples import BRONZE_BUCKET, SAMPLES_PREFIX
from georag_dagster.clients.review_queue_writer import (
    build_review_queue_row,
    write_review_queue_rows,
)
from georag_dagster.parsers.csv_sample import parse_csv_samples
from georag_dagster.resources import S3Resource, PostgresResource

PROVENANCE_INSERT_SQL = """
INSERT INTO bronze.provenance (
    target_schema, target_table, target_id,
    source_file, source_file_sha256, source_row, source_col_map,
    parser_name, parser_version, ingest_run_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING;
"""


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverSamplesConfig(Config):
    """Runtime configuration for the silver_samples asset."""

    # The filename (basename only) of the CSV that was uploaded in the Bronze asset.
    # Example: "sample_samples.csv"
    csv_filename: str

    # Project ID to scope the collar lookup. Must exist in silver.collars.
    project_id: str

    # Sprint 5 Phase 1 plumbing — vendor column-mapping profile ID.
    # Extracted from MinIO object metadata x-georag-vendor-profile-id by the
    # minio_upload_sensor.  The parser does NOT use this yet (Phase 2).
    vendor_profile_id: int | None = None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

COLLAR_LOOKUP_SQL = """
SELECT hole_id, collar_id
FROM silver.collars
WHERE hole_id = ANY(%(hole_ids)s)
  AND project_id = %(project_id)s
"""

INSERT_SAMPLE_SQL = """
INSERT INTO silver.samples (
    sample_id,
    collar_id,
    from_depth,
    to_depth,
    sample_type,
    lab_id,
    commodity_assays,
    qaqc_type
) VALUES (
    %(sample_id)s,
    %(collar_id)s,
    %(from_depth)s,
    %(to_depth)s,
    %(sample_type)s,
    %(lab_id)s,
    %(commodity_assays)s,
    %(qaqc_type)s
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

POSTLOAD_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'samples'
          AND indexname  = 'samples_collar_depth_idx'
    ) THEN
        CREATE INDEX samples_collar_depth_idx
            ON silver.samples (collar_id, from_depth, to_depth);
    END IF;
    -- GIN index on commodity_assays JSONB for key/value lookups.
    -- DB review #5 — converge on the Laravel-migration name
    -- (idx_samples_assays_gin) so Dagster doesn't race-create a duplicate
    -- GIN on commodity_assays.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'samples'
          AND indexname  = 'idx_samples_assays_gin'
    ) THEN
        CREATE INDEX idx_samples_assays_gin
            ON silver.samples USING GIN (commodity_assays);
    END IF;
END$$;

ANALYZE silver.samples;
"""


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=["bronze_samples"],
    pool="csv_silver_ingest",  # 2026-05-23 CSV audit gap #3 — see silver.py
    description=(
        "Download raw geochemical sample CSV from MinIO Bronze, parse and validate it, "
        "resolve hole_id to collar_id FK, then insert valid records into silver.samples "
        "with commodity_assays stored as JSONB."
    ),
)
def silver_samples(
    context: AssetExecutionContext,
    config: SilverSamplesConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Parse Bronze sample CSV → validate → FK resolve → bulk insert into silver.samples."""

    context.log.info("vendor_profile_id: %s", config.vendor_profile_id)
    object_name = f"{SAMPLES_PREFIX}/{config.csv_filename}"
    context.log.info("Silver: downloading '%s/%s' from MinIO", BRONZE_BUCKET, object_name)

    # --- Download from Bronze ---
    raw_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)
    csv_text = raw_bytes.decode("utf-8", errors="replace")

    # --- Parse ---
    parse_result = parse_csv_samples(StringIO(csv_text))

    context.log.info(
        "Parse complete — total: %d, valid: %d, skipped: %d, quality: %.1f%%, assay cols: %s",
        parse_result.total_rows,
        parse_result.valid_rows,
        parse_result.skipped_rows,
        parse_result.parse_quality_pct,
        parse_result.assay_columns,
    )

    if parse_result.unmapped_columns:
        context.log.warning(
            "Unmapped CSV columns (dropped): %s",
            parse_result.unmapped_columns,
        )

    for skip in parse_result.skipped_details:
        context.log.warning("Skipped row: %s", skip.get("reason", skip))

    if not parse_result.records:
        context.log.warning("No valid records from parser — silver.samples unchanged.")
        return MaterializeResult(
            metadata={
                "total_rows":        MetadataValue.int(parse_result.total_rows),
                "valid_rows":        MetadataValue.int(0),
                "skipped_rows":      MetadataValue.int(parse_result.total_rows),
                "fk_miss_rows":      MetadataValue.int(0),
                "inserted_count":    MetadataValue.int(0),
                "parse_quality_pct": MetadataValue.float(parse_result.parse_quality_pct),
                "assay_columns":     MetadataValue.text(str(parse_result.assay_columns)),
                "csv_filename":      MetadataValue.text(config.csv_filename),
            }
        )

    # --- Collar FK lookup: batch query for all hole_ids in one round-trip ---
    all_hole_ids = list({r["hole_id"] for r in parse_result.records if r.get("hole_id")})
    context.log.info(
        "Looking up collar_id for %d unique hole_id(s) in project '%s'",
        len(all_hole_ids),
        config.project_id,
    )

    collar_map: dict = {}
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(COLLAR_LOOKUP_SQL, {
                "hole_ids":   all_hole_ids,
                "project_id": config.project_id,
            })
            for row in cur.fetchall():
                collar_map[row["hole_id"]] = str(row["collar_id"])

    context.log.info(
        "Collar lookup resolved %d / %d hole_id(s)",
        len(collar_map),
        len(all_hole_ids),
    )

    # --- Build insert params, log FK misses ---
    insert_params: list = []
    fk_miss_count = 0

    for rec in parse_result.records:
        hole_id = rec.get("hole_id")
        collar_id = collar_map.get(hole_id)
        if collar_id is None:
            context.log.warning(
                "FK miss: hole_id '%s' not found in silver.collars for project '%s' — row skipped",
                hole_id,
                config.project_id,
            )
            fk_miss_count += 1
            continue

        # Wrap commodity_assays dict in psycopg2.extras.Json for JSONB column.
        # An empty dict ({}) is a valid JSONB value — do not replace with NULL.
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
            "_source_row":           rec.get("_source_row"),
        })

    to_insert = len(insert_params)
    inserted_count = 0

    context.log.info(
        "Inserting %d samples into silver.samples (%d FK misses skipped)",
        to_insert,
        fk_miss_count,
    )

    # --- Bulk insert ---
    if insert_params:
        # Strip _source_row tracking key before inserting into the DB
        db_params = [{k: v for k, v in p.items() if k != "_source_row"} for p in insert_params]

        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    INSERT_SAMPLE_SQL,
                    db_params,
                    page_size=200,
                )
                inserted_count = to_insert
            conn.commit()

        # --- Provenance INSERT (bronze.provenance) ---
        prov = parse_result.provenance
        if prov:
            ingest_run_id = str(uuid.uuid4())
            prov_params = [
                (
                    "silver", "samples", p["sample_id"],
                    prov.get("source_file"), prov.get("source_file_sha256"),
                    p.get("_source_row"),
                    psycopg2.extras.Json(prov.get("source_col_map") or {}),
                    prov.get("parser_name"), prov.get("parser_version"),
                    ingest_run_id,
                )
                for p in insert_params
            ]
            try:
                with postgres.get_connection() as conn:
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_batch(
                            cur, PROVENANCE_INSERT_SQL, prov_params, page_size=200
                        )
                    conn.commit()
                context.log.info(
                    "Provenance: inserted %d rows into bronze.provenance for silver.samples",
                    len(prov_params),
                )
            except Exception as prov_exc:
                context.log.warning(
                    "Provenance INSERT skipped (table may not exist yet): %s", prov_exc
                )

        # --- Post-load tuning: B-tree on collar/depth, GIN on JSONB assays ---
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(POSTLOAD_SQL)
            conn.commit()

        context.log.info(
            "Post-load: B-tree and GIN indices ensured, ANALYZE run on silver.samples"
        )
    else:
        context.log.warning(
            "No records to insert after FK resolution — silver.samples unchanged."
        )

    # ── CC-01 Item 1 Slice 2 — write flagged rows to silver.review_queue ──
    # The parser emits per-record outlier_flags aligned with parse_result.
    # records; we re-align with the FK-resolved subset (insert_params has
    # the _source_row pointer back into the parser's record list).
    flagged_queue_rows = _build_queue_rows_for_flagged_samples(
        context=context,
        postgres=postgres,
        project_id=config.project_id,
        bronze_uri=f"s3://{BRONZE_BUCKET}/{object_name}",
        insert_params=insert_params,
        parse_result=parse_result,
    )

    review_inserted = 0
    if flagged_queue_rows:
        try:
            with postgres.get_connection() as conn:
                review_inserted = write_review_queue_rows(conn=conn, rows=flagged_queue_rows)
                conn.commit()
            context.log.info(
                "Review queue: inserted %d sample rows with outlier flags",
                review_inserted,
            )
        except Exception as q_exc:
            # Never block silver materialisation on the SRQ write.
            context.log.warning(
                "Review queue write skipped: %s", q_exc
            )

    skipped_total = (parse_result.total_rows - parse_result.valid_rows) + fk_miss_count

    return MaterializeResult(
        metadata={
            "total_rows":        MetadataValue.int(parse_result.total_rows),
            "valid_rows":        MetadataValue.int(parse_result.valid_rows),
            "fk_miss_rows":      MetadataValue.int(fk_miss_count),
            "skipped_rows":      MetadataValue.int(skipped_total),
            "inserted_count":    MetadataValue.int(inserted_count),
            "parse_quality_pct": MetadataValue.float(parse_result.parse_quality_pct),
            "assay_columns":     MetadataValue.text(str(parse_result.assay_columns)),
            "unmapped_columns":  MetadataValue.text(str(parse_result.unmapped_columns)),
            "csv_filename":      MetadataValue.text(config.csv_filename),
            "vendor_profile_id":   MetadataValue.text(str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"),
            "project_id":        MetadataValue.text(config.project_id),
            "review_queue_rows": MetadataValue.int(review_inserted),
        }
    )


# ---------------------------------------------------------------------------
# CC-01 Item 1 Slice 2 — review_queue plumbing
# ---------------------------------------------------------------------------

WORKSPACE_LOOKUP_SQL = """
SELECT workspace_id::text AS workspace_id
FROM silver.projects
WHERE project_id = %(project_id)s
"""


def _build_queue_rows_for_flagged_samples(
    *,
    context: AssetExecutionContext,
    postgres: PostgresResource,
    project_id: str,
    bronze_uri: str,
    insert_params: list[dict],
    parse_result,
) -> list[dict]:
    """Build silver.review_queue rows for any sample whose parser flagged.

    Aligns ``insert_params`` (FK-resolved, deduped subset) with
    ``parse_result.outlier_flags`` (parser-aligned with parse_result.records)
    via the ``_source_row`` pointer stamped during validation.

    Returns an empty list when no rows are flagged or when the workspace
    can't be resolved (defensive — the asset will log + skip).
    """
    if not parse_result.outlier_flags:
        return []

    # Resolve workspace_id once for the project.
    workspace_id: str | None = None
    try:
        with postgres.get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(WORKSPACE_LOOKUP_SQL, {"project_id": project_id})
                row = cur.fetchone()
                if row is not None:
                    workspace_id = row["workspace_id"]
    except Exception as exc:
        context.log.warning("review_queue: workspace lookup failed — %s", exc)
        return []

    if workspace_id is None:
        context.log.warning(
            "review_queue: no workspace for project_id=%s — skipping queue write",
            project_id,
        )
        return []

    # parse_result.records and parse_result.outlier_flags are 1:1 aligned.
    # Build a lookup keyed by the _source_row (which is preserved on the
    # parser record AND copied through to insert_params before strip-out).
    source_row_to_flags: dict[int, dict] = {}
    for rec, flags in zip(parse_result.records, parse_result.outlier_flags):
        src = rec.get("_source_row")
        if src is not None and flags:
            source_row_to_flags[src] = flags

    parser_version = (parse_result.provenance or {}).get(
        "parser_version", "csv_sample:unknown"
    )

    queue_rows: list[dict] = []
    for p in insert_params:
        src = p.get("_source_row")
        flags = source_row_to_flags.get(src) if src is not None else None
        if not flags:
            continue

        # Build a payload that mirrors the silver.assays_v2 record shape
        # the reviewer will be approving. We use the FK-resolved version
        # because that's what would otherwise have landed clean.
        payload = {
            "sample_id": p["sample_id"],
            "collar_id": p["collar_id"],
            "from_depth": p.get("from_depth"),
            "to_depth": p.get("to_depth"),
            "sample_type": p.get("sample_type"),
            "lab_id": p.get("lab_id"),
            "qaqc_type": p.get("qaqc_type"),
        }

        queue_rows.append(
            build_review_queue_row(
                workspace_id=workspace_id,
                project_id=project_id,
                target_table="silver.assays_v2",
                target_record_kind="sample",
                bronze_uri=bronze_uri,
                payload=payload,
                outlier_flags=flags,
                parser_version=parser_version,
                bronze_row_offset=src,
            )
        )

    return queue_rows
