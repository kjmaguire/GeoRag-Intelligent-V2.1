"""Ingest geology vector data into silver.spatial_features.

Formats
-------
Everything ``georag_geoparsers.spatial_parser`` reads — ESRI Shapefile,
GeoJSON, GeoPackage, GML, GPX, DXF, File Geodatabase, FlatGeobuf — plus QGIS
projects (``.qgs`` / ``.qgz``) via ``georag_geoparsers.qgis_parser``.

Why this workflow exists
------------------------
The `spatial` upload category was answered with ``422 retired_pipeline`` from
2026-07-28, when the Dagster services were removed. The parsers were never the
problem — they are the most heavily audited code in the ingestion path (CRS
confidence scoring, QField detection, multi-layer GeoPackage handling) and
they kept working. What went away was the thing that called them. Uploading a
shapefile has been impossible since, which is a strange gap in a geology
platform.

A QGIS project is not a data file
---------------------------------
``.qgs`` / ``.qgz`` describe a *map*: which layers to draw and where their
data lives. Two outcomes, and they are reported differently on purpose:

  * The project bundles its data (the usual ``.qgz`` case) — each resolvable
    layer is parsed and written, tagged with its own ``source_layer``.
  * The project points at the geologist's own disk — nothing can be read.
    That is NOT a parse failure, and reporting "0 features" would read like
    one. The run completes with ``manifest_only`` set and the layer inventory
    in its stats, so the answer to "what is in this project" survives even
    when the data did not come with it.

Tenancy
-------
Every row carries workspace_id and project_id, and the connection binds the
workspace GUC before writing, so silver.spatial_features' RLS policy applies
to the insert rather than being bypassed.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time as _t
from pathlib import Path
from typing import Any

import asyncpg
from georag_object_storage import Bucket, get_storage_client
from hatchet_sdk import Context
from pydantic import BaseModel, Field, field_validator

from app.hatchet_workflows import _progress, hatchet

log = logging.getLogger("georag.hatchet.ingest_spatial")

#: Extensions this workflow claims. Anything else should never be routed here
#: — the upload controller decides, and a mismatch is a routing bug worth
#: failing loudly on rather than silently producing zero features.
VECTOR_EXTENSIONS = frozenset({
    ".shp", ".geojson", ".json", ".gpkg", ".gml", ".gpx",
    ".dxf", ".dgn", ".gdb", ".fgb",
})
QGIS_PROJECT_EXTENSIONS = frozenset({".qgs", ".qgz"})
SUPPORTED_EXTENSIONS = VECTOR_EXTENSIONS | QGIS_PROJECT_EXTENSIONS

#: Rows per executemany batch. Large enough to amortise round trips, small
#: enough that one oversized layer cannot build a single multi-hundred-MB
#: statement in memory.
_INSERT_BATCH = 500


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


class IngestSpatialInput(BaseModel):
    """Payload handed over by Laravel's UploadController."""

    workspace_id: str
    project_id: str
    minio_key: str
    run_id: str | None = None
    #: Overrides the parser's per-feature heuristic. The geologist knows what
    #: they uploaded ("these are all faults") better than a name-sniffing rule.
    feature_type: str | None = Field(default=None)

    @field_validator("workspace_id", "project_id")
    @classmethod
    def _must_be_uuid(cls, v: str) -> str:
        import uuid  # noqa: PLC0415

        uuid.UUID(v)  # raises on malformed input, at the trigger boundary
        return v


class IngestSpatialOut(BaseModel):
    run_id: str | None
    source_format: str
    features_written: int
    layers: list[str]
    #: True when a QGIS project was catalogued but none of its data shipped.
    manifest_only: bool = False
    #: Layer inventory for a QGIS project, whether or not it resolved.
    project_layers: list[dict[str, Any]] = Field(default_factory=list)
    empty_geom_skipped: int = 0
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0


_INSERT_SQL = """
INSERT INTO silver.spatial_features (
    feature_id, workspace_id, project_id,
    feature_type, feature_name, source, source_file, source_crs,
    source_layer, source_feature_id, properties,
    crs_epsg_native, crs_confidence, georef_method,
    created_at, updated_at, geom
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid,
    $3, $4, $5, $6, $7,
    $8, $9, $10::jsonb,
    $11, $12, $13,
    NOW(), NOW(),
    ST_SetSRID(ST_GeomFromText($14::text), 4326)
)
"""


