"""GeoRAG Dagster definitions — pipeline entry point.

Wires together Bronze → Silver → Gold → Index assets with their required
resources. Gold and Index layers remain placeholder assets until their
feature-engineering rules are provided by the SME (Kyle).

Schedules:
  full_ingest_schedule  — daily at 02:00 UTC, materializes all Bronze→Silver
                          pairs + Index assets.

Sensors:
  minio_upload_sensor   — polls MinIO every 5 minutes for new objects in the
                          bronze bucket. When new files are detected,
                          triggers materialization of the relevant Bronze asset
                          based on the file path prefix.
"""


from dagster import (
    AssetExecutionContext,
    AssetSelection,
    DefaultScheduleStatus,
    Definitions,
    EnvVar,
    RunRequest,
    ScheduleDefinition,
    SensorEvaluationContext,
    asset,
    sensor,
)

# Phase 7 Step 1 (R-P6-1) — bootstrap the OTel TracerProvider at
# module-load so silver_reports' call to parse_pdf_report emits spans
# under service.name=georag-dagster-daemon. install_tracer_provider is
# a no-op when OTEL_EXPORTER_OTLP_ENDPOINT isn't set, so importing
# this module in tests / offline scripts stays cheap.
from georag_dagster.observability import install_tracer_provider

install_tracer_provider(default_service_name="georag-dagster-daemon")

