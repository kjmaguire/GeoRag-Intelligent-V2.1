"""Bronze Public Geoscience — raw ArcGIS REST FeatureServer pulls.

Phase 2.2 of the Public Geoscience feature. Four assets in the `bronze` group,
one per (jurisdiction, canonical_type) pair registered in Phase 1:

    bronze_pg_ca_sk_mine_loc            → pg_mine         (layer 1)
    bronze_pg_ca_sk_smdi                → pg_mineral_occurrence (layer 2)
    bronze_pg_ca_sk_drillhole           → pg_drillhole_collar   (layer 3)
    bronze_pg_ca_sk_resource_potential  → pg_resource_potential_zone
                                          (multi-layer — one per commodity)

Each asset:

  1. Reads its (service_url, source_crs) from `public_geo.sources`
     (seeded in Phase 1) — source registry is the single source of truth for
     endpoint URLs.
  2. Fetches service-level metadata (`serviceLastEditDate` for audit).
  3. Paginates `/query?f=geojson&outSR=<source_crs>` via the shared
     `arcgis_rest` client, preserving native CRS (EPSG:2957 for SK). No
     reprojection — that's Silver's job (plan §05b).
  4. Writes the FeatureCollection verbatim to MinIO at
        bronze/public_geoscience/{jurisdiction}/{source_id}/{run_id}.geojson
     and a sidecar manifest at
        …/public_geoscience/{jurisdiction}/{source_id}/{run_id}.manifest.json
     capturing HTTP metadata, server edit date, feature count, and the
     SHA-256 checksum of the GeoJSON body (plan §05a).
  5. Updates `public_geo.sources.last_refreshed_at` on success so the
     jurisdictions API surface (Phase 1) reflects reality.

Idempotency: Bronze is immutable — each run produces a new `{run_id}.geojson`
file. No de-duplication at Bronze; the checksum is the audit anchor. Silver
(Phase 2.3) drives the "no-op if unchanged" behaviour via per-feature
checksums against the canonical tables.

The Resource Potential asset is special: its `service_url` in the registry
points at the FeatureServer root (no layer index) because SK publishes one
layer per commodity. On first run, the asset enumerates layers and upserts a
per-commodity source row into `public_geo.sources` (e.g.
`CA-SK-RESOURCE-POTENTIAL-GOLD`), then fetches each layer independently so
Phase 2.3 Silver sees one (source_id, source_feature_id) per feature —
plan §11 item 3.
"""

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.clients.arcgis_rest import (
    FetchResult,
    fetch_layer_geojson,
    fetch_layer_metadata,
    fetch_service_metadata,
)
from georag_dagster.resources import S3Resource, PostgresResource

