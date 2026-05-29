"""Silver layer asset — land geophysics survey metadata into silver.geophysics_surveys.

Phase 4 Step 4.2 left the geophysics_surveys table in place but the ingestion
writer deferred. This asset closes the gap: it accepts a manually-prepared
JSON payload (survey_type, line_ids, aoi_geom WKT, contractor, dates, summary)
and inserts one row into silver.geophysics_surveys.

Why JSON-payload-first rather than a full PDF interpretation parser:

  * V1 geophysics scope is narrow — the master plan only requires the survey
    METADATA to be queryable (so the agentic-retrieval anomaly subgraph can
    answer "show me magnetic surveys covering this AOI" + show the
    interpretation PDF as a citation).
  * Full-waveform / channel parsing is Roadmap (see §11b SEG-Y row and
    Geosoft GDB row).
  * A JSON-payload writer unblocks the agentic retrieval path immediately
    and is forward-compatible with a future PDF-derived parser that emits
    the same dict shape.

The asset reads its payload from MinIO Bronze (one .json file per survey)
under a `geophysics/` prefix. The expected JSON keys match the column shape
of silver.geophysics_surveys 1:1.

silver.geophysics_surveys schema contract (Phase 4 Step 4.2):
  survey_id              uuid PK (auto)
  workspace_id           uuid NOT NULL (FK silver.workspaces)
  project_id             uuid nullable (FK silver.projects)
  survey_type            varchar(16) CHECK ∈
      seismic | magnetic | gravity | radiometric | IP | EM | other
  survey_name            text NOT NULL
  contractor             text nullable
  acquisition_date       date nullable
  line_ids               text[] nullable
  aoi_geom               geometry(Polygon, 4326) nullable
  crs_epsg               int nullable
  processing_notes       text nullable
  interpretation_pdf_id  uuid → bronze.source_files(id) nullable
  anomaly_summary        text nullable
  created_at / updated_at

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import json
import uuid

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.resources import S3Resource, PostgresResource


BRONZE_BUCKET = "georag-bronze"
GEOPHYSICS_PREFIX = "geophysics"


VALID_SURVEY_TYPES = frozenset({
    "seismic", "magnetic", "gravity", "radiometric", "IP", "EM", "other",
})


INSERT_SURVEY_SQL = """
INSERT INTO silver.geophysics_surveys (
    survey_id,
    workspace_id,
    project_id,
    survey_type,
    survey_name,
    contractor,
    acquisition_date,
    line_ids,
    aoi_geom,
    crs_epsg,
    processing_notes,
    interpretation_pdf_id,
    anomaly_summary
) VALUES (
    %(survey_id)s,
    %(workspace_id)s,
    %(project_id)s,
    %(survey_type)s,
    %(survey_name)s,
    %(contractor)s,
    %(acquisition_date)s,
    %(line_ids)s,
    CASE WHEN %(aoi_wkt)s IS NULL THEN NULL
         ELSE ST_GeomFromText(%(aoi_wkt)s, 4326) END,
    %(crs_epsg)s,
    %(processing_notes)s,
    %(interpretation_pdf_id)s,
    %(anomaly_summary)s
)
ON CONFLICT (workspace_id, survey_name) DO UPDATE SET
    survey_type           = EXCLUDED.survey_type,
    contractor            = EXCLUDED.contractor,
    acquisition_date      = EXCLUDED.acquisition_date,
    line_ids              = EXCLUDED.line_ids,
    aoi_geom               = EXCLUDED.aoi_geom,
    crs_epsg              = EXCLUDED.crs_epsg,
    processing_notes      = EXCLUDED.processing_notes,
    interpretation_pdf_id = EXCLUDED.interpretation_pdf_id,
    anomaly_summary       = EXCLUDED.anomaly_summary,
    project_id            = EXCLUDED.project_id,
    updated_at            = now();
"""


class SilverGeophysicsConfig(Config):
    """Runtime configuration for the silver_geophysics asset."""

    # Full MinIO object key under the geophysics/ prefix.
    # Laravel UploadController writes to ``geophysics/{project_id}/{ts}_{file}.json``;
    # ad-hoc Dagster runs may use ``geophysics/{file}.json``. Either way we
    # take the full key here so the caller controls the layout.
    object_key: str

    # Workspace UUID — required for tenant isolation.
    workspace_id: str

    # Optional project UUID. Empty string leaves NULL (multi-project survey).
    project_id: str = ""


@asset(
    group_name="silver",
    description=(
        "Read geophysics survey metadata JSON from MinIO Bronze and insert / "
        "upsert into silver.geophysics_surveys. JSON keys map 1:1 onto the "
        "schema; aoi_geom is provided as WKT in EPSG:4326."
    ),
)
def silver_geophysics(
    context: AssetExecutionContext,
    config: SilverGeophysicsConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Read geophysics survey JSON from Bronze → upsert silver.geophysics_surveys."""

    object_name = config.object_key
    if not object_name.startswith(GEOPHYSICS_PREFIX + "/"):
        raise ValueError(
            f"silver_geophysics: object_key must start with '{GEOPHYSICS_PREFIX}/' "
            f"(got {object_name!r})"
        )
    context.log.info("silver_geophysics: downloading '%s/%s'", BRONZE_BUCKET, object_name)

    raw_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)
    payload = json.loads(raw_bytes.decode("utf-8"))

    survey_type = payload.get("survey_type", "other")
    if survey_type not in VALID_SURVEY_TYPES:
        raise ValueError(
            f"Invalid survey_type '{survey_type}'. Must be one of: "
            f"{sorted(VALID_SURVEY_TYPES)}"
        )

    survey_name = payload.get("survey_name")
    if not survey_name:
        raise ValueError("payload missing required 'survey_name'")

    project_id_val = config.project_id if config.project_id else None
    survey_id = payload.get("survey_id") or str(uuid.uuid4())

    params = {
        "survey_id":             survey_id,
        "workspace_id":          config.workspace_id,
        "project_id":            project_id_val,
        "survey_type":           survey_type,
        "survey_name":           survey_name,
        "contractor":            payload.get("contractor"),
        "acquisition_date":      payload.get("acquisition_date"),  # ISO date string OK
        "line_ids":              payload.get("line_ids"),           # psycopg2 adapts list → text[]
        "aoi_wkt":               payload.get("aoi_wkt"),
        "crs_epsg":              payload.get("crs_epsg"),
        "processing_notes":      payload.get("processing_notes"),
        "interpretation_pdf_id": payload.get("interpretation_pdf_id"),
        "anomaly_summary":       payload.get("anomaly_summary"),
    }

    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(INSERT_SURVEY_SQL, params)
        conn.commit()

    context.log.info(
        "silver_geophysics: upserted survey_id=%s type=%s name='%s'",
        survey_id, survey_type, survey_name,
    )

    return MaterializeResult(
        metadata={
            "survey_id":      MetadataValue.text(survey_id),
            "survey_type":    MetadataValue.text(survey_type),
            "survey_name":    MetadataValue.text(survey_name),
            "workspace_id":   MetadataValue.text(config.workspace_id),
            "project_id":     MetadataValue.text(project_id_val or ""),
            "has_aoi":        MetadataValue.bool(bool(payload.get("aoi_wkt"))),
            "line_count":     MetadataValue.int(len(payload.get("line_ids") or [])),
        }
    )
