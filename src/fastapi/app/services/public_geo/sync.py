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
commodities, pg_mineral_disposition carries tenure holders and expiry dates),
so a single generic INSERT cannot serve them. Each type gets a ``TableSpec``
naming its columns and a mapper that turns one GeoJSON feature into a column
dict; the INSERT is generated from the spec.

**Field names come from the live services, not from a guess.** Every candidate
list below was read off the layer's own ``?f=json`` metadata. They are still
written as candidate *lists* because two feeds of the same canonical type
routinely disagree (CA-SK-SMDI publishes ``PRIMARYCOMMODITIES``; CA-BC-MINFILE
spreads ``COMMODITY_DESCRIPTION1``…``8``), and because a survey renaming a
column should degrade one field rather than fail the feed.

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

**Everything is stored in WGS84.** We ask the service for ``outSR=4326``, so
``source_crs`` is written as 4326 rather than the layer's native CRS (2957 for
most of Saskatchewan, 3005 for BC). Writing the native code beside a 4326
geometry — which the stored pipeline did — is worse than useless: it says the
coordinates are in a projection they are not in.

**source_geom_wkt is populated for point feeds only.** It is a debugging
convenience that duplicates ``geom``; on the polygon feeds (dispositions,
assessment surveys, potential zones) a single ring set can run to hundreds of
kilobytes and the text copy would roughly double the table for no gain.
``geom`` is authoritative for every type.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.services.public_geo import arcgis
from app.services.public_geo.registry import PublicGeoSource, sources_for

logger = logging.getLogger(__name__)

# Columns every canonical table carries, in the order they are bound.
_COMMON_COLUMNS: tuple[str, ...] = (
    "jurisdiction_code",
    "source_id",
    "source_feature_id",
    "source_crs",
    "source_url",
    "source_attributes",
    "checksum",
)

# Natural key — never overwritten by ON CONFLICT DO UPDATE.
_KEY_COLUMNS = frozenset({"source_id", "source_feature_id"})


@dataclass(frozen=True)
class TableSpec:
    """Destination shape for one canonical type."""

    table: str
    #: 'POINT' or 'MULTIPOLYGON' — decides the geom expression and whether
    #: source_geom_wkt is materialised (see module docstring).
    geom_kind: str
    #: Type-specific columns, in bind order. The common core is appended.
    columns: tuple[str, ...]
    #: Bulk page size. Polygon feeds page smaller: 1000 disposition parcels in
    #: one response is tens of megabytes and routinely trips the read timeout.
    page_size: int = 1000


SPECS: dict[str, TableSpec] = {
    "mine": TableSpec(
        table="public_geo.pg_mine",
        geom_kind="POINT",
        columns=("name", "status", "commodities", "commodity_grouping", "operator"),
    ),
    "mineral_occurrence": TableSpec(
        table="public_geo.pg_mineral_occurrence",
        geom_kind="POINT",
        columns=(
            "external_id", "name", "historic_names", "status", "primary_commodities",
            "associated_commodities", "commodity_grouping", "discovery_type",
            "production_flag", "reserves_resources",
        ),
    ),
    "drillhole_collar": TableSpec(
        table="public_geo.pg_drillhole_collar",
        geom_kind="POINT",
        columns=(
            "drillhole_id", "drillhole_name", "company", "project_name",
            "date_drilled", "drill_type", "commodity_of_interest", "total_length_m",
            "inclination_deg", "azimuth_deg", "collar_elevation_m",
            "stratigraphic_depths", "core_availability", "core_storage",
            "disposition",
        ),
    ),
    "resource_potential_zone": TableSpec(
        table="public_geo.pg_resource_potential_zone",
        geom_kind="MULTIPOLYGON",
        columns=("commodity", "commodity_grouping", "potential_rank", "methodology_ref"),
        page_size=250,
    ),
    "rock_sample": TableSpec(
        table="public_geo.pg_rock_sample",
        geom_kind="POINT",
        columns=(
            "station", "sample_number", "geologist", "geographic_area",
            "report_number", "map_number", "map_scale", "nts_250k", "nts_50k",
            "date_collected",
        ),
    ),
    "assessment_survey": TableSpec(
        table="public_geo.pg_assessment_survey",
        geom_kind="MULTIPOLYGON",
        columns=("survey_type",),
        page_size=250,
    ),
    "mineral_disposition": TableSpec(
        table="public_geo.pg_mineral_disposition",
        geom_kind="MULTIPOLYGON",
        columns=(
            "disposition_number", "disposition_type", "status", "holder_name",
            "issue_date", "expiry_date", "area_ha", "commodity_codes",
            "geographic_area",
        ),
        page_size=250,
    ),
}

# Back-compat alias — callers referred to this before SPECS existed.
TABLE_FOR_TYPE: dict[str, str] = {t: s.table for t, s in SPECS.items()}


