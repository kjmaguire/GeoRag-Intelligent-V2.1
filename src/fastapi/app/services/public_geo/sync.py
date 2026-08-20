"""Sync public-geoscience feeds from the live ArcGIS services into public_geo.*.

Public geoscience is kept as a synced mirror of what provincial and federal
surveys publish: we do not author any of it, and it is refreshed from the
upstream APIs rather than hand-loaded. The canonical tables were already
shaped for exactly this — ``(source_id, source_feature_id)`` is the natural
key, ``checksum`` drives change detection, and ``first_seen_at`` /
``last_seen_at`` are sync bookkeeping. What was missing was something to run
it: the Dagster pipeline that used to has been dormant since 2026-07-28, which
is why the data went three weeks stale and never reached Azure at all.

Design notes
------------
**Every type writes a common core; type-specific columns are mapped per type.**
The seven canonical tables do NOT share a schema (pg_drillhole_collar carries
azimuth/inclination, pg_mineral_occurrence splits primary vs associated
commodities, and so on), so a single generic INSERT cannot serve them. Each
type gets a mapper that turns one GeoJSON feature into a column dict.

**Raw attributes are always preserved.** Whatever the mapper does not lift into
a typed column is stored verbatim in ``source_attributes`` jsonb. A mapping gap
therefore loses nothing — the data is present and a later mapper improvement
can backfill from it without re-fetching.

**Upsert, never truncate-and-load.** A survey being briefly unreachable, or a
partial page, must not delete rows. ``last_seen_at`` records what this run
observed; rows the upstream has genuinely dropped are identifiable as stale by
that column rather than by their absence.

**checksum is over the raw attributes**, so an unchanged feature does not churn
``updated_at`` and re-trigger downstream work.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from app.services.public_geo import arcgis
from app.services.public_geo.registry import PublicGeoSource, sources_for

logger = logging.getLogger(__name__)

# canonical_type -> destination table
TABLE_FOR_TYPE: dict[str, str] = {
    "mine": "public_geo.pg_mine",
    "mineral_occurrence": "public_geo.pg_mineral_occurrence",
    "drillhole_collar": "public_geo.pg_drillhole_collar",
    "resource_potential_zone": "public_geo.pg_resource_potential_zone",
    "rock_sample": "public_geo.pg_rock_sample",
    "assessment_survey": "public_geo.pg_assessment_survey",
    "mineral_disposition": "public_geo.pg_mineral_disposition",
}


def _checksum(props: dict[str, Any]) -> str:
    """Stable hash of the upstream attributes.

    Sorted keys so ArcGIS field ordering cannot produce a spurious change.
    """
    blob = json.dumps(props, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


def _geom_wkt(feature: dict[str, Any]) -> str | None:
    """GeoJSON geometry -> WKT, for the source_geom_wkt column.

    Points cover the overwhelming majority of these feeds. Anything else is
    handed to PostGIS as GeoJSON instead (see _geom_sql), so this returning
    None is not a loss.
    """
    geom = feature.get("geometry") or {}
    if geom.get("type") == "Point":
        coords = geom.get("coordinates") or []
        if len(coords) >= 2:
            return f"POINT({float(coords[0])} {float(coords[1])})"
    return None


@dataclass
class AliasTables:
    """Upstream label -> canonical code, loaded once per sync run.

    These lookups are NOT optional decoration: pg_mine.status and
    pg_mine.commodity_grouping both carry CHECK constraints over canonical
    snake_case codes, while the surveys publish human labels. Writing the raw
    value fails the constraint outright:

        new row violates check constraint "pg_mine_commodity_grouping_check"
        ... commodity_grouping = 'Uranium'

    public_geo.commodity_aliases (77 rows) and public_geo.status_aliases
    (39 rows) already carry the mappings, so normalisation is a lookup rather
    than a hand-written table in this file.
    """

    grouping_by_alias: dict[str, str]
    status_by_source_value: dict[tuple[str | None, str | None, str], str]

    @classmethod
    async def load(cls, conn: Any) -> AliasTables:
        grouping: dict[str, str] = {}
        for r in await conn.fetch(
            "SELECT alias_lower, commodity_grouping FROM public_geo.commodity_aliases "
            "WHERE commodity_grouping IS NOT NULL"
        ):
            grouping[r["alias_lower"]] = r["commodity_grouping"]

        # Scoped by (jurisdiction, canonical_type). status_aliases carries both
        # columns for a reason: "producing" means different things per survey,
        # and a flat lookup lets a BC mineral_occurrence mapping silently apply
        # to an SK mine.
        status: dict[tuple[str | None, str | None, str], str] = {}
        for r in await conn.fetch(
            "SELECT jurisdiction_code, canonical_type, source_value_lower, "
            "canonical_status FROM public_geo.status_aliases"
        ):
            status[
                (r["jurisdiction_code"], r["canonical_type"], r["source_value_lower"])
            ] = r["canonical_status"]

        return cls(grouping_by_alias=grouping, status_by_source_value=status)

    def grouping(self, raw: str | None) -> str | None:
        """Canonical commodity grouping, or None when unmapped.

        None is always constraint-legal (the column is nullable) and is
        deliberately preferred over guessing 'other': an unmapped label is a
        gap in commodity_aliases worth seeing, not a value to invent.
        """
        if not raw:
            return None
        return self.grouping_by_alias.get(raw.strip().lower())

    def status(
        self, raw: str | None, *, jurisdiction: str, canonical_type: str
    ) -> tuple[str, str | None]:
        """Resolve to (canonical_status, unmapped_raw_value).

        Returns the raw value as a second element when nothing matched, so the
        caller can COUNT what it could not translate. status is NOT NULL and
        'unknown' is a legal member of the CHECK, so an unmapped value still
        writes — but silently defaulting 140 of 140 mines to 'unknown' while
        reporting a clean sync is exactly the kind of green-but-wrong result
        this codebase keeps getting bitten by.

        Tries the specific scope first, then progressively wider fallbacks, so
        a survey-specific mapping wins over a generic one.
        """
        if not raw:
            return "unknown", None

        key = raw.strip().lower()
        for scope in (
            (jurisdiction, canonical_type, key),
            (jurisdiction, None, key),
            (None, canonical_type, key),
            (None, None, key),
        ):
            hit = self.status_by_source_value.get(scope)
            if hit:
                return hit, None

        return "unknown", raw.strip()


def _map_mine(
    src: PublicGeoSource, feature: dict[str, Any], aliases: AliasTables
) -> dict[str, Any] | None:
    """Map one feature into public_geo.pg_mine.

    status and commodities are NOT NULL on this table, so both resolve to a
    non-null value rather than dropping the row — a mine the survey publishes
    without a status is still a mine.
    """
    props = feature.get("properties") or {}
    oid = arcgis.object_id_of(feature)
    if not oid:
        return None

    commodities_raw = arcgis.first_present(props, ["COMMODITY", "COMMODITIES"]) or ""
    commodities = [c.strip() for c in commodities_raw.replace(";", ",").split(",") if c.strip()]

    return {
        "jurisdiction_code": src.jurisdiction_code,
        "source_id": src.source_id,
        "source_feature_id": str(oid),
        "name": arcgis.first_present(props, ["NAME", "PROPERTY", "MINE_NAME"]),
        "_status_raw": arcgis.first_present(props, ["STATUS", "DEP_CLASS"]),
        "commodities": commodities,
        "commodity_grouping": aliases.grouping(
            arcgis.first_present(props, ["COMMODITY_GROUPING"])
        ),
        "operator": arcgis.first_present(props, ["OPERATOR", "COMPANY", "OWNER"]),
        "source_crs": src.source_crs or 4326,
        "source_geom_wkt": _geom_wkt(feature),
        "source_url": src.service_url,
        "source_attributes": json.dumps(props, default=str, ensure_ascii=False),
        "checksum": _checksum(props),
    }


# canonical_type -> mapper. Types without a mapper are skipped loudly rather
# than half-written; see sync_source().
MAPPERS: dict[
    str, Callable[[PublicGeoSource, dict[str, Any], "AliasTables"], dict[str, Any] | None]
] = {
    "mine": _map_mine,
}


_UPSERT_SQL = """
INSERT INTO {table} (
    id, jurisdiction_code, source_id, source_feature_id, name, status,
    commodities, commodity_grouping, operator, source_crs, source_geom_wkt,
    source_url, source_attributes, first_seen_at, last_seen_at, checksum,
    created_at, updated_at, geom
) VALUES (
    gen_random_uuid(), $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10::text,
    $11, $12::jsonb, now(), now(), $13,
    now(), now(),
    -- $10 is explicitly ::text at BOTH use sites. Without the cast asyncpg
    -- cannot infer a type for a parameter that appears once as a plain value
    -- and once inside ST_GeomFromText(), and every row fails with
    -- "could not determine data type of parameter $10".
    CASE WHEN $10::text IS NULL THEN NULL
         ELSE ST_SetSRID(ST_GeomFromText($10::text), 4326) END
)
ON CONFLICT (source_id, source_feature_id) DO UPDATE SET
    last_seen_at = now(),
    -- Only churn updated_at when the upstream attributes actually changed;
    -- an unchanged feature must not re-trigger downstream work.
    updated_at = CASE WHEN {table}.checksum IS DISTINCT FROM EXCLUDED.checksum
                      THEN now() ELSE {table}.updated_at END,
    name = EXCLUDED.name,
    status = EXCLUDED.status,
    commodities = EXCLUDED.commodities,
    commodity_grouping = EXCLUDED.commodity_grouping,
    operator = EXCLUDED.operator,
    source_geom_wkt = EXCLUDED.source_geom_wkt,
    source_url = EXCLUDED.source_url,
    source_attributes = EXCLUDED.source_attributes,
    checksum = EXCLUDED.checksum,
    geom = EXCLUDED.geom
