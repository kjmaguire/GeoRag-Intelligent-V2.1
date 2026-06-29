"""Assessment Survey adapters (§6.1, §6.2) — doc-phase 154.

Two §6 adapters in one file:
  - `sync_sk_assessment_surveys()` — SaskGeoAtlas Assessment Files
  - `sync_bc_aris_assessment_surveys()` — BC ARIS

Lands MultiPolygon footprints in `public_geo.pg_assessment_survey`
under the respective `source_id` values.

Both follow the same shape as the prior 5 adapters; the only material
difference is `geom` is `MultiPolygon` (not `Point`), so the synthetic
stubs generate small square footprints from a centroid lat/lon.

Survey types are constrained: airborne / ground / underground / unknown.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import asyncpg

from app.audit import emit_audit

log = logging.getLogger("georag.publicgeo.assessment_survey_adapters")


class AssessmentSurveySyncResult(NamedTuple):
    source_id: str
    inserted: int
    updated: int
    skipped_no_geom: int
    total_features: int
    audit_ledger_entry_id: UUID
    sync_method: str


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _square_footprint_wkt(lat: float, lon: float, side_deg: float = 0.04) -> str:
    """Build a MultiPolygon WKT for a square footprint centered at
    (lat, lon) with side length `side_deg` degrees (~4 km at 50° N)."""
    half = side_deg / 2.0
    n, s = lat + half, lat - half
    e, w = lon + half, lon - half
    return (
        f"MULTIPOLYGON((("
        f"{w} {s}, {e} {s}, {e} {n}, {w} {n}, {w} {s}"
        f")))"
    )


def _feature_checksum(feature: dict[str, Any]) -> str:
    canon = json.dumps(feature, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def _sync_assessment_surveys(
    *,
    source_id: str,
    features: list[dict[str, Any]],
    doc_phase: int,
    pool: asyncpg.Pool | None = None,
) -> AssessmentSurveySyncResult:
    """Shared upsert + audit body. Both SK + BC paths call into this."""
    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        async with pool.acquire() as conn:
            source_row = await conn.fetchrow(
                """
                SELECT source_id, jurisdiction_code, source_crs
                  FROM public_geo.sources
                 WHERE source_id = $1
                """,
                source_id,
            )
            if source_row is None:
                raise RuntimeError(f"{source_id} source not registered.")

            jurisdiction_code = source_row["jurisdiction_code"]
            source_crs = source_row["source_crs"]

            inserted = 0
            updated = 0
            skipped_no_geom = 0

            for f in features:
                if f.get("lat") is None or f.get("lon") is None:
                    skipped_no_geom += 1
                    continue

                checksum = _feature_checksum(f)
                wkt = _square_footprint_wkt(
                    f["lat"], f["lon"],
                    side_deg=f.get("side_deg", 0.04),
                )

                row = await conn.fetchrow(
                    """
                    INSERT INTO public_geo.pg_assessment_survey (
                        id, jurisdiction_code, source_id, source_feature_id,
                        survey_type, source_crs, source_geom_wkt,
                        source_attributes, checksum, geom
                    )
                    VALUES (
                        $1::uuid, $2, $3, $4,
                        $5, $6, $7,
                        $8::jsonb, $9, ST_SetSRID(ST_GeomFromText($7), 4326)
                    )
                    ON CONFLICT (source_id, source_feature_id) DO UPDATE
                       SET survey_type = EXCLUDED.survey_type,
                           geom = EXCLUDED.geom,
                           source_geom_wkt = EXCLUDED.source_geom_wkt,
                           source_attributes = EXCLUDED.source_attributes,
                           checksum = EXCLUDED.checksum,
                           last_seen_at = now(),
                           updated_at = now()
                     WHERE pg_assessment_survey.checksum <> EXCLUDED.checksum
                    RETURNING (xmax = 0) AS inserted_row
                    """,
                    str(uuid4()),
                    jurisdiction_code,
                    source_id,
                    f["source_feature_id"],
                    f.get("survey_type", "unknown"),
                    source_crs,
                    wkt,
                    json.dumps({k: v for k, v in f.items() if k not in {"lat", "lon", "side_deg"}}, default=str),
                    checksum,
                )
                if row is None:
                    pass
                elif row["inserted_row"]:
                    inserted += 1
                else:
                    updated += 1

            await conn.execute(
                "UPDATE public_geo.sources SET last_refreshed_at = now() "
                "WHERE source_id = $1",
                source_id,
            )

            ledger = await emit_audit(
                conn,
                action_type="public_geo.pull.complete",
                actor_kind="workflow",
                target_schema="public_geo",
                target_table="pg_assessment_survey",
                target_id=source_id,
                payload={
                    "evaluator": "synthetic_stub",
                    "doc_phase": doc_phase,
                    "source_id": source_id,
                    "jurisdiction_code": jurisdiction_code,
                    "fetched_features": len(features),
                    "inserted": inserted,
                    "updated": updated,
                    "skipped_no_geom": skipped_no_geom,
                },
            )

        log.info(
            "sync_assessment.%s.completed fetched=%d inserted=%d updated=%d",
            source_id, len(features), inserted, updated,
        )

        return AssessmentSurveySyncResult(
            source_id=source_id,
            inserted=inserted,
            updated=updated,
            skipped_no_geom=skipped_no_geom,
            total_features=len(features),
            audit_ledger_entry_id=ledger.id,
            sync_method="synthetic_stub",
        )
    finally:
        if owns_pool and pool is not None:
            await pool.close()


# ----------------------------------------------------------------------
# Saskatchewan assessment surveys
# ----------------------------------------------------------------------
_SYNTHETIC_SK_SURVEYS: list[dict[str, Any]] = [
    {"source_feature_id": "SK_AS_2543-MAG", "survey_type": "airborne", "subject": "magnetic + radiometric over Athabasca", "lat": 57.78, "lon": -105.04},
    {"source_feature_id": "SK_AS_2611-EM", "survey_type": "airborne", "subject": "VTEM EM survey Cigar Lake area", "lat": 58.06, "lon": -104.51},
    {"source_feature_id": "SK_AS_3201-GRAV", "survey_type": "ground", "subject": "ground gravity Patterson Lake South", "lat": 58.30, "lon": -105.05},
    {"source_feature_id": "SK_AS_1812-IP", "survey_type": "ground", "subject": "IP-resistivity Santoy Zone", "lat": 56.34, "lon": -103.96},
    {"source_feature_id": "SK_AS_1456-VMS-EM", "survey_type": "airborne", "subject": "Flin Flon VMS belt regional EM", "lat": 54.78, "lon": -101.85, "side_deg": 0.10},
    {"source_feature_id": "SK_AS_5101-POTASH-SEISMIC", "survey_type": "ground", "subject": "potash basin 2D seismic", "lat": 51.79, "lon": -105.04, "side_deg": 0.08},
    {"source_feature_id": "SK_AS_6402-HOIDAS-MAG", "survey_type": "airborne", "subject": "high-res mag Hoidas Lake REE", "lat": 59.55, "lon": -108.65},
    {"source_feature_id": "SK_AS_REGIONAL-AIRBORNE-2018", "survey_type": "airborne", "subject": "regional Saskatchewan-wide reconnaissance", "lat": 56.0, "lon": -105.0, "side_deg": 0.30},
]


def _fetch_sk_assessment_features() -> list[dict[str, Any]]:
    return list(_SYNTHETIC_SK_SURVEYS)


async def sync_sk_assessment_surveys(
    *, pool: asyncpg.Pool | None = None,
) -> AssessmentSurveySyncResult:
    """Sync SaskGeoAtlas assessment-file footprints into pg_assessment_survey."""
    return await _sync_assessment_surveys(
        source_id="sk_assessment_survey",
        features=_fetch_sk_assessment_features(),
        doc_phase=154,
        pool=pool,
    )


# ----------------------------------------------------------------------
# BC ARIS assessment surveys
# ----------------------------------------------------------------------
_SYNTHETIC_BC_SURVEYS: list[dict[str, Any]] = [
    {"source_feature_id": "BC_ARIS_38901", "survey_type": "airborne", "subject": "mag + radiometric — Highland Valley", "lat": 50.49, "lon": -121.02},
    {"source_feature_id": "BC_ARIS_39112", "survey_type": "ground", "subject": "IP survey Brucejack area", "lat": 56.47, "lon": -130.21},
    {"source_feature_id": "BC_ARIS_36505", "survey_type": "airborne", "subject": "ZTEM EM Mount Milligan", "lat": 55.13, "lon": -124.07},
    {"source_feature_id": "BC_ARIS_37820", "survey_type": "ground", "subject": "soil geochem Eskay Creek", "lat": 56.65, "lon": -130.45},
    {"source_feature_id": "BC_ARIS_36911", "survey_type": "airborne", "subject": "VTEM Kemess East", "lat": 57.00, "lon": -126.75},
    {"source_feature_id": "BC_ARIS_40021", "survey_type": "ground", "subject": "geochem + structural mapping Rossland", "lat": 49.08, "lon": -117.80},
    {"source_feature_id": "BC_ARIS_38440", "survey_type": "underground", "subject": "underground sampling Sullivan reclamation", "lat": 49.69, "lon": -115.95},
    {"source_feature_id": "BC_ARIS_BC-REGIONAL-2019", "survey_type": "airborne", "subject": "BC northwest regional reconnaissance", "lat": 57.5, "lon": -130.0, "side_deg": 0.25},
]


def _fetch_bc_aris_features() -> list[dict[str, Any]]:
    return list(_SYNTHETIC_BC_SURVEYS)


async def sync_bc_aris_assessment_surveys(
    *, pool: asyncpg.Pool | None = None,
) -> AssessmentSurveySyncResult:
    """Sync BC ARIS assessment-file footprints into pg_assessment_survey."""
    return await _sync_assessment_surveys(
        source_id="bc_aris_assessment_survey",
        features=_fetch_bc_aris_features(),
        doc_phase=154,
        pool=pool,
    )


__all__ = [
    "AssessmentSurveySyncResult",
    "sync_sk_assessment_surveys",
    "sync_bc_aris_assessment_surveys",
]
