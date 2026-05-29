"""SMDI deposits ingestion — standalone refresh asset.

Implements the v1.1 SMDI ingestion plan (2026-05-24, see
docs/handoffs/smdi_ingestion_2026_05_25.md). Lands ~6,012 mineral-deposit
point features into the dedicated `public.smdi_deposits` table.

Pipeline:
  1. Cheap count check against the upstream FeatureServer.
  2. If upstream count == local count and force=False, skip the full fetch.
  3. Otherwise paginate `/query?f=geojson&resultRecordCount=2000` via the
     shared `arcgis_rest` client, assembling one FeatureCollection.
  4. Upsert into `public.smdi_deposits` keyed on objectid.

Why this lives alongside the existing `public_geo.pg_mineral_occurrence`
pipeline (which currently holds 14 synthetic SK stubs): the plan called
for a single-purpose SK-only table tied to a specific upstream URL,
decoupled from the multi-jurisdiction Bronze→Silver lakehouse. The
unification question (collapse into pg_mineral_occurrence or keep
parallel) is documented in the handoff doc — Kyle's call.
"""

from typing import Any, Optional

import httpx
import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.clients.arcgis_rest import (
    DEFAULT_TIMEOUT_SECONDS,
    fetch_layer_geojson,
)
from georag_dagster.resources import PostgresResource

# ---------------------------------------------------------------------------
# Upstream — Saskatchewan Mineral Deposit Index
# ---------------------------------------------------------------------------

SMDI_SERVICE_URL = (
    "https://gis.saskatchewan.ca/egis/rest/services/Economy/"
    "Mineral_Exploration/FeatureServer/2"
)
SMDI_COUNT_URL = (
    f"{SMDI_SERVICE_URL}/query?where=1%3D1&returnCountOnly=true&f=json"
)
SMDI_SOURCE_WKID = 4326  # plan v1.1: always use f=geojson (WGS84 native)
SMDI_PAGE_SIZE = 2000


class SmdiRefreshConfig(Config):
    """Runtime knobs for the SMDI refresh asset.

    `force` bypasses the count-equality short-circuit. Use for first-time
    bootstrap (when local count is 0 it auto-proceeds anyway) and for
    operator-initiated re-pulls.
    """

    force: bool = False
    page_size: int = SMDI_PAGE_SIZE
    http_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    override_service_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yes_no_to_bool(value: Any) -> Optional[bool]:
    """Source emits 'Yes' / 'No' (mixed case observed). Anything else → None."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("yes", "y", "true", "1"):
        return True
    if text in ("no", "n", "false", "0"):
        return False
    return None


def _fetch_upstream_count(url: str, *, timeout: float) -> int:
    """Hit the cheap returnCountOnly endpoint. Raises on transport failure."""
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    if "count" not in data:
        raise RuntimeError(
            f"Upstream count endpoint returned no 'count' key: keys={list(data)}"
        )
    return int(data["count"])


def _fetch_local_count(postgres: PostgresResource) -> int:
    with postgres.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM public.smdi_deposits")
        row = cur.fetchone()
        return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

UPSERT_SQL = """
INSERT INTO public.smdi_deposits (
    objectid, smdi, name, historic_names,
    primary_commodities, associated_commodities, grouping,
    discovery_type, production, reserves_resources,
    status, symbology_status, symbology_grouping,
    utm13e, utm13n, weblink, global_id, geom,
    fetched_at, updated_at
)
VALUES (
    %(objectid)s, %(smdi)s, %(name)s, %(historic_names)s,
    %(primary_commodities)s, %(associated_commodities)s, %(grouping)s,
    %(discovery_type)s, %(production)s, %(reserves_resources)s,
    %(status)s, %(symbology_status)s, %(symbology_grouping)s,
    %(utm13e)s, %(utm13n)s, %(weblink)s, %(global_id)s,
    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
    NOW(), NOW()
)
ON CONFLICT (objectid) DO UPDATE SET
    smdi                   = EXCLUDED.smdi,
    name                   = EXCLUDED.name,
    historic_names         = EXCLUDED.historic_names,
    primary_commodities    = EXCLUDED.primary_commodities,
    associated_commodities = EXCLUDED.associated_commodities,
    grouping               = EXCLUDED.grouping,
    discovery_type         = EXCLUDED.discovery_type,
    production             = EXCLUDED.production,
    reserves_resources     = EXCLUDED.reserves_resources,
    status                 = EXCLUDED.status,
    symbology_status       = EXCLUDED.symbology_status,
    symbology_grouping     = EXCLUDED.symbology_grouping,
    utm13e                 = EXCLUDED.utm13e,
    utm13n                 = EXCLUDED.utm13n,
    weblink                = EXCLUDED.weblink,
    global_id              = EXCLUDED.global_id,
    geom                   = EXCLUDED.geom,
    updated_at             = NOW()
