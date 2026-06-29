"""SaskGeoAtlas Drillhole Collar adapter (§6.1) — doc-phase 152.

Fourth §6 adapter. Lands drillhole-collar rows in
`public_geo.pg_drillhole_collar` under
`source_id='sk_drillhole_collar'`.

Same pattern as the mineral-occurrence adapters; different target
table + schema mapping for drillhole-specific columns
(drillhole_id, drill_type, total_length_m, inclination/azimuth,
core_availability, stratigraphic_depths jsonb).

The 12 seeded collars cover the Athabasca Basin uranium camp + the
La Ronge Au belt + a southern potash exploration hole.
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

log = logging.getLogger("georag.publicgeo.sk_drillhole_adapter")


class SKDrillholeSyncResult(NamedTuple):
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


# 12 SK drillhole collars. Athabasca + Trans-Hudson + potash.
_SYNTHETIC_SK_DRILLHOLES: list[dict[str, Any]] = [
    # Athabasca — uranium exploration drillholes
    {
        "source_feature_id": "SK_DH_MR-101",
        "drillhole_id": "MR-101",
        "drillhole_name": "McArthur River MR-101",
        "company": "Cameco",
        "project_name": "McArthur River Resource Definition",
        "date_drilled": "2023-03-15",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["U"],
        "total_length_m": 685.50,
        "inclination_deg": -75.0,
        "azimuth_deg": 045.0,
        "collar_elevation_m": 521.5,
        "core_availability": "available",
        "lat": 57.78, "lon": -105.03,
    },
    {
        "source_feature_id": "SK_DH_PLS22-001",
        "drillhole_id": "PLS22-001",
        "drillhole_name": "Patterson Lake South 22-001",
        "company": "Fission Uranium",
        "project_name": "Triple R Delineation",
        "date_drilled": "2022-07-22",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["U"],
        "total_length_m": 412.30,
        "inclination_deg": -68.0,
        "azimuth_deg": 320.0,
        "core_availability": "available",
        "lat": 58.30, "lon": -105.05,
    },
    {
        "source_feature_id": "SK_DH_AR23-189",
        "drillhole_id": "AR23-189",
        "drillhole_name": "Arrow AR23-189",
        "company": "NexGen Energy",
        "project_name": "Rook I — Arrow Definition",
        "date_drilled": "2023-09-08",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["U"],
        "total_length_m": 580.0,
        "inclination_deg": -70.0,
        "azimuth_deg": 130.0,
        "core_availability": "available",
        "lat": 58.31, "lon": -105.06,
    },
    {
        "source_feature_id": "SK_DH_CL-204",
        "drillhole_id": "CL-204",
        "drillhole_name": "Cigar Lake CL-204",
        "company": "Cameco",
        "project_name": "Cigar Lake Mine — Underground Definition",
        "date_drilled": "2023-11-02",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["U"],
        "total_length_m": 245.0,
        "inclination_deg": -85.0,
        "azimuth_deg": 0.0,
        "core_availability": "available",
        "lat": 58.06, "lon": -104.51,
    },
    {
        "source_feature_id": "SK_DH_WR22-021",
        "drillhole_id": "WR22-021",
        "drillhole_name": "Wheeler River WR22-021",
        "company": "Denison Mines",
        "project_name": "Phoenix Deposit ISR Pilot",
        "date_drilled": "2022-12-10",
        "drill_type": "rotary",
        "commodity_of_interest": ["U"],
        "total_length_m": 405.0,
        "inclination_deg": -88.0,
        "azimuth_deg": 0.0,
        "core_availability": "partial",
        "lat": 57.95, "lon": -104.97,
    },
    # Trans-Hudson — gold + base metal
    {
        "source_feature_id": "SK_DH_SB-2247",
        "drillhole_id": "SB-2247",
        "drillhole_name": "Seabee SB-2247",
        "company": "SSR Mining",
        "project_name": "Seabee Mine — Santoy Zone",
        "date_drilled": "2023-05-30",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Au"],
        "total_length_m": 320.0,
        "inclination_deg": -55.0,
        "azimuth_deg": 280.0,
        "core_availability": "available",
        "lat": 56.34, "lon": -103.96,
    },
    {
        "source_feature_id": "SK_DH_FF-CU-1108",
        "drillhole_id": "FF-CU-1108",
        "drillhole_name": "Flin Flon Cu-1108",
        "company": "Hudbay Minerals",
        "project_name": "Flin Flon Belt VMS Definition",
        "date_drilled": "2021-06-18",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Cu", "Zn"],
        "total_length_m": 612.0,
        "inclination_deg": -60.0,
        "azimuth_deg": 110.0,
        "core_availability": "available",
        "lat": 54.78, "lon": -101.84,
    },
    {
        "source_feature_id": "SK_DH_JL-405",
        "drillhole_id": "JL-405",
        "drillhole_name": "Jolu JL-405",
        "company": "Claude Resources",
        "project_name": "Jolu Au Historic",
        "date_drilled": "1998-08-12",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Au"],
        "total_length_m": 215.5,
        "inclination_deg": -50.0,
        "azimuth_deg": 270.0,
        "core_availability": "unknown",
        "lat": 55.42, "lon": -105.31,
    },
    {
        "source_feature_id": "SK_DH_LARONGE-2109",
        "drillhole_id": "LARONGE-2109",
        "drillhole_name": "La Ronge Au Belt 21-09",
        "company": "Taiga Gold",
        "project_name": "Eagle Lake",
        "date_drilled": "2021-08-04",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["Au"],
        "total_length_m": 280.0,
        "inclination_deg": -45.0,
        "azimuth_deg": 165.0,
        "core_availability": "available",
        "lat": 55.10, "lon": -104.65,
    },
    # Southern — potash exploration
    {
        "source_feature_id": "SK_DH_POTASH-MA-12",
        "drillhole_id": "MA-12",
        "drillhole_name": "Muskowekwan MA-12",
        "company": "Encanto Potash",
        "project_name": "Muskowekwan Potash Exploration",
        "date_drilled": "2019-04-22",
        "drill_type": "rotary",
        "commodity_of_interest": ["K"],
        "total_length_m": 1825.0,
        "inclination_deg": -90.0,
        "azimuth_deg": 0.0,
        "core_availability": "partial",
        "lat": 51.10, "lon": -103.42,
    },
    {
        "source_feature_id": "SK_DH_KICKINGHORSE-3",
        "drillhole_id": "KH-3",
        "drillhole_name": "Kicking Horse KH-3",
        "company": "BHP",
        "project_name": "Jansen Project",
        "date_drilled": "2014-10-05",
        "drill_type": "rotary",
        "commodity_of_interest": ["K"],
        "total_length_m": 1095.0,
        "inclination_deg": -90.0,
        "azimuth_deg": 0.0,
        "core_availability": "available",
        "lat": 51.50, "lon": -104.55,
    },
    # REE
    {
        "source_feature_id": "SK_DH_HOIDAS-HL-91",
        "drillhole_id": "HL-91",
        "drillhole_name": "Hoidas Lake HL-91",
        "company": "Great Western Minerals",
        "project_name": "Hoidas Lake REE",
        "date_drilled": "2009-07-18",
        "drill_type": "diamond_core",
        "commodity_of_interest": ["REE"],
        "total_length_m": 155.0,
        "inclination_deg": -75.0,
        "azimuth_deg": 295.0,
        "core_availability": "partial",
        "lat": 59.55, "lon": -108.65,
    },
]


def _fetch_sk_drillhole_features() -> list[dict[str, Any]]:
    """Synthetic-stub fetcher. Real impl uses SaskGeoAtlas Drillholes
    REST endpoint (stored in public_geo.sources.service_url)."""
    return list(_SYNTHETIC_SK_DRILLHOLES)


def _feature_checksum(feature: dict[str, Any]) -> str:
    canon = json.dumps(feature, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def sync_sk_drillhole_collars(
    *, pool: asyncpg.Pool | None = None,
) -> SKDrillholeSyncResult:
    """Sync SaskGeoAtlas drillhole collars into pg_drillhole_collar."""
    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        features = _fetch_sk_drillhole_features()
        log.info(
            "sync_sk_drillhole.fetched features=%d (synthetic_stub)",
            len(features),
        )

        async with pool.acquire() as conn:
            source_row = await conn.fetchrow(
                """
                SELECT source_id, jurisdiction_code, source_crs
                  FROM public_geo.sources
                 WHERE source_id = 'sk_drillhole_collar'
                """
            )
            if source_row is None:
                raise RuntimeError(
                    "sk_drillhole_collar source not registered."
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

                # Date may be ISO string in synthetic dict.
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
                    "sk_drillhole_collar",
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
                 WHERE source_id = 'sk_drillhole_collar'
                """
            )

            ledger = await emit_audit(
                conn,
                action_type="public_geo.pull.complete",
                actor_kind="workflow",
                target_schema="public_geo",
                target_table="pg_drillhole_collar",
                target_id="sk_drillhole_collar",
                payload={
                    "evaluator": "synthetic_stub",
                    "doc_phase": 152,
                    "source_id": "sk_drillhole_collar",
                    "jurisdiction_code": jurisdiction_code,
                    "fetched_features": len(features),
                    "inserted": inserted,
                    "updated": updated,
                    "skipped_no_geom": skipped_no_geom,
                },
            )

        log.info(
            "sync_sk_drillhole.completed fetched=%d inserted=%d updated=%d",
            len(features), inserted, updated,
        )

        return SKDrillholeSyncResult(
            source_id="sk_drillhole_collar",
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
    "SKDrillholeSyncResult",
    "sync_sk_drillhole_collars",
]