# ── value coercion ────────────────────────────────────────────────────────
# asyncpg infers each parameter's type from the target column, which means the
# Python value has to match: Decimal for numeric, datetime.date for date, list
# for text[], str for jsonb. Passing a float into a numeric column raises
# "invalid input for query argument ... expected Decimal", so every numeric
# goes through _dec().


def _checksum(props: dict[str, Any]) -> str:
    """Stable hash of the upstream attributes.

    Sorted keys so ArcGIS field ordering cannot produce a spurious change.
    """
    blob = json.dumps(props, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


def _dec(raw: Any) -> Decimal | None:
    """Numeric column value, or None when the field is absent/unparseable."""
    if raw in (None, "", " "):
        return None
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None


def _int(raw: Any, *, lo: int | None = None, hi: int | None = None) -> int | None:
    """Integer column value, optionally range-gated to the column's CHECK."""
    if raw in (None, "", " "):
        return None
    try:
        val = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    if lo is not None and val < lo:
        return None
    if hi is not None and val > hi:
        return None
    return val


def _as_date(raw: Any) -> date | None:
    """Parse an ArcGIS date field.

    ArcGIS emits ``esriFieldTypeDate`` as epoch **milliseconds** in JSON, but
    some services (and some ``f=geojson`` conversions) emit an ISO-8601 string
    instead. Both shapes appear across these feeds, so both are handled;
    anything else yields None rather than a wrong date.
    """
    if raw in (None, "", " "):
        return None

    is_numeric = isinstance(raw, (int, float)) or (
        isinstance(raw, str) and raw.strip().lstrip("-").isdigit()
    )
    if is_numeric:
        try:
            ms = float(raw)
        except (TypeError, ValueError):
            return None

        # Reject anything too small to be a real epoch-ms timestamp.
        #
        # A value that is actually epoch SECONDS (1577836800 = 2020-01-01), or
        # a bare year (2019), or a zero standing in for null, all divide down
        # into January 1970 and would be stored as a confident, wrong date.
        # 1e11 ms is 1973-03-03, so this also discards genuine 1970-1972 dates
        # — an accepted loss, because at that magnitude a real 1971 timestamp
        # and a seconds-encoded modern one are genuinely indistinguishable,
        # and these services emit milliseconds per the ArcGIS spec anyway.
        if abs(ms) < 1e11:
            return None

        try:
            parsed = datetime.fromtimestamp(ms / 1000.0, tz=UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
        return parsed if 1800 <= parsed.year <= 2200 else None

    text = str(raw).strip().replace("Z", "+00:00")
    for candidate in (text, text[:10]):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
    return None


def _split_list(raw: Any) -> list[str]:
    """Split a delimited commodity/name string into a clean list.

    Surveys use commas, semicolons and pipes interchangeably within a single
    layer, so all three are treated as separators.
    """
    if raw in (None, "", " "):
        return []
    text = str(raw).replace(";", ",").replace("|", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _numbered(props: dict[str, Any], stem: str, count: int) -> list[str]:
    """Collect ``STEM1``…``STEMn`` into a list, skipping blanks.

    CA-BC-MINFILE spreads commodities across eight numbered columns rather
    than one delimited string.
    """
    out: list[str] = []
    for i in range(1, count + 1):
        val = arcgis.first_present(props, [f"{stem}{i}"])
        if val and val not in out:
            out.append(val)
    return out


def _truthy(raw: Any) -> bool:
    """Interpret the assorted boolean spellings these feeds use."""
    if raw in (None, "", " "):
        return False
    return str(raw).strip().lower() in {"y", "yes", "t", "true", "1", "produced"}


def _geom_geojson(feature: dict[str, Any]) -> str | None:
    """The feature's geometry as a GeoJSON string, for ST_GeomFromGeoJSON.

    Handing PostGIS the GeoJSON directly (rather than WKT built here) means
    polygons, multipolygons and rings-with-holes all round-trip without this
    module reimplementing WKT serialisation.
    """
    geom = feature.get("geometry")
    if not geom or not geom.get("type"):
        return None
    return json.dumps(geom, default=str)


@dataclass
class AliasTables:
    """Upstream label -> canonical code, loaded once per sync run.

    These lookups are NOT optional decoration: pg_mine.status and
    pg_mine.commodity_grouping both carry CHECK constraints over canonical
    codes, while the surveys publish human labels. Writing the raw value fails
    the constraint outright:

        new row violates check constraint "pg_mine_commodity_grouping_check"
        ... commodity_grouping = 'Uranium'

    public_geo.commodity_aliases and public_geo.status_aliases already carry
    the mappings, so normalisation is a lookup rather than a hand-written
    table in this file.
    """

    grouping_by_alias: dict[str, str]
    status_by_source_value: dict[tuple[str, str, str], str]

    @classmethod
    async def load(cls, conn: Any) -> AliasTables:
        grouping: dict[str, str] = {}
        for r in await conn.fetch(
            "SELECT alias_lower, commodity_grouping FROM public_geo.commodity_aliases "
            "WHERE commodity_grouping IS NOT NULL"
        ):
            grouping[r["alias_lower"]] = r["commodity_grouping"]

        # Scoped by (jurisdiction, canonical_type). status_aliases carries both
        # columns NOT NULL for a reason: "Producer" means different things per
        # survey, and a flat lookup would let a BC mineral_occurrence mapping
        # silently apply to an SK mine.
        status: dict[tuple[str, str, str], str] = {}
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

    def grouping_of_any(self, values: list[str]) -> str | None:
        """First resolvable grouping across several commodity labels.

        Occurrence feeds list several commodities and no grouping column; the
        leading commodity is the one the survey considers primary, so scanning
        in order gives the grouping a geologist would assign.
        """
        for v in values:
            hit = self.grouping(v)
            if hit:
                return hit
        return None

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
        """
        if not raw:
            return "unknown", None
        hit = self.status_by_source_value.get(
            (jurisdiction, canonical_type, raw.strip().lower())
        )
        if hit:
            return hit, None
        return "unknown", raw.strip()


# ── per-source constants ──────────────────────────────────────────────────
# Two canonical types carry a discriminator that the layer's own attributes do
# not express, because the survey encodes it in the LAYER rather than in a
# column: P_Mineral_Assessment_File_Information/1|2|3 are underground, ground
# and airborne surveys respectively, and Mining/MapServer/0..8 each publish a
# single (disposition_type, status) combination. Both are properties of the
# feed, so they are declared per source_id rather than sniffed per feature.

SURVEY_TYPE_BY_SOURCE: dict[str, str] = {
    "CA-SK-ASSESSMENT-AIRBORNE": "airborne",
    "CA-SK-ASSESSMENT-GROUND": "ground",
    "CA-SK-ASSESSMENT-UNDERGROUND": "underground",
}

# (disposition_type, status) — the mapping documented in the 2026-04-15
# pg_mineral_disposition migration.
DISPOSITION_BY_SOURCE: dict[str, tuple[str, str]] = {
    "CA-SK-MINERAL-DISPOSITION-MINING-0": ("mineral", "active"),
    "CA-SK-MINERAL-DISPOSITION-MINING-1": ("mineral", "legacy"),
    "CA-SK-MINERAL-DISPOSITION-MINING-2": ("mineral", "pending"),
    "CA-SK-MINERAL-DISPOSITION-MINING-3": ("mineral", "reopening"),
    "CA-SK-MINERAL-DISPOSITION-MINING-4": ("mineral", "lapsed"),
    "CA-SK-MINERAL-DISPOSITION-MINING-5": ("potash", "active"),
    "CA-SK-MINERAL-DISPOSITION-MINING-6": ("alkali", "active"),
    "CA-SK-MINERAL-DISPOSITION-MINING-7": ("coal", "active"),
    "CA-SK-MINERAL-DISPOSITION-MINING-8": ("quarry", "active"),
    "CA-SK-MINERAL-DISPOSITION-CROWN-OIL-GAS": ("oil_gas", "active"),
}

# Resource_Map layers publish a COMMODITY column on five of eleven layers and
# nothing on the rest — but pg_resource_potential_zone.commodity is NOT NULL.
# The commodity is in the source_id because it is what the layer IS, so it is
# read from there when the attribute is missing rather than dropping the row.
_RPZ_COMMODITY_BY_SOURCE: dict[str, str] = {
    "CA-SK-RESOURCE-POTENTIAL-BASE": "base metals",
    "CA-SK-RESOURCE-POTENTIAL-BITUMEN": "bitumen",
    "CA-SK-RESOURCE-POTENTIAL-COAL": "coal",
    "CA-SK-RESOURCE-POTENTIAL-GOLD": "gold",
    "CA-SK-RESOURCE-POTENTIAL-HELIUM": "helium",
    "CA-SK-RESOURCE-POTENTIAL-LITHIUM": "lithium",
    "CA-SK-RESOURCE-POTENTIAL-OIL": "oil",
    "CA-SK-RESOURCE-POTENTIAL-POTASH": "potash",
    "CA-SK-RESOURCE-POTENTIAL-RARE": "rare earth elements",
    "CA-SK-RESOURCE-POTENTIAL-URANIUM": "uranium",
}

# CA-SK-DRILLHOLE publishes four stratigraphic contacts as depth/elevation
# pairs. They are lifted into the stratigraphic_depths jsonb under stable keys
# so a consumer does not have to know SK's column spellings.
_STRAT_CONTACTS: dict[str, tuple[str, str]] = {
    "base_of_quaternary": ("BASE_OF_QUATERNARY_DEPTH_M", "BASE_OF_QUATERNARY_ELEV_M"),
    "base_of_phanerozoic": ("BASE_OF_PHANEROZOIC_DEPTH_M", "BASE_OF_PHANEROZOIC_ELEV_M"),
    "base_of_athabasca_sg": ("BASE_OF_ATHABASCA_SG_DEPTH_M", "BASE_OF_ATHABASCA_SG_ELEV_M"),
    "top_crystalline_basement": ("TOP_CRYSTALLINE_BSMT_DEPTH_M", "TOP_CRYSTALLINE_BSMT_ELEV_M"),
}

_CORE_AVAILABILITY = {
    "available": "available",
    "yes": "available",
    "y": "available",
    "partial": "partial",
    "partially available": "partial",
    "some": "partial",
    "unavailable": "unavailable",
    "none": "unavailable",
    "no": "unavailable",
    "n": "unavailable",
    "not available": "unavailable",
}


def _core_availability(raw: str | None) -> str:
    """Normalise to the pg_drillhole_collar CHECK vocabulary."""
    if not raw:
        return "unknown"
    return _CORE_AVAILABILITY.get(str(raw).strip().lower(), "unknown")


# ── mappers ───────────────────────────────────────────────────────────────
# Each returns the type-specific columns plus the common core. ``_status_raw``
# is a sentinel: when present, sync_source() resolves it through the alias
# tables and counts what it could not translate. Types whose status is a
# property of the feed rather than of the feature (dispositions) set ``status``
# directly and omit the sentinel.


def _core(src: PublicGeoSource, feature: dict[str, Any]) -> dict[str, Any] | None:
    """Common columns, or None when the feature carries no usable identity."""
    props = feature.get("properties") or {}
    oid = arcgis.object_id_of(feature)
    if not oid:
        return None
    return {
        "jurisdiction_code": src.jurisdiction_code,
        "source_id": src.source_id,
        "source_feature_id": str(oid),
        # 4326, not src.source_crs — we requested outSR=4326. See module docs.
        "source_crs": 4326,
        "source_url": src.service_url,
        "source_attributes": json.dumps(props, default=str, ensure_ascii=False),
        "checksum": _checksum(props),
    }


def _map_mine(
    src: PublicGeoSource, feature: dict[str, Any], aliases: AliasTables
) -> dict[str, Any] | None:
    """public_geo.pg_mine — CA-SK-MINE-LOC (Mineral_Exploration/1)."""
    row = _core(src, feature)
    if row is None:
        return None
    props = feature.get("properties") or {}

    commodities = _split_list(arcgis.first_present(props, ["COMMODITY", "COMMODITIES"]))
    row.update({
        "name": arcgis.first_present(props, ["NAME", "PROPERTY", "MINE_NAME"]),
        "_status_raw": arcgis.first_present(props, ["STATUS", "DEP_CLASS"]),
        "commodities": commodities,
        "commodity_grouping": (
            aliases.grouping(arcgis.first_present(props, ["COMMODITY_GROUPING", "GROUPING"]))
            or aliases.grouping_of_any(commodities)
        ),
        "operator": arcgis.first_present(props, ["OPERATOR", "COMPANY", "OWNER"]),
    })
    return row


def _map_mineral_occurrence(
    src: PublicGeoSource, feature: dict[str, Any], aliases: AliasTables
) -> dict[str, Any] | None:
    """public_geo.pg_mineral_occurrence — CA-SK-SMDI and CA-BC-MINFILE.

    The two feeds are shaped very differently: SMDI publishes delimited
    PRIMARYCOMMODITIES/ASSOCIATEDCOMMODITIES strings and a GROUPING column,
    MINFILE spreads eight numbered COMMODITY_DESCRIPTION columns and has no
    grouping at all. Both are handled here rather than in two near-identical
    mappers because everything downstream of the field lookup is the same.
    """
    row = _core(src, feature)
    if row is None:
        return None
    props = feature.get("properties") or {}

    primary = _split_list(arcgis.first_present(props, ["PRIMARYCOMMODITIES", "COMMODITY"]))
    associated = _split_list(arcgis.first_present(props, ["ASSOCIATEDCOMMODITIES"]))
    if not primary:
        # MINFILE: COMMODITY_DESCRIPTION1..8. The first is the primary
        # commodity and the rest are associated, which is how MINFILE orders
        # them and how the SMDI split reads.
        numbered = _numbered(props, "COMMODITY_DESCRIPTION", 8)
        primary, associated = numbered[:1], numbered[1:]

    row.update({
        # The column was renamed smdi_id -> external_id in the 2026-04-14 V1.2
        # migration: the slot is jurisdiction-agnostic and holds an SMDI number
        # for CA-SK, a MINFILE_NUMBER for CA-BC, and so on.
        "external_id": arcgis.first_present(props, ["SMDI", "MINFILE_NUMBER", "SMDI_ID"]),
        "name": arcgis.first_present(props, ["NAME", "MINFILE_NAME1", "DEPOSIT_NAME"]),
        "historic_names": _split_list(
            arcgis.first_present(props, ["HISTORICNAMES", "MINFILE_NAME2"])
        ),
        "_status_raw": arcgis.first_present(props, ["STATUS", "STATUS_DESCRIPTION"]),
        "primary_commodities": primary,
        "associated_commodities": associated,
        "commodity_grouping": (
            aliases.grouping(arcgis.first_present(props, ["GROUPING", "SYMBOLOGY_GROUPING"]))
            or aliases.grouping_of_any(primary)
        ),
        "discovery_type": arcgis.first_present(
            props, ["DISCOVERYTYPE", "DEPOSIT_TYPE_DESCRIPTION1"]
        ),
        "production_flag": _truthy(
            arcgis.first_present(props, ["PRODUCTION", "PRODUCTION_IND"])
        ),
        "reserves_resources": arcgis.first_present(
            props, ["RESERVESRESOURCES", "RESERVES_IND"]
        ),
    })
    return row


def _map_drillhole_collar(
    src: PublicGeoSource, feature: dict[str, Any], aliases: AliasTables
) -> dict[str, Any] | None:
    """public_geo.pg_drillhole_collar — CA-SK-DRILLHOLE (Mineral_Exploration/3)."""
    row = _core(src, feature)
    if row is None:
        return None
    props = feature.get("properties") or {}

    strat: dict[str, Any] = {}
    for key, (depth_field, elev_field) in _STRAT_CONTACTS.items():
        depth = _dec(arcgis.first_present(props, [depth_field]))
        elev = _dec(arcgis.first_present(props, [elev_field]))
        if depth is None and elev is None:
            continue
        strat[key] = {
            "depth_m": float(depth) if depth is not None else None,
            "elevation_m": float(elev) if elev is not None else None,
        }

    row.update({
        "drillhole_id": arcgis.first_present(
            props, ["GOS_UNIQUE_DRILLHOLE_ID", "DRILLHOLE_ID", "HOLE_ID"]
        ),
        "drillhole_name": arcgis.first_present(props, ["DRILLHOLE_NAME", "HOLE_NAME"]),
        "company": arcgis.first_present(props, ["COMPANY", "OPERATOR"]),
        "project_name": arcgis.first_present(
            props, ["PROJECT_OR_PROPERTY_NAME", "PROJECT_NAME", "PROPERTY"]
        ),
        "date_drilled": _as_date(arcgis.first_present(props, ["DATE_DRILLED", "DRILL_DATE"])),
        "drill_type": arcgis.first_present(props, ["DRILL_TYPE", "HOLE_TYPE"]),
        "commodity_of_interest": _split_list(
            arcgis.first_present(props, ["COMMODITY_OF_INTEREST", "COMMODITY"])
        ),
        "total_length_m": _dec(
            arcgis.first_present(props, ["TOTAL_DH_LENGTH_M", "TOTAL_LENGTH_M"])
        ),
        "inclination_deg": _dec(arcgis.first_present(props, ["DH_INCLINATION", "INCLINATION"])),
        "azimuth_deg": _dec(arcgis.first_present(props, ["DH_AZIMUTH", "AZIMUTH"])),
        # Prefer the DEM-corrected elevation where the survey publishes one:
        # ORIGINAL_COLLAR_ELEVATION_M is as-reported by the operator and is
        # frequently blank or a rounded map estimate on older holes.
        "collar_elevation_m": _dec(
            arcgis.first_present(
                props, ["ELEV_CORRECTED_1ARCSEC_DEM_M", "ORIGINAL_COLLAR_ELEVATION_M"]
            )
        ),
        "stratigraphic_depths": json.dumps(strat, default=str),
        "core_availability": _core_availability(
            arcgis.first_present(props, ["COREAVAILABILITY", "CORE_AVAILABILITY"])
        ),
        "core_storage": arcgis.first_present(
            props, ["STORAGE_LOCATIONS", "CORESTORAGEDETAILS", "CORE_STORAGE"]
        ),
        "disposition": arcgis.first_present(props, ["DISPOSITION"]),
    })
    return row


def _map_resource_potential_zone(
    src: PublicGeoSource, feature: dict[str, Any], aliases: AliasTables
) -> dict[str, Any] | None:
    """public_geo.pg_resource_potential_zone — Resource_Map/MapServer/*."""
    row = _core(src, feature)
    if row is None:
        return None
    props = feature.get("properties") or {}

    commodity = (
        arcgis.first_present(props, ["COMMODITY"])
        or _RPZ_COMMODITY_BY_SOURCE.get(src.source_id)
        or "unknown"
    )
    row.update({
        "commodity": commodity[:64],
        "commodity_grouping": aliases.grouping(commodity),
        # MAPKEY / ZLEVEL are the only ordinal-looking attributes on these
        # layers and they carry the legend class, which is the potential rank
        # on the layers that publish one. Anything outside the column's 1..6
        # CHECK is dropped rather than clamped — a legend key of 12 is not a
        # rank-6 zone.
        "potential_rank": _int(
            arcgis.first_present(props, ["POTENTIAL_RANK", "MAPKEY", "ZLEVEL"]),
            lo=1, hi=6,
        ),
        "methodology_ref": arcgis.first_present(props, ["METHODOLOGY", "POOLNAME", "LAYER"]),
    })
    return row


def _map_rock_sample(
    src: PublicGeoSource, feature: dict[str, Any], aliases: AliasTables
) -> dict[str, Any] | None:
    """public_geo.pg_rock_sample — CA-SK-ROCK-SAMPLES (Mineral_Exploration/4)."""
    row = _core(src, feature)
    if row is None:
        return None
    props = feature.get("properties") or {}

    row.update({
        "station": arcgis.first_present(props, ["STATION"]),
        "sample_number": arcgis.first_present(props, ["SAMPLE_NUM", "SAMPLE_NUMBER"]),
        "geologist": arcgis.first_present(props, ["GEOLOGIST"]),
        "geographic_area": arcgis.first_present(props, ["GEOG_AREA", "GEOGRAPHIC_AREA"]),
        "report_number": arcgis.first_present(props, ["REPORT_NUM", "REPORT_NUMBER"]),
        "map_number": arcgis.first_present(props, ["MAP_NUM", "MAP_NUMBER"]),
        "map_scale": arcgis.first_present(props, ["MAP_SCALE"]),
        "nts_250k": arcgis.first_present(props, ["NTS_250K"]),
        "nts_50k": arcgis.first_present(props, ["NTS_50K"]),
        # SK names this column DATE_ — a trailing underscore because DATE is
        # reserved. Not a typo.
        "date_collected": _as_date(arcgis.first_present(props, ["DATE_", "DATE_COLLECTED"])),
    })
    return row


def _map_assessment_survey(
    src: PublicGeoSource, feature: dict[str, Any], aliases: AliasTables
) -> dict[str, Any] | None:
    """public_geo.pg_assessment_survey — P_Mineral_Assessment_File_Information/1-3.

    The table carries only survey_type as a typed column; FILENUMBER, COMPANY,
    WORK_DATE and the four WORK_n descriptors stay in source_attributes, which
    is where the assessment-file citation path reads them from.
    """
    row = _core(src, feature)
    if row is None:
        return None
    row["survey_type"] = SURVEY_TYPE_BY_SOURCE.get(src.source_id, "unknown")
    return row


def _map_mineral_disposition(
    src: PublicGeoSource, feature: dict[str, Any], aliases: AliasTables
) -> dict[str, Any] | None:
    """public_geo.pg_mineral_disposition — Mining/0-8 + Crown_Dispositions/8.

    Ten layers with three different field vocabularies: legacy cryptic names
    truncated to ten characters (DISPOSITIO, DISPOSIT_1, OWNERS, EFFECTIVED)
    on Mining/0 and /4, modern names (DISPOSITION, HOLDER, STATUS, HECTARES)
    on Mining/5-8, and a third set on the Crown oil-and-gas layer (DISPID,
    LESSEES, PARCELHECT). The candidate lists below cover all three.

    disposition_type and status come from DISPOSITION_BY_SOURCE, not from the
    feature: the survey encodes them by publishing each combination as its own
    layer, so every feature in Mining/4 is a lapsed mineral disposition
    whatever its attributes say.
    """
    row = _core(src, feature)
    if row is None:
        return None
    props = feature.get("properties") or {}

    disp_type, disp_status = DISPOSITION_BY_SOURCE.get(src.source_id, ("mineral", "unknown"))

    # Area: three different units across the ten layers. Normalised to
    # hectares, which is what the column is.
    area_ha = _dec(arcgis.first_present(props, ["HECTARES", "PARCELHECT"]))
    if area_ha is None:
        acres = _dec(arcgis.first_present(props, ["ACRES"]))
        if acres is not None:
            area_ha = acres * Decimal("0.40468564224")
    if area_ha is None:
        m2 = _dec(arcgis.first_present(props, ["AREA_M2", "AREA_"]))
        if m2 is not None:
            area_ha = m2 / Decimal(10000)

    row.update({
        "disposition_number": arcgis.first_present(
            props,
            ["DISPOSITION", "DISPOSITIO", "DISPID", "DISP_NUM", "DISPACQAPP", "APPLICATIO"],
        ),
        "disposition_type": disp_type,
        "status": disp_status,
        "holder_name": arcgis.first_present(
            props, ["HOLDER", "OWNERS", "LESSEES", "HOLDERDESC"]
        ),
        "issue_date": _as_date(
            arcgis.first_present(
                props, ["ISSUEDATE", "ISSUEDDATE", "EFFECTIVEDATE", "EFFECTIVED", "POSTEDON"]
            )
        ),
        "expiry_date": _as_date(
            arcgis.first_present(props, ["EXPIRYDATE", "RENEWALDATE", "ANNIVERSARYDATE"])
        ),
        "area_ha": None if area_ha is None else round(area_ha, 2),
        "commodity_codes": _split_list(arcgis.first_present(props, ["MATERIAL", "COMMODITY"])),
        "geographic_area": arcgis.first_present(
            props, ["GEOAREA", "LOCATION", "LANDDESC", "SHORTDESC"]
        ),
    })
    return row


MAPPERS: dict[
    str, Callable[[PublicGeoSource, dict[str, Any], "AliasTables"], dict[str, Any] | None]
] = {
    "mine": _map_mine,
    "mineral_occurrence": _map_mineral_occurrence,
    "drillhole_collar": _map_drillhole_collar,
    "resource_potential_zone": _map_resource_potential_zone,
    "rock_sample": _map_rock_sample,
    "assessment_survey": _map_assessment_survey,
    "mineral_disposition": _map_mineral_disposition,
}


# ── SQL generation ────────────────────────────────────────────────────────

def _geom_expr(spec: TableSpec, param: int) -> str:
    """SQL producing the geom value from a GeoJSON text parameter.

    The parameter is cast ``::text`` explicitly at every use site. Without it
    asyncpg cannot infer a type for a parameter that only ever appears inside
    a function call, and every row fails with "could not determine data type
    of parameter $N".

    Polygon feeds go through MakeValid + CollectionExtract because ArcGIS
    routinely emits ring sets that GeoJSON declares as one Polygon but that
    are geometrically several — self-intersecting, or with an outer ring
    listed as a hole. Inserting those straight into a MULTIPOLYGON column
    stores an invalid geometry that then breaks ST_Intersects for every map
    query that touches it.
    """
    expr = f"ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(${param}::text), 4326))"
    if spec.geom_kind == "MULTIPOLYGON":
        expr = f"ST_Multi(ST_CollectionExtract(ST_MakeValid({expr}), 3))"
    return f"CASE WHEN ${param}::text IS NULL THEN NULL ELSE {expr} END"


def build_upsert(spec: TableSpec) -> tuple[str, tuple[str, ...]]:
    """Generate the INSERT … ON CONFLICT for one table.

    Returns the SQL and the bind order, so sync_source() reads values out of
    the mapper's dict by name instead of by a hand-maintained positional list
    that silently rots the moment a column is added.
    """
    columns = spec.columns + _COMMON_COLUMNS
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    geom = _geom_expr(spec, len(columns) + 1)

    # WKT duplicates geom; only worth it on the point feeds. See module docs.
    wkt = f"ST_AsText({geom})" if spec.geom_kind == "POINT" else "NULL"

    updates = ",\n    ".join(
        f"{c} = EXCLUDED.{c}" for c in columns if c not in _KEY_COLUMNS
    )

    sql = f"""
INSERT INTO {spec.table} (
    id, {", ".join(columns)}, source_geom_wkt,
    first_seen_at, last_seen_at, created_at, updated_at, geom
) VALUES (
    gen_random_uuid(), {placeholders}, {wkt},
    now(), now(), now(), now(), {geom}
)
ON CONFLICT (source_id, source_feature_id) DO UPDATE SET
    last_seen_at = now(),
    -- Only churn updated_at when the upstream attributes actually changed;
    -- an unchanged feature must not re-trigger downstream work.
    updated_at = CASE WHEN {spec.table}.checksum IS DISTINCT FROM EXCLUDED.checksum
                      THEN now() ELSE {spec.table}.updated_at END,
    {updates},
    source_geom_wkt = EXCLUDED.source_geom_wkt,
    geom = EXCLUDED.geom
"""
    return sql, columns


async def text_limits(conn: Any, table: str) -> dict[str, int]:
    """``column -> character_maximum_length`` for one canonical table.

    Read from the catalogue rather than hard-coded here. The widths are not
    uniform (pg_rock_sample.geographic_area is varchar(255) while
    pg_mineral_disposition.geographic_area is varchar(128)) and an upstream
    value that overruns one of them fails the whole row:

        value too long for type character varying(128)

    which is how CA-SK-MINERAL-DISPOSITION-MINING-7 lost half its features to
    long legal land descriptions. Asking the database means a later column
    widening takes effect without touching this file.
    """
    schema, _, name = table.partition(".")
    rows = await conn.fetch(
        "SELECT column_name, character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = $2 "
        "AND character_maximum_length IS NOT NULL",
        schema, name,
    )
    return {r["column_name"]: int(r["character_maximum_length"]) for r in rows}


def _fit(value: Any, limit: int | None) -> Any:
    """Truncate an over-long string to its column width.

    Truncation is lossless in the sense that matters here: the untruncated
    value is still in source_attributes, which is written verbatim. Dropping
    the row instead would lose the feature entirely.
    """
    if limit is None or not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit]


async def sync_source(
    conn: Any,
    src: PublicGeoSource,
    *,
    aliases: AliasTables | None = None,
    max_features: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    """Pull one feed and upsert it. Returns per-source stats."""
    stats: dict[str, Any] = {
        "source_id": src.source_id,
        "canonical_type": src.canonical_type,
        "fetched": 0,
        "upserted": 0,
        "unmapped": 0,
        "errors": 0,
    }

    mapper = MAPPERS.get(src.canonical_type)
    spec = SPECS.get(src.canonical_type)
    if mapper is None or spec is None:
        # Loud skip. Writing a partial row into a typed table would look like
        # a successful sync while silently losing the type-specific columns.
        # bedrock_geology is the live example: it is addressable in the
        # registry but has no canonical table, so it is reported, not written.
        stats["skipped_reason"] = f"no mapper for canonical_type={src.canonical_type}"
        logger.warning("public_geo.sync: %s", stats["skipped_reason"])
        return stats

    if aliases is None:
        aliases = await AliasTables.load(conn)

    sql, bind_order = build_upsert(spec)
    limits = await text_limits(conn, spec.table)
    unmapped_statuses: dict[str, int] = {}
    truncated = 0

    async for feature in arcgis.iter_all_features(
        src, page_size=page_size or spec.page_size, max_features=max_features
    ):
        stats["fetched"] += 1
        row = mapper(src, feature, aliases)
        if row is None:
            stats["unmapped"] += 1
            continue

        # Alias-driven status is resolved here rather than in the mapper so the
        # run can COUNT what it failed to translate. See AliasTables.status().
        if "_status_raw" in row:
            status, unmapped_status = aliases.status(
                row.pop("_status_raw"),
                jurisdiction=src.jurisdiction_code,
                canonical_type=src.canonical_type,
            )
            row["status"] = status
            if unmapped_status:
                unmapped_statuses[unmapped_status] = (
                    unmapped_statuses.get(unmapped_status, 0) + 1
                )

        values = []
        for col in bind_order:
            fitted = _fit(row[col], limits.get(col))
            if fitted is not row[col]:
                truncated += 1
            values.append(fitted)

        try:
            await conn.execute(sql, *values, _geom_geojson(feature))
            stats["upserted"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad feature must not end the feed
            stats["errors"] += 1
            if stats["errors"] <= 3:
                logger.warning(
                    "public_geo.sync: %s feature %s failed: %s",
                    src.source_id, row.get("source_feature_id"), exc,
                )
                stats.setdefault("first_error", str(exc)[:400])

    if truncated:
        # Reported so a systematically-too-narrow column is visible rather
        # than quietly clipping every row.
        stats["truncated_values"] = truncated

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
    source_ids: list[str] | None = None,
    max_features_per_source: int | None = None,
) -> dict[str, Any]:
    """Sync every queryable feed matching the filters."""
    feeds = sources_for(
        canonical_types=canonical_types, jurisdiction_codes=jurisdiction_codes
    )
    if source_ids:
        wanted = set(source_ids)
        feeds = [f for f in feeds if f.source_id in wanted]

    # Loaded once and threaded through: re-reading the alias rows per feed
    # would be pure waste, and a mid-run change to the mapping would make one
    # sync internally inconsistent.
    aliases = await AliasTables.load(conn)

    started = datetime.now(UTC)
    per_source = []
    for src in feeds:
        try:
            per_source.append(
                await sync_source(
                    conn, src, aliases=aliases, max_features=max_features_per_source
                )
            )
        except Exception as exc:  # noqa: BLE001 — one dead survey must not end the run
            logger.exception("public_geo.sync: %s aborted", src.source_id)
            per_source.append({
                "source_id": src.source_id,
                "canonical_type": src.canonical_type,
                "fetched": 0, "upserted": 0, "unmapped": 0, "errors": 1,
                "aborted_reason": str(exc)[:400],
            })

    return {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "feeds": len(feeds),
        "fetched": sum(s["fetched"] for s in per_source),
        "upserted": sum(s["upserted"] for s in per_source),
        "errors": sum(s["errors"] for s in per_source),
        "skipped": [s["source_id"] for s in per_source if "skipped_reason" in s],
        "per_source": per_source,
    }