"""


def _feature_to_row(feature: dict[str, Any]) -> Optional[dict]:
    """Map a single GeoJSON feature to a UPSERT_SQL param dict.

    Returns None if the feature is missing the required geometry or objectid —
    those features get skipped rather than failing the whole asset (the plan's
    "fail loudly" rule applies to pagination, not to malformed individual
    rows, which are exceedingly rare in this dataset).
    """
    props = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") or []

    if len(coords) != 2:
        return None

    objectid = props.get("OBJECTID")
    if objectid is None:
        return None

    return {
        "objectid": int(objectid),
        "smdi": str(props.get("SMDI") or "").strip() or None,
        "name": props.get("NAME"),
        "historic_names": props.get("HISTORICNAMES"),
        "primary_commodities": props.get("PRIMARYCOMMODITIES"),
        "associated_commodities": props.get("ASSOCIATEDCOMMODITIES"),
        "grouping": props.get("GROUPING"),
        "discovery_type": props.get("DISCOVERYTYPE"),
        "production": _yes_no_to_bool(props.get("PRODUCTION")),
        "reserves_resources": _yes_no_to_bool(props.get("RESERVESRESOURCES")),
        "status": props.get("STATUS"),
        "symbology_status": props.get("SYMBOLOGY_STATUS"),
        "symbology_grouping": props.get("SYMBOLOGY_GROUPING"),
        "utm13e": props.get("UTM13E"),
        "utm13n": props.get("UTM13N"),
        "weblink": props.get("WEBLINK"),
        "global_id": props.get("GLOBALID"),
        "lon": float(coords[0]),
        "lat": float(coords[1]),
    }


def _upsert_features(
    postgres: PostgresResource, features: list[dict[str, Any]]
) -> tuple[int, int]:
    """Bulk-upsert features. Returns (rows_written, rows_skipped_no_geom)."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    for feat in features:
        mapped = _feature_to_row(feat)
        if mapped is None:
            skipped += 1
            continue
        rows.append(mapped)

    if not rows:
        return 0, skipped

    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=500)
    return len(rows), skipped


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="smdi",
    description=(
        "Saskatchewan Mineral Deposit Index — daily refresh into "
        "public.smdi_deposits. Cheap count-only probe gates the full "
        "paginated fetch; an empty local table auto-proceeds to bootstrap. "
        "Plan: docs/handoffs/smdi_ingestion_2026_05_25.md."
    ),
)
def smdi_deposits_refresh(
    context: AssetExecutionContext,
    config: SmdiRefreshConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    service_url = config.override_service_url or SMDI_SERVICE_URL
    count_url = (
        f"{service_url}/query?where=1%3D1&returnCountOnly=true&f=json"
    )

    upstream_count = _fetch_upstream_count(
        count_url, timeout=config.http_timeout_seconds
    )
    local_count = _fetch_local_count(postgres)

    context.log.info(
        "SMDI refresh: upstream=%d, local=%d, force=%s",
        upstream_count, local_count, config.force,
    )

    if not config.force and upstream_count == local_count and local_count > 0:
        context.log.info(
            "SMDI refresh: upstream count unchanged (%d) — skipping full fetch",
            upstream_count,
        )
        return MaterializeResult(
            metadata={
                "status":         MetadataValue.text("skipped"),
                "reason":         MetadataValue.text("no_change"),
                "upstream_count": MetadataValue.int(upstream_count),
                "local_count":    MetadataValue.int(local_count),
                "skipped":        MetadataValue.bool(True),
            }
        )

    # Empty-table bootstrap path: local=0 ≠ upstream>0 → naturally proceeds.
    context.log.info(
        "SMDI refresh: fetching all features (page_size=%d, timeout=%ds)",
        config.page_size, int(config.http_timeout_seconds),
    )
    fetch_result = fetch_layer_geojson(
        service_url,
        source_wkid=SMDI_SOURCE_WKID,
        page_size=config.page_size,
        timeout=config.http_timeout_seconds,
    )

    features = fetch_result.feature_collection.get("features") or []
    written, skipped_no_geom = _upsert_features(postgres, features)
    final_count = _fetch_local_count(postgres)

    context.log.info(
        "SMDI refresh: complete — fetched=%d, written=%d, skipped_no_geom=%d, "
        "final_local=%d",
        len(features), written, skipped_no_geom, final_count,
    )

    return MaterializeResult(
        metadata={
            "status":              MetadataValue.text("updated"),
            "skipped":             MetadataValue.bool(False),
            "upstream_count":      MetadataValue.int(upstream_count),
            "local_count_before":  MetadataValue.int(local_count),
            "local_count_after":   MetadataValue.int(final_count),
            "fetched_features":    MetadataValue.int(len(features)),
            "upserted":            MetadataValue.int(written),
            "skipped_no_geom":     MetadataValue.int(skipped_no_geom),
            "pages_fetched":       MetadataValue.int(fetch_result.pages_fetched),
            "service_url":         MetadataValue.url(service_url),
        }
    )
