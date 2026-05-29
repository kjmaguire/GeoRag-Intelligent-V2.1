"""Silver layer asset — radiometric age samples (CC-03 Item 3).

Reads a CSV from MinIO bronze, runs it through ``parse_csv_geochronology``,
and bulk-inserts validated rows into ``silver.geochronology_samples``
(geom built from lat/lon via ``ST_GeomFromText(geom_wkt, 4326)``).

Multi-domain tag: writes one ``silver.document_domain_tag`` row per
distinct isotopic system observed in the parse, using ``domain_id=2``
(Geology) plus the ``sub_type_id`` from ``ISOTOPIC_SYSTEM_SUB_TYPE_ID``
(seeded in migration ``2026_05_24_010100``). The tag write is skipped
when ``config.document_id`` is unset — academic CSVs not yet tied to a
``bronze.source_files`` row remain insertable.

NOTE: Do NOT add ``from __future__ import annotations`` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that
import breaks runtime annotation evaluation.
"""

from io import StringIO

import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.parsers.csv_geochronology import (
    ISOTOPIC_SYSTEM_SUB_TYPE_ID,
    parse_csv_geochronology,
)
from georag_dagster.resources import PostgresResource, S3Resource


BRONZE_BUCKET = "bronze"
GEOCHRON_PREFIX = "geochronology"

# 2 = Geology (silver.data_domain seed, migration 2026_05_23_040000).
DOMAIN_ID_GEOLOGY = 2


class SilverGeochronologyConfig(Config):
    """Runtime config for the silver_geochronology_samples asset."""

    csv_filename: str
    workspace_id: str
    project_id: str | None = None
    # bronze.source_files.id for the CSV. When set, the asset writes one
    # silver.document_domain_tag row per distinct isotopic system.
    document_id: str | None = None
    assigned_by: str = "auto"
    vendor_profile_id: int | None = None


INSERT_GEOCHRON_SQL = """
INSERT INTO silver.geochronology_samples (
    workspace_id, project_id, sample_id, rock_type,
    isotopic_system, mineral_dated,
    age_ma, age_uncertainty_ma, uncertainty_kind,
    analytical_method, laboratory, publication_ref,
    geom
) VALUES (
    %(workspace_id)s,
    %(project_id)s,
    %(sample_id)s,
    %(rock_type)s,
    %(isotopic_system)s,
    %(mineral_dated)s,
    %(age_ma)s,
    %(age_uncertainty_ma)s,
    %(uncertainty_kind)s,
    %(analytical_method)s,
    %(laboratory)s,
    %(publication_ref)s,
    CASE
        WHEN %(geom_wkt)s IS NULL THEN NULL
        ELSE ST_GeomFromText(%(geom_wkt)s, 4326)
    END
)
ON CONFLICT (workspace_id, sample_id, isotopic_system) DO UPDATE SET
    project_id         = EXCLUDED.project_id,
    rock_type          = EXCLUDED.rock_type,
    mineral_dated      = EXCLUDED.mineral_dated,
    age_ma             = EXCLUDED.age_ma,
    age_uncertainty_ma = EXCLUDED.age_uncertainty_ma,
    uncertainty_kind   = EXCLUDED.uncertainty_kind,
    analytical_method  = EXCLUDED.analytical_method,
    laboratory         = EXCLUDED.laboratory,
    publication_ref    = EXCLUDED.publication_ref,
    geom               = EXCLUDED.geom,
    updated_at         = now()
;
"""

# Multi-domain tag — one row per (document, domain, sub_type). The
# COALESCE expression matches the unique index in migration
# 2026_05_23_040000 (uq_ddt_document_domain_sub_type).
INSERT_DOMAIN_TAG_SQL = """
INSERT INTO silver.document_domain_tag (
    document_id, domain_id, sub_type_id, workspace_id,
    assigned_by, assigned_confidence, extraction_status
) VALUES (
    %(document_id)s::uuid,
    %(domain_id)s,
    %(sub_type_id)s,
    %(workspace_id)s::uuid,
    %(assigned_by)s,
    %(assigned_confidence)s,
    'extracted'
)
ON CONFLICT (document_id, domain_id, COALESCE(sub_type_id, 0))
DO UPDATE SET
    extraction_status = 'extracted',
    updated_at        = now()
;
"""