from georag_dagster.assets.bronze import bronze_collars
from georag_dagster.assets.silver import silver_collars
from georag_dagster.assets.bronze_surveys import bronze_surveys
from georag_dagster.assets.silver_surveys import silver_surveys
from georag_dagster.assets.bronze_lithology import bronze_lithology
from georag_dagster.assets.silver_lithology import silver_lithology
from georag_dagster.assets.bronze_samples import bronze_samples
from georag_dagster.assets.silver_samples import silver_samples
from georag_dagster.assets.bronze_well_logs import bronze_well_logs
from georag_dagster.assets.silver_well_logs import silver_well_logs
from georag_dagster.assets.bronze_spatial import bronze_spatial
from georag_dagster.assets.silver_spatial import silver_spatial
from georag_dagster.assets.index_neo4j import index_neo4j
from georag_dagster.assets.bronze_reports import bronze_reports
from georag_dagster.assets.silver_reports import silver_reports
# index_reports retired 2026-05-28 per ADR-0010 Session C — the
# georag_reports collection it populated was dropped after the full
# 119-question per-slice benchmark confirmed georag_chunks (fed by
# index_document_passages) was functionally equivalent: identical
# pass count on every question_set, latency drift on 111/119 questions
# confirming the new code path was active.
from georag_dagster.assets.index_document_passages import index_document_passages
# ADR-0012 — synthesize NL summary passages from structured silver tables
# so the reranker training corpus includes the assays / lithology /
# collars data, not just PDF prose. Each asset UPSERTs into
# silver.document_passages with chunk_kind='structured_summary'.
from georag_dagster.assets.silver_nl_summaries import (
    silver_assays_v2_nl_summary,
    silver_collars_nl_summary,
    silver_lithology_nl_summary,
)
from georag_dagster.assets.bronze_xlsx import bronze_xlsx
from georag_dagster.assets.silver_xlsx import silver_xlsx
from georag_dagster.assets.bronze_seismic import bronze_seismic
from georag_dagster.assets.silver_seismic import silver_seismic
from georag_dagster.assets.bronze_xyz import bronze_xyz
from georag_dagster.assets.silver_xyz import silver_xyz
from georag_dagster.assets.silver_raster import silver_raster
from georag_dagster.assets.bronze_public_geoscience import (
    bronze_pg_ca_bc_minfile,
    bronze_pg_ca_sk_assessment_airborne,
    bronze_pg_ca_sk_assessment_ground,
    bronze_pg_ca_sk_assessment_underground,
    bronze_pg_ca_sk_bedrock_geology,
    bronze_pg_ca_sk_drillhole,
    bronze_pg_ca_sk_mine_loc,
    bronze_pg_ca_sk_mineral_disposition,
    bronze_pg_ca_sk_resource_potential,
    bronze_pg_ca_sk_rock_samples,
    bronze_pg_ca_sk_smdi,
)
from georag_dagster.assets.silver_public_geoscience import (
    silver_pg_ca_bc_minfile,
    silver_pg_ca_sk_assessment_airborne,
    silver_pg_ca_sk_assessment_ground,
    silver_pg_ca_sk_assessment_underground,
    silver_pg_ca_sk_bedrock_geology,
    silver_pg_ca_sk_drillhole,
    silver_pg_ca_sk_mine_loc,
    silver_pg_ca_sk_mineral_disposition,
    silver_pg_ca_sk_resource_potential,
    silver_pg_ca_sk_rock_samples,
    silver_pg_ca_sk_smdi,
)
from georag_dagster.assets.gold_public_geoscience import (
    gold_public_geoscience_neo4j,
)
from georag_dagster.assets.gold_h3_density import (
    gold_h3_density_choropleth,
)
from georag_dagster.assets.gold_cross_corpus_linker import (
    gold_cross_corpus_linker,
)
from georag_dagster.assets.gold_drillhole_intervals_visual import (
    gold_drillhole_intervals_visual,
)
# §B/S/G build-out 2026-05-22 — five new assets across the three layers.
from georag_dagster.assets.bronze_geophysics import bronze_geophysics
from georag_dagster.assets.silver_geophysics import silver_geophysics
# CC-03 Item 3 — radiometric age samples (silver-only; bronze landing is a
# generic CSV upload, no dedicated bronze asset).
from georag_dagster.assets.silver_geochronology import silver_geochronology_samples
from georag_dagster.assets.silver_structure_derive import silver_structure_derive
from georag_dagster.assets.silver_structure_populate import silver_structure_populate
from georag_dagster.assets.silver_entity_ner_backfill import silver_entity_ner_backfill
from georag_dagster.assets.silver_collars_canonicalize_backfill import (
    silver_collars_canonicalize_backfill,
)
from georag_dagster.assets.silver_collar_dq import silver_collar_dq
from georag_dagster.assets.silver_assay_dq import silver_assay_dq
from georag_dagster.assets.silver_crs_dq import silver_crs_dq
from georag_dagster.assets.silver_unit_consistency_dq import (
    silver_unit_consistency_dq,
)
from georag_dagster.assets.gold_cross_section_panels import gold_cross_section_panels
from georag_dagster.assets.gold_structure_measurements_visual import (
    gold_structure_measurements_visual,
)
from georag_dagster.assets.index_public_geoscience import (
    index_public_geoscience_qdrant,
)
from georag_dagster.assets.smdi_deposits import smdi_deposits_refresh
from georag_dagster.assets.commit_ingestion_run import commit_ingestion_run
# Appendix F-data-dictionary / Z.7 — per-table JSON dump + ERD groupings
# + CI drift guard. Lands snapshots in S3 under
# catalogs/data_dictionary/<UTC date>/.
from georag_dagster.assets.data_dictionary_dump import (
    data_dictionary_dump,
    data_dictionary_drift_check,
)
from georag_dagster.assets.silver_drill_traces import silver_drill_traces
from georag_dagster.assets.silver_cog_rasters import (
    bronze_raster_uploads,
    silver_cog_rasters,
    bronze_raster_sources_discoverable_check,
    cog_readable_check,
)
from georag_dagster.checks import (
    # Silver — collars
    silver_collars_check_collar_count_positive,
    silver_collars_check_schema_conformance,
    silver_collars_check_crs_srid_populated,
    # Silver — tabular
    silver_surveys_check_parse_total_positive,
    silver_lithology_check_parse_total_positive,
    silver_samples_check_parse_total_positive,
    silver_well_logs_check_parse_total_positive,
    # CC-01 Item 1 Slice 3 — interval-overlap checks (WARN, non-blocking)
    silver_lithology_interval_overlap,
    silver_samples_assays_v2_interval_overlap,
    # Silver — spatial
    silver_spatial_check_geom_not_null,
    silver_spatial_check_srid_populated,
    # Silver — reports/xlsx
    silver_reports_check_parse_total_positive,
    silver_reports_check_schema_conformance,
    silver_xlsx_check_parse_total_positive,
    # Silver — seismic + xyz (Silver-trapped, DAG-05)
    silver_seismic_check_parse_total_positive,
    silver_seismic_check_schema_conformance,
    silver_xyz_check_parse_total_positive,
    silver_xyz_check_schema_conformance,
    # Evidence model
    document_passages_check_no_duplicate_passage_ids,
    document_passages_check_text_hash_sha256_valid,
    document_revisions_check_document_id_not_null,
    document_revisions_check_sha256_format,
    evidence_items_check_exactly_one_ref,
    # Index
    index_reports_check_embedding_id_present,
    index_reports_check_parser_quality_floor,
    # Drill traces
    desurvey_trace_count_matches_collar_count_with_surveys,
)
from georag_dagster.assets.reranker_labels import (
    reranker_chunk_population,
    reranker_chunk_sample,
    reranker_generated_queries,
    reranker_label_dataset,
    reranker_label_dataset_minimum_size_check,
    reranker_mined_negatives,
)
from georag_dagster.resources import (
    Neo4jResource,
    PostgresResource,
    QdrantResource,
    S3Resource,
    VllmResource,
)


# ---------------------------------------------------------------------------
# Placeholder downstream assets (Gold and Index layers)
# SME-provided feature-engineering config not yet delivered — do not hard-code
# grade thresholds or net pay formulas here.
# ---------------------------------------------------------------------------

