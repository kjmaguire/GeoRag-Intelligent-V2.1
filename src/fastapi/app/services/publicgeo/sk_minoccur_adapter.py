"""SaskGeoAtlas Mineral Occurrence adapter (§6.1) — doc-phase 151.

Third §6 adapter graduation. Saskatchewan is the primary launch
jurisdiction for GeoRAG. Lands canonical mineral-occurrence rows in
`public_geo.pg_mineral_occurrence` under
`source_id='sk_mineral_occurrence'`.

Same shape as the doc-phase 149 BC MINFILE adapter:
  - Synthetic-stub fetcher with real schema mapping
  - Idempotent UPSERT keyed on (source_id, source_feature_id)
  - Audit anchor + sources.last_refreshed_at bump

The 14 seeded occurrences span the Athabasca Basin (uranium), Trans-
Hudson orogen (gold/base metals), and the southern potash belt —
the three major Saskatchewan mineral provinces.
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

log = logging.getLogger("georag.publicgeo.sk_minoccur_adapter")


class SKMinOccurSyncResult(NamedTuple):
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


# 14 Saskatchewan mineral occurrences. SMDI-style external IDs.
# Real lat/lon. Covers the 3 major SK mineral provinces.
_SYNTHETIC_SK_OCCURRENCES: list[dict[str, Any]] = [
    # ── Athabasca Basin — uranium ────────────────────────────────────
    {
        "source_feature_id": "SK_SMDI_2543",
        "external_id": "SMDI_2543",
        "name": "McArthur River Deposit",
        "status": "deposit",
        "primary_commodities": ["U"],
        "associated_commodities": [],
        "commodity_grouping": "uranium",
        "discovery_type": "unconformity_uranium",
        "production_flag": True,
        "lat": 57.77, "lon": -105.04,
        "reserves_resources": "World's largest high-grade U deposit",
    },
    {
        "source_feature_id": "SK_SMDI_2611",
        "external_id": "SMDI_2611",
        "name": "Cigar Lake Deposit",
        "status": "deposit",
        "primary_commodities": ["U"],
        "associated_commodities": [],
        "commodity_grouping": "uranium",
        "discovery_type": "unconformity_uranium",
        "production_flag": True,
        "lat": 58.06, "lon": -104.51,
    },
    {
        "source_feature_id": "SK_SMDI_2398",
        "external_id": "SMDI_2398",
        "name": "Key Lake (closed)",
        "status": "past-producer",
        "primary_commodities": ["U"],
        "associated_commodities": [],
        "commodity_grouping": "uranium",
        "discovery_type": "unconformity_uranium",
        "production_flag": True,
        "lat": 57.20, "lon": -105.62,
    },
    {
        "source_feature_id": "SK_SMDI_3201",
        "external_id": "SMDI_3201",
        "name": "Triple R Deposit",
        "status": "deposit",
        "primary_commodities": ["U"],
        "associated_commodities": [],
        "commodity_grouping": "uranium",
        "discovery_type": "unconformity_uranium",
        "production_flag": False,
        "lat": 58.30, "lon": -105.05,
        "reserves_resources": "Patterson Lake South discovery",
    },
    {
        "source_feature_id": "SK_SMDI_3215",
        "external_id": "SMDI_3215",
        "name": "Arrow Deposit",
        "status": "deposit",
        "primary_commodities": ["U"],
        "associated_commodities": [],
        "commodity_grouping": "uranium",
        "discovery_type": "basement_hosted_uranium",
        "production_flag": False,
        "lat": 58.31, "lon": -105.07,
    },
    {
        "source_feature_id": "SK_SMDI_3299",
        "external_id": "SMDI_3299",
        "name": "Wheeler River",
        "status": "deposit",
        "primary_commodities": ["U"],
        "associated_commodities": [],
        "commodity_grouping": "uranium",
        "discovery_type": "unconformity_uranium",
        "production_flag": False,
        "lat": 57.95, "lon": -104.97,
    },
    # ── Trans-Hudson Orogen — gold + base metals ─────────────────────
    {
        "source_feature_id": "SK_SMDI_1812",
        "external_id": "SMDI_1812",
        "name": "Seabee Gold Mine",
        "status": "producer",
        "primary_commodities": ["Au"],
        "associated_commodities": ["Ag"],
        "commodity_grouping": "precious_metals",
        "discovery_type": "orogenic_gold",
        "production_flag": True,
        "lat": 56.34, "lon": -103.96,
    },
    {
        "source_feature_id": "SK_SMDI_1456",
        "external_id": "SMDI_1456",
        "name": "Flin Flon (SK side)",
        "status": "past-producer",
        "primary_commodities": ["Cu", "Zn"],
        "associated_commodities": ["Au", "Ag"],
        "commodity_grouping": "base_metals",
        "discovery_type": "vms",
        "production_flag": True,
        "lat": 54.78, "lon": -101.85,
    },
    {
        "source_feature_id": "SK_SMDI_1521",
        "external_id": "SMDI_1521",
        "name": "Star Lake",
        "status": "past-producer",
        "primary_commodities": ["Au"],
        "associated_commodities": [],
        "commodity_grouping": "precious_metals",
        "discovery_type": "orogenic_gold",
        "production_flag": True,
        "lat": 55.40, "lon": -105.50,
    },
    {
        "source_feature_id": "SK_SMDI_1689",
        "external_id": "SMDI_1689",
        "name": "Jolu Gold Mine",
        "status": "past-producer",
        "primary_commodities": ["Au"],
        "associated_commodities": ["Ag"],
        "commodity_grouping": "precious_metals",
        "discovery_type": "orogenic_gold",
        "production_flag": True,
        "lat": 55.42, "lon": -105.31,
    },
    # ── Southern potash belt + REE ───────────────────────────────────
    {
        "source_feature_id": "SK_SMDI_5101",
        "external_id": "SMDI_5101",
        "name": "Lanigan Potash Mine",
        "status": "producer",
        "primary_commodities": ["K"],
        "associated_commodities": [],
        "commodity_grouping": "potash_salt",
        "discovery_type": "bedded_potash",
        "production_flag": True,
        "lat": 51.79, "lon": -105.04,
    },
    {
        "source_feature_id": "SK_SMDI_5108",
        "external_id": "SMDI_5108",
        "name": "Rocanville Potash Mine",
        "status": "producer",
        "primary_commodities": ["K"],
        "associated_commodities": [],
        "commodity_grouping": "potash_salt",
        "discovery_type": "bedded_potash",
        "production_flag": True,
        "lat": 50.42, "lon": -101.65,
    },
    {
        "source_feature_id": "SK_SMDI_5219",
        "external_id": "SMDI_5219",
        "name": "Cory Potash Mine",
        "status": "producer",
        "primary_commodities": ["K"],
        "associated_commodities": [],
        "commodity_grouping": "potash_salt",
        "discovery_type": "bedded_potash",
        "production_flag": True,
        "lat": 52.05, "lon": -106.91,
    },
    {
        "source_feature_id": "SK_SMDI_6402",
        "external_id": "SMDI_6402",
        "name": "Hoidas Lake REE Prospect",
        "status": "prospect",
        "primary_commodities": ["REE"],
        "associated_commodities": [],
        "commodity_grouping": "ree",
        "discovery_type": "carbonatite_dyke",
        "production_flag": False,
        "lat": 59.55, "lon": -108.65,
    },
]


def _fetch_sk_minoccur_features() -> list[dict[str, Any]]:
    """Synthetic-stub fetcher. Real impl queries SaskGeoAtlas ArcGIS
    REST endpoint stored in public_geo.sources.service_url."""
    return list(_SYNTHETIC_SK_OCCURRENCES)


def _feature_checksum(feature: dict[str, Any]) -> str:
    canon = json.dumps(feature, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def sync_sk_mineral_occurrences(
    *, pool: asyncpg.Pool | None = None,
) -> SKMinOccurSyncResult:
    """Sync SaskGeoAtlas mineral occurrences into pg_mineral_occurrence."""
    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        features = _fetch_sk_minoccur_features()
        log.info(
            "sync_sk_minoccur.fetched features=%d (synthetic_stub)",
            len(features),
        )

        async with pool.acquire() as conn:
            source_row = await conn.fetchrow(
                """
                SELECT source_id, jurisdiction_code, source_crs
                  FROM public_geo.sources
                 WHERE source_id = 'sk_mineral_occurrence'
                """
            )
            if source_row is None:
                raise RuntimeError(
                    "sk_mineral_occurrence source not registered. "
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
                    INSERT INTO public_geo.pg_mineral_occurrence (
                        id, jurisdiction_code, source_id, source_feature_id,
                        external_id, name, status, primary_commodities,
                        associated_commodities, commodity_grouping,
                        discovery_type, production_flag,
                        reserves_resources, source_crs, source_geom_wkt,
                        source_attributes, checksum, geom
                    )
                    VALUES (
                        $1::uuid, $2, $3, $4,
                        $5, $6, $7, $8::text[],
                        $9::text[], $10,
                        $11, $12,
                        $13, $14, $15,
                        $16::jsonb, $17, ST_SetSRID(ST_GeomFromText($15), 4326)
                    )
                    ON CONFLICT (source_id, source_feature_id) DO UPDATE
                       SET name = EXCLUDED.name,
                           status = EXCLUDED.status,
                           primary_commodities = EXCLUDED.primary_commodities,
                           associated_commodities = EXCLUDED.associated_commodities,
                           commodity_grouping = EXCLUDED.commodity_grouping,
                           discovery_type = EXCLUDED.discovery_type,
                           production_flag = EXCLUDED.production_flag,
                           reserves_resources = EXCLUDED.reserves_resources,
                           source_geom_wkt = EXCLUDED.source_geom_wkt,
                           geom = EXCLUDED.geom,
                           source_attributes = EXCLUDED.source_attributes,
                           checksum = EXCLUDED.checksum,
                           last_seen_at = now(),
                           updated_at = now()
                     WHERE pg_mineral_occurrence.checksum <> EXCLUDED.checksum
                    RETURNING (xmax = 0) AS inserted_row
                    """,
                    str(uuid4()),
                    jurisdiction_code,
                    "sk_mineral_occurrence",
                    f["source_feature_id"],
                    f.get("external_id"),
                    f.get("name"),
                    f.get("status", "unknown"),
                    f.get("primary_commodities", []),
                    f.get("associated_commodities", []),
                    f.get("commodity_grouping"),
                    f.get("discovery_type"),
                    bool(f.get("production_flag", False)),
                    f.get("reserves_resources"),
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
                 WHERE source_id = 'sk_mineral_occurrence'
                """
            )

            ledger = await emit_audit(
                conn,
                action_type="public_geo.pull.complete",
                actor_kind="workflow",
                target_schema="public_geo",
                target_table="pg_mineral_occurrence",
                target_id="sk_mineral_occurrence",
                payload={
                    "evaluator": "synthetic_stub",
                    "doc_phase": 151,
                    "source_id": "sk_mineral_occurrence",
                    "jurisdiction_code": jurisdiction_code,
                    "fetched_features": len(features),
                    "inserted": inserted,
                    "updated": updated,
                    "skipped_no_geom": skipped_no_geom,
                },
            )

        log.info(
            "sync_sk_minoccur.completed fetched=%d inserted=%d updated=%d",
            len(features), inserted, updated,
        )

        return SKMinOccurSyncResult(
            source_id="sk_mineral_occurrence",
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
    "SKMinOccurSyncResult",
    "sync_sk_mineral_occurrences",
]