@asset(
    group_name="silver",
    description=(
        "CC-03 Item 3 — parse a CSV of radiometric age samples and insert "
        "into silver.geochronology_samples. Writes one silver.document_"
        "domain_tag row per distinct isotopic system (domain_id=2 Geology, "
        "sub_type_id 211–217) when document_id is provided."
    ),
)
def silver_geochronology_samples(
    context: AssetExecutionContext,
    config: SilverGeochronologyConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    object_name = f"{GEOCHRON_PREFIX}/{config.csv_filename}"
    context.log.info("downloading '%s/%s'", BRONZE_BUCKET, object_name)

    raw_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)
    csv_text = raw_bytes.decode("utf-8", errors="replace")

    parse_result = parse_csv_geochronology(StringIO(csv_text))

    context.log.info(
        "parse complete — total: %d, valid: %d, skipped: %d, quality: %.1f%%",
        parse_result.total_rows,
        parse_result.valid_rows,
        parse_result.skipped_rows,
        parse_result.parse_quality_pct,
    )

    for skip in parse_result.skipped_details:
        context.log.warning("skipped row: %s", skip.get("reason", skip))

    insert_params: list[dict] = []
    isotopic_systems_seen: set[str] = set()
    for rec in parse_result.records:
        isotopic_systems_seen.add(rec["isotopic_system"])
        insert_params.append({
            "workspace_id":       config.workspace_id,
            "project_id":         config.project_id,
            "sample_id":          rec["sample_id"],
            "rock_type":          rec.get("rock_type"),
            "isotopic_system":    rec["isotopic_system"],
            "mineral_dated":      rec.get("mineral_dated"),
            "age_ma":             rec.get("age_ma"),
            "age_uncertainty_ma": rec.get("age_uncertainty_ma"),
            "uncertainty_kind":   rec.get("uncertainty_kind"),
            "analytical_method":  rec.get("analytical_method"),
            "laboratory":         rec.get("laboratory"),
            "publication_ref":    rec.get("publication_ref"),
            "geom_wkt":           rec.get("geom_wkt"),
        })

    inserted = 0
    domain_tags_written = 0

    if insert_params:
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur, INSERT_GEOCHRON_SQL, insert_params, page_size=200,
                )
                inserted = len(insert_params)
            conn.commit()

        # Multi-domain tag write — one row per distinct (document_id, domain,
        # sub_type) triple. Skip if document_id wasn't supplied (academic
        # CSV with no bronze.source_files row yet).
        if config.document_id and isotopic_systems_seen:
            sub_type_ids = {
                ISOTOPIC_SYSTEM_SUB_TYPE_ID.get(sys_)
                for sys_ in isotopic_systems_seen
            }
            sub_type_ids.discard(None)
            tag_params = [
                {
                    "document_id":         config.document_id,
                    "domain_id":           DOMAIN_ID_GEOLOGY,
                    "sub_type_id":         sub_type_id,
                    "workspace_id":        config.workspace_id,
                    "assigned_by":         config.assigned_by,
                    "assigned_confidence": 0.95,
                }
                for sub_type_id in sub_type_ids
            ]
            # Always also write the top-level Geology tag (sub_type_id=NULL)
            # per the spec ("domain_id=2 (Geology) + sub_type_id=NULL plus
            # possibly the appropriate per-sample sub-type"). The unique
            # index treats NULL sub_type_id as a distinct slot (COALESCE 0).
            tag_params.append({
                "document_id":         config.document_id,
                "domain_id":           DOMAIN_ID_GEOLOGY,
                "sub_type_id":         None,
                "workspace_id":        config.workspace_id,
                "assigned_by":         config.assigned_by,
                "assigned_confidence": 0.95,
            })

            with postgres.get_connection() as conn:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(
                        cur, INSERT_DOMAIN_TAG_SQL, tag_params, page_size=50,
                    )
                    domain_tags_written = len(tag_params)
                conn.commit()
            context.log.info(
                "wrote %d document_domain_tag row(s) for document_id=%s",
                domain_tags_written, config.document_id,
            )
        elif not config.document_id:
            context.log.info(
                "document_id not provided — skipping document_domain_tag write"
            )

    return MaterializeResult(
        metadata={
            "total_rows":         MetadataValue.int(parse_result.total_rows),
            "valid_rows":         MetadataValue.int(parse_result.valid_rows),
            "skipped_rows":       MetadataValue.int(parse_result.skipped_rows),
            "inserted_count":     MetadataValue.int(inserted),
            "parse_quality_pct":  MetadataValue.float(parse_result.parse_quality_pct),
            "isotopic_systems":   MetadataValue.text(
                ", ".join(sorted(isotopic_systems_seen)) or "(none)"
            ),
            "domain_tags_written": MetadataValue.int(domain_tags_written),
            "csv_filename":       MetadataValue.text(config.csv_filename),
        }
    )
