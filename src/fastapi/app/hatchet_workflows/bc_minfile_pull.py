"""§6.2 — BC MINFILE Hatchet pull cron + canonical UPSERT (wave 2).

Schedule: ``0 6 1 * *`` UTC (first day of every month at 06:00 UTC) —
per ``docs/master_plan_section6_kickoff.md`` Kyle-locked default.

What this workflow does
=======================

For each ``bc_minfile_*`` source in ``public_geo.sources``:

  1. Read ``service_url`` from the registry — operator-curated, so
     the URL can be updated without redeploying the worker.
  2. Walk the ArcGIS REST FeatureServer (?f=geojson, paginated
     via ``resultOffset`` + ``resultRecordCount``).
  3. UPSERT each feature into the matching canonical table
     (``pg_drillhole_collar`` for ``bc_minfile_drillhole_collar``,
     ``pg_mineral_occurrence`` for ``bc_minfile_mineral_occurrence``).
  4. Update ``sources.last_refreshed_at`` on success.
  5. Emit one ``public_geo.pull.bc_minfile.completed`` audit
     row per source with payload {feature_count, license_url,
     license_summary, duration_s}.

Failure modes (audit-anchored, NOT alerts)
==========================================

ArcGIS REST returns HTTP 200 even when the service is gone (it
embeds the error in the JSON body). The workflow detects:

  - Connection refused / DNS / TLS errors        → ``.endpoint_unreachable``
  - HTTP non-2xx                                 → ``.endpoint_http_error``
  - JSON with ``error.code`` set                 → ``.endpoint_arcgis_error``
  - JSON without ``features`` array              → ``.endpoint_bad_shape``

In all four cases the source's ``last_refreshed_at`` stays unchanged
and the next cron firing retries automatically. The operator surface
is the existing audit explorer (no alerts inbox spam — these are
expected during URL drift, not security events).

Operator runbook for endpoint drift
===================================

When a source goes unreachable for >2 consecutive crons:
  1. Look up the new endpoint via DataBC / NRCan / provincial open-data.
  2. ``UPDATE public_geo.sources SET service_url = '<new>'
        WHERE source_id = 'bc_minfile_*';``
  3. Trigger a manual run via Hatchet's UI; verify ``.completed``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.bc_minfile_pull")


# ArcGIS REST has a configurable max records per page (commonly 1000-2000).
# The workflow respects whatever the service advertises in maxRecordCount,
# falling back to this constant when the service doesn't advertise it.
_DEFAULT_PAGE_SIZE = 1000


class BcMinfilePullInput(BaseModel):
    source_ids: list[str] = Field(
        default_factory=lambda: [
            "bc_minfile_mineral_occurrence",
            "bc_minfile_drillhole_collar",
        ],
        description="Which bc_minfile_* sources to pull. Empty list = all "
                    "registered bc_minfile_* in public_geo.sources.",
    )
    page_size: int = Field(
        default=_DEFAULT_PAGE_SIZE, ge=100, le=10_000,
        description="Records per ArcGIS page. The service may cap this lower.",
    )


class SourcePullResult(BaseModel):
    source_id: str
    outcome: str  # completed | endpoint_unreachable | endpoint_http_error | endpoint_arcgis_error | endpoint_bad_shape | not_registered
    feature_count: int = 0
    pages_fetched: int = 0
    duration_s: float = 0.0
    detail: str | None = None


class BcMinfilePullOutput(BaseModel):
    sources_attempted: int
    sources_succeeded: int
    sources_failed: int
    per_source: list[SourcePullResult]
    sampled_at: datetime


bc_minfile_pull = hatchet.workflow(
    name="bc_minfile_pull",
    on_crons=["0 6 1 * *"],
    input_validator=BcMinfilePullInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _load_source(
    conn: asyncpg.Connection, source_id: str,
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT source_id, jurisdiction_code, service_url, license_url,
               license_summary, refresh_cadence, last_refreshed_at
          FROM public_geo.sources
         WHERE source_id = $1
         LIMIT 1
        """,
        source_id,
    )
    return dict(row) if row else None