@asset(
    description=(
        "Placeholder Gold asset — feature-engineered data with SME-provided rules. "
        "Rules are loaded from config files editable by the geologist (Kyle). "
        "Do not bake grade thresholds or net pay formulas into code."
    ),
    group_name="gold",
    deps=[silver_collars],
)
def gold_placeholder(context: AssetExecutionContext) -> None:
    """Placeholder for the Gold (feature-engineered) layer."""
    context.log.info(
        "Gold placeholder: awaiting SME-provided feature-engineering config. "
        "Replace this asset once rules/thresholds are defined in a config file."
    )


@asset(
    description=(
        "Placeholder Index asset — embeddings to Qdrant, entities to Neo4j, "
        "GIST spatial indices to PostGIS, materialized views refreshed."
    ),
    group_name="index",
    deps=[gold_placeholder],
)
def index_placeholder(context: AssetExecutionContext) -> None:
    """Placeholder for the Index layer."""
    context.log.info("Index placeholder: embeddings and graph indexing not yet wired.")


# ---------------------------------------------------------------------------
# Schedule — full daily materialization at 02:00 UTC
# ---------------------------------------------------------------------------

full_ingest_schedule = ScheduleDefinition(
    name="full_ingest_schedule",
    cron_schedule="0 2 * * *",  # daily at 02:00 UTC
    target=AssetSelection.all(),
    description=(
        "Daily full materialization of all Bronze→Silver→Index assets. "
        "Runs at 02:00 UTC to avoid interference with interactive queries."
    ),
    default_status=DefaultScheduleStatus.STOPPED,  # disabled by default — enable via Dagster UI
)


# ---------------------------------------------------------------------------
# Public Geoscience schedules (Phase 2.3, plan §05e)
# ---------------------------------------------------------------------------

# Asset selection for the full Public Geoscience Bronze → Silver → Gold →
# Index chain across all active jurisdictions. Phase 4 widened this from
# SK-only to SK + BC. Gold + Index + linker are jurisdiction-agnostic —
# they read all canonical rows at once — so they're added unqualified.
#
# To onboard a third jurisdiction: flip its row to `active` in the seeder,
# add matching bronze_/silver_ asset keys here, and the schedule picks it
# up on the next tick. Nothing else (Gold / Index / Martin) needs editing.
_PG_ACTIVE_ASSETS = AssetSelection.assets(
    # Saskatchewan
    "bronze_pg_ca_sk_mine_loc", "silver_pg_ca_sk_mine_loc",
    "bronze_pg_ca_sk_smdi",    "silver_pg_ca_sk_smdi",
    "bronze_pg_ca_sk_drillhole", "silver_pg_ca_sk_drillhole",
    "bronze_pg_ca_sk_resource_potential", "silver_pg_ca_sk_resource_potential",
    "bronze_pg_ca_sk_rock_samples", "silver_pg_ca_sk_rock_samples",
    "bronze_pg_ca_sk_assessment_underground", "silver_pg_ca_sk_assessment_underground",
    "bronze_pg_ca_sk_assessment_ground", "silver_pg_ca_sk_assessment_ground",
    "bronze_pg_ca_sk_assessment_airborne", "silver_pg_ca_sk_assessment_airborne",
    # Tier 2 — Mineral Tenure / Dispositions
    "bronze_pg_ca_sk_mineral_disposition", "silver_pg_ca_sk_mineral_disposition",
    # Tier 2 — Geology
    "bronze_pg_ca_sk_bedrock_geology", "silver_pg_ca_sk_bedrock_geology",
    # British Columbia
    "bronze_pg_ca_bc_minfile", "silver_pg_ca_bc_minfile",
    # Jurisdiction-agnostic downstream assets
    "gold_public_geoscience_neo4j",
    "gold_h3_density_choropleth",
    "gold_cross_corpus_linker",
    "index_public_geoscience_qdrant",
)

# Weekly full-pull: Sunday 03:00 UTC — force a complete ArcGIS REST sweep +
# Silver upsert regardless of upstream edit date. Gives us a guaranteed fresh
# audit trail every 7 days and catches any edge case the daily short-circuit
# might miss (e.g. silent attribute changes without a bumped edit date).
public_geoscience_weekly_refresh = ScheduleDefinition(
    name="public_geoscience_weekly_refresh",
    cron_schedule="0 3 * * 0",  # Sundays at 03:00 UTC
    target=_PG_ACTIVE_ASSETS,
    description=(
        "Weekly forced full-pull for all active Public Geoscience "
        "jurisdictions (SK + BC as of Phase 4). Bronze assets ignore "
        "serviceLastEditDate (skip_if_unchanged=false) and paginate every "
        "feature regardless; Silver upsert follows. Gives a guaranteed "
        "weekly audit snapshot even if an upstream edit date is stale. "
        "Plan §05e."
    ),
    default_status=DefaultScheduleStatus.STOPPED,  # enable via Dagster UI once endpoints are verified
)

