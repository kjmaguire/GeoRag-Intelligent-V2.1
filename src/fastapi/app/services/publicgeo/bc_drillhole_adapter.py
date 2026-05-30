"""BC MINFILE Drillhole Collar adapter (§6.2) — doc-phase 153.

Fifth §6 adapter. Lands drillhole-collar rows in
`public_geo.pg_drillhole_collar` under
`source_id='bc_minfile_drillhole_collar'`.

Clones the doc-phase 152 SK drillhole pattern with BC-specific
synthetic data (10 collars across BC mineral districts).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import asyncpg

from app.audit import emit_audit

log = logging.getLogger("georag.publicgeo.bc_drillhole_adapter")


class BCDrillholeSyncResult(NamedTuple):
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


_SYNTHETIC_BC_DRILLHOLES: list[dict[str, Any]] = [
    {
        "source_feature_id": "BC_DH_HVC-2023-184",
        "drillhole_id": "HVC-2023-184",
        "drillhole_name": "Highland Valley HVC-2023-184",
        "company": "Teck Resources",
        "project_name": "Highland Valley Copper — Valley Pit Extension",
        "date_drilled": "2023-08-12",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Cu", "Mo"],
        "total_length_m": 740.0,
        "inclination_deg": -65.0,
        "azimuth_deg": 240.0,
        "core_availability": "available",
        "lat": 50.49, "lon": -121.02,
    },
    {
        "source_feature_id": "BC_DH_BJ-22-491",
        "drillhole_id": "BJ-22-491",
        "drillhole_name": "Brucejack BJ-22-491",
        "company": "Pretium Resources",
        "project_name": "Brucejack — Valley of Kings",
        "date_drilled": "2022-11-18",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Au", "Ag"],
        "total_length_m": 385.0,
        "inclination_deg": -55.0,
        "azimuth_deg": 050.0,
        "core_availability": "available",
        "lat": 56.47, "lon": -130.21,
    },
    {
        "source_feature_id": "BC_DH_MM-23-1106",
        "drillhole_id": "MM-23-1106",
        "drillhole_name": "Mount Milligan MM-23-1106",
        "company": "Centerra Gold",
        "project_name": "Mount Milligan Reserve Replacement",
        "date_drilled": "2023-06-04",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Cu", "Au"],
        "total_length_m": 520.0,
        "inclination_deg": -60.0,
        "azimuth_deg": 130.0,
        "core_availability": "available",
        "lat": 55.13, "lon": -124.07,
    },
    {
        "source_feature_id": "BC_DH_ESK22-018",
        "drillhole_id": "ESK22-018",
        "drillhole_name": "Eskay Creek ESK22-018",
        "company": "Skeena Resources",
        "project_name": "Eskay Creek Restart — 21B Zone",
        "date_drilled": "2022-09-30",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Au", "Ag"],
        "total_length_m": 295.0,
        "inclination_deg": -70.0,
        "azimuth_deg": 200.0,
        "core_availability": "available",
        "lat": 56.65, "lon": -130.45,
    },
    {
        "source_feature_id": "BC_DH_KMS-S-2018-04",
        "drillhole_id": "KMS-S-2018-04",
        "drillhole_name": "Kemess South KMS-S-2018-04",
        "company": "Centerra Gold",
        "project_name": "Kemess East",
        "date_drilled": "2018-07-22",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Cu", "Au"],
        "total_length_m": 670.0,
        "inclination_deg": -75.0,
        "azimuth_deg": 010.0,
        "core_availability": "partial",
        "lat": 57.00, "lon": -126.75,
    },
    {
        "source_feature_id": "BC_DH_ENDK-22-302",
        "drillhole_id": "ENDK-22-302",
        "drillhole_name": "Endako ENDK-22-302",
        "company": "Thompson Creek Metals",
        "project_name": "Endako Mo Restart Evaluation",
        "date_drilled": "2022-04-15",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Mo"],
        "total_length_m": 540.0,
        "inclination_deg": -50.0,
        "azimuth_deg": 270.0,
        "core_availability": "partial",
        "lat": 54.06, "lon": -125.10,
    },
    {
        "source_feature_id": "BC_DH_TC-21-007",
        "drillhole_id": "TC-21-007",
        "drillhole_name": "Tulsequah Chief TC-21-007",
        "company": "Chieftain Metals",
        "project_name": "Tulsequah VMS — Updip Definition",
        "date_drilled": "2021-08-20",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Cu", "Zn", "Au", "Ag"],
        "total_length_m": 380.0,
        "inclination_deg": -60.0,
        "azimuth_deg": 080.0,
        "core_availability": "available",
        "lat": 58.74, "lon": -133.59,
    },
    {
        "source_feature_id": "BC_DH_WEDGE-LI-12",
        "drillhole_id": "WEDGE-LI-12",
        "drillhole_name": "Wedge Pegmatite LI-12",
        "company": "Spey Resources",
        "project_name": "Wedge Lithium Pegmatite Definition",
        "date_drilled": "2024-02-14",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Li"],
        "total_length_m": 165.0,
        "inclination_deg": -45.0,
        "azimuth_deg": 145.0,
        "core_availability": "available",
        "lat": 50.20, "lon": -126.50,
    },
    {
        "source_feature_id": "BC_DH_ROSS-AG-2019-22",
        "drillhole_id": "ROSS-AG-2019-22",
        "drillhole_name": "Rossland Gold-Silver 2019-22",
        "company": "Kootenay Silver",
        "project_name": "Rossland Camp Historical Vein Definition",
        "date_drilled": "2019-09-08",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Au", "Ag"],
        "total_length_m": 195.0,
        "inclination_deg": -55.0,
        "azimuth_deg": 320.0,
        "core_availability": "partial",
        "lat": 49.08, "lon": -117.80,
    },
    {
        "source_feature_id": "BC_DH_BOSS-MO-2021-08",
        "drillhole_id": "BOSS-MO-2021-08",
        "drillhole_name": "Boss Mountain Mo 2021-08",
        "company": "Hi Ho Silver",
        "project_name": "Boss Mountain Mo Restart Eval",
        "date_drilled": "2021-06-12",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Mo"],
        "total_length_m": 410.0,
        "inclination_deg": -70.0,
        "azimuth_deg": 095.0,
        "core_availability": "unknown",
        "lat": 52.10, "lon": -120.85,
    },
]


def _fetch_bc_drillhole_features() -> list[dict[str, Any]]:
    return list(_SYNTHETIC_BC_DRILLHOLES)


def _feature_checksum(feature: dict[str, Any]) -> str:
    canon = json.dumps(feature, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def sync_bc_drillhole_collars(
    *, pool: asyncpg.Pool | None = None,
) -> BCDrillholeSyncResult:
    """Sync BC MINFILE drillholes into pg_drillhole_collar."""
    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        features = _fetch_bc_drillhole_features()
        log.info(
            "sync_bc_drillhole.fetched features=%d (synthetic_stub)",
            len(features),
        )

        async with pool.acquire() as conn:
            source_row = await conn.fetchrow(
                """
                SELECT source_id, jurisdiction_code, source_crs
                  FROM public_geo.sources
                 WHERE source_id = 'bc_minfile_drillhole_collar'
                """
            )
            if source_row is None:
                raise RuntimeError(
                    "bc_minfile_drillhole_collar source not registered."
                )

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
                wkt = f"POINT({f['lon']} {f['lat']})"

                drilled = f.get("date_drilled")
                if drilled and isinstance(drilled, str):
                    drilled = date.fromisoformat(drilled)

                row = await conn.fetchrow(
                    """
                    INSERT INTO public_geo.pg_drillhole_collar (
                        id, jurisdiction_code, source_id, source_feature_id,
                        drillhole_id, drillhole_name, company, project_name,
                        date_drilled, drill_type, commodity_of_interest,
                        total_length_m, inclination_deg, azimuth_deg,
                        collar_elevation_m, core_availability,
                        source_crs, source_geom_wkt, source_attributes,
                        checksum, geom
                    )
                    VALUES (
                        $1::uuid, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10, $11::text[],
                        $12, $13, $14,
                        $15, $16,
                        $17, $18, $19::jsonb,
                        $20, ST_SetSRID(ST_GeomFromText($18), 4326)
                    )
                    ON CONFLICT (source_id, source_feature_id) DO UPDATE
                       SET drillhole_id = EXCLUDED.drillhole_id,
                           drillhole_name = EXCLUDED.drillhole_name,
                           company = EXCLUDED.company,
                           project_name = EXCLUDED.project_name,
                           date_drilled = EXCLUDED.date_drilled,
                           drill_type = EXCLUDED.drill_type,
                           commodity_of_interest = EXCLUDED.commodity_of_interest,
                           total_length_m = EXCLUDED.total_length_m,
                           inclination_deg = EXCLUDED.inclination_deg,
                           azimuth_deg = EXCLUDED.azimuth_deg,
                           collar_elevation_m = EXCLUDED.collar_elevation_m,
                           core_availability = EXCLUDED.core_availability,
                           geom = EXCLUDED.geom,
                           source_attributes = EXCLUDED.source_attributes,
                           checksum = EXCLUDED.checksum,
                           last_seen_at = now(),
                           updated_at = now()
                     WHERE pg_drillhole_collar.checksum <> EXCLUDED.checksum
                    RETURNING (xmax = 0) AS inserted_row
                    """,
                    str(uuid4()),
                    jurisdiction_code,
                    "bc_minfile_drillhole_collar",
                    f["source_feature_id"],
                    f.get("drillhole_id"),
                    f.get("drillhole_name"),
                    f.get("company"),
                    f.get("project_name"),
                    drilled,
                    f.get("drill_type"),
                    f.get("commodity_of_interest", []),
                    f.get("total_length_m"),
                    f.get("inclination_deg"),
                    f.get("azimuth_deg"),
                    f.get("collar_elevation_m"),
                    f.get("core_availability", "unknown"),
                    source_crs,
                    wkt,
                    json.dumps({k: v for k, v in f.items() if k not in {"lat", "lon"}}, default=str),
                    checksum,
                )
                if row is None:
                    pass
                elif row["inserted_row"]:
                    inserted += 1
                else:
                    updated += 1

            await conn.execute(
                """
                UPDATE public_geo.sources
                   SET last_refreshed_at = now()
                 WHERE source_id = 'bc_minfile_drillhole_collar'
                """
            )

            ledger = await emit_audit(
                conn,
                action_type="public_geo.pull.complete",
                actor_kind="workflow",
                target_schema="public_geo",
                target_table="pg_drillhole_collar",
                target_id="bc_minfile_drillhole_collar",
                payload={
                    "evaluator": "synthetic_stub",
                    "doc_phase": 153,
                    "source_id": "bc_minfile_drillhole_collar",
                    "jurisdiction_code": jurisdiction_code,
                    "fetched_features": len(features),
                    "inserted": inserted,
                    "updated": updated,
                    "skipped_no_geom": skipped_no_geom,
                },
            )

        log.info(
            "sync_bc_drillhole.completed fetched=%d inserted=%d updated=%d",
            len(features), inserted, updated,
        )

        return BCDrillholeSyncResult(
            source_id="bc_minfile_drillhole_collar",
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


__all__ = [
    "BCDrillholeSyncResult",
    "sync_bc_drillhole_collars",
]
