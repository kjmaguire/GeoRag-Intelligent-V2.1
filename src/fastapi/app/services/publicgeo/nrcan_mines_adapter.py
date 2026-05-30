"""NRCan Canadian Mines adapter (§6.3) — doc-phase 150.

Second §6 adapter graduation. Pulls major operating + past-producing
mines across Canada into `public_geo.pg_mine` under
`source_id='nrcan_canadian_mines'`.

Same shape as the doc-phase 149 BC MINFILE adapter:
  - Synthetic-stub fetcher with real schema mapping
  - Idempotent UPSERT keyed on (source_id, source_feature_id)
  - Audit anchor + sources.last_refreshed_at bump

The 12 seeded mines cover major Canadian operations across multiple
provinces + commodity groupings — gives the federal-coverage view
of `/public-geoscience` something to render.
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

log = logging.getLogger("georag.publicgeo.nrcan_mines_adapter")


class NRCanMinesSyncResult(NamedTuple):
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


# 12 major Canadian mines spanning provinces + commodity groups.
# Real coordinates; real operators; status accurate as of late 2025.
_SYNTHETIC_NRCAN_MINES: list[dict[str, Any]] = [
    {
        "source_feature_id": "NRCAN_MINES_DIAVIK",
        "name": "Diavik Diamond Mine",
        "status": "producing",
        "commodities": ["diamonds"],
        "commodity_grouping": "gemstones",
        "operator": "Rio Tinto",
        "province": "NT",
        "lat": 64.50, "lon": -110.27,
    },
    {
        "source_feature_id": "NRCAN_MINES_ESKAY",
        "name": "Eskay Creek (restart)",
        "status": "developed-deposit",
        "commodities": ["Au", "Ag"],
        "commodity_grouping": "precious_metals",
        "operator": "Skeena Resources",
        "province": "BC",
        "lat": 56.65, "lon": -130.45,
    },
    {
        "source_feature_id": "NRCAN_MINES_DETOUR",
        "name": "Detour Lake Mine",
        "status": "producing",
        "commodities": ["Au"],
        "commodity_grouping": "precious_metals",
        "operator": "Agnico Eagle",
        "province": "ON",
        "lat": 50.02, "lon": -79.69,
    },
    {
        "source_feature_id": "NRCAN_MINES_VOISEY",
        "name": "Voisey's Bay Mine",
        "status": "producing",
        "commodities": ["Ni", "Cu", "Co"],
        "commodity_grouping": "base_metals",
        "operator": "Vale",
        "province": "NL",
        "lat": 56.32, "lon": -61.85,
    },
    {
        "source_feature_id": "NRCAN_MINES_HIGHLAND",
        "name": "Highland Valley Copper",
        "status": "producing",
        "commodities": ["Cu", "Mo"],
        "commodity_grouping": "base_metals",
        "operator": "Teck Resources",
        "province": "BC",
        "lat": 50.49, "lon": -121.02,
    },
    {
        "source_feature_id": "NRCAN_MINES_CIGAR",
        "name": "Cigar Lake Mine",
        "status": "producing",
        "commodities": ["U"],
        "commodity_grouping": "uranium",
        "operator": "Cameco",
        "province": "SK",
        "lat": 58.06, "lon": -104.51,
    },
    {
        "source_feature_id": "NRCAN_MINES_MCARTHUR",
        "name": "McArthur River Mine",
        "status": "producing",
        "commodities": ["U"],
        "commodity_grouping": "uranium",
        "operator": "Cameco",
        "province": "SK",
        "lat": 57.77, "lon": -105.04,
    },
    {
        "source_feature_id": "NRCAN_MINES_RAGLAN",
        "name": "Raglan Mine",
        "status": "producing",
        "commodities": ["Ni", "Cu"],
        "commodity_grouping": "base_metals",
        "operator": "Glencore",
        "province": "QC",
        "lat": 61.69, "lon": -73.66,
    },
    {
        "source_feature_id": "NRCAN_MINES_SULLIVAN",
        "name": "Sullivan Mine",
        "status": "closed",
        "commodities": ["Pb", "Zn", "Ag"],
        "commodity_grouping": "base_metals",
        "operator": "Teck Resources (closed 2001)",
        "province": "BC",
        "lat": 49.69, "lon": -115.95,
    },
    {
        "source_feature_id": "NRCAN_MINES_NECHALACHO",
        "name": "Nechalacho REE Project",
        "status": "developed-deposit",
        "commodities": ["REE", "Nb", "Ta"],
        "commodity_grouping": "ree",
        "operator": "Vital Metals",
        "province": "NT",
        "lat": 62.51, "lon": -112.36,
    },
    {
        "source_feature_id": "NRCAN_MINES_POTASH_LANIGAN",
        "name": "Lanigan Potash Mine",
        "status": "producing",
        "commodities": ["K"],
        "commodity_grouping": "potash_salt",
        "operator": "Nutrien",
        "province": "SK",
        "lat": 51.79, "lon": -105.04,
    },
    {
        "source_feature_id": "NRCAN_MINES_ELKVALLEY",
        "name": "Elk Valley Coal Operations",
        "status": "producing",
        "commodities": ["coal_metallurgical"],
        "commodity_grouping": "coal",
        "operator": "Glencore (formerly Teck)",
        "province": "BC",
        "lat": 49.79, "lon": -114.92,
    },
]


def _fetch_nrcan_mines_features() -> list[dict[str, Any]]:
    """Synthetic-stub fetcher. Returns 12 major Canadian mines.
    Real impl pulls from NRCan Atlas / GEO.ca registry."""
    return list(_SYNTHETIC_NRCAN_MINES)


def _feature_checksum(feature: dict[str, Any]) -> str:
    canon = json.dumps(feature, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def sync_nrcan_canadian_mines(
    *, pool: asyncpg.Pool | None = None,
) -> NRCanMinesSyncResult:
    """Sync NRCan Canadian Mines into pg_mine."""
    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        features = _fetch_nrcan_mines_features()
        log.info(
            "sync_nrcan_mines.fetched features=%d (synthetic_stub)",
            len(features),
        )

        async with pool.acquire() as conn:
            source_row = await conn.fetchrow(
                """
                SELECT source_id, jurisdiction_code, source_crs
                  FROM public_geo.sources
                 WHERE source_id = 'nrcan_canadian_mines'
                """
            )
            if source_row is None:
                raise RuntimeError(
                    "nrcan_canadian_mines source not registered. "
                    "Run the doc-phase 135 migration first."
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

                row = await conn.fetchrow(
                    """
                    INSERT INTO public_geo.pg_mine (
                        id, jurisdiction_code, source_id, source_feature_id,
                        name, status, commodities, commodity_grouping,
                        operator, source_crs, source_geom_wkt,
                        source_attributes, checksum, geom
                    )
                    VALUES (
                        $1::uuid, $2, $3, $4,
                        $5, $6, $7::text[], $8,
                        $9, $10, $11,
                        $12::jsonb, $13, ST_SetSRID(ST_GeomFromText($11), 4326)
                    )
                    ON CONFLICT (source_id, source_feature_id) DO UPDATE
                       SET name = EXCLUDED.name,
                           status = EXCLUDED.status,
                           commodities = EXCLUDED.commodities,
                           commodity_grouping = EXCLUDED.commodity_grouping,
                           operator = EXCLUDED.operator,
                           source_geom_wkt = EXCLUDED.source_geom_wkt,
                           geom = EXCLUDED.geom,
                           source_attributes = EXCLUDED.source_attributes,
                           checksum = EXCLUDED.checksum,
                           last_seen_at = now(),
                           updated_at = now()
                     WHERE pg_mine.checksum <> EXCLUDED.checksum
                    RETURNING (xmax = 0) AS inserted_row
                    """,
                    str(uuid4()),
                    jurisdiction_code,
                    "nrcan_canadian_mines",
                    f["source_feature_id"],
                    f.get("name"),
                    f.get("status", "unknown"),
                    f.get("commodities", []),
                    f.get("commodity_grouping"),
                    f.get("operator"),
                    source_crs,
                    wkt,
                    json.dumps({k: v for k, v in f.items() if k not in {"lat", "lon"}}),
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
                 WHERE source_id = 'nrcan_canadian_mines'
                """
            )

            ledger = await emit_audit(
                conn,
                action_type="public_geo.pull.complete",
                actor_kind="workflow",
                target_schema="public_geo",
                target_table="pg_mine",
                target_id="nrcan_canadian_mines",
                payload={
                    "evaluator": "synthetic_stub",
                    "doc_phase": 150,
                    "source_id": "nrcan_canadian_mines",
                    "jurisdiction_code": jurisdiction_code,
                    "fetched_features": len(features),
                    "inserted": inserted,
                    "updated": updated,
                    "skipped_no_geom": skipped_no_geom,
                },
            )

        log.info(
            "sync_nrcan_mines.completed fetched=%d inserted=%d updated=%d "
            "skipped_no_geom=%d",
            len(features), inserted, updated, skipped_no_geom,
        )

        return NRCanMinesSyncResult(
            source_id="nrcan_canadian_mines",
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
    "NRCanMinesSyncResult",
    "sync_nrcan_canadian_mines",
]