# Daily short-circuit check: cheap layer-metadata GET per source; if the
# upstream serviceLastEditDate matches what we stored on the last successful
# run, skip the pull. Downstream Silver for a skipped Bronze becomes a no-op
# (no new Bronze object → Silver sees "no Bronze object found" and returns
# early). Covers the "run every day but only actually pull on change" pattern
# in plan §05e.
# ---------------------------------------------------------------------------
# SMDI standalone refresh — plan v1.1 (2026-05-24).
# Lands in public.smdi_deposits (separate from public_geo.pg_mineral_occurrence).
# Count-only short-circuit lives inside the asset; the schedule just runs it
# daily and lets the asset decide whether to skip or fetch.
# ---------------------------------------------------------------------------
smdi_deposits_daily_refresh = ScheduleDefinition(
    name="smdi_deposits_daily_refresh",
    cron_schedule="30 3 * * *",  # daily at 03:30 UTC
    target=AssetSelection.assets("smdi_deposits_refresh"),
    description=(
        "Daily SMDI refresh (plan v1.1) — count-gated. Skips full fetch "
        "when upstream count matches local. ~21:30 MST / 20:30 MDT — "
        "overnight, outside operational hours."
    ),
    default_status=DefaultScheduleStatus.STOPPED,  # enable in Dagster UI after bootstrap verification
)


# ---------------------------------------------------------------------------
# §6a daily data-quality flag sweep (2026-05-28 — overnight side work)
# ---------------------------------------------------------------------------
# Re-materialises the four §6a DQ rule families against silver tables that
# may have churned since the last sweep. Idempotent via the writer
# (workspace_id, record_type, record_id, flag_type, rule_version) upsert
# key, so re-running is a no-op on stable rows. Fires after the global
# 02:00 UTC full_ingest so it sees freshly-loaded silver data.
#
# Default STOPPED — same convention as full_ingest_schedule. Operator
# enables in Dagster UI once they're ready for the badge to track live
# data drift on a daily cadence.
silver_dq_daily_schedule = ScheduleDefinition(
    name="silver_dq_daily_schedule",
    cron_schedule="0 4 * * *",  # daily at 04:00 UTC (2h after full_ingest)
    target=AssetSelection.assets(
        "silver_collar_dq",
        "silver_assay_dq",
        "silver_crs_dq",
        "silver_unit_consistency_dq",
    ),
    description=(
        "Plan §6a — daily re-sweep of the four DQ rule families "
        "(collar, assay, CRS/georef, unit consistency). Idempotent "
        "via the silver_dq_flag_writer upsert key. Fires at 04:00 UTC "
        "after the global full_ingest at 02:00 UTC."
    ),
    default_status=DefaultScheduleStatus.STOPPED,
)


# ---------------------------------------------------------------------------
# Chat-cards backfill — silver_structure_populate + silver_entity_ner_backfill
# ---------------------------------------------------------------------------
# Both assets are workspace-scoped, idempotent (WHERE NULL guards everywhere)
# and cheap when there is no new work — they no-op in a few hundred ms when
# every report has already been scanned. A sensor watching silver.reports
# would be more elegant but a 30-minute schedule is simpler and good enough
# for early prod. The selection covers PR-2 (structure_populate) and PR-3
# (entity_ner_backfill); the latter declares deps=[silver_structure_populate]
# so Dagster fires them in the correct order in one run.
silver_chat_cards_backfill_schedule = ScheduleDefinition(
    name="silver_chat_cards_backfill_schedule",
    cron_schedule="*/30 * * * *",  # every 30 minutes
    target=AssetSelection.assets(
        "silver_structure_populate",
        "silver_entity_ner_backfill",
        # ADR-0007 PR-4 — populate silver.drill_traces (straight-line
        # fallback when silver.surveys is empty) so the 3D card has data
        # without manual `dagster asset materialize` runs. Idempotent
        # via survey_hash / orientation hash — re-runs no-op when nothing
        # changed.
        "silver_drill_traces",
    ),
    description=(
        "Every 30 minutes — re-runs silver_structure_populate + "
        "silver_entity_ner_backfill + silver_drill_traces so new projects "
        "auto-populate the chat-card data (contractor / geologist / lab "
        "names, structural rows, QP nodes, 3-D drill traces) without "
        "manual `dagster asset materialize`. All three assets are "
        "idempotent (WHERE NULL guards / survey_hash) and cheap when "
        "there is no new work."
    ),
    default_status=DefaultScheduleStatus.RUNNING,  # enabled on deploy
)


