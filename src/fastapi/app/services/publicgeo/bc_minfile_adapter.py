"""BC MINFILE adapter (§6.2) — doc-phase 149.

Pulls mineral-occurrence rows from BC's MINFILE database and lands
them in `public_geo.pg_mineral_occurrence` under
`source_id='bc_minfile_mineral_occurrence'`.

What's live in this graduation:

  - `sync_bc_minfile_mineral_occurrences()` — full sync orchestration:
      1. Resolve `bc_minfile_mineral_occurrence` source row from
         public_geo.sources (seeded in doc-phase 135)
      2. Fetch occurrence features via `_fetch_bc_minfile_features()`
         (currently synthetic stub — 15 realistic seed occurrences)
      3. Idempotent UPSERT into pg_mineral_occurrence keyed on
         (source_id, source_feature_id)
      4. Update sources.last_refreshed_at
      5. Emit `public_geo.pull.complete` audit anchor

  - `_fetch_bc_minfile_features()` — synthetic stub returning 15
    realistic BC mineral occurrences (real BC place names, real
    MINFILE-style metadata structure). Replace with httpx call to the
    BC MINFILE ArcGIS REST endpoint when ready.

The 15 synthetic occurrences are scattered across BC and cover
multiple commodity groupings (precious_metals, base_metals, uranium,
lithium) so the §6 admin map + the §6.5 commodity-aliases service
have something visible to render.
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

log = logging.getLogger("georag.publicgeo.bc_minfile_adapter")


class BCMinfileSyncResult(NamedTuple):
    """Result of one sync run."""

    source_id: str
    inserted: int
    updated: int
    skipped_no_geom: int
    total_features: int
    audit_ledger_entry_id: UUID
    sync_method: str  # 'synthetic_stub' | 'arcgis_rest' | ...


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


# Synthetic seed data — 15 BC mineral occurrences across the province.
# Coordinates are real BC locations; commodities + MINFILE-style metadata
# are realistic and align with the BC MINFILE schema. Real ArcGIS REST
# fetcher replaces this list without changing the upsert orchestration.
_SYNTHETIC_BC_OCCURRENCES: list[dict[str, Any]] = [
    {
        "source_feature_id": "BC_MINFILE_092I-001",
        "external_id": "MINFILE_092I-001",
        "name": "Highland Valley Copper",
        "status": "producer",
        "primary_commodities": ["Cu", "Mo"],
        "associated_commodities": ["Au", "Ag"],
        "commodity_grouping": "base_metals",
        "discovery_type": "porphyry_copper",
        "production_flag": True,
        "lat": 50.49,
        "lon": -121.02,
        "reserves_resources": "Large-tonnage porphyry; multi-decade life",
    },
    {
        "source_feature_id": "BC_MINFILE_104B-001",
        "external_id": "MINFILE_104B-001",
        "name": "Eskay Creek",
        "status": "past-producer",
        "primary_commodities": ["Au", "Ag"],
        "associated_commodities": ["Cu", "Zn", "Pb"],
        "commodity_grouping": "precious_metals",
        "discovery_type": "epithermal_gold",
        "production_flag": True,
        "lat": 56.6,
        "lon": -130.45,
        "reserves_resources": "Past producer; restart in evaluation",
    },
    {
        "source_feature_id": "BC_MINFILE_093O-001",
        "external_id": "MINFILE_093O-001",
        "name": "Mount Milligan",
        "status": "producer",
        "primary_commodities": ["Cu", "Au"],
        "associated_commodities": [],
        "commodity_grouping": "base_metals",
        "discovery_type": "porphyry_copper",
        "production_flag": True,
        "lat": 55.13,
        "lon": -124.07,
    },
    {
        "source_feature_id": "BC_MINFILE_104K-001",
        "external_id": "MINFILE_104K-001",
        "name": "Brucejack",
        "status": "producer",
        "primary_commodities": ["Au"],
        "associated_commodities": ["Ag"],
        "commodity_grouping": "precious_metals",
        "discovery_type": "epithermal_gold_high_sulfidation",
        "production_flag": True,
        "lat": 56.47,
        "lon": -130.21,
    },
    {
        "source_feature_id": "BC_MINFILE_093K-001",
        "external_id": "MINFILE_093K-001",
        "name": "Endako",
        "status": "past-producer",
        "primary_commodities": ["Mo"],
        "associated_commodities": [],
        "commodity_grouping": "base_metals",
        "discovery_type": "porphyry_molybdenum",
        "production_flag": True,
        "lat": 54.06,
        "lon": -125.10,
    },
    {
        "source_feature_id": "BC_MINFILE_082F-001",
        "external_id": "MINFILE_082F-001",
        "name": "Sullivan",
        "status": "past-producer",
        "primary_commodities": ["Pb", "Zn", "Ag"],
        "associated_commodities": ["Sn", "Cu"],
        "commodity_grouping": "base_metals",
        "discovery_type": "sedex",
        "production_flag": True,
        "lat": 49.69,
        "lon": -115.95,
        "reserves_resources": "Closed 2001; world-class SEDEX",
    },
    {
        "source_feature_id": "BC_MINFILE_093N-001",
        "external_id": "MINFILE_093N-001",
        "name": "Kemess South",
        "status": "past-producer",
        "primary_commodities": ["Cu", "Au"],
        "associated_commodities": [],
        "commodity_grouping": "base_metals",
        "discovery_type": "porphyry_copper",
        "production_flag": True,
        "lat": 57.00,
        "lon": -126.75,
    },
    {
        "source_feature_id": "BC_MINFILE_103I-001",
        "external_id": "MINFILE_103I-001",
        "name": "Premier",
        "status": "past-producer",
        "primary_commodities": ["Au", "Ag"],
        "associated_commodities": [],
        "commodity_grouping": "precious_metals",
        "discovery_type": "epithermal_gold",
        "production_flag": True,
        "lat": 55.91,
        "lon": -130.00,
    },
    {
        "source_feature_id": "BC_MINFILE_092M-001",
        "external_id": "MINFILE_092M-001",
        "name": "Britannia",
        "status": "past-producer",
        "primary_commodities": ["Cu", "Zn"],
        "associated_commodities": ["Au", "Ag", "Pb"],
        "commodity_grouping": "base_metals",
        "discovery_type": "vms",
        "production_flag": True,
        "lat": 49.65,
        "lon": -123.13,
        "reserves_resources": "Closed 1974; VMS deposit, now under environmental remediation",
    },
    {
        "source_feature_id": "BC_MINFILE_092L-001",
        "external_id": "MINFILE_092L-001",
        "name": "Wedge Lithium Pegmatite",
        "status": "prospect",
        "primary_commodities": ["Li"],
        "associated_commodities": ["Ta", "Cs"],
        "commodity_grouping": "lithium",
        "discovery_type": "lithium_pegmatite",
        "production_flag": False,
        "lat": 50.20,
        "lon": -126.50,
    },
    {
        "source_feature_id": "BC_MINFILE_103P-001",
        "external_id": "MINFILE_103P-001",
        "name": "Iskut River U Prospect",
        "status": "occurrence",
        "primary_commodities": ["U"],
        "associated_commodities": ["REE"],
        "commodity_grouping": "uranium",
        "discovery_type": "vein_uranium",
        "production_flag": False,
        "lat": 56.85,
        "lon": -130.95,
    },
    {
        "source_feature_id": "BC_MINFILE_082K-001",
        "external_id": "MINFILE_082K-001",
        "name": "Rossland Camp",
        "status": "past-producer",
        "primary_commodities": ["Au"],
        "associated_commodities": ["Ag", "Cu"],
        "commodity_grouping": "precious_metals",
        "discovery_type": "vein_gold",
        "production_flag": True,
        "lat": 49.08,
        "lon": -117.80,
    },
    {
        "source_feature_id": "BC_MINFILE_104I-001",
        "external_id": "MINFILE_104I-001",
        "name": "Tulsequah Chief",
        "status": "prospect",
        "primary_commodities": ["Cu", "Zn", "Au", "Ag"],
        "associated_commodities": [],
        "commodity_grouping": "base_metals",
        "discovery_type": "vms",
        "production_flag": False,
        "lat": 58.74,
        "lon": -133.59,
    },
    {
        "source_feature_id": "BC_MINFILE_082E-001",
        "external_id": "MINFILE_082E-001",
        "name": "Phoenix Camp",
        "status": "past-producer",
        "primary_commodities": ["Cu"],
        "associated_commodities": ["Au", "Ag"],
        "commodity_grouping": "base_metals",
        "discovery_type": "skarn",
        "production_flag": True,
        "lat": 49.05,
        "lon": -118.62,
    },
    {
        "source_feature_id": "BC_MINFILE_092H-001",
        "external_id": "MINFILE_092H-001",
        "name": "Boss Mountain Mo",
        "status": "past-producer",
        "primary_commodities": ["Mo"],
        "associated_commodities": [],
        "commodity_grouping": "base_metals",
        "discovery_type": "porphyry_molybdenum",
        "production_flag": True,
        "lat": 52.10,
        "lon": -120.85,
    },
]


def _fetch_bc_minfile_features() -> list[dict[str, Any]]:
    """Synthetic-stub fetcher. Returns 15 realistic BC mineral
    occurrences. Real implementation:

        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://maps.gov.bc.ca/arcgis/rest/services/mpcm/"
                "MINFILE_PUB/MapServer/0/query",
                params={
                    "f": "geojson",
                    "where": "1=1",
                    "outFields": "*",
                    "outSR": "4326",
                },
            )
        return r.json()["features"]

    The replacement keeps the same `dict[str, Any]` shape used by
    `_feature_checksum()` + the upsert call below.
    """
    return list(_SYNTHETIC_BC_OCCURRENCES)


def _feature_checksum(feature: dict[str, Any]) -> str:
    """SHA-256 over canonical-JSON of the feature's stable fields.
    Used by the upsert to detect "no change" vs "needs update"."""
    canon = json.dumps(feature, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def sync_bc_minfile_mineral_occurrences(
    *,
    pool: asyncpg.Pool | None = None,
) -> BCMinfileSyncResult:
    """Sync BC MINFILE mineral occurrences into pg_mineral_occurrence.

    Idempotent on (source_id, source_feature_id) — re-runs with
    unchanged checksums skip the update; changed checksums update
    the row + bump last_seen_at.

    Returns:
        BCMinfileSyncResult with insert/update/skip counts + the
        audit ledger anchor id.
    """
    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        # Fetch features (synthetic stub today).
        features = _fetch_bc_minfile_features()
        log.info(
            "sync_bc_minfile.fetched features=%d (synthetic_stub)",
            len(features),
        )

        async with pool.acquire() as conn:
            # Resolve source row.
            source_row = await conn.fetchrow(
                """
                SELECT source_id, jurisdiction_code, source_crs, service_url
                  FROM public_geo.sources
                 WHERE source_id = 'bc_minfile_mineral_occurrence'
                """
            )
            if source_row is None:
                raise RuntimeError(
                    "bc_minfile_mineral_occurrence source not registered. "
                    "Run the doc-phase 135 migration first."
                )

            jurisdiction_code = source_row["jurisdiction_code"]  # 'CA-BC'
            source_crs = source_row["source_crs"]  # 3005

            inserted = 0
            updated = 0
            skipped_no_geom = 0

            for f in features:
                if f.get("lat") is None or f.get("lon") is None:
                    skipped_no_geom += 1
                    continue

                checksum = _feature_checksum(f)
                wkt = f"POINT({f['lon']} {f['lat']})"

                # Idempotent UPSERT keyed on (source_id, source_feature_id).
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
                    "bc_minfile_mineral_occurrence",
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
                    # Checksum matched existing row — no update.
                    pass
                elif row["inserted_row"]:
                    inserted += 1
                else:
                    updated += 1

            # Bump source's last_refreshed_at.
            await conn.execute(
                """
                UPDATE public_geo.sources
                   SET last_refreshed_at = now()
                 WHERE source_id = 'bc_minfile_mineral_occurrence'
                """
            )

            # Audit anchor.
            ledger = await emit_audit(
                conn,
                action_type="public_geo.pull.complete",
                actor_kind="workflow",
                target_schema="public_geo",
                target_table="pg_mineral_occurrence",
                target_id="bc_minfile_mineral_occurrence",
                payload={
                    "evaluator": "synthetic_stub",
                    "doc_phase": 149,
                    "source_id": "bc_minfile_mineral_occurrence",
                    "jurisdiction_code": jurisdiction_code,
                    "fetched_features": len(features),
                    "inserted": inserted,
                    "updated": updated,
                    "skipped_no_geom": skipped_no_geom,
                },
            )

        log.info(
            "sync_bc_minfile.completed fetched=%d inserted=%d updated=%d "
            "skipped_no_geom=%d",
            len(features), inserted, updated, skipped_no_geom,
        )

        return BCMinfileSyncResult(
            source_id="bc_minfile_mineral_occurrence",
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
    "BCMinfileSyncResult",
    "sync_bc_minfile_mineral_occurrences",
]