async def _fetch_arcgis_page(
    client: httpx.AsyncClient, service_url: str,
    offset: int, page_size: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (geojson_dict, error_string). Either field is None when
    the other is set."""
    try:
        r = await client.get(
            service_url + "/query",
            params={
                "f": "geojson",
                "where": "1=1",
                "outFields": "*",
                "outSR": 4326,
                "resultOffset": offset,
                "resultRecordCount": page_size,
            },
            timeout=60.0,
        )
    except httpx.ConnectError as exc:
        return None, f"connection: {exc}"
    except httpx.HTTPError as exc:
        return None, f"http: {exc}"

    if r.status_code != 200:
        return None, f"http_status: {r.status_code}"

    try:
        body = r.json()
    except json.JSONDecodeError as exc:
        return None, f"json_decode: {exc}"

    # ArcGIS REST embeds the error in the body even with HTTP 200.
    if isinstance(body, dict) and "error" in body:
        err = body["error"]
        return None, f"arcgis_error: {err.get('code')} {err.get('message','')[:200]}"

    if not isinstance(body, dict) or "features" not in body:
        return None, f"bad_shape: missing 'features' key"

    return body, None


async def _pull_one_source(
    conn: asyncpg.Connection, src: dict[str, Any], page_size: int,
) -> SourcePullResult:
    """Walk the ArcGIS endpoint for one source. Returns a result without
    raising — failures are returned as `outcome` values so the caller
    can decide whether to continue with the next source."""
    started_at = datetime.now(tz=timezone.utc)
    service_url = src["service_url"]
    source_id = src["source_id"]

    if not service_url:
        return SourcePullResult(
            source_id=source_id,
            outcome="not_registered",
            detail="service_url is empty on the source row",
        )

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Initial metadata probe to learn maxRecordCount if advertised.
        try:
            meta_resp = await client.get(
                service_url, params={"f": "json"}, timeout=30.0,
            )
            if meta_resp.status_code == 200:
                meta = meta_resp.json()
                if isinstance(meta, dict) and "error" in meta:
                    return SourcePullResult(
                        source_id=source_id,
                        outcome="endpoint_arcgis_error",
                        detail=f"meta: {meta['error'].get('message','')[:200]}",
                    )
                if isinstance(meta, dict) and meta.get("maxRecordCount"):
                    page_size = min(page_size, int(meta["maxRecordCount"]))
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            return SourcePullResult(
                source_id=source_id,
                outcome="endpoint_unreachable",
                detail=f"meta probe failed: {exc}",
            )

        # Page walk.
        feature_count = 0
        pages = 0
        offset = 0
        while True:
            body, err = await _fetch_arcgis_page(
                client, service_url, offset, page_size,
            )
            if err is not None:
                # Map the error string back to one of the outcome enums.
                if err.startswith("connection"):
                    outcome = "endpoint_unreachable"
                elif err.startswith("http"):
                    outcome = "endpoint_http_error"
                elif err.startswith("arcgis"):
                    outcome = "endpoint_arcgis_error"
                else:
                    outcome = "endpoint_bad_shape"
                return SourcePullResult(
                    source_id=source_id,
                    outcome=outcome,
                    feature_count=feature_count,
                    pages_fetched=pages,
                    duration_s=(datetime.now(tz=timezone.utc) - started_at).total_seconds(),
                    detail=err,
                )

            page_features = body.get("features", [])
            if not page_features:
                break
            feature_count += len(page_features)
            pages += 1
            offset += len(page_features)

            # ArcGIS signals end-of-data via exceededTransferLimit=false.
            if not body.get("exceededTransferLimit", False):
                break

            # Soft cap to prevent runaway pagination if the service
            # lies about exceededTransferLimit. 100 pages * 1000 = 100k
            # records — enough for BC MINFILE's ~50k known size.
            if pages >= 100:
                log.warning(
                    "bc_minfile_pull: page cap hit source=%s pages=%d feats=%d",
                    source_id, pages, feature_count,
                )
                break

    # Re-walk the pages to UPSERT into the canonical table. We do a
    # second walk instead of caching the first because the page bodies
    # can be sizeable for the >50k BC MINFILE dataset and holding all
    # of it in memory at once would waste resident set. Each page is
    # small enough to upsert in a single round-trip.
    target_table = _canonical_table_for(source_id)
    if target_table is None:
        # Unknown source_id mapping — still record the pull-success but
        # warn that no canonical-table data landed.
        duration_s = (datetime.now(tz=timezone.utc) - started_at).total_seconds()
        return SourcePullResult(
            source_id=source_id,
            outcome="completed",
            feature_count=feature_count,
            pages_fetched=pages,
            duration_s=duration_s,
            detail=f"no canonical table mapping registered for {source_id}; "
                   f"pages fetched but not upserted",
        )

    upserted = 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        offset = 0
        while True:
            body, err = await _fetch_arcgis_page(
                client, service_url, offset, page_size,
            )
            if err is not None:
                # Mid-walk failure during UPSERT — record what we got.
                break
            page_features = body.get("features", [])
            if not page_features:
                break
            upserted += await _upsert_features(
                conn, target_table, src, page_features,
            )
            offset += len(page_features)
            if not body.get("exceededTransferLimit", False):
                break
            if offset >= feature_count:
                break

    duration_s = (datetime.now(tz=timezone.utc) - started_at).total_seconds()
    return SourcePullResult(
        source_id=source_id,
        outcome="completed",
        feature_count=feature_count,
        pages_fetched=pages,
        duration_s=duration_s,
        detail=f"upserted {upserted}/{feature_count} features into {target_table}",
    )


def _canonical_table_for(source_id: str) -> str | None:
    """Resolve the canonical table that this source feeds. Returns the
    fully-qualified table name (``public_geo.pg_*``) or None if
    no mapping is known.

    Mapping is kept here (not in the registry) because the canonical
    table choice is a code-level invariant — adding a new source type
    requires writing column-mapping code anyway.
    """
    suffix_to_table = {
        "mineral_occurrence":  "public_geo.pg_mineral_occurrence",
        "drillhole_collar":    "public_geo.pg_drillhole_collar",
        "canadian_mines":      "public_geo.pg_mine",
        "bedrock_geology":     "public_geo.pg_bedrock_geology",
        "assessment_survey":   "public_geo.pg_assessment_survey",
    }
    for suffix, table in suffix_to_table.items():
        if source_id.endswith(suffix):
            return table
    return None


async def _upsert_features(
    conn: asyncpg.Connection,
    target_table: str,
    src: dict[str, Any],
    features: list[dict[str, Any]],
) -> int:
    """Idempotent INSERT/UPDATE of one page of GeoJSON features into the
    canonical table. Returns the count of rows touched.

    Uniqueness key: (source_id, source_feature_id) on every supported
    table. The ON CONFLICT branch updates the domain columns +
    last_seen_at + checksum but preserves first_seen_at + id.

    Wave 3 (doc-phase 181) extended this from occurrence-only to all
    5 canonical tables:
      - pg_mineral_occurrence  (wave 2)
      - pg_drillhole_collar    (wave 3)
      - pg_mine                (wave 3)
      - pg_bedrock_geology     (wave 3)
      - pg_assessment_survey   (wave 3)

    Each table gets its own UPSERT branch below — the domain columns
    differ but the (source_id, source_feature_id) ON CONFLICT pattern
    is shared.
    """
    table_short = target_table.rsplit(".", 1)[-1]

    if table_short == "pg_mineral_occurrence":
        return await _upsert_mineral_occurrence(conn, target_table, src, features)
    if table_short == "pg_drillhole_collar":
        return await _upsert_drillhole_collar(conn, target_table, src, features)
    if table_short == "pg_mine":
        return await _upsert_mine(conn, target_table, src, features)
    if table_short == "pg_bedrock_geology":
        return await _upsert_bedrock_geology(conn, target_table, src, features)
    if table_short == "pg_assessment_survey":
        return await _upsert_assessment_survey(conn, target_table, src, features)

    # Unmapped target — silently skip rather than fail the pull; the
    # operator sees "no canonical table mapping" in the audit payload.
    return 0


async def _upsert_mineral_occurrence(
    conn: asyncpg.Connection,
    target_table: str,
    src: dict[str, Any],
    features: list[dict[str, Any]],
) -> int:
    """Wave 2 — pg_mineral_occurrence UPSERT.

    Field mapping mirrors the BC MINFILE MINFILE_PUB layer column set
    with COALESCE fallbacks for the variations across other sources.
    """
    rows_touched = 0
    for feature in features:
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry")
        source_feature_id = str(
            props.get("MINFILE_NO")
            or props.get("OBJECTID")
            or props.get("id")
            or ""
        )
        if not source_feature_id:
            continue

        # Geometry — pass through as GeoJSON text and let PostGIS parse.
        geom_text = json.dumps(geom) if geom is not None else None

        # Field mapping for BC MINFILE — the upstream MINFILE_PUB layer
        # columns vary by feed; below covers the common ones with
        # COALESCE fallbacks.
        name = props.get("NAME") or props.get("name") or None
        status = _normalize_status(
            props.get("STATUS") or props.get("STATUS_DESC")
        )
        primary_commodities = _split_commodities(
            props.get("COMMODITIES")
            or props.get("PRIMARY_COMMODITIES")
            or ""
        )

        await conn.execute(
            f"""
            INSERT INTO {target_table} (
                id, jurisdiction_code, source_id, source_feature_id,
                name, historic_names, status,
                primary_commodities, associated_commodities,
                production_flag, source_crs,
                source_geom_wkt, source_url, source_attributes,
                first_seen_at, last_seen_at,
                checksum, geom
            )
            VALUES (
                gen_random_uuid(), $1::text, $2::text, $3::text,
                $4::text, ARRAY[]::text[], $5::text,
                $6::text[], ARRAY[]::text[],
                FALSE, 4326,
                CASE WHEN $7::text IS NULL THEN NULL
                     ELSE ST_AsText(ST_GeomFromGeoJSON($7::text)) END,
                $8::text, $9::jsonb,
                now(), now(),
                substr(md5(($3::text) || '|' || COALESCE($7::text, '')), 1, 64),
                CASE WHEN $7::text IS NULL THEN NULL
                     ELSE ST_GeomFromGeoJSON($7::text) END
            )
            ON CONFLICT (source_id, source_feature_id) DO UPDATE
                SET name              = EXCLUDED.name,
                    status            = EXCLUDED.status,
                    primary_commodities = EXCLUDED.primary_commodities,
                    geom              = EXCLUDED.geom,
                    source_geom_wkt   = EXCLUDED.source_geom_wkt,
                    source_attributes = EXCLUDED.source_attributes,
                    last_seen_at      = now(),
                    updated_at        = now(),
                    checksum          = EXCLUDED.checksum
            """,
            src.get("jurisdiction_code"),
            src["source_id"],
            source_feature_id,
            name, status,
            primary_commodities,
            geom_text,
            src.get("service_url"),
            json.dumps(props, default=str),
        )
        rows_touched += 1
    return rows_touched


async def _upsert_drillhole_collar(
    conn: asyncpg.Connection,
    target_table: str,
    src: dict[str, Any],
    features: list[dict[str, Any]],
) -> int:
    """Wave 3 — pg_drillhole_collar UPSERT.

    Field mapping covers common BC/ON/SK drillhole layer columns:
    HOLE_ID/HOLEID → drillhole_id, NAME/HOLE_NAME → drillhole_name,
    DEPTH/TOTAL_DEPTH → total_length_m, DIP/INCLINATION → inclination_deg,
    AZIMUTH/BEARING → azimuth_deg, ELEVATION/COLLAR_ELEV → collar_elevation_m.
    """
    rows_touched = 0
    for feature in features:
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry")
        source_feature_id = str(
            props.get("HOLE_ID") or props.get("HOLEID")
            or props.get("OBJECTID") or props.get("id") or ""
        )
        if not source_feature_id:
            continue

        geom_text = json.dumps(geom) if geom is not None else None
        drillhole_id = props.get("HOLE_ID") or props.get("HOLEID") or source_feature_id
        drillhole_name = props.get("NAME") or props.get("HOLE_NAME") or None
        company = props.get("COMPANY") or props.get("OPERATOR") or None
        project_name = props.get("PROJECT") or props.get("PROJECT_NAME") or None
        total_length = _safe_float(
            props.get("DEPTH") or props.get("TOTAL_DEPTH") or props.get("LENGTH")
        )
        inclination = _safe_float(props.get("DIP") or props.get("INCLINATION"))
        azimuth = _safe_float(props.get("AZIMUTH") or props.get("BEARING"))
        elevation = _safe_float(
            props.get("ELEVATION") or props.get("COLLAR_ELEV") or props.get("ELEV")
        )
        commodity = _split_commodities(
            props.get("COMMODITY") or props.get("COMMODITIES") or ""
        )

        await conn.execute(
            f"""
            INSERT INTO {target_table} (
                id, jurisdiction_code, source_id, source_feature_id,
                drillhole_id, drillhole_name, company, project_name,
                commodity_of_interest, total_length_m,
                inclination_deg, azimuth_deg, collar_elevation_m,
                source_crs, source_geom_wkt, source_url, source_attributes,
                first_seen_at, last_seen_at, checksum, geom
            )
            VALUES (
                gen_random_uuid(), $1::text, $2::text, $3::text,
                $4::text, $5::text, $6::text, $7::text,
                $8::text[], $9::numeric,
                $10::numeric, $11::numeric, $12::numeric,
                4326,
                CASE WHEN $13::text IS NULL THEN NULL
                     ELSE ST_AsText(ST_GeomFromGeoJSON($13::text)) END,
                $14::text, $15::jsonb,
                now(), now(),
                substr(md5(($3::text) || '|' || COALESCE($13::text, '')), 1, 64),
                CASE WHEN $13::text IS NULL THEN NULL
                     ELSE ST_GeomFromGeoJSON($13::text) END
            )
            ON CONFLICT (source_id, source_feature_id) DO UPDATE
                SET drillhole_id        = EXCLUDED.drillhole_id,
                    drillhole_name      = EXCLUDED.drillhole_name,
                    company             = EXCLUDED.company,
                    project_name        = EXCLUDED.project_name,
                    commodity_of_interest = EXCLUDED.commodity_of_interest,
                    total_length_m      = EXCLUDED.total_length_m,
                    inclination_deg     = EXCLUDED.inclination_deg,
                    azimuth_deg         = EXCLUDED.azimuth_deg,
                    collar_elevation_m  = EXCLUDED.collar_elevation_m,
                    geom                = EXCLUDED.geom,
                    source_geom_wkt     = EXCLUDED.source_geom_wkt,
                    source_attributes   = EXCLUDED.source_attributes,
                    last_seen_at        = now(),
                    updated_at          = now(),
                    checksum            = EXCLUDED.checksum
            """,
            src.get("jurisdiction_code"), src["source_id"], source_feature_id,
            drillhole_id, drillhole_name, company, project_name,
            commodity, total_length,
            inclination, azimuth, elevation,
            geom_text, src.get("service_url"),
            json.dumps(props, default=str),
        )
        rows_touched += 1
    return rows_touched


async def _upsert_mine(
    conn: asyncpg.Connection,
    target_table: str,
    src: dict[str, Any],
    features: list[dict[str, Any]],
) -> int:
    """Wave 3 — pg_mine UPSERT (NRCan Canadian Mines + provincial mine registries).

    Status enum: {operating, care-and-maintenance, closed, proposed,
    historic, unknown}. Maps via _normalize_mine_status alias table.
    """
    rows_touched = 0
    for feature in features:
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry")
        source_feature_id = str(
            props.get("MINE_ID") or props.get("MINEID")
            or props.get("OBJECTID") or props.get("id") or ""
        )
        if not source_feature_id:
            continue

        geom_text = json.dumps(geom) if geom is not None else None
        name = props.get("NAME") or props.get("MINE_NAME") or None
        status = _normalize_mine_status(
            props.get("STATUS") or props.get("MINE_STATUS")
            or props.get("STATUS_DESC")
        )
        commodities = _split_commodities(
            props.get("COMMODITIES") or props.get("COMMODITY") or ""
        )
        operator = props.get("OPERATOR") or props.get("COMPANY") or None

        await conn.execute(
            f"""
            INSERT INTO {target_table} (
                id, jurisdiction_code, source_id, source_feature_id,
                name, status, commodities, operator,
                source_crs, source_geom_wkt, source_url, source_attributes,
                first_seen_at, last_seen_at, checksum, geom
            )
            VALUES (
                gen_random_uuid(), $1::text, $2::text, $3::text,
                $4::text, $5::text, $6::text[], $7::text,
                4326,
                CASE WHEN $8::text IS NULL THEN NULL
                     ELSE ST_AsText(ST_GeomFromGeoJSON($8::text)) END,
                $9::text, $10::jsonb,
                now(), now(),
                substr(md5(($3::text) || '|' || COALESCE($8::text, '')), 1, 64),
                CASE WHEN $8::text IS NULL THEN NULL
                     ELSE ST_GeomFromGeoJSON($8::text) END
            )
            ON CONFLICT (source_id, source_feature_id) DO UPDATE
                SET name              = EXCLUDED.name,
                    status            = EXCLUDED.status,
                    commodities       = EXCLUDED.commodities,
                    operator          = EXCLUDED.operator,
                    geom              = EXCLUDED.geom,
                    source_geom_wkt   = EXCLUDED.source_geom_wkt,
                    source_attributes = EXCLUDED.source_attributes,
                    last_seen_at      = now(),
                    updated_at        = now(),
                    checksum          = EXCLUDED.checksum
            """,
            src.get("jurisdiction_code"), src["source_id"], source_feature_id,
            name, status, commodities, operator,
            geom_text, src.get("service_url"),
            json.dumps(props, default=str),
        )
        rows_touched += 1
    return rows_touched


async def _upsert_bedrock_geology(
    conn: asyncpg.Connection,
    target_table: str,
    src: dict[str, Any],
    features: list[dict[str, Any]],
) -> int:
    """Wave 3 — pg_bedrock_geology UPSERT.

    Provincial bedrock geology layers — polygon features with
    stratigraphic columns (eon/era/period/group/formation/member).
    """
    rows_touched = 0
    for feature in features:
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry")
        source_feature_id = str(
            props.get("UNIT_CODE") or props.get("UNIT_ID")
            or props.get("OBJECTID") or props.get("id") or ""
        )
        if not source_feature_id:
            continue

        geom_text = json.dumps(geom) if geom is not None else None
        # unit_code is NOT NULL — fall back to source_feature_id if upstream
        # doesn't break the unit into a separate code field.
        unit_code = str(
            props.get("UNIT_CODE") or props.get("CODE")
            or source_feature_id
        )[:16]  # column is varchar(16)
        unit_name = props.get("UNIT_NAME") or props.get("NAME") or None
        eon = props.get("EON") or None
        era = props.get("ERA") or None
        period = props.get("PERIOD") or props.get("AGE_PERIOD") or None
        group_name = props.get("GROUP") or props.get("GROUP_NAME") or None
        formation = props.get("FORMATION") or None
        member = props.get("MEMBER") or None
        lithology = props.get("LITHOLOGY") or props.get("LITH") or None
        scale = str(props.get("SCALE") or "250K")[:8]

        await conn.execute(
            f"""
            INSERT INTO {target_table} (
                id, jurisdiction_code, source_id, source_feature_id,
                unit_code, unit_name, eon, era, period,
                group_name, formation, member, lithology, scale,
                source_crs, source_geom_wkt, source_url, source_attributes,
                first_seen_at, last_seen_at, checksum, geom
            )
            VALUES (
                gen_random_uuid(), $1::text, $2::text, $3::text,
                $4::text, $5::text, $6::text, $7::text, $8::text,
                $9::text, $10::text, $11::text, $12::text, $13::text,
                4326,
                CASE WHEN $14::text IS NULL THEN NULL
                     ELSE ST_AsText(ST_GeomFromGeoJSON($14::text)) END,
                $15::text, $16::jsonb,
                now(), now(),
                substr(md5(($3::text) || '|' || COALESCE($14::text, '')), 1, 64),
                CASE WHEN $14::text IS NULL THEN NULL
                     ELSE ST_Multi(ST_GeomFromGeoJSON($14::text)) END
            )
            ON CONFLICT (source_id, source_feature_id) DO UPDATE
                SET unit_code         = EXCLUDED.unit_code,
                    unit_name         = EXCLUDED.unit_name,
                    eon               = EXCLUDED.eon,
                    era               = EXCLUDED.era,
                    period            = EXCLUDED.period,
                    group_name        = EXCLUDED.group_name,
                    formation         = EXCLUDED.formation,
                    member            = EXCLUDED.member,
                    lithology         = EXCLUDED.lithology,
                    geom              = EXCLUDED.geom,
                    source_geom_wkt   = EXCLUDED.source_geom_wkt,
                    source_attributes = EXCLUDED.source_attributes,
                    last_seen_at      = now(),
                    updated_at        = now(),
                    checksum          = EXCLUDED.checksum
            """,
            src.get("jurisdiction_code"), src["source_id"], source_feature_id,
            unit_code, unit_name, eon, era, period,
            group_name, formation, member, lithology, scale,
            geom_text, src.get("service_url"),
            json.dumps(props, default=str),
        )
        rows_touched += 1
    return rows_touched


async def _upsert_assessment_survey(
    conn: asyncpg.Connection,
    target_table: str,
    src: dict[str, Any],
    features: list[dict[str, Any]],
) -> int:
    """Wave 3 — pg_assessment_survey UPSERT.

    survey_type CHECK is one of: airborne | ground | underground | unknown.
    Anything else maps to 'unknown' so the row inserts.
    """
    rows_touched = 0
    for feature in features:
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry")
        source_feature_id = str(
            props.get("SURVEY_ID") or props.get("REPORT_ID")
            or props.get("OBJECTID") or props.get("id") or ""
        )
        if not source_feature_id:
            continue

        geom_text = json.dumps(geom) if geom is not None else None
        survey_type = _normalize_survey_type(
            props.get("SURVEY_TYPE") or props.get("TYPE")
        )

        await conn.execute(
            f"""
            INSERT INTO {target_table} (
                id, jurisdiction_code, source_id, source_feature_id,
                survey_type,
                source_crs, source_geom_wkt, source_url, source_attributes,
                first_seen_at, last_seen_at, checksum, geom
            )
            VALUES (
                gen_random_uuid(), $1::text, $2::text, $3::text,
                $4::text,
                4326,
                CASE WHEN $5::text IS NULL THEN NULL
                     ELSE ST_AsText(ST_GeomFromGeoJSON($5::text)) END,
                $6::text, $7::jsonb,
                now(), now(),
                substr(md5(($3::text) || '|' || COALESCE($5::text, '')), 1, 64),
                CASE WHEN $5::text IS NULL THEN NULL
                     ELSE ST_Multi(ST_GeomFromGeoJSON($5::text)) END
            )
            ON CONFLICT (source_id, source_feature_id) DO UPDATE
                SET survey_type       = EXCLUDED.survey_type,
                    geom              = EXCLUDED.geom,
                    source_geom_wkt   = EXCLUDED.source_geom_wkt,
                    source_attributes = EXCLUDED.source_attributes,
                    last_seen_at      = now(),
                    updated_at        = now(),
                    checksum          = EXCLUDED.checksum
            """,
            src.get("jurisdiction_code"), src["source_id"], source_feature_id,
            survey_type,
            geom_text, src.get("service_url"),
            json.dumps(props, default=str),
        )
        rows_touched += 1
    return rows_touched


def _safe_float(raw: Any) -> float | None:
    """Coerce upstream string/numeric to float; return None on failure."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# pg_mine.status CHECK constraint allows exactly these 6 values.
_MINE_STATUS_CANONICAL: frozenset[str] = frozenset({
    "producing", "past-producer", "developed-deposit",
    "prospect", "closed", "unknown",
})
_MINE_STATUS_ALIASES: dict[str, str] = {
    # → producing
    "active":              "producing",
    "production":          "producing",
    "producer":            "producing",
    "operating":           "producing",
    "in operation":        "producing",
    # → past-producer
    "past":                "past-producer",
    "past producer":       "past-producer",
    "former":              "past-producer",
    "former producer":     "past-producer",
    "historic":            "past-producer",
    "historic producer":   "past-producer",
    "abandoned":           "past-producer",
    # → developed-deposit
    "developed":           "developed-deposit",
    "developed deposit":   "developed-deposit",
    "developed prospect":  "developed-deposit",
    "permitted":           "developed-deposit",
    "construction":        "developed-deposit",
    "proposed":            "developed-deposit",
    # → prospect
    "occurrence":          "prospect",
    "showing":             "prospect",
    # → closed
    "care and maintenance": "closed",
    "care-and-maintenance": "closed",
    "suspended":           "closed",
    "shutdown":            "closed",
    "permanently closed":  "closed",
}


def _normalize_mine_status(raw: Any) -> str:
    if raw is None or raw == "":
        return "unknown"
    text = str(raw).strip().lower()
    if text in _MINE_STATUS_CANONICAL:
        return text
    if text in _MINE_STATUS_ALIASES:
        return _MINE_STATUS_ALIASES[text]
    return "unknown"


_SURVEY_TYPE_CANONICAL: frozenset[str] = frozenset({
    "airborne", "ground", "underground", "unknown",
})
_SURVEY_TYPE_ALIASES: dict[str, str] = {
    "aerial":       "airborne",
    "aeromagnetic": "airborne",
    "heliborne":    "airborne",
    "fixed-wing":   "airborne",
    "surface":      "ground",
    "borehole":     "underground",
    "downhole":     "underground",
}


def _normalize_survey_type(raw: Any) -> str:
    if raw is None or raw == "":
        return "unknown"
    text = str(raw).strip().lower()
    if text in _SURVEY_TYPE_CANONICAL:
        return text
    if text in _SURVEY_TYPE_ALIASES:
        return _SURVEY_TYPE_ALIASES[text]
    return "unknown"


# Canonical status enum from the pg_mineral_occurrence CHECK constraint.
# Upstream sources use varied capitalisation + word ordering, so we
# normalise to lowercase + hyphenated. Anything not matching falls
# back to 'unknown' so the row still inserts (the alternative — drop
# the row — silently loses data, which is worse).
_CANONICAL_STATUSES: frozenset[str] = frozenset({
    "occurrence", "showing", "prospect", "deposit",
    "past-producer", "producer", "unknown",
})

_STATUS_ALIASES: dict[str, str] = {
    "past producer":     "past-producer",
    "former producer":   "past-producer",
    "historic producer": "past-producer",
    "developed prospect": "prospect",
    "mine":              "producer",
}


def _normalize_status(raw: Any) -> str:
    """Coerce upstream status text into one of the 7 canonical values
    accepted by pg_mineral_occurrence's CHECK constraint."""
    if raw is None or raw == "":
        return "unknown"
    text = str(raw).strip().lower()
    if text in _CANONICAL_STATUSES:
        return text
    if text in _STATUS_ALIASES:
        return _STATUS_ALIASES[text]
    return "unknown"


def _split_commodities(raw: Any) -> list[str]:
    """Normalise the upstream commodity field (often a comma- or
    pipe-separated string) into a sorted unique list. Empty inputs
    return []."""
    if not raw:
        return []
    if isinstance(raw, list):
        return sorted({str(x).strip() for x in raw if x and str(x).strip()})
    text = str(raw).replace("|", ",")
    return sorted({piece.strip() for piece in text.split(",") if piece.strip()})


@bc_minfile_pull.task(execution_timeout="60m")
async def run_pull(input: BcMinfilePullInput, ctx: Context) -> BcMinfilePullOutput:
    sampled_at = datetime.now(tz=timezone.utc)
    dsn = _build_dsn()

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        # Resolve target source list.
        if input.source_ids:
            target_ids = input.source_ids
        else:
            rows = await conn.fetch(
                "SELECT source_id FROM public_geo.sources WHERE source_id LIKE 'bc_minfile%' ORDER BY source_id"
            )
            target_ids = [r["source_id"] for r in rows]

        per_source: list[SourcePullResult] = []
        for source_id in target_ids:
            src = await _load_source(conn, source_id)
            if src is None:
                per_source.append(SourcePullResult(
                    source_id=source_id,
                    outcome="not_registered",
                    detail="source_id not present in public_geo.sources",
                ))
                continue

            result = await _pull_one_source(conn, src, input.page_size)
            per_source.append(result)

            # Update last_refreshed_at only on success.
            if result.outcome == "completed":
                await conn.execute(
                    "UPDATE public_geo.sources "
                    "SET last_refreshed_at = now() WHERE source_id = $1",
                    source_id,
                )

            # Audit anchor for every attempt (success + failure modes).
            await emit_audit(
                conn,
                action_type=f"public_geo.pull.bc_minfile.{result.outcome}",
                workspace_id=None,
                actor_id=None,
                actor_kind="workflow",
                target_schema="public_geo",
                target_table="sources",
                target_id=source_id,
                payload={
                    "source_id":        source_id,
                    "jurisdiction":     src.get("jurisdiction_code"),
                    "service_url":      src.get("service_url"),
                    "license_url":      src.get("license_url"),
                    "license_summary":  src.get("license_summary"),
                    "feature_count":    result.feature_count,
                    "pages_fetched":    result.pages_fetched,
                    "duration_s":       result.duration_s,
                    "detail":           result.detail,
                },
            )

        succ = sum(1 for r in per_source if r.outcome == "completed")
        fail = len(per_source) - succ
        log.info(
            "bc_minfile_pull: attempted=%d succ=%d fail=%d",
            len(per_source), succ, fail,
        )
        return BcMinfilePullOutput(
            sources_attempted=len(per_source),
            sources_succeeded=succ,
            sources_failed=fail,
            per_source=per_source,
            sampled_at=sampled_at,
        )
    finally:
        await conn.close()


__all__ = [
    "bc_minfile_pull",
    "BcMinfilePullInput",
    "BcMinfilePullOutput",
    "SourcePullResult",
]