public_geoscience_daily_edit_check = ScheduleDefinition(
    name="public_geoscience_daily_edit_check",
    cron_schedule="30 5 * * *",  # daily at 05:30 UTC (after the global full_ingest)
    target=_PG_ACTIVE_ASSETS,
    run_config={
        "ops": {
            "bronze_pg_ca_sk_mine_loc":            {"config": {"skip_if_unchanged": True}},
            "bronze_pg_ca_sk_smdi":                {"config": {"skip_if_unchanged": True}},
            "bronze_pg_ca_sk_drillhole":           {"config": {"skip_if_unchanged": True}},
            "bronze_pg_ca_sk_resource_potential":  {"config": {"skip_if_unchanged": True}},
            "bronze_pg_ca_sk_rock_samples":         {"config": {"skip_if_unchanged": True}},
            "bronze_pg_ca_sk_assessment_underground":{"config": {"skip_if_unchanged": True}},
            "bronze_pg_ca_sk_assessment_ground":    {"config": {"skip_if_unchanged": True}},
            "bronze_pg_ca_sk_assessment_airborne":  {"config": {"skip_if_unchanged": True}},
            "bronze_pg_ca_sk_mineral_disposition": {"config": {"skip_if_unchanged": True}},
            "bronze_pg_ca_sk_bedrock_geology":     {"config": {"skip_if_unchanged": True}},
            "bronze_pg_ca_bc_minfile":             {"config": {"skip_if_unchanged": True}},
        }
    },
    description=(
        "Daily short-circuit check for all active Public Geoscience "
        "jurisdictions. Each Bronze asset runs with skip_if_unchanged=True, "
        "compares upstream serviceLastEditDate against the stored marker, "
        "and skips the pull if unchanged. When it does pull, the paired "
        "Silver upsert follows. Plan §05e."
    ),
    default_status=DefaultScheduleStatus.STOPPED,
)



# ---------------------------------------------------------------------------
# Vendor profile metadata helper (Sprint 5 Phase 1)
# ---------------------------------------------------------------------------

def _extract_vendor_profile_id(metadata: dict | None) -> int | None:
    """Extract x-georag-vendor-profile-id from MinIO object metadata.

    The minio Python client returns raw HTTP response headers in stat_object().
    Custom metadata uploaded via the S3 SDK is stored under the key
    x-amz-meta-x-georag-vendor-profile-id (lowercase; the SDK adds the
    x-amz-meta- prefix on upload, the server echoes it back on HEAD).

    To remain robust against future client changes or direct S3 SDK uploads
    (boto3 strips the prefix, exposing just x-georag-vendor-profile-id),
    both forms are checked, case-insensitively.

    Returns:
        The vendor_profile_id as an int, or None if absent or unparseable.
    """
    import logging as _logging

    if not metadata:
        return None

    # Normalise: build a lowercase-key dict so the lookup is case-insensitive
    # regardless of whether the caller passes an HTTPHeaderDict or a plain dict.
    lower = {k.lower(): v for k, v in metadata.items()}

    # boto3 strips the x-amz-meta- prefix; minio keeps it.
    raw = (
        lower.get("x-georag-vendor-profile-id")
        or lower.get("x-amz-meta-x-georag-vendor-profile-id")
    )
    if raw is None:
        return None

    # The value may be a list (HTTPHeaderDict) or a plain string.
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return None

    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        _logging.getLogger(__name__).warning(
            "vendor_profile_id metadata value %r is not a valid integer; ignoring", raw
        )
        return None

# ---------------------------------------------------------------------------
# Sensor — poll MinIO for new uploads and trigger relevant Bronze assets
# ---------------------------------------------------------------------------

# Map MinIO path prefixes to Bronze asset keys for targeted materialization.
_PREFIX_TO_ASSET = {
    "collars/":    "bronze_collars",
    "surveys/":    "bronze_surveys",
    "lithology/":  "bronze_lithology",
    "samples/":    "bronze_samples",
    "well_logs/":  "bronze_well_logs",
    "spatial/":    "bronze_spatial",
    "reports/":    "bronze_reports",
    "excel/":      "bronze_xlsx",
    "seismic/":    "bronze_seismic",
    "xyz/":        "bronze_xyz",
    # §B/S/G build-out 2026-05-22 — geophysics interpretation JSON uploads.
    # Laravel UploadController writes to geophysics/{project_id}/{ts}_{file}.json.
    "geophysics/": "bronze_geophysics",
}

# Sensor helpers extracted to georag_dagster.sensor_helpers so unit
# tests can exercise the run-config builder without dragging the full
# asset import chain. The module-level alias preserves the back-compat
# private names referenced by sensor code below.
from georag_dagster.sensor_helpers import (
    build_sensor_run_config as _build_sensor_run_config,
)


