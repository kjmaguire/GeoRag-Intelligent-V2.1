"""Bedrock Geology adapters (§6.3 + §6.4) — doc-phase 155.

Closes the §6 PublicGeo adapter set (9 of 9 graduated).

Two §6 adapters:
  - `sync_ab_ags_bedrock_geology()` — Alberta Geological Survey 1:1M bedrock
  - `sync_nrcan_geo_bedrock_geology()` — NRCan GEO.ca 1:5M bedrock

Lands MultiPolygon geological units in `public_geo.pg_bedrock_geology`.

Each adapter seeds 8-10 representative geological units (real Canadian
chronostratigraphy: Canadian Shield Precambrian basement, WCSB Mesozoic
cover, Cordilleran orogenic belts, etc.).
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

log = logging.getLogger("georag.publicgeo.bedrock_geology_adapters")


class BedrockGeologySyncResult(NamedTuple):
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


def _box_wkt(lat: float, lon: float, w_deg: float = 1.0, h_deg: float = 0.6) -> str:
    """MultiPolygon box around (lat, lon) — used as a stand-in for the
    real geological-unit polygon."""
    n, s = lat + h_deg / 2, lat - h_deg / 2
    e, w = lon + w_deg / 2, lon - w_deg / 2
    return f"MULTIPOLYGON((({w} {s}, {e} {s}, {e} {n}, {w} {n}, {w} {s})))"


def _feature_checksum(feature: dict[str, Any]) -> str:
    canon = json.dumps(feature, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def _sync_bedrock(
    *,
    source_id: str,
    features: list[dict[str, Any]],
    doc_phase: int,
    pool: asyncpg.Pool | None = None,
) -> BedrockGeologySyncResult:
    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        async with pool.acquire() as conn:
            source_row = await conn.fetchrow(
                "SELECT source_id, jurisdiction_code, source_crs "
                "FROM public_geo.sources WHERE source_id = $1",
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
                wkt = _box_wkt(
                    f["lat"], f["lon"],
                    w_deg=f.get("w_deg", 1.0),
                    h_deg=f.get("h_deg", 0.6),
                )

                row = await conn.fetchrow(
                    """
                    INSERT INTO public_geo.pg_bedrock_geology (
                        id, jurisdiction_code, source_id, source_feature_id,
                        unit_code, unit_name, eon, era, period, group_name,
                        formation, member, structural_domain, lithology,
                        scale, source_crs, source_geom_wkt,
                        source_attributes, checksum, geom
                    )
                    VALUES (
                        $1::uuid, $2, $3, $4,
                        $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14,
                        $15, $16, $17,
                        $18::jsonb, $19,
                        ST_SetSRID(ST_GeomFromText($17), 4326)
                    )
                    ON CONFLICT (source_id, source_feature_id) DO UPDATE
                       SET unit_code = EXCLUDED.unit_code,
                           unit_name = EXCLUDED.unit_name,
                           eon = EXCLUDED.eon,
                           era = EXCLUDED.era,
                           period = EXCLUDED.period,
                           group_name = EXCLUDED.group_name,
                           formation = EXCLUDED.formation,
                           lithology = EXCLUDED.lithology,
                           geom = EXCLUDED.geom,
                           source_geom_wkt = EXCLUDED.source_geom_wkt,
                           source_attributes = EXCLUDED.source_attributes,
                           checksum = EXCLUDED.checksum,
                           last_seen_at = now(),
                           updated_at = now()
                     WHERE pg_bedrock_geology.checksum <> EXCLUDED.checksum
                    RETURNING (xmax = 0) AS inserted_row
                    """,
                    str(uuid4()),
                    jurisdiction_code,
                    source_id,
                    f["source_feature_id"],
                    f["unit_code"],
                    f.get("unit_name"),
                    f.get("eon"),
                    f.get("era"),
                    f.get("period"),
                    f.get("group_name"),
                    f.get("formation"),
                    f.get("member"),
                    f.get("structural_domain"),
                    f.get("lithology"),
                    f.get("scale", "250K"),
                    source_crs,
                    wkt,
                    json.dumps({k: v for k, v in f.items() if k not in {"lat", "lon", "w_deg", "h_deg"}}, default=str),
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
                target_table="pg_bedrock_geology",
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
            "sync_bedrock.%s.completed fetched=%d inserted=%d updated=%d",
            source_id, len(features), inserted, updated,
        )

        return BedrockGeologySyncResult(
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
# Alberta — AGS 1:1M bedrock
# ----------------------------------------------------------------------
_SYNTHETIC_AB_BEDROCK: list[dict[str, Any]] = [
    {"source_feature_id": "AB_BED_001", "unit_code": "ABFB", "unit_name": "Athabasca Basin Fort McMurray Fm", "eon": "Phanerozoic", "era": "Mesozoic", "period": "Cretaceous", "group_name": "Mannville", "formation": "McMurray", "lithology": "bitumen-rich sandstone", "scale": "1M", "lat": 57.0, "lon": -111.5},
    {"source_feature_id": "AB_BED_002", "unit_code": "ABBR", "unit_name": "Belly River Group", "eon": "Phanerozoic", "era": "Mesozoic", "period": "Cretaceous", "group_name": "Belly River", "lithology": "sandstone-shale", "scale": "1M", "lat": 52.5, "lon": -114.0, "w_deg": 2.0},
    {"source_feature_id": "AB_BED_003", "unit_code": "ABCS", "unit_name": "Canadian Shield exposure NE Alberta", "eon": "Precambrian", "era": "Proterozoic", "period": "Paleoproterozoic", "lithology": "granitoid gneiss", "scale": "1M", "lat": 58.5, "lon": -110.5},
    {"source_feature_id": "AB_BED_004", "unit_code": "ABMS", "unit_name": "Misty Mountain Fm", "eon": "Phanerozoic", "era": "Mesozoic", "period": "Jurassic", "formation": "Misty Mountain", "lithology": "siltstone", "scale": "1M", "lat": 50.5, "lon": -115.0},
    {"source_feature_id": "AB_BED_005", "unit_code": "ABRC", "unit_name": "Rocky Mountain Cordillera thrust belt", "eon": "Phanerozoic", "era": "Mesozoic", "period": "Cretaceous", "structural_domain": "Cordilleran thrust", "lithology": "limestone-shale-dolomite", "scale": "1M", "lat": 50.0, "lon": -115.5, "h_deg": 1.5},
    {"source_feature_id": "AB_BED_006", "unit_code": "ABLD", "unit_name": "Leduc Reef Complex", "eon": "Phanerozoic", "era": "Paleozoic", "period": "Devonian", "group_name": "Woodbend", "formation": "Leduc", "lithology": "reefal carbonate", "scale": "1M", "lat": 53.5, "lon": -113.5},
    {"source_feature_id": "AB_BED_007", "unit_code": "ABCP", "unit_name": "Cretaceous Paskapoo Fm", "eon": "Phanerozoic", "era": "Cenozoic", "period": "Paleocene", "formation": "Paskapoo", "lithology": "sandstone-mudstone", "scale": "1M", "lat": 52.0, "lon": -114.0},
    {"source_feature_id": "AB_BED_008", "unit_code": "ABDM", "unit_name": "Devonian Manitoba Group salt", "eon": "Phanerozoic", "era": "Paleozoic", "period": "Devonian", "group_name": "Elk Point", "lithology": "evaporite (salt + anhydrite)", "scale": "1M", "lat": 56.5, "lon": -112.5},
]


async def sync_ab_ags_bedrock_geology(
    *, pool: asyncpg.Pool | None = None,
) -> BedrockGeologySyncResult:
    """Sync Alberta AGS bedrock units into pg_bedrock_geology."""
    return await _sync_bedrock(
        source_id="ab_ags_bedrock_geology",
        features=_SYNTHETIC_AB_BEDROCK,
        doc_phase=155,
        pool=pool,
    )


# ----------------------------------------------------------------------
# NRCan GEO.ca 1:5M bedrock
# ----------------------------------------------------------------------
_SYNTHETIC_NRCAN_BEDROCK: list[dict[str, Any]] = [
    {"source_feature_id": "NRCAN_BED_CS", "unit_code": "PCSH", "unit_name": "Canadian Shield — Precambrian crystalline basement", "eon": "Precambrian", "era": "Archean", "lithology": "granitoid-gneiss", "scale": "5M", "lat": 55.0, "lon": -90.0, "w_deg": 30.0, "h_deg": 12.0},
    {"source_feature_id": "NRCAN_BED_WCSB", "unit_code": "WCSB", "unit_name": "Western Canada Sedimentary Basin cover", "eon": "Phanerozoic", "era": "Mesozoic", "lithology": "sandstone-shale-carbonate", "scale": "5M", "lat": 53.0, "lon": -110.0, "w_deg": 15.0, "h_deg": 8.0},
    {"source_feature_id": "NRCAN_BED_COR", "unit_code": "CORO", "unit_name": "Cordilleran Orogen", "eon": "Phanerozoic", "era": "Mesozoic", "structural_domain": "Cordillera", "lithology": "metamorphic-igneous-sedimentary", "scale": "5M", "lat": 54.0, "lon": -125.0, "w_deg": 8.0, "h_deg": 10.0},
    {"source_feature_id": "NRCAN_BED_APP", "unit_code": "APPN", "unit_name": "Appalachian Orogen — Atlantic Canada", "eon": "Phanerozoic", "era": "Paleozoic", "structural_domain": "Appalachian", "lithology": "metamorphic-sedimentary", "scale": "5M", "lat": 46.0, "lon": -65.0, "w_deg": 8.0, "h_deg": 4.0},
    {"source_feature_id": "NRCAN_BED_TPB", "unit_code": "THPB", "unit_name": "Trans-Hudson Orogen (greenstone belts)", "eon": "Precambrian", "era": "Proterozoic", "period": "Paleoproterozoic", "lithology": "metavolcanic-metasedimentary", "scale": "5M", "lat": 55.0, "lon": -103.0, "w_deg": 5.0, "h_deg": 4.0},
    {"source_feature_id": "NRCAN_BED_FRA", "unit_code": "FRAN", "unit_name": "Franklin Orogen Arctic Islands", "eon": "Phanerozoic", "era": "Paleozoic", "lithology": "sedimentary cover + folded basement", "scale": "5M", "lat": 75.0, "lon": -95.0, "w_deg": 15.0, "h_deg": 8.0},
    {"source_feature_id": "NRCAN_BED_ATB", "unit_code": "ATHB", "unit_name": "Athabasca Basin sandstone cover", "eon": "Precambrian", "era": "Proterozoic", "period": "Mesoproterozoic", "group_name": "Athabasca", "lithology": "quartzose sandstone", "scale": "5M", "lat": 58.5, "lon": -106.0, "w_deg": 4.0, "h_deg": 2.0},
    {"source_feature_id": "NRCAN_BED_INN", "unit_code": "INNU", "unit_name": "Innuitian Orogen", "eon": "Phanerozoic", "era": "Paleozoic", "lithology": "carbonate-clastic-folded", "scale": "5M", "lat": 80.0, "lon": -95.0, "w_deg": 12.0, "h_deg": 5.0},
]


async def sync_nrcan_geo_bedrock_geology(
    *, pool: asyncpg.Pool | None = None,
) -> BedrockGeologySyncResult:
    """Sync NRCan GEO.ca 1:5M bedrock geology into pg_bedrock_geology."""
    return await _sync_bedrock(
        source_id="nrcan_geo_bedrock_geology",
        features=_SYNTHETIC_NRCAN_BEDROCK,
        doc_phase=155,
        pool=pool,
    )


__all__ = [
    "BedrockGeologySyncResult",
    "sync_ab_ags_bedrock_geology",
    "sync_nrcan_geo_bedrock_geology",
]