BRONZE_BUCKET = "bronze"
ROOT_PREFIX = "public_geoscience"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class BronzePublicGeoscienceConfig(Config):
    """Runtime overrides for a single Public Geoscience Bronze run.

    Defaults are read from the `public_geo.sources` registry. Override
    here when you need to force a specific timeout or page size for a flaky
    upstream.
    """

    # Per-page cap for ArcGIS REST `resultRecordCount`. Saskatchewan services
    # max out at 2000.
    page_size: int = 2000

    # Per-request timeout in seconds.
    http_timeout_seconds: float = 120.0

    # If set, override the registry `service_url`. Useful for dry-running
    # against a mirror / fixture.
    override_service_url: str | None = None

    # When True, skip the paginated fetch if the upstream's serviceLastEditDate
    # matches the last value we recorded on the source row. Drives the Phase-
    # 2.3 daily short-circuit schedule (plan §05e). The weekly full-pull
    # schedule leaves this False so it always fetches fresh.
    skip_if_unchanged: bool = False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_source_row(
    postgres: PostgresResource,
    source_id: str,
) -> dict[str, Any]:
    """Fetch the `public_geo.sources` row for a given source_id.

    Raises if the row is missing — that means the Phase-1 seeder hasn't been
    run and ingestion is not ready.
    """
    with postgres.get_cursor() as cur:
        cur.execute(
            """
            SELECT
                s.source_id,
                s.jurisdiction_code,
                s.name,
                s.canonical_type,
                s.service_url,
                s.layer_index,
                s.source_crs,
                s.license_summary,
                s.license_url,
                s.last_service_edit_ms,
                j.default_source_crs AS jurisdiction_default_crs
            FROM public_geo.sources s
            JOIN public_geo.jurisdictions j
                 ON j.jurisdiction_code = s.jurisdiction_code
            WHERE s.source_id = %s
            """,
            (source_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"public_geo.sources has no row for source_id={source_id!r}. "
                "Run the Phase-1 CanadaJurisdictionsSeeder before materializing "
                "Public Geoscience Bronze assets."
            )
        return dict(row)


def _persist_service_edit(
    postgres: PostgresResource,
    source_id: str,
    service_edit_ms: int | None,
) -> None:
    """Store the freshly observed `serviceLastEditDate` on the source row.

    Kept in a separate helper (not folded into `_touch_source_last_refreshed`)
    so callers can choose to touch the timestamp without changing the edit
    marker (e.g. placeholder rows in the Resource Potential parent case).
    """
    if service_edit_ms is None:
        return
    with postgres.get_cursor() as cur:
        cur.execute(
            """
            UPDATE public_geo.sources
               SET last_service_edit_ms = %s,
                   updated_at           = NOW()
             WHERE source_id = %s
            """,
            (int(service_edit_ms), source_id),
        )


def _skipped_result(
    *, source_id: str, source_row: dict[str, Any], reason: str,
    service_edit_ms: int | None,
) -> MaterializeResult:
    """Uniform MaterializeResult for short-circuited runs."""
    return MaterializeResult(
        metadata={
            "source_id":         MetadataValue.text(source_id),
            "jurisdiction_code": MetadataValue.text(source_row["jurisdiction_code"]),
            "canonical_type":    MetadataValue.text(source_row["canonical_type"]),
            "skipped":           MetadataValue.bool(True),
            "skip_reason":       MetadataValue.text(reason),
            "service_last_edit_ms": MetadataValue.int(
                int(service_edit_ms) if service_edit_ms is not None else -1
            ),
        }
    )


def _upsert_resource_potential_layer_source(
    postgres: PostgresResource,
    *,
    parent_source_id: str,
    jurisdiction_code: str,
    layer_id: int,
    layer_name: str,
    base_service_url: str,
    source_crs: int,
    license_summary: str | None,
    license_url: str | None,
) -> str:
    """Upsert a per-commodity `CA-SK-RESOURCE-POTENTIAL-<COMMODITY>` source row.

    Returns the derived source_id (idempotent on re-run).

    Saskatchewan's Resource_Map FeatureServer publishes one layer per
    commodity; plan §11 item 3 locks us to "one source_id per commodity at
    Bronze, unified at Silver". We auto-register these on first sight of
    each layer so Silver never races against a missing FK.
    """
    suffix = _slugify_commodity(layer_name)
    derived_source_id = f"{parent_source_id}-{suffix}"
    service_url = f"{base_service_url.rstrip('/')}/{layer_id}"

    name = f"Saskatchewan Resource Potential — {layer_name}"
    canonical_type = "resource_potential_zone"

    with postgres.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO public_geo.sources (
                source_id, jurisdiction_code, name, canonical_type,
                service_url, layer_index, source_crs,
                license_summary, license_url, refresh_cadence, notes,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (source_id) DO UPDATE SET
                service_url       = EXCLUDED.service_url,
                layer_index       = EXCLUDED.layer_index,
                source_crs        = EXCLUDED.source_crs,
                license_summary   = EXCLUDED.license_summary,
                license_url       = EXCLUDED.license_url,
                refresh_cadence   = EXCLUDED.refresh_cadence,
                notes             = EXCLUDED.notes,
                updated_at        = NOW()
            """,
            (
                derived_source_id,
                jurisdiction_code,
                name,
                canonical_type,
                service_url,
                layer_id,
                source_crs,
                license_summary,
                license_url,
                "weekly",
                f"Auto-registered from Resource_Map FeatureServer layer {layer_id} "
                f"('{layer_name}'). Parent registry row: {parent_source_id}.",
            ),
        )
    return derived_source_id


def _slugify_commodity(name: str) -> str:
    """Turn a layer name like 'Uranium Potential' into 'URANIUM'.

    We take the first token (stripped of 'Potential' / 'Map' suffixes),
    uppercase, and strip accents so the source_id suffix is stable and
    URL-safe.
    """
    cleaned = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    # Drop trailing 'Potential', 'Map', 'Resource Potential' style suffixes.
    cleaned = re.sub(r"\b(resource potential|potential|map)\b", "", cleaned, flags=re.I)
    token = re.split(r"[^A-Za-z0-9]+", cleaned.strip())[0] or "UNKNOWN"
    return token.upper()


def _write_bronze_artifacts(
    *,
    minio: S3Resource,
    context: AssetExecutionContext,
    jurisdiction_code: str,
    source_id: str,
    fetch_result: FetchResult,
    source_row: dict[str, Any],
) -> tuple[str, str, str, int]:
    """Write the FeatureCollection + sidecar manifest to MinIO.

    Returns (geojson_path, manifest_path, sha256, geojson_bytes_len).
    """
    body = json.dumps(fetch_result.feature_collection, ensure_ascii=False, sort_keys=False)
    body_bytes = body.encode("utf-8")
    sha256 = hashlib.sha256(body_bytes).hexdigest()

    run_id = context.run_id
    base = f"{ROOT_PREFIX}/{jurisdiction_code}/{source_id}/{run_id}"
    geojson_key = f"{base}.geojson"
    manifest_key = f"{base}.manifest.json"

    geojson_path = minio.upload_bytes(
        bucket=BRONZE_BUCKET,
        object_name=geojson_key,
        data=body_bytes,
        content_type="application/geo+json",
    )

    manifest = {
        "source_id": source_id,
        "jurisdiction_code": jurisdiction_code,
        "run_id": run_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "service_url": source_row["service_url"],
        "canonical_type": source_row["canonical_type"],
        "source_crs": source_row.get("source_crs") or source_row.get("jurisdiction_default_crs"),
        "license_summary": source_row.get("license_summary"),
        "license_url": source_row.get("license_url"),
        "query_params": fetch_result.query_params,
        "response_headers": fetch_result.response_headers,
        "pages_fetched": fetch_result.pages_fetched,
        "feature_count": fetch_result.feature_count,
        "spatial_reference_wkid": fetch_result.spatial_reference_wkid,
        "layer_last_edit_date_ms": fetch_result.layer_last_edit_date_ms,
        "service_last_edit_date_ms": fetch_result.service_last_edit_date_ms,
        "geojson_sha256": sha256,
        "geojson_bytes": len(body_bytes),
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    manifest_path = minio.upload_bytes(
        bucket=BRONZE_BUCKET,
        object_name=manifest_key,
        data=manifest_bytes,
        content_type="application/json",
    )

    context.log.info(
        "Bronze Public Geoscience: source=%s jurisdiction=%s features=%d bytes=%d pages=%d sha256=%s",
        source_id,
        jurisdiction_code,
        fetch_result.feature_count,
        len(body_bytes),
        fetch_result.pages_fetched,
        sha256[:12],
    )

    return geojson_path, manifest_path, sha256, len(body_bytes)


def _touch_source_last_refreshed(
    postgres: PostgresResource,
    source_id: str,
) -> None:
    with postgres.get_cursor() as cur:
        cur.execute(
            """
            UPDATE public_geo.sources
               SET last_refreshed_at = NOW(),
                   updated_at        = NOW()
             WHERE source_id = %s
            """,
            (source_id,),
        )


def _run_single_layer_asset(
    *,
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
    source_id: str,
) -> MaterializeResult:
    """Generic Bronze materialization for a single-layer source (mine / SMDI /
    drillhole). Reads the registry, fetches, writes, bumps `last_refreshed_at`.
    """
    source_row = _get_source_row(postgres, source_id)
    service_url = config.override_service_url or source_row["service_url"]
    source_crs = source_row.get("source_crs") or source_row.get("jurisdiction_default_crs")

    # ── Optional short-circuit (daily schedule, plan §05e) ─────────────
    # We fetch the cheap layer metadata (one GET) rather than the full
    # paginated query. If the upstream's serviceLastEditDate matches what we
    # recorded on the last successful run, skip the full pull entirely.
    if config.skip_if_unchanged:
        meta = fetch_layer_metadata(service_url, timeout=config.http_timeout_seconds)
        current_edit_ms = meta.last_edit_date_ms
        stored_edit_ms = source_row.get("last_service_edit_ms")
        if (
            current_edit_ms is not None
            and stored_edit_ms is not None
            and int(current_edit_ms) == int(stored_edit_ms)
        ):
            context.log.info(
                "Short-circuit: %s serviceLastEditDate unchanged (%s) — skipping fetch.",
                source_id, current_edit_ms,
            )
            return _skipped_result(
                source_id=source_id,
                source_row=source_row,
                reason="serviceLastEditDate unchanged since last successful refresh",
                service_edit_ms=current_edit_ms,
            )
        context.log.info(
            "Short-circuit check failed (stored=%s current=%s) — proceeding with full fetch.",
            stored_edit_ms, current_edit_ms,
        )

    context.log.info(
        "Fetching %s from %s (source_crs=%s, page_size=%d)",
        source_id,
        service_url,
        source_crs,
        config.page_size,
    )

    result = fetch_layer_geojson(
        service_url,
        source_wkid=source_crs,
        page_size=config.page_size,
        timeout=config.http_timeout_seconds,
    )

    geojson_path, manifest_path, sha256, body_len = _write_bronze_artifacts(
        minio=minio,
        context=context,
        jurisdiction_code=source_row["jurisdiction_code"],
        source_id=source_id,
        fetch_result=result,
        source_row=source_row,
    )
    _touch_source_last_refreshed(postgres, source_id)
    # Prefer the layer-level editing date over the service root's — finer-
    # grained for multi-layer services where one layer refreshes weekly and
    # another monthly.
    edit_ms = result.layer_last_edit_date_ms or result.service_last_edit_date_ms
    _persist_service_edit(postgres, source_id, edit_ms)

    return MaterializeResult(
        metadata={
            "source_id":           MetadataValue.text(source_id),
            "jurisdiction_code":   MetadataValue.text(source_row["jurisdiction_code"]),
            "canonical_type":      MetadataValue.text(source_row["canonical_type"]),
            "skipped":             MetadataValue.bool(False),
            "feature_count":       MetadataValue.int(result.feature_count),
            "pages_fetched":       MetadataValue.int(result.pages_fetched),
            "geojson_bytes":       MetadataValue.int(body_len),
            "geojson_sha256":      MetadataValue.text(sha256),
            "source_crs":          MetadataValue.int(int(source_crs) if source_crs else 0),
            "service_last_edit_ms": MetadataValue.int(int(edit_ms) if edit_ms is not None else -1),
            "geojson_path":        MetadataValue.path(geojson_path),
            "manifest_path":       MetadataValue.path(manifest_path),
        }
    )


# ---------------------------------------------------------------------------
# Assets — Saskatchewan (CA-SK)
# ---------------------------------------------------------------------------

@asset(
    group_name="bronze",
    description=(
        "Saskatchewan Mine Locations (ArcGIS FeatureServer layer 1) → "
        "verbatim GeoJSON archive under bronze/public_geoscience/CA-SK/"
        "CA-SK-MINE-LOC/{run_id}.geojson. Preserves native EPSG:2957 geometry; "
        "reprojection lives at Silver tier."
    ),
)
def bronze_pg_ca_sk_mine_loc(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    return _run_single_layer_asset(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-MINE-LOC",
    )


@asset(
    group_name="bronze",
    description=(
        "Saskatchewan Mineral Deposits Index / SMDI (ArcGIS FeatureServer "
        "layer 2). Canonical target: pg_mineral_occurrence. Public identifier "
        "SMDI is preserved verbatim in the source attribute block."
    ),
)
def bronze_pg_ca_sk_smdi(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    return _run_single_layer_asset(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-SMDI",
    )


@asset(
    group_name="bronze",
    description=(
        "Saskatchewan Minerals & Quaternary Drillhole Compilation (ArcGIS "
        "FeatureServer layer 3). Collar-level only — downhole assays/lithology "
        "flow through SMAD documents (cross-corpus linker, plan §07)."
    ),
)
def bronze_pg_ca_sk_drillhole(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    return _run_single_layer_asset(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-DRILLHOLE",
    )


@asset(
    group_name="bronze",
    description=(
        "Saskatchewan Government Rock Samples (Mineral_Exploration/MapServer "
        "layer 4) — point locations with station IDs, geologist, NTS sheet."
    ),
)
def bronze_pg_ca_sk_rock_samples(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    return _run_single_layer_asset(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-ROCK-SAMPLES",
    )


@asset(
    group_name="bronze",
    description=(
        "Saskatchewan SMAD Assessment Survey footprints — Underground "
        "(P_Mineral_Assessment_File_Information/MapServer/1)."
    ),
)
def bronze_pg_ca_sk_assessment_underground(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    return _run_single_layer_asset(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-ASSESSMENT-UNDERGROUND",
    )


@asset(
    group_name="bronze",
    description=(
        "Saskatchewan SMAD Assessment Survey footprints — Ground "
        "(P_Mineral_Assessment_File_Information/MapServer/2)."
    ),
)
def bronze_pg_ca_sk_assessment_ground(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    return _run_single_layer_asset(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-ASSESSMENT-GROUND",
    )


@asset(
    group_name="bronze",
    description=(
        "Saskatchewan SMAD Assessment Survey footprints — Airborne "
        "(P_Mineral_Assessment_File_Information/MapServer/3)."
    ),
)
def bronze_pg_ca_sk_assessment_airborne(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    return _run_single_layer_asset(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-ASSESSMENT-AIRBORNE",
    )


@asset(
    group_name="bronze",
    description=(
        "BC MINFILE (ArcGIS MapServer layer 0) — provincial mineral "
        "occurrence database. Preserves native EPSG:3005 (BC Albers) "
        "geometry verbatim; Silver reprojects to EPSG:4326 and crosswalks "
        "via the CA-BC-MINFILE FieldMapping. First active BC source "
        "(Phase 4 — second jurisdiction)."
    ),
)
def bronze_pg_ca_bc_minfile(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    return _run_single_layer_asset(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-BC-MINFILE",
    )


@asset(
    group_name="bronze",
    description=(
        "Saskatchewan Resource Potential — multi-layer Resource_Map "
        "FeatureServer. Enumerates commodity sub-layers on each run and "
        "auto-registers a per-commodity source_id (CA-SK-RESOURCE-POTENTIAL-"
        "<COMMODITY>) in public_geo.sources before fetching. One "
        "Bronze object per commodity layer (plan §11 item 3)."
    ),
)
def bronze_pg_ca_sk_resource_potential(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    parent_source_id = "CA-SK-RESOURCE-POTENTIAL"
    parent = _get_source_row(postgres, parent_source_id)
    root_url = config.override_service_url or parent["service_url"]
    source_crs = parent.get("source_crs") or parent.get("jurisdiction_default_crs")

    # ── Enumerate the FeatureServer's commodity sub-layers ──────────────
    service_meta = fetch_service_metadata(root_url, timeout=config.http_timeout_seconds)
    if not service_meta.layers:
        raise RuntimeError(
            f"{root_url} reported no layers. Cannot proceed with Resource "
            "Potential ingestion — verify endpoint is reachable."
        )

    context.log.info(
        "Resource_Map FeatureServer: %d sub-layers discovered (service_last_edit_ms=%s)",
        len(service_meta.layers),
        service_meta.service_last_edit_date_ms,
    )

    # ── Optional short-circuit on the parent service edit date ──────────
    # The parent placeholder row stores the FeatureServer-level edit date;
    # individual layers can still be finer-grained, but for a daily check the
    # service-level marker is the right anchor.
    if config.skip_if_unchanged:
        current_edit_ms = service_meta.service_last_edit_date_ms
        stored_edit_ms = parent.get("last_service_edit_ms")
        if (
            current_edit_ms is not None
            and stored_edit_ms is not None
            and int(current_edit_ms) == int(stored_edit_ms)
        ):
            context.log.info(
                "Short-circuit: Resource_Map serviceLastEditDate unchanged (%s) — skipping fetch.",
                current_edit_ms,
            )
            return _skipped_result(
                source_id=parent_source_id,
                source_row=parent,
                reason="Resource_Map serviceLastEditDate unchanged since last refresh",
                service_edit_ms=current_edit_ms,
            )

    # ── Per-layer fetch + MinIO write. Auto-register source rows as we go. ─
    # Filter sub-layers to the ones that should actually feed the canonical
    # pg_resource_potential_zone table:
    #   - Skip group-layer headers (no polygon geometry).
    #   - Skip plan §01 out-of-scope layers (Oil and Gas Pools — SK publishes
    #     it on Resource_Map but it's covered by the separate Petroleum
    #     service at a later phase).
    # V1.0 excluded Oil and Gas Pools per plan §01. User directive in V1.2
    # overrides: "add all of them." Petroleum/Pipeline services are still
    # separate services (not on this MapServer), so those stay out-of-scope
    # here — they have their own endpoint if onboarded later.
    OUT_OF_SCOPE_LAYER_SUBSTRINGS: tuple[str, ...] = ()

    def _is_relevant_layer(layer) -> bool:
        # Group layers advertise no geometryType; only include polygon layers.
        if not layer.geometry_type or "polygon" not in str(layer.geometry_type).lower():
            return False
        lname = (layer.name or "").lower()
        for bad in OUT_OF_SCOPE_LAYER_SUBSTRINGS:
            if bad in lname:
                return False
        return True

    relevant_layers = [layer for layer in service_meta.layers if _is_relevant_layer(layer)]
    skipped = [
        (layer.layer_id, layer.name)
        for layer in service_meta.layers
        if layer not in relevant_layers
    ]
    if skipped:
        context.log.info(
            "Resource_Map: skipping %d non-relevant layer(s) (group headers or "
            "out-of-scope): %s",
            len(skipped), skipped,
        )

    per_layer: list[dict[str, Any]] = []
    total_features = 0
    total_bytes = 0

    for layer in relevant_layers:
        derived_source_id = _upsert_resource_potential_layer_source(
            postgres,
            parent_source_id=parent_source_id,
            jurisdiction_code=parent["jurisdiction_code"],
            layer_id=layer.layer_id,
            layer_name=layer.name,
            base_service_url=root_url,
            source_crs=int(source_crs) if source_crs else 2957,
            license_summary=parent.get("license_summary"),
            license_url=parent.get("license_url"),
        )

        layer_url = f"{root_url.rstrip('/')}/{layer.layer_id}"
        context.log.info(
            "Fetching layer %d ('%s') → source_id=%s",
            layer.layer_id, layer.name, derived_source_id,
        )
        try:
            result = fetch_layer_geojson(
                layer_url,
                source_wkid=source_crs,
                page_size=config.page_size,
                timeout=config.http_timeout_seconds,
            )
        except Exception as exc:
            # Fail loudly per plan §07c — do NOT swallow partial failures. One
            # bad commodity layer blocks the whole asset so the operator sees it.
            context.log.error(
                "Layer %d ('%s') fetch failed: %s", layer.layer_id, layer.name, exc,
            )
            raise

        # Synthesize a source_row-shape dict for artifact writing; we don't
        # re-SELECT the row we just upserted to save a round-trip.
        derived_source_row = {
            "jurisdiction_code": parent["jurisdiction_code"],
            "canonical_type":    "resource_potential_zone",
            "service_url":       layer_url,
            "source_crs":        source_crs,
            "license_summary":   parent.get("license_summary"),
            "license_url":       parent.get("license_url"),
        }
        geojson_path, manifest_path, sha256, body_len = _write_bronze_artifacts(
            minio=minio,
            context=context,
            jurisdiction_code=parent["jurisdiction_code"],
            source_id=derived_source_id,
            fetch_result=result,
            source_row=derived_source_row,
        )
        _touch_source_last_refreshed(postgres, derived_source_id)
        _persist_service_edit(
            postgres,
            derived_source_id,
            result.layer_last_edit_date_ms or result.service_last_edit_date_ms,
        )

        total_features += result.feature_count
        total_bytes += body_len
        per_layer.append(
            {
                "layer_id": layer.layer_id,
                "layer_name": layer.name,
                "source_id": derived_source_id,
                "feature_count": result.feature_count,
                "geojson_path": geojson_path,
                "manifest_path": manifest_path,
                "geojson_sha256": sha256,
            }
        )

    # Touch the parent placeholder row too — it's a useful last-refresh
    # indicator for the UI jurisdiction view even though Silver won't read it.
    _touch_source_last_refreshed(postgres, parent_source_id)
    _persist_service_edit(postgres, parent_source_id, service_meta.service_last_edit_date_ms)

    return MaterializeResult(
        metadata={
            "source_id":         MetadataValue.text(parent_source_id),
            "jurisdiction_code": MetadataValue.text(parent["jurisdiction_code"]),
            "canonical_type":    MetadataValue.text("resource_potential_zone"),
            "skipped":           MetadataValue.bool(False),
            "layers_fetched":    MetadataValue.int(len(per_layer)),
            "total_features":    MetadataValue.int(total_features),
            "total_bytes":       MetadataValue.int(total_bytes),
            "service_last_edit_ms": MetadataValue.int(
                int(service_meta.service_last_edit_date_ms)
                if service_meta.service_last_edit_date_ms is not None
                else -1
            ),
            "per_layer": MetadataValue.json(per_layer),
        }
    )


# ---------------------------------------------------------------------------
# Tier 2 — Mineral Tenure / Dispositions (plan Phase 2)
# ---------------------------------------------------------------------------
#
# The SK tenure story spans two ArcGIS services:
#   - Economy/Mining/MapServer    layers 0-8 (legacy + modern schemas)
#   - Economy/Mineral_Tenure_Crown_Dispositions/MapServer layer 8 (Oil & Gas)
#
# Both feed the single pg_mineral_disposition canonical table. Bronze
# registers one source_id per concrete layer (auto-on-first-run) so Silver
# can later unify them with a (disposition_type, status, commodity_type)
# hint per layer — see _MINERAL_DISPOSITION_LAYER_HINTS in the Silver
# module.
#
# Layer selection is hardcoded rather than auto-enumerated: the Mining
# service exposes 16 layers total (0-15), but 9-15 are CR Preclude
# variants that are deferred per docs/field-inventory-sk-tier2-tier3.md.
# Crown Dispositions layers 0-7 duplicate Mining 0-7, so we take only
# Crown layer 8 (the unique Oil & Gas data).

# (service_key, layer_id, suffix) — the suffix matches the Silver hint
# table; change both together or the unifier breaks.
_MINERAL_DISPOSITION_LAYERS: tuple[tuple[str, int, str], ...] = (
    ("Mining", 0, "MINING-0"),
    ("Mining", 1, "MINING-1"),
    ("Mining", 2, "MINING-2"),
    ("Mining", 3, "MINING-3"),
    ("Mining", 4, "MINING-4"),
    ("Mining", 5, "MINING-5"),
    ("Mining", 6, "MINING-6"),
    ("Mining", 7, "MINING-7"),
    ("Mining", 8, "MINING-8"),
    ("Crown",  8, "CROWN-OIL-GAS"),
)


def _upsert_mineral_disposition_layer_source(
    postgres: PostgresResource,
    *,
    parent_source_id: str,
    jurisdiction_code: str,
    suffix: str,
    service_url: str,
    layer_id: int,
    source_crs: int,
    license_summary: str | None,
    license_url: str | None,
) -> str:
    """Upsert one per-layer CA-SK-MINERAL-DISPOSITION-<suffix> source row.

    Returns the derived source_id (idempotent on re-run).
    """
    derived_source_id = f"CA-SK-MINERAL-DISPOSITION-{suffix}"
    name = f"Saskatchewan Mineral Tenure - {suffix}"
    with postgres.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO public_geo.sources (
                source_id, jurisdiction_code, name, canonical_type,
                service_url, layer_index, source_crs,
                license_summary, license_url, refresh_cadence, notes,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (source_id) DO UPDATE SET
                service_url       = EXCLUDED.service_url,
                layer_index       = EXCLUDED.layer_index,
                source_crs        = EXCLUDED.source_crs,
                license_summary   = EXCLUDED.license_summary,
                license_url       = EXCLUDED.license_url,
                refresh_cadence   = EXCLUDED.refresh_cadence,
                updated_at        = NOW()
            """,
            (
                derived_source_id,
                jurisdiction_code,
                name,
                "mineral_disposition",
                f"{service_url.rstrip('/')}/{layer_id}",
                layer_id,
                source_crs,
                license_summary,
                license_url,
                "weekly",
                (
                    f"Auto-registered from {service_url} layer {layer_id}. "
                    f"Parent registry row: {parent_source_id}. Tenure layer "
                    f"tuple hint: {suffix}."
                ),
            ),
        )
    return derived_source_id


@asset(
    group_name="bronze",
    description=(
        "Ingest SK Mineral Tenure / Dispositions from two ArcGIS services "
        "(Mining 0-8 + Crown 8). Auto-registers per-layer source_ids in "
        "public_geo.sources. Silver unifies them into the single "
        "pg_mineral_disposition table via (disposition_type, status, "
        "commodity_type) hints - see _MINERAL_DISPOSITION_LAYER_HINTS "
        "in silver_public_geoscience.py. Plan Phase 2."
    ),
)
def bronze_pg_ca_sk_mineral_disposition(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    mining_parent = _get_source_row(postgres, "CA-SK-MINERAL-DISPOSITION-MINING")
    crown_parent  = _get_source_row(postgres, "CA-SK-MINERAL-DISPOSITION-CROWN")
    source_crs = int(
        mining_parent.get("source_crs")
        or mining_parent.get("jurisdiction_default_crs")
        or 2957,
    )

    service_roots = {
        "Mining": mining_parent["service_url"],
        "Crown":  crown_parent["service_url"],
    }
    parent_row_for_service = {
        "Mining": mining_parent,
        "Crown":  crown_parent,
    }

    per_layer: list[dict[str, Any]] = []
    total_features = 0
    total_bytes = 0

    for service_key, layer_id, suffix in _MINERAL_DISPOSITION_LAYERS:
        parent = parent_row_for_service[service_key]
        service_url = service_roots[service_key]
        parent_source_id = (
            "CA-SK-MINERAL-DISPOSITION-MINING"
            if service_key == "Mining"
            else "CA-SK-MINERAL-DISPOSITION-CROWN"
        )

        derived_source_id = _upsert_mineral_disposition_layer_source(
            postgres,
            parent_source_id=parent_source_id,
            jurisdiction_code=parent["jurisdiction_code"],
            suffix=suffix,
            service_url=service_url,
            layer_id=layer_id,
            source_crs=source_crs,
            license_summary=parent.get("license_summary"),
            license_url=parent.get("license_url"),
        )

        layer_url = f"{service_url.rstrip('/')}/{layer_id}"
        context.log.info(
            "Fetching Mineral Tenure layer (%s/%d, %s) -> source_id=%s",
            service_key, layer_id, suffix, derived_source_id,
        )
        try:
            result = fetch_layer_geojson(
                layer_url,
                source_wkid=source_crs,
                page_size=config.page_size,
                timeout=config.http_timeout_seconds,
            )
        except Exception as exc:
            context.log.error(
                "Mineral Tenure layer %s/%d fetch failed: %s",
                service_key, layer_id, exc,
            )
            raise

        derived_source_row = {
            "jurisdiction_code": parent["jurisdiction_code"],
            "canonical_type":    "mineral_disposition",
            "service_url":       layer_url,
            "source_crs":        source_crs,
            "license_summary":   parent.get("license_summary"),
            "license_url":       parent.get("license_url"),
        }
        geojson_path, manifest_path, sha256, body_len = _write_bronze_artifacts(
            minio=minio,
            context=context,
            jurisdiction_code=parent["jurisdiction_code"],
            source_id=derived_source_id,
            fetch_result=result,
            source_row=derived_source_row,
        )
        _touch_source_last_refreshed(postgres, derived_source_id)
        _persist_service_edit(
            postgres,
            derived_source_id,
            result.layer_last_edit_date_ms or result.service_last_edit_date_ms,
        )

        total_features += result.feature_count
        total_bytes += body_len
        per_layer.append({
            "service": service_key,
            "layer_id": layer_id,
            "suffix": suffix,
            "source_id": derived_source_id,
            "feature_count": result.feature_count,
            "geojson_path": geojson_path,
            "manifest_path": manifest_path,
            "geojson_sha256": sha256,
        })

    _touch_source_last_refreshed(postgres, "CA-SK-MINERAL-DISPOSITION-MINING")
    _touch_source_last_refreshed(postgres, "CA-SK-MINERAL-DISPOSITION-CROWN")

    return MaterializeResult(
        metadata={
            "source_id":         MetadataValue.text("CA-SK-MINERAL-DISPOSITION"),
            "jurisdiction_code": MetadataValue.text(mining_parent["jurisdiction_code"]),
            "canonical_type":    MetadataValue.text("mineral_disposition"),
            "skipped":           MetadataValue.bool(False),
            "layers_fetched":    MetadataValue.int(len(per_layer)),
            "total_features":    MetadataValue.int(total_features),
            "total_bytes":       MetadataValue.int(total_bytes),
            "per_layer":         MetadataValue.json(per_layer),
        }
    )


# ---------------------------------------------------------------------------
# Tier 3 — Bedrock Geology (plan Phase 6)
# ---------------------------------------------------------------------------

@asset(
    group_name="bronze",
    description=(
        "Bronze layer for SK Bedrock Geology 250K. "
        "Pulls MULTIPOLYGON features from Economy/Geology/MapServer/10 "
        "into MinIO bronze zone. Fields: ROCK_CODE, EON, ERA, PERIOD, "
        "GROUP_, FORMATION, MEMBER, DOMAIN, LITHOLOGY, NAME."
    ),
)
def bronze_pg_ca_sk_bedrock_geology(
    context: AssetExecutionContext,
    config: BronzePublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    return _run_single_layer_asset(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-GEOLOGY-BEDROCK-250K",
    )