@sensor(
    name="minio_upload_sensor",
    minimum_interval_seconds=300,  # poll every 5 minutes
    required_resource_keys={"minio"},
    description=(
        "Polls the bronze SeaweedFS bucket for new objects. When a new file "
        "is detected (based on modification time > last cursor), triggers "
        "materialization of the corresponding Bronze asset."
    ),
)
def minio_upload_sensor(context: SensorEvaluationContext):
    """Sensor that watches MinIO for new file uploads.

    Sprint 5 Phase 1: also reads the x-georag-vendor-profile-id object metadata
    header and threads it through to each triggered silver asset via run_config.
    The parser does not consume it yet (Phase 2).  If the header is absent
    (uploads without a vendor profile) vendor_profile_id is None — backward-compat.
    """
    from datetime import datetime, timezone

    try:
        s3_resource: S3Resource = context.resources.minio  # type: ignore[attr-defined]
        s3_client = s3_resource.get_client()
    except Exception as e:
        context.log.warning(f"minio_upload_sensor: cannot connect to S3: {e}")
        return

    bucket = "bronze"
    if not s3_resource.bucket_exists(bucket):
        context.log.warning(f"minio_upload_sensor: bucket {bucket} not found")
        return

    # Load cursor -- last seen timestamp (ISO format).
    cursor_str = context.cursor or "2020-01-01T00:00:00+00:00"
    last_seen = datetime.fromisoformat(cursor_str)

    new_max = last_seen
    triggered_assets: set[str] = set()
    # Sprint 5 Phase 1: track vendor_profile_id per bronze asset key.
    # If multiple new files map to the same asset type in one poll, last-seen wins.
    asset_vendor_profile: dict[str, int | None] = {}
    # 2026-05-23 bronze-MinIO-unification: track the observed MinIO key
    # per bronze asset so the run_config can feed it back to the asset
    # (which now accepts object_key in lieu of *_file_path). Last-write
    # wins per asset key in a single poll, matching vendor_profile_id.
    asset_object_key: dict[str, str] = {}

    # Paginate bucket using boto3 list_objects_v2 paginator.
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            object_name: str = obj["Key"]
            mod_time = obj.get("LastModified")
            if mod_time is None:
                continue
            # Ensure timezone-aware comparison
            if mod_time.tzinfo is None:
                mod_time = mod_time.replace(tzinfo=timezone.utc)
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)

            if mod_time > last_seen:
                # Find which asset this file maps to.
                matched_asset: str | None = None
                for prefix, asset_key in _PREFIX_TO_ASSET.items():
                    if object_name.startswith(prefix):
                        matched_asset = asset_key
                        triggered_assets.add(asset_key)
                        break

                # 2026-05-23 bronze-MinIO-unification: capture the
                # observed key so the bronze asset can read MinIO
                # directly instead of demanding a *_file_path.
                if matched_asset is not None:
                    asset_object_key[matched_asset] = object_name

                # Sprint 5 Phase 1: fetch object metadata to extract vendor_profile_id.
                if matched_asset is not None:
                    try:
                        stat = s3_resource.stat_object(bucket, object_name)
                        # boto3 stat_object returns metadata under 'metadata' key (already
                        # stripped of x-amz-meta- prefix). _extract_vendor_profile_id handles both.
                        vendor_id = _extract_vendor_profile_id(stat.get("metadata"))
                    except Exception as meta_exc:
                        context.log.warning(
                            "minio_upload_sensor: stat_object failed for %s: %s",
                            object_name,
                            meta_exc,
                        )
                        vendor_id = None
                    # Last-write wins per asset key (see docstring note).
                    asset_vendor_profile[matched_asset] = vendor_id
                    if vendor_id is not None:
                        context.log.info(
                            "minio_upload_sensor: object %s has vendor_profile_id=%d",
                            object_name,
                            vendor_id,
                        )

            if mod_time > new_max:
                new_max = mod_time

    if triggered_assets:
        context.log.info(
            f"minio_upload_sensor: {len(triggered_assets)} asset(s) to materialize: "
            f"{triggered_assets}"
        )
        context.update_cursor(new_max.isoformat())

        # Build run_config -- include vendor_profile_id for each silver asset
        # paired with a triggered bronze asset.  Uses the module-level helper
        # so it can be tested independently.
        run_config = _build_sensor_run_config(
            triggered_assets,
            asset_vendor_profile,
            asset_object_key=asset_object_key,
        )

        yield RunRequest(
            run_key=f"minio-{new_max.isoformat()}",
            asset_selection=AssetSelection.assets(*list(triggered_assets)),
            run_config=run_config,
        )
    else:
        # Update cursor even if no new files -- avoids re-scanning old files.
        context.update_cursor(new_max.isoformat())


# ---------------------------------------------------------------------------
# Definitions — resources injected by name into asset function signatures
# ---------------------------------------------------------------------------