def _crs_epsg(source_crs: str | None) -> int | None:
    """Pull the numeric code out of an ``EPSG:26913``-style string."""
    if not source_crs:
        return None
    text = str(source_crs).strip().upper()
    if text.startswith("EPSG:"):
        try:
            return int(text.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
    return None


async def _write_features(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    project_id: str,
    parse_result: Any,
    source_file: str,
    source_label: str,
    layer_override: str | None,
    georef_method: str,
    crs_confidence: float | None,
) -> int:
    """Insert one parse result's features. Returns the row count written."""
    import json  # noqa: PLC0415

    epsg = _crs_epsg(parse_result.source_crs)
    rows = []
    for feat in parse_result.features:
        props = dict(feat.properties or {})
        # _layer_name is the parser's bookkeeping column, not upstream data.
        # It becomes source_layer, so leaving it in properties duplicates it
        # into every row's jsonb for no benefit.
        #
        # Popped UNCONDITIONALLY. Writing this as
        # `layer_override or props.pop(...)` short-circuits whenever an
        # override is supplied — which is exactly the QGIS-project path — so
        # the column stayed in the jsonb for every row that had a named layer.
        parsed_layer = props.pop("_layer_name", None)
        layer_name = layer_override or parsed_layer
        rows.append((
            workspace_id,
            project_id,
            feat.feature_type,
            feat.name,
            source_label,
            source_file,
            parse_result.source_crs,
            layer_name,
            props.get("fid") and str(props.get("fid")) or None,
            json.dumps(props, default=str),
            epsg,
            crs_confidence,
            georef_method,
            feat.geometry_wkt,
        ))

    written = 0
    for start in range(0, len(rows), _INSERT_BATCH):
        chunk = rows[start:start + _INSERT_BATCH]
        await conn.executemany(_INSERT_SQL, chunk)
        written += len(chunk)
    return written


ingest_spatial = hatchet.workflow(
    name="ingest_spatial",
    input_validator=IngestSpatialInput,
)


@ingest_spatial.task(execution_timeout="2h", retries=1)
async def run_ingest_spatial(
    input: IngestSpatialInput, ctx: Context,
) -> IngestSpatialOut:
    """Download, parse and persist one vector file or QGIS project."""
    from georag_geoparsers.qgis_parser import parse_qgis_project  # noqa: PLC0415
    from georag_geoparsers.spatial_parser import parse_spatial_file  # noqa: PLC0415

    t0 = _t.monotonic()
    store = get_storage_client()
    filename = input.minio_key.rsplit("/", 1)[-1]
    suffix = Path(filename).suffix.lower()

    if suffix not in SUPPORTED_EXTENSIONS:
        # A routing bug, not user error: the upload controller decides which
        # workflow a file goes to. Failing loudly beats writing zero features
        # and reporting success.
        raise ValueError(
            f"ingest_spatial cannot handle {suffix!r} ({filename}); "
            f"supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    run_id = input.run_id or await _progress.start_run(
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        minio_key=input.minio_key,
        triggered_by="upload",
        workflow_run_id=getattr(ctx, "workflow_run_id", None),
    )

    warnings: list[dict[str, Any]] = []
    project_layers: list[dict[str, Any]] = []
    manifest_only = False
    layers_written: list[str] = []
    features_written = 0
    empty_skipped = 0
    source_format = suffix.lstrip(".")

    try:
        if run_id:
            await _progress.mark_stage_started(run_id=run_id, stage="preflight")

        with tempfile.TemporaryDirectory(prefix="georag_spatial_") as tmpdir:
            local = Path(tmpdir) / filename
            import asyncio  # noqa: PLC0415

            await asyncio.to_thread(
                store.get_file, Bucket.BRONZE, input.minio_key, str(local),
            )

            if run_id:
                await _progress.mark_stage_started(run_id=run_id, stage="parse")

            # ── QGIS project ────────────────────────────────────────────
            if suffix in QGIS_PROJECT_EXTENSIONS:
                project = parse_qgis_project(str(local))
                source_format = project.source_format
                warnings.extend(project.warnings)
                manifest_only = project.is_manifest_only
                project_layers = [
                    {
                        "name": lyr.name,
                        "provider": lyr.provider,
                        "crs": lyr.crs,
                        "geometry_type": lyr.geometry_type,
                        "resolved": lyr.resolved,
                        "sublayer": lyr.sublayer,
                    }
                    for lyr in project.layers
                ]

                parsed: list[tuple[str, Any]] = []
                for lyr in project.layers:
                    if not lyr.resolved or not lyr.resolved_path:
                        continue
                    try:
                        parsed.append((
                            lyr.name,
                            parse_spatial_file(
                                lyr.resolved_path,
                                feature_type=input.feature_type,
                                layer=lyr.sublayer,
                            ),
                        ))
                    except Exception as exc:  # noqa: BLE001 — one layer must not sink the project
                        warnings.append({
                            "code": "layer_parse_failed",
                            "layer": lyr.name,
                            "detail": str(exc)[:300],
                        })
            else:
                parsed = [(None, parse_spatial_file(
                    str(local), feature_type=input.feature_type,
                ))]

            if run_id:
                await _progress.mark_stage_started(run_id=run_id, stage="persist")

            conn = await asyncpg.connect(_build_dsn())
            try:
                # Bind the workspace GUC so silver.spatial_features' RLS
                # policy applies to these inserts rather than being bypassed
                # by the owner role. is_local=false because this is a plain
                # connection, not a transaction block — see the Hyperdrive
                # GUC note in the tenancy docs before changing it.
                await conn.execute(
                    "SELECT set_config('app.workspace_id', $1, false)",
                    input.workspace_id,
                )
                for layer_name, result in parsed:
                    empty_skipped += result.empty_geom_skipped
                    warnings.extend(result.warnings or [])
                    crs_conf, georef = _crs_quality(result)
                    n = await _write_features(
                        conn,
                        workspace_id=input.workspace_id,
                        project_id=input.project_id,
                        parse_result=result,
                        source_file=filename,
                        source_label=source_format,
                        layer_override=layer_name,
                        georef_method=georef,
                        crs_confidence=crs_conf,
                    )
                    features_written += n
                    layers_written.extend(
                        [layer_name] if layer_name else (result.layer_names or [])
                    )
            finally:
                await conn.close()

        if run_id:
            await _progress.mark_completed_by_run(run_id=run_id)

    except Exception as exc:
        if run_id:
            await _progress.mark_failed_by_run(
                run_id=run_id, error_text=str(exc)[:1000],
            )
        log.exception("ingest_spatial failed for %s", input.minio_key)
        raise

    out = IngestSpatialOut(
        run_id=run_id,
        source_format=source_format,
        features_written=features_written,
        layers=sorted({lyr for lyr in layers_written if lyr}),
        manifest_only=manifest_only,
        project_layers=project_layers,
        empty_geom_skipped=empty_skipped,
        warnings=warnings[:20],
        duration_ms=int((_t.monotonic() - t0) * 1000),
    )
    log.info("ingest_spatial complete: %s", out.model_dump(exclude={"project_layers"}))
    return out


def _crs_quality(result: Any) -> tuple[float | None, str]:
    """Map the parser's CRS finding onto the CC-01 georef columns.

    ``georef_method`` is CHECK-constrained to
    declared / detected / assumed / manual / survey, and the distinction is
    not cosmetic: it drives the map's positional-uncertainty ring. A CRS
    assumed to be WGS84 that is really UTM puts features a continent away,
    and 'assumed' is the only honest way to say the location may be wrong.

    QField captures come from a GNSS receiver in someone's hand, which is a
    genuine survey fix — the parser detects those separately and they get
    the fixed 0.9 confidence the pipeline has always assigned them.
    """
    if getattr(result, "is_qfield", False):
        return 0.9, "survey"

    confidence = getattr(result, "crs_confidence", None)
    if confidence is None:
        return None, "assumed"

    confidence = float(confidence)
    if confidence >= 0.85:
        # The file carried an explicit, self-consistent CRS.
        return confidence, "declared"
    if confidence >= 0.5:
        # Inferred from coordinate ranges rather than stated outright.
        return confidence, "detected"
    return confidence, "assumed"


__all__ = [
    "QGIS_PROJECT_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "VECTOR_EXTENSIONS",
    "IngestSpatialInput",
    "IngestSpatialOut",
    "ingest_spatial",
]
