"""Silver layer asset — parse Shapefiles/GeoJSON/GPKG from Bronze and insert into silver.spatial_features.

Downloads the spatial file from MinIO Bronze, runs it through the spatial_parser
(which reprojects to EPSG:4326 if necessary), and bulk-inserts one row per
feature into the silver.spatial_features PostGIS table.

CC-03 Item 4 — QField field-observation ingestion. When the parser flags a
GeoPackage as QField-authored (mobile-collected waypoints/polygons), this
asset:
  - sets georef_method='survey', crs_confidence=0.9 on every feature from a
    QField layer (matching the CC-01 Item 2 vocabulary);
  - maps the QField 'accuracy' attribute → spatial_uncertainty_m;
  - uploads BLOB photos to bronze/qfield_photos/{workspace}/{sha}.jpg in
    MinIO and stashes the object key in properties.qfield_photo_key;
  - writes one silver.document_domain_tag row tagging the source file as
    (domain=geology, sub_type=field_observation [218]) when document_id +
    workspace_id are configured.

silver.spatial_features schema contract (Section 04e):
  feature_id    UUID PRIMARY KEY
  project_id    UUID (nullable — not all spatial features belong to a single project)
  feature_type  TEXT
  feature_name  TEXT
  source        TEXT        -- e.g. "shapefile" or "geojson"
  source_file   TEXT
  source_crs    TEXT        -- original CRS before reprojection
  properties    JSONB       -- all non-geometry attributes
  geom          GEOMETRY(Geometry, 4326)
  created_at    TIMESTAMPTZ DEFAULT NOW()
  updated_at    TIMESTAMPTZ DEFAULT NOW()

Each run generates fresh UUIDs for all features.  To avoid unbounded growth on
re-runs the asset deletes all existing rows for (source_file, project_id) before
inserting — this is safe because the Bronze layer is the immutable source of
truth and re-runs always start from MinIO.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import hashlib
import tempfile
import uuid

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.bronze_spatial import BRONZE_BUCKET, SPATIAL_PREFIX, bronze_spatial
from georag_dagster.parsers.spatial_parser import parse_spatial_file
from georag_dagster.resources import S3Resource, PostgresResource

# CC-03 Item 4 — QField → Geology / field_observation tag.
# sub_type_id 218 is seeded by migration 2026_05_24_020000.
DOMAIN_GEOLOGY_ID = 2
SUB_TYPE_FIELD_OBSERVATION_ID = 218
QFIELD_PHOTOS_PREFIX = "qfield_photos"

PROVENANCE_INSERT_SQL = """
INSERT INTO bronze.provenance (
    target_schema, target_table, target_id,
    source_file, source_file_sha256, source_row, source_col_map,
    parser_name, parser_version, ingest_run_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING;
"""


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Delete existing rows for this (source_file, project_id) pair before re-inserting.
# This makes re-runs idempotent without needing a unique constraint on the
# content — the Bronze file is the authoritative source and replaces prior state.
DELETE_EXISTING_SQL = """
DELETE FROM silver.spatial_features
WHERE source_file = %(source_file)s
  AND (
      %(project_id)s IS NULL AND project_id IS NULL
      OR project_id = %(project_id)s::uuid
  );
"""

INSERT_FEATURE_SQL = """
INSERT INTO silver.spatial_features (
    feature_id,
    project_id,
    feature_type,
    feature_name,
    feature_role,
    source,
    source_file,
    source_file_sha256,
    source_layer,
    source_feature_id,
    source_crs,
    crs_epsg_native,
    properties,
    confidence,
    spatial_uncertainty_m,
    crs_confidence,
    georef_method,
    interpretation_pdf_id,
    geom
) VALUES (
    %(feature_id)s,
    %(project_id)s,
    %(feature_type)s,
    %(feature_name)s,
    %(feature_role)s,
    %(source)s,
    %(source_file)s,
    %(source_file_sha256)s,
    %(source_layer)s,
    %(source_feature_id)s,
    %(source_crs)s,
    %(crs_epsg_native)s,
    %(properties)s::jsonb,
    %(confidence)s,
    %(spatial_uncertainty_m)s,
    %(crs_confidence)s,
    %(georef_method)s,
    %(interpretation_pdf_id)s,
    ST_GeomFromText(%(geometry_wkt)s, 4326)
);
"""

# CC-03 Item 4 follow-on — register an uploaded QField photo as a row in
# bronze.source_files so silver.spatial_features.interpretation_pdf_id can
# FK to it. Idempotent on (workspace_id, file_sha256). RETURNING id lets us
# capture the existing row's UUID on conflict.
UPSERT_PHOTO_SOURCE_FILE_SQL = """
INSERT INTO bronze.source_files (
    workspace_id, seaweedfs_key, original_filename, file_sha256,
    file_size_bytes, mime_type, source_type, data_type
) VALUES (
    %(workspace_id)s::uuid, %(seaweedfs_key)s, %(original_filename)s,
    %(file_sha256)s, %(file_size_bytes)s, 'image/jpeg', 'qfield_photo', 'image'
)
ON CONFLICT (workspace_id, file_sha256) DO UPDATE
   SET seaweedfs_key = EXCLUDED.seaweedfs_key
RETURNING id;
"""

# CC-03 Item 4 — tag the source file with (geology, field_observation) when
# the parser detected a QField .gpkg. ON CONFLICT swallows replays under the
# uq_ddt_document_domain_sub_type unique index.
DOCUMENT_DOMAIN_TAG_INSERT_SQL = """
INSERT INTO silver.document_domain_tag (
    document_id, domain_id, sub_type_id, workspace_id,
    assigned_by, assigned_confidence, extraction_status
) VALUES (
    %(document_id)s::uuid, %(domain_id)s, %(sub_type_id)s, %(workspace_id)s::uuid,
    'auto', %(confidence)s, 'extracted'
)
ON CONFLICT (document_id, domain_id, COALESCE(sub_type_id, 0)) DO UPDATE
   SET assigned_confidence = EXCLUDED.assigned_confidence,
       extraction_status   = EXCLUDED.extraction_status,
       updated_at          = now();
"""


def _epsg_from_crs_string(crs: str | None) -> int | None:
    """Pull the integer EPSG code out of a 'EPSG:1234' source_crs string."""
    if not crs:
        return None
    if crs.upper().startswith("EPSG:"):
        try:
            return int(crs.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
    return None

# Post-load tuning: GIST index on the geometry column + ANALYZE.
POSTLOAD_SQL = """
DO $$
BEGIN
    -- DB review #5 — converge on the Laravel-migration index name
    -- (idx_spatial_features_geom) so Dagster doesn't race-create a
    -- duplicate GIST.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'spatial_features'
          AND indexname  = 'idx_spatial_features_geom'
    ) THEN
        CREATE INDEX idx_spatial_features_geom
            ON silver.spatial_features USING GIST (geom);
    END IF;