"""


async def sync_source(
    conn: Any,
    src: PublicGeoSource,
    *,
    aliases: AliasTables | None = None,
    max_features: int | None = None,
    page_size: int = 1000,
) -> dict[str, Any]:
    """Pull one feed and upsert it. Returns per-source stats."""
    stats = {
        "source_id": src.source_id,
        "canonical_type": src.canonical_type,
        "fetched": 0,
        "upserted": 0,
        "unmapped": 0,
        "errors": 0,
    }

    if aliases is None:
        aliases = await AliasTables.load(conn)

    mapper = MAPPERS.get(src.canonical_type)
    if mapper is None:
        # Loud skip. Writing a partial row into a typed table would look like
        # a successful sync while silently losing the type-specific columns.
        stats["skipped_reason"] = f"no mapper for canonical_type={src.canonical_type}"
        logger.warning("public_geo.sync: %s", stats["skipped_reason"])
        return stats

    table = TABLE_FOR_TYPE[src.canonical_type]
    sql = _UPSERT_SQL.format(table=table)
    unmapped_statuses: dict[str, int] = {}

    async for feature in arcgis.iter_all_features(
        src, page_size=page_size, max_features=max_features
    ):
        stats["fetched"] += 1
        row = mapper(src, feature, aliases)
        if row is None:
            stats["unmapped"] += 1
            continue

        # Status is resolved here rather than in the mapper so the run can
        # COUNT what it failed to translate. See AliasTables.status().
        status, unmapped_status = aliases.status(
            row.pop("_status_raw", None),
            jurisdiction=src.jurisdiction_code,
            canonical_type=src.canonical_type,
        )
        row["status"] = status
        if unmapped_status:
            unmapped_statuses[unmapped_status] = (
                unmapped_statuses.get(unmapped_status, 0) + 1
            )
        try:
            await conn.execute(
                sql,
                row["jurisdiction_code"], row["source_id"], row["source_feature_id"],
                row["name"], row["status"], row["commodities"],
                row["commodity_grouping"], row["operator"], row["source_crs"],
                row["source_geom_wkt"], row["source_url"], row["source_attributes"],
                row["checksum"],
            )
            stats["upserted"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad feature must not end the feed
            stats["errors"] += 1
            if stats["errors"] <= 3:
                logger.warning(
                    "public_geo.sync: %s feature %s failed: %s",
                    src.source_id, row.get("source_feature_id"), exc,
                )

    if unmapped_statuses:
        # Surfaced, not swallowed: these rows wrote as 'unknown', and each one
        # is a missing row in public_geo.status_aliases rather than a property
        # of the data.
        stats["unmapped_statuses"] = dict(
            sorted(unmapped_statuses.items(), key=lambda kv: -kv[1])[:10]
        )
        logger.warning(
            "public_geo.sync: %s had %d status value(s) with no alias mapping: %s",
            src.source_id, len(unmapped_statuses), stats["unmapped_statuses"],
        )

    logger.info("public_geo.sync: %s", stats)
    return stats


async def sync_all(
    conn: Any,
    *,
    canonical_types: list[str] | None = None,
    jurisdiction_codes: list[str] | None = None,
    max_features_per_source: int | None = None,
) -> dict[str, Any]:
    """Sync every queryable feed matching the filters."""
    feeds = sources_for(
        canonical_types=canonical_types, jurisdiction_codes=jurisdiction_codes
    )
    # Loaded once and threaded through: 116 alias rows re-read per feed would
    # be pure waste, and a mid-run change to the mapping would make one sync
    # internally inconsistent.
    aliases = await AliasTables.load(conn)

    per_source = []
    for src in feeds:
        per_source.append(
            await sync_source(
                conn, src, aliases=aliases, max_features=max_features_per_source
            )
        )

    return {
        "started_at": datetime.now(UTC).isoformat(),
        "feeds": len(feeds),
        "fetched": sum(s["fetched"] for s in per_source),
        "upserted": sum(s["upserted"] for s in per_source),
        "errors": sum(s["errors"] for s in per_source),
        "skipped": [s["source_id"] for s in per_source if "skipped_reason" in s],
        "per_source": per_source,
    }