defs = Definitions(
    assets=[
        # --- Bronze / Silver pairs (medallion order) ---
        bronze_collars,
        silver_collars,
        bronze_surveys,
        silver_surveys,
        bronze_lithology,
        silver_lithology,
        bronze_samples,
        silver_samples,
        bronze_well_logs,
        silver_well_logs,
        bronze_spatial,
        silver_spatial,
        bronze_reports,
        silver_reports,
        bronze_xlsx,
        silver_xlsx,
        bronze_seismic,
        silver_seismic,
        bronze_xyz,
        silver_xyz,
        # --- Silver Raster (Sprint 4b) ---
        silver_raster,
        # --- Chunk 2: B5 desurvey + B6 COG normalization ---
        silver_drill_traces,
        bronze_raster_uploads,
        silver_cog_rasters,
        # --- §B/S/G build-out 2026-05-22 ---
        # Bronze: geophysics interpretation JSON upload landing.
        # Silver: geophysics survey metadata writer; α/β → true_dip derivation.
        # Gold:   cross-section panel + stereonet projection generators.
        bronze_geophysics,
        silver_geophysics,
        # CC-03 Item 3 — radiometric age samples.
        silver_geochronology_samples,
        silver_structure_populate,
        silver_entity_ner_backfill,
        # One-off legacy backfill for hole_id_canonical NULLs on silver.collars.
        # Idempotent (WHERE hole_id_canonical IS NULL) so safe to leave registered.
        silver_collars_canonicalize_backfill,
        # Plan §6a — collar + assay validation rule families writing
        # flags to silver.data_quality_flags. Idempotent (writer helper
        # handles upsert by rule_version) so safe to schedule daily.
        silver_collar_dq,
        silver_assay_dq,
        silver_crs_dq,
        silver_unit_consistency_dq,
        silver_structure_derive,
        gold_cross_section_panels,
        gold_structure_measurements_visual,
        # ADR-0012 — structured-to-NL summary corpus expansion.
        # These materialise once after the structured silver tables
        # have rows + can be re-materialised any time. They UPSERT
        # synthesised passages into silver.document_passages with
        # chunk_kind='structured_summary'; the existing embed cron
        # picks them up via the ADR-0010 §A path.
        silver_assays_v2_nl_summary,
        silver_lithology_nl_summary,
        silver_collars_nl_summary,
        # --- Public Geoscience Bronze (Phase 2.2 + Phase 4) ---
        # External ArcGIS REST pulls, not upload-triggered. Scheduled below.
        bronze_pg_ca_sk_mine_loc,
        bronze_pg_ca_sk_smdi,
        bronze_pg_ca_sk_drillhole,
        bronze_pg_ca_sk_resource_potential,
        bronze_pg_ca_sk_rock_samples,
        bronze_pg_ca_sk_assessment_underground,
        bronze_pg_ca_sk_assessment_ground,
        bronze_pg_ca_sk_assessment_airborne,
        bronze_pg_ca_sk_mineral_disposition,  # Tier 2 — Mineral Tenure
        bronze_pg_ca_sk_bedrock_geology,      # Tier 2 — Geology
        bronze_pg_ca_bc_minfile,
        # --- Public Geoscience Silver (Phase 2.3 + Phase 4) ---
        # Canonical-table upsert with history; deps point back to each
        # matching Bronze asset so full-pull schedule runs end-to-end.
        # BC MINFILE uses the same pg_mineral_occurrence target as SK SMDI;
        # the FieldMapping registry decides which raw column names to pick up.
        silver_pg_ca_sk_mine_loc,
        silver_pg_ca_sk_smdi,
        silver_pg_ca_sk_drillhole,
        silver_pg_ca_sk_resource_potential,
        silver_pg_ca_sk_rock_samples,
        silver_pg_ca_sk_assessment_underground,
        silver_pg_ca_sk_assessment_ground,
        silver_pg_ca_sk_assessment_airborne,
        silver_pg_ca_sk_mineral_disposition,  # Tier 2 — Mineral Tenure
        silver_pg_ca_sk_bedrock_geology,      # Tier 2 — Geology
        silver_pg_ca_bc_minfile,
        # --- Public Geoscience Gold (Neo4j, Phase 3.1) ---
        # Knowledge-graph population. Depends on all 4 SK silvers so a full
        # weekly refresh writes graph state from fresh canonical data.
        gold_public_geoscience_neo4j,
        # --- §6.6 h3 density choropleth (cross-tenant) ---
        # Per-(commodity, h3, resolution) counts feeding the §6.13
        # Martin function for the density layer in MapView.
        gold_h3_density_choropleth,
        # --- Cross-corpus linker (Phase 3.6) ---
        # Scans silver.reports for SMDI/drillhole/NTS references and writes
        # (:Document)-[:REFERENCES]-> edges + document_entity_links rows.
        # Ships empty until SMAD docs ingest; asset runs green with 0 links.
        gold_cross_corpus_linker,
        # §5 Phase H4 — drillhole intervals enriched for strip-log
        # rendering (lithology + max-assay + mineralisation flag).
        gold_drillhole_intervals_visual,
        # --- Public Geoscience Index (Qdrant, Phase 3.2) ---
        # Four dedicated Qdrant collections for chat-tool retrieval.
        index_public_geoscience_qdrant,
        # --- Gold ---
        gold_placeholder,
        # --- Index ---
        index_neo4j,
        # index_reports retired 2026-05-28 — ADR-0010 Session C
        # confirmed functional equivalence on the full 119-question
        # golden set + dropped the georag_reports collection.
        # index_document_passages → georag_chunks is the canonical
        # retrieval index.
        index_document_passages,
        index_placeholder,
        # --- Commit gate (Module 3 Phase B1/B9/B10) ---
        # Terminal asset: executes ONLY when all upstream blocking checks pass.
        # Bumps data_version atomically, then runs post-ingest PostgreSQL tuning.
        commit_ingestion_run,
        # --- Reranker label dataset (offline, manual-trigger v1) ---
        # Five-asset graph that builds synthetic (query, c+, hard-negatives)
        # triples for bge-reranker-base fine-tuning. Multi-hop deferred to v2.
        reranker_chunk_population,
        reranker_chunk_sample,
        reranker_generated_queries,
        reranker_mined_negatives,
        reranker_label_dataset,
        # --- SMDI deposits (standalone, plan v1.1) ---
        # Daily count-gated refresh into public.smdi_deposits. Parallel to
        # the existing public_geo.pg_mineral_occurrence pipeline; see
        # docs/handoffs/smdi_ingestion_2026_05_25.md for the unification
        # question.
        smdi_deposits_refresh,
        # --- Catalog (Appendix F / Z.7) ---
        # Daily JSON dump of silver + gold schema → S3 catalogs bucket.
        data_dictionary_dump,
    ],
    asset_checks=[
        # Silver — collars (3 blocking checks)
        silver_collars_check_collar_count_positive,
        silver_collars_check_schema_conformance,
        silver_collars_check_crs_srid_populated,
        # Silver — tabular (4 blocking checks, one per asset)
        silver_surveys_check_parse_total_positive,
        silver_lithology_check_parse_total_positive,
        silver_samples_check_parse_total_positive,
        silver_well_logs_check_parse_total_positive,
        # CC-01 Item 1 Slice 3 — interval-overlap detection on silver tables.
        silver_lithology_interval_overlap,
        silver_samples_assays_v2_interval_overlap,
        # Silver — spatial (2 blocking checks)
        silver_spatial_check_geom_not_null,
        silver_spatial_check_srid_populated,
        # Silver — reports + XLSX (2 + 1 checks)
        silver_reports_check_parse_total_positive,
        silver_reports_check_schema_conformance,
        silver_xlsx_check_parse_total_positive,
        # Silver — seismic + xyz (Silver-trapped, DAG-05; 2 + 2 checks)
        silver_seismic_check_parse_total_positive,
        silver_seismic_check_schema_conformance,
        silver_xyz_check_parse_total_positive,
        silver_xyz_check_schema_conformance,
        # Evidence model — anchored to silver_reports (5 blocking checks)
        document_passages_check_no_duplicate_passage_ids,
        document_passages_check_text_hash_sha256_valid,
        document_revisions_check_document_id_not_null,
        document_revisions_check_sha256_format,
        evidence_items_check_exactly_one_ref,
        # Index — reports (2 checks: 1 blocking, 1 warn-only at non-zero rates)
        index_reports_check_embedding_id_present,
        index_reports_check_parser_quality_floor,
        # Chunk 2 — drill_traces + COG (3 new blocking checks)
        desurvey_trace_count_matches_collar_count_with_surveys,
        bronze_raster_sources_discoverable_check,
        cog_readable_check,
        # Reranker label dataset — blocking gate on triple count + leakage rate
        reranker_label_dataset_minimum_size_check,
        # Catalog drift guard — compares today's data dictionary to yesterday's.
        data_dictionary_drift_check,
    ],
    schedules=[
        full_ingest_schedule,
        public_geoscience_weekly_refresh,
        public_geoscience_daily_edit_check,
        smdi_deposits_daily_refresh,
        silver_chat_cards_backfill_schedule,
        silver_dq_daily_schedule,
    ],
    sensors=[minio_upload_sensor],
    resources={
        "postgres": PostgresResource(
            password=EnvVar("POSTGRES_PASSWORD"),
        ),
        "minio": S3Resource(
            endpoint_url=EnvVar("S3_ENDPOINT_URL"),
            access_key=EnvVar("MINIO_ROOT_USER"),
            secret_key=EnvVar("MINIO_ROOT_PASSWORD"),
        ),
        "qdrant": QdrantResource(
            host=EnvVar("QDRANT_HOST"),
            port=6333,
        ),
        "neo4j": Neo4jResource(
            uri=EnvVar("NEO4J_URI"),
            # N4J-01 fix (2026-04-19 Module 2 Phase B): auth enabled to match
            # NEO4J_AUTH set on the neo4j service. Credentials read from env vars
            # injected by the dagster-daemon compose service env block.
            auth_enabled=True,
            username=EnvVar("NEO4J_USERNAME"),
            password=EnvVar("NEO4J_PASSWORD"),
        ),
        # vLLM OpenAI-compatible endpoint — used by the reranker_labels
        # asset group for synthetic query generation + self-critique.
        # Defaults route to the in-cluster vllm service; override via env
        # for staging clusters that point at a different inference host.
        "vllm": VllmResource(
            base_url=EnvVar("VLLM_URL"),
            model=EnvVar("VLLM_MODEL"),
        ),
    },
)