END$$;

CLUSTER silver.spatial_features USING idx_spatial_features_geom;
ANALYZE silver.spatial_features;
"""


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverSpatialConfig(Config):
    """Runtime configuration for the silver_spatial asset."""

    # Basename of the spatial file uploaded in the bronze_spatial asset.
    # Example: "prospect_boundaries.shp" or "alteration_polygons.geojson"
    spatial_filename: str

    # Optional project_id (UUID string).  Set to empty string if the spatial
    # file covers multiple projects or is not project-scoped.
    project_id: str = ""

    # Optional explicit feature_type override.  If set, all features in the
    # file are tagged with this type instead of heuristic inference.
    # Example values: "boundary", "alteration", "fault", "contact", "target"
    feature_type: str = ""

    # Sprint 5 Phase 1 plumbing — vendor column-mapping profile ID.
    # Extracted from MinIO object metadata x-georag-vendor-profile-id by the
    # minio_upload_sensor.  The parser does NOT use this yet (Phase 2).
    vendor_profile_id: int | None = None

    # CC-03 Item 4 — QField plumbing. All optional. When document_id +
    # workspace_id are supplied and the parser flags the file as QField,
    # a silver.document_domain_tag row (domain=geology,
    # sub_type=field_observation) is written and workspace_id partitions
    # the bronze/qfield_photos/ MinIO path.
    document_id: str = ""
    workspace_id: str = ""
    # CC-03 Item 4 follow-on — resolve QField filename-ref photos (where
    # the .gpkg stores e.g. 'DCIM/IMG_0001.JPG' instead of an inline BLOB).
    # The asset will look up '{photos_minio_prefix}/{ref}' inside the
    # bronze bucket; on download success the bytes are uploaded to the
    # canonical qfield_photos/ path and registered just like a BLOB photo.
    # Empty string disables resolution.
    qfield_photos_minio_prefix: str = ""


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=[bronze_spatial],
    description=(
        "Download Shapefile/GeoJSON from MinIO Bronze, parse and reproject to "
        "EPSG:4326, then insert features into silver.spatial_features."
    ),
)
def silver_spatial(
    context: AssetExecutionContext,
    config: SilverSpatialConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Parse Bronze spatial file → reproject → insert into silver.spatial_features."""

    context.log.info("vendor_profile_id: %s", config.vendor_profile_id)
    object_name = f"{SPATIAL_PREFIX}/{config.spatial_filename}"
    context.log.info(
        "Silver spatial: downloading '%s/%s' from MinIO", BRONZE_BUCKET, object_name
    )

    # --- Download from Bronze to a temporary file ---
    file_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)

    # Preserve extension so GeoPandas / Fiona detects the driver correctly
    suffix = "." + config.spatial_filename.rsplit(".", 1)[-1] if "." in config.spatial_filename else ""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    context.log.info(
        "Silver spatial: downloaded %d bytes to temp file '%s'",
        len(file_bytes),
        tmp_path,
    )

    # --- Parse ---
    feature_type_override = config.feature_type if config.feature_type else None
    parse_result = parse_spatial_file(tmp_path, feature_type=feature_type_override)

    context.log.info(
        "Silver spatial: parse complete — format=%s, source_crs=%s, "
        "features=%d, empty_skipped=%d",
        parse_result.source_format,
        parse_result.source_crs,
        parse_result.feature_count,
        parse_result.empty_geom_skipped,
    )

    if parse_result.skipped_details:
        for detail in parse_result.skipped_details:
            context.log.warning("Skipped feature: %s", detail)

    if parse_result.feature_count == 0:
        context.log.warning(
            "Silver spatial: parse returned 0 valid features — silver.spatial_features unchanged."
        )
        return MaterializeResult(
            metadata={
                "spatial_filename":   MetadataValue.text(config.spatial_filename),
                "source_format":      MetadataValue.text(parse_result.source_format),
                "source_crs":         MetadataValue.text(parse_result.source_crs),
                "feature_count":      MetadataValue.int(0),
                "empty_geom_skipped": MetadataValue.int(parse_result.empty_geom_skipped),
                "project_id":         MetadataValue.text(config.project_id or ""),
            }
        )

    project_id_val = config.project_id if config.project_id else None
    document_id_val = config.document_id or None
    workspace_id_val = config.workspace_id or None

    # --- Build insert params ---
    crs_epsg_native = _epsg_from_crs_string(parse_result.source_crs)
    source_sha256 = (parse_result.provenance or {}).get("source_file_sha256")
    photo_upload_count = 0
    photo_fk_count = 0

    # When QField features carry photo BLOBs AND we have a workspace_id,
    # register each uploaded photo as a bronze.source_files row so the
    # spatial_features.interpretation_pdf_id FK can point at it. Without a
    # workspace_id the FK is left NULL and only the MinIO key is recorded
    # in the properties JSONB blob.
    photo_register_conn = None
    photo_register_cur = None
    if parse_result.is_qfield and workspace_id_val:
        photo_register_conn = postgres.get_connection()
        photo_register_cur = photo_register_conn.cursor()

    insert_params: list[dict] = []
    for feature in parse_result.features:
        # source_layer + source_feature_id ride along inside feature.properties
        # for multi-layer formats (GPKG, OpenFileGDB) — see spatial_parser.
        feature_props = dict(feature.properties or {})
        source_layer = feature_props.pop("_layer_name", None)
        source_feature_id = feature_props.pop("_fid", None)
        confidence = feature_props.pop("_crs_confidence", None)

        # --- QField field-observation hoist ---
        # The parser populates these synthetic keys on every feature from a
        # QField-detected layer. Pop them out of properties so they don't
        # leak into the JSONB blob (we promote them to first-class columns
        # / hand the photo bytes off to MinIO).
        is_qfield_feature = feature_props.pop("_qfield", False)
        qfield_accuracy = feature_props.pop("_qfield_accuracy_m", None)
        # _qfield_timestamp / _qfield_device / _qfield_photo_ref are left in
        # feature_props for traceability — they ride into the JSONB blob.
        photo_bytes = feature_props.pop("_qfield_photo_bytes", None)
        interpretation_pdf_id: str | None = None

        # Filename-ref → BLOB resolution: when the QField row pointed at a
        # photo by name (e.g. 'DCIM/IMG_0001.JPG') instead of inlining it,
        # try to fetch the bytes from the configured bronze/<prefix>/<ref>
        # location. On success the rest of the photo pipeline treats it
        # identically to a BLOB photo (upload + bronze.source_files + FK).
        photo_ref = feature_props.get("_qfield_photo_ref")
        if (
            not photo_bytes
            and is_qfield_feature
            and photo_ref
            and config.qfield_photos_minio_prefix
        ):
            ref_key = f"{config.qfield_photos_minio_prefix.rstrip('/')}/{photo_ref.lstrip('/')}"
            try:
                photo_bytes = minio.download_bytes(BRONZE_BUCKET, ref_key)
                context.log.info(
                    "Silver spatial: resolved QField photo ref '%s' → %d bytes from '%s/%s'",
                    photo_ref, len(photo_bytes), BRONZE_BUCKET, ref_key,
                )
            except Exception as ref_exc:
                feature_props["qfield_photo_ref_unresolved"] = photo_ref
                context.log.warning(
                    "Silver spatial: QField photo ref '%s' not found at '%s/%s': %s",
                    photo_ref, BRONZE_BUCKET, ref_key, ref_exc,
                )

        if photo_bytes and is_qfield_feature:
            try:
                photo_sha = hashlib.sha256(photo_bytes).hexdigest()
                workspace_part = workspace_id_val or "unknown"
                photo_key = f"{QFIELD_PHOTOS_PREFIX}/{workspace_part}/{photo_sha}.jpg"
                minio.upload_bytes(
                    BRONZE_BUCKET,
                    photo_key,
                    photo_bytes,
                    content_type="image/jpeg",
                )
                feature_props["qfield_photo_key"] = photo_key
                feature_props["qfield_photo_sha256"] = photo_sha
                photo_upload_count += 1

                # Register the photo as a bronze.source_files row so the
                # interpretation_pdf_id FK is non-NULL. Only possible when
                # workspace_id is configured (NOT NULL on source_files).
                if photo_register_cur is not None:
                    try:
                        photo_register_cur.execute(
                            UPSERT_PHOTO_SOURCE_FILE_SQL,
                            {
                                "workspace_id":      workspace_id_val,
                                "seaweedfs_key":     photo_key,
                                "original_filename": f"qfield_photo_{photo_sha[:12]}.jpg",
                                "file_sha256":       photo_sha,
                                "file_size_bytes":   len(photo_bytes),
                            },
                        )
                        row = photo_register_cur.fetchone()
                        if row:
                            interpretation_pdf_id = str(row[0])
                            photo_fk_count += 1
                    except Exception as fk_exc:
                        context.log.warning(
                            "Silver spatial: bronze.source_files upsert failed for "
                            "photo %s: %s",
                            photo_sha, fk_exc,
                        )
            except Exception as photo_exc:
                context.log.warning(
                    "Silver spatial: QField photo upload failed for feature %s: %s",
                    feature.name, photo_exc,
                )

        # --- CC-01 Item 2 + CC-03 Item 4 — uncertainty / CRS provenance ---
        if is_qfield_feature:
            spatial_uncertainty_m = (
                float(qfield_accuracy) if qfield_accuracy is not None else None
            )
            crs_confidence_val = 0.9
            georef_method_val = "survey"
            feature_role = "qfield_observation"
        else:
            spatial_uncertainty_m = None
            crs_confidence_val = None
            georef_method_val = None
            feature_role = None

        insert_params.append(
            {
                "feature_id":          str(uuid.uuid4()),
                "project_id":          project_id_val,
                "feature_type":        feature.feature_type,
                "feature_name":        feature.name,
                "feature_role":        feature_role,
                "source":              parse_result.source_format,
                "source_file":         parse_result.source_file,
                "source_file_sha256":  source_sha256,
                "source_layer":        source_layer,
                "source_feature_id":   str(source_feature_id) if source_feature_id is not None else None,
                "source_crs":          parse_result.source_crs,
                "crs_epsg_native":     crs_epsg_native,
                "properties":          psycopg2.extras.Json(feature_props),
                "confidence":          float(confidence) if confidence is not None else None,
                "spatial_uncertainty_m": spatial_uncertainty_m,
                "crs_confidence":      crs_confidence_val,
                "georef_method":       georef_method_val,
                "interpretation_pdf_id": interpretation_pdf_id,
                "geometry_wkt":        feature.geometry_wkt,
            }
        )

    # Commit + close the photo-registration cursor before the main feature
    # INSERT so the FKs are visible inside the same transaction window.
    if photo_register_conn is not None:
        try:
            photo_register_conn.commit()
        finally:
            if photo_register_cur is not None:
                photo_register_cur.close()
            photo_register_conn.close()

    inserted_count = 0

    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            # Delete prior rows for this file to keep re-runs idempotent
            cur.execute(
                DELETE_EXISTING_SQL,
                {
                    "source_file": parse_result.source_file,
                    "project_id":  project_id_val,
                },
            )
            deleted_count = cur.rowcount
            if deleted_count > 0:
                context.log.info(
                    "Silver spatial: removed %d existing rows for source_file='%s'",
                    deleted_count,
                    parse_result.source_file,
                )

            # Insert all features — execute_batch is efficient for moderate sets;
            # LargeGeometries (MultiPolygon, complex boundaries) are row-by-row
            # in the SQL so page_size is kept moderate to avoid huge param lists.
            psycopg2.extras.execute_batch(
                cur,
                INSERT_FEATURE_SQL,
                insert_params,
                page_size=100,
            )
            inserted_count = len(insert_params)
        conn.commit()

    context.log.info(
        "Silver spatial: inserted %d features into silver.spatial_features",
        inserted_count,
    )

    # --- Provenance INSERT (bronze.provenance) ---
    prov = parse_result.provenance
    if prov:
        ingest_run_id = str(uuid.uuid4())
        prov_params = [
            (
                "silver", "spatial_features", p["feature_id"],
                prov.get("source_file"), prov.get("source_file_sha256"),
                None,  # source_row not applicable for spatial features
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
                        cur, PROVENANCE_INSERT_SQL, prov_params, page_size=100
                    )
                conn.commit()
            context.log.info(
                "Provenance: inserted %d rows into bronze.provenance for silver.spatial_features",
                len(prov_params),
            )
        except Exception as prov_exc:
            context.log.warning(
                "Provenance INSERT skipped (table may not exist yet): %s", prov_exc
            )

    # --- Post-load PostGIS tuning ---
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(POSTLOAD_SQL)
        conn.commit()

    context.log.info(
        "Silver spatial: GIST index ensured, CLUSTER run, ANALYZE complete on "
        "silver.spatial_features"
    )

    # --- CC-03 Item 4 — QField document_domain_tag write ---
    domain_tag_written = False
    if parse_result.is_qfield and document_id_val and workspace_id_val:
        try:
            with postgres.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        DOCUMENT_DOMAIN_TAG_INSERT_SQL,
                        {
                            "document_id":  document_id_val,
                            "domain_id":    DOMAIN_GEOLOGY_ID,
                            "sub_type_id": SUB_TYPE_FIELD_OBSERVATION_ID,
                            "workspace_id": workspace_id_val,
                            "confidence":   0.9,
                        },
                    )
                conn.commit()
            domain_tag_written = True
            context.log.info(
                "Silver spatial: tagged source_file=%s as (geology, field_observation)",
                parse_result.source_file,
            )
        except Exception as tag_exc:
            context.log.warning(
                "Silver spatial: document_domain_tag write skipped: %s", tag_exc
            )
    elif parse_result.is_qfield:
        context.log.warning(
            "Silver spatial: QField detected on '%s' but document_id / "
            "workspace_id not configured — domain tag NOT written. "
            "Re-run with both set to record the (geology, field_observation) tag.",
            parse_result.source_file,
        )

    return MaterializeResult(
        metadata={
            "spatial_filename":   MetadataValue.text(config.spatial_filename),
            "source_format":      MetadataValue.text(parse_result.source_format),
            "source_crs":         MetadataValue.text(parse_result.source_crs),
            "target_crs":         MetadataValue.text("EPSG:4326"),
            "feature_count":      MetadataValue.int(inserted_count),
            "empty_geom_skipped": MetadataValue.int(parse_result.empty_geom_skipped),
            "project_id":         MetadataValue.text(project_id_val or ""),
            "vendor_profile_id":   MetadataValue.text(str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"),
            "feature_type_override": MetadataValue.text(feature_type_override or ""),
            "is_qfield":           MetadataValue.bool(parse_result.is_qfield),
            "qfield_layers":       MetadataValue.text(", ".join(parse_result.qfield_layers)),
            "qfield_photos_uploaded": MetadataValue.int(photo_upload_count),
            "qfield_photo_fk_count": MetadataValue.int(photo_fk_count),
            "qfield_domain_tag_written": MetadataValue.bool(domain_tag_written),
        }
    )
