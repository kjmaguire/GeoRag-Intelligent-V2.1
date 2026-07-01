"""Silver Public Geoscience — canonical upsert with history.

Phase 2.3 of the Public Geoscience feature, extended in Phase 4 to support a
second jurisdiction (BC MINFILE). Silver assets paired with each Phase-2.2 /
Phase-4 Bronze asset:

    silver_pg_ca_sk_mine_loc            → public_geo.pg_mine
    silver_pg_ca_sk_smdi                → public_geo.pg_mineral_occurrence
    silver_pg_ca_sk_drillhole           → public_geo.pg_drillhole_collar
    silver_pg_ca_sk_resource_potential  → public_geo.pg_resource_potential_zone
    silver_pg_ca_bc_minfile             → public_geo.pg_mineral_occurrence
                                          (BC MINFILE — second jurisdiction)

Phase 4 refactor — jurisdiction-aware field mappings:

  Each canonical-type extractor used to hardcode SK-specific attribute names
  (`SMDI`, `PRIMARYCOMMODITIES`, `STATUS`, etc.). Adding BC MINFILE with its
  own schema (`MINFILE_NO`, `COMMODITIES`, `STATUS_DESC`, etc.) forced the
  extractors to become mapping-driven.

  Each source registers one or more `FieldMapping` instances in
  `FIELD_MAPPINGS` below, declaring how that source's raw attribute names
  map to canonical fields. The shared extractors (`_extract_mine`,
  `_extract_mineral_occurrence`, etc.) look up the mapping at call time.
  Sources with no registered mapping fall back to a sensible SK-SMDI-style
  default so mistakes produce empty rows + WARN logs, not hard crashes.

  This is the abstraction the kickoff §04 asked for:
  "Everything jurisdiction-specific should be config + crosswalk entries
   + a source adapter." The `FieldMapping` is the source adapter.

Responsibilities, per plan §05b (unchanged from Phase 2.3):

  1. Download the most-recent Bronze FeatureCollection for each source_id
     (latest `{run_id}.geojson` under public_geoscience/{jurisdiction}/
     {source_id}/ by last_modified).
  2. Parse GeoJSON; for each feature extract canonical fields via the
     mapping-driven extractor for its canonical_type.
  3. Reproject geometry from source CRS (2957 for SK, 3005 for BC) →
     EPSG:4326 using pyproj + shapely. Store both original source-CRS WKT
     (for round-trip fidelity) and 4326 WKT (for spatial queries).
  4. Apply commodity + status crosswalks from `public_geo.commodity_
     aliases` and `public_geo.status_aliases`.
  5. Compute a per-feature SHA-256 checksum over the raw source attributes +
     source-CRS geometry WKT. This is the "did upstream change?" anchor.
  6. Bulk-UPSERT into the canonical table, with per-feature history semantics.
  7. Missing features are NOT hard-deleted; their `last_seen_at` drifts.
"""

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable

import psycopg2.extras
import shapely.geometry
import shapely.ops
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)
from pyproj import Transformer

from georag_dagster.resources import S3Resource, PostgresResource

logger = logging.getLogger(__name__)

# Dedupe set so a missing DrillholeFieldMapping is logged once per source_id
# per Python-process lifetime rather than spamming the log on every feature.
_warned_missing_drillhole_mappings: set[str] = set()

BRONZE_BUCKET = "bronze"
ROOT_PREFIX = "public_geoscience"


# ---------------------------------------------------------------------------
# Asset config (shared across all 4 Silver assets)
# ---------------------------------------------------------------------------

class SilverPublicGeoscienceConfig(Config):
    """Runtime overrides for a single Silver run."""

    # Override the source_id's MinIO object key. Defaults to the latest
    # available `{run_id}.geojson` under the source's bronze prefix.
    override_object_name: str | None = None

    # Batch size for per-feature inserts into the staging table.
    batch_size: int = 500


# ---------------------------------------------------------------------------
# Canonical specs — one per target table, declares the column layout and the
# per-feature extractor function. Declared near the bottom of this module
# because extractors reference crosswalks.
# ---------------------------------------------------------------------------

@dataclass
class CanonicalRecord:
    """Result of extracting a single feature's canonical fields.

    `attrs` is the open-ended kv dict specific to the canonical type (e.g.
    {"name": "...", "status": "producing", "commodities": ["Au", "Cu"], ...}).
    The generic upsert scaffolding reads this dict and composes SQL
    parameters keyed by the target table's column names.
    """

    source_id: str
    source_feature_id: str
    jurisdiction_code: str
    source_crs: int
    source_geom_wkt: str | None
    geom_wkt_4326: str | None
    source_url: str | None
    source_attributes: dict[str, Any]
    checksum: str
    canonical_attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalSpec:
    """Static declaration of how to upsert one canonical table."""

    target_table: str            # e.g. "public_geo.pg_mine"
    history_table: str           # e.g. "public_geo.pg_mine_history"
    geometry_type: str           # "POINT" or "MULTIPOLYGON"

    # Columns specific to this canonical table (excluding the ones that are
    # always there: id, source_id, source_feature_id, jurisdiction_code,
    # source_crs, source_geom_wkt, source_url, source_attributes, geom,
    # checksum, first_seen_at, last_seen_at, created_at, updated_at).
    # Each canonical_attr key must be a PostgreSQL-safe column name.
    canonical_columns: list[str]

    # Function (source_id, props, geom_json, crosswalks) → CanonicalRecord
    extractor: Callable[
        [str, dict[str, Any], dict[str, Any] | None, dict[str, Any], "CrosswalkSet"],
        CanonicalRecord | None,
    ]


# ---------------------------------------------------------------------------
# Crosswalks
# ---------------------------------------------------------------------------

@dataclass
class CommodityRecord:
    canonical_code: str
    canonical_name: str
    commodity_grouping: str


@dataclass
class CrosswalkSet:
    """In-memory cache of `commodity_aliases` + `status_aliases` for one run."""

    commodity: dict[str, CommodityRecord]
    status: dict[tuple[str, str, str], str]  # (jurisdiction, canonical_type, lower_raw) → canonical

    def resolve_commodity(self, raw: str | None) -> CommodityRecord | None:
        if not raw:
            return None
        return self.commodity.get(raw.strip().lower())

    def resolve_status(
        self, *, jurisdiction: str, canonical_type: str, raw: str | None, default: str = "unknown"
    ) -> str:
        if not raw:
            return default
        key = (jurisdiction, canonical_type, raw.strip().lower())
        return self.status.get(key, default)


def _load_crosswalks(postgres: PostgresResource) -> CrosswalkSet:
    commodity: dict[str, CommodityRecord] = {}
    status: dict[tuple[str, str, str], str] = {}

    with postgres.get_cursor() as cur:
        cur.execute(
            """
            SELECT alias_lower, canonical_code, canonical_name, commodity_grouping
              FROM public_geo.commodity_aliases
            """
        )
        for row in cur.fetchall():
            commodity[row["alias_lower"]] = CommodityRecord(
                canonical_code=row["canonical_code"],
                canonical_name=row["canonical_name"],
                commodity_grouping=row["commodity_grouping"],
            )

        cur.execute(
            """
            SELECT jurisdiction_code, canonical_type, source_value_lower, canonical_status
              FROM public_geo.status_aliases
            """
        )
        for row in cur.fetchall():
            status[
                (
                    row["jurisdiction_code"],
                    row["canonical_type"],
                    row["source_value_lower"],
                )
            ] = row["canonical_status"]

    return CrosswalkSet(commodity=commodity, status=status)


# ---------------------------------------------------------------------------
# MinIO — find latest Bronze object for a source_id
# ---------------------------------------------------------------------------

def _find_latest_bronze(
    minio: S3Resource,
    *,
    jurisdiction_code: str,
    source_id: str,
) -> str | None:
    """Return the object_name of the most recent `{run_id}.geojson` Bronze
    file for the given source, or None if nothing has been ingested yet.
    """
    prefix = f"{ROOT_PREFIX}/{jurisdiction_code}/{source_id}/"
    latest_name: str | None = None
    latest_mod: datetime | None = None
    # Use boto3 paginator via S3Resource.get_client() for list_objects_v2
    s3_client = minio.get_client()
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BRONZE_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            if not key.endswith(".geojson"):
                continue
            mod = obj.get("LastModified")
            if mod is None:
                continue
            if mod.tzinfo is None:
                mod = mod.replace(tzinfo=timezone.utc)
            if latest_mod is None or mod > latest_mod:
                latest_mod = mod
                latest_name = key
    return latest_name


def _download_feature_collection(
    minio: S3Resource,
    *,
    object_name: str,
) -> dict[str, Any]:
    body = minio.download_bytes(BRONZE_BUCKET, object_name)
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Bronze object {object_name!r} is not valid JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Field mappings — jurisdiction-aware "source adapters" (Phase 4 refactor)
#
# Each mapping declares how one source's raw property names map to the
# canonical schema for its canonical_type. The shared extractors below read
# from these dicts rather than string-literal attribute names so adding a
# new jurisdiction is a seed entry + a mapping entry, not a new extractor.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MineFieldMapping:
    """Per-source field mapping for canonical_type='mine'."""
    name_field: str
    status_field: str
    commodities_field: str
    operator_field: str | None
    source_url_field: str | None


@dataclass(frozen=True)
class MineralOccurrenceFieldMapping:
    """Per-source field mapping for canonical_type='mineral_occurrence'.

    `external_id_field` is the jurisdiction-native identifier (SMDI, MINFILE,
    MODS, etc.) — stored in pg_mineral_occurrence.external_id (V1.2 rename;
    was `smdi_id` in V1.0–V1.1, kept the FieldMapping field name generic
    throughout so the rename was invisible to source adapters).

    Commodity fields accept EITHER:
      - a single string → that one property holds a delimiter-separated
        list (SK SMDI pattern: "Au, Cu, Zn"), OR
      - a tuple/list of strings → each property holds one value, and we
        merge the non-empty values into a list (BC MINFILE pattern:
        COMMODITY_CODE1..8 are eight separate fields).
    """
    external_id_field: str | None
    name_field: str
    historic_names_field: str | None
    status_field: str
    primary_commodities_field: str | tuple[str, ...]
    associated_commodities_field: str | tuple[str, ...] | None
    grouping_field: str | None
    discovery_type_field: str | None
    production_flag_field: str | None
    reserves_resources_field: str | None
    source_url_field: str | None


@dataclass(frozen=True)
class DrillholeFieldMapping:
    """Per-source field mapping for canonical_type='drillhole_collar'."""
    drillhole_id_field: str
    drillhole_name_field: str | None
    company_field: str | None
    project_name_field: str | None
    date_drilled_field: str | None
    drill_type_field: str | None
    commodity_of_interest_field: str | None
    total_length_field: str | None
    inclination_field: str | None
    azimuth_field: str | None
    collar_elevation_field: str | None
    core_availability_field: str | None
    core_storage_field: str | None
    disposition_field: str | None
    strat_depth_fields: dict[str, str]          # canonical_key → raw_field
    source_url_field: str | None


# Registry keyed on source_id. Mine/occurrence/drillhole/resource-potential
# each have their own dict so lookups stay O(1) and typed.

MINE_FIELD_MAPPINGS: dict[str, MineFieldMapping] = {
    # Saskatchewan — Mine Locations (layer 1). Field names per plan §03c.
    "CA-SK-MINE-LOC": MineFieldMapping(
        name_field="NAME",
        status_field="STATUS",
        commodities_field="COMMODITY",
        operator_field="OPERATOR",
        source_url_field="WEBLINK",
    ),
}


MINERAL_OCCURRENCE_FIELD_MAPPINGS: dict[str, MineralOccurrenceFieldMapping] = {
    # Saskatchewan SMDI (layer 2). Field names per plan §03c.
    "CA-SK-SMDI": MineralOccurrenceFieldMapping(
        external_id_field="SMDI",
        name_field="NAME",
        historic_names_field="HISTORICNAMES",
        status_field="STATUS",
        primary_commodities_field="PRIMARYCOMMODITIES",
        associated_commodities_field="ASSOCIATEDCOMMODITIES",
        grouping_field="GROUPING",
        discovery_type_field="DISCOVERYTYPE",
        production_flag_field="PRODUCTION",
        reserves_resources_field="RESERVESRESOURCES",
        source_url_field="WEBLINK",
    ),
    # BC MINFILE (Phase 4 — second jurisdiction). Field names verified
    # live against layer 137 of
    # https://delivery.maps.gov.bc.ca/arcgis/rest/services/mpcm/bcgwpub/MapServer
    # on 2026-04-14. BC publishes eight separate COMMODITY_CODE* fields
    # per occurrence (not a delimited string like SK), which the shared
    # extractor handles via the tuple form of primary_commodities_field.
    "CA-BC-MINFILE": MineralOccurrenceFieldMapping(
        external_id_field="MINFILE_NUMBER",
        name_field="MINFILE_NAME1",
        historic_names_field="MINFILE_NAME2",
        status_field="STATUS_DESCRIPTION",
        primary_commodities_field=(
            "COMMODITY_CODE1", "COMMODITY_CODE2", "COMMODITY_CODE3",
            "COMMODITY_CODE4", "COMMODITY_CODE5", "COMMODITY_CODE6",
            "COMMODITY_CODE7", "COMMODITY_CODE8",
        ),
        associated_commodities_field=None,    # MINFILE rolls everything into COMMODITY_CODE*
        grouping_field=None,                  # grouping inferred from commodities
        discovery_type_field="DEPOSIT_CLASS_DESCRIPTION1",
        production_flag_field="PRODUCTION_IND",   # 'Y'/'N' string
        reserves_resources_field=None,
        source_url_field="MINFILE_SUMMARY_URL",
    ),
}


DRILLHOLE_FIELD_MAPPINGS: dict[str, DrillholeFieldMapping] = {
    # Saskatchewan Drillhole Compilation (layer 3). Field names per §03c.
    "CA-SK-DRILLHOLE": DrillholeFieldMapping(
        drillhole_id_field="GOS_UNIQUE_DRILLHOLE_ID",
        drillhole_name_field="DRILLHOLE_NAME",
        company_field="COMPANY",
        project_name_field="PROJECT_OR_PROPERTY_NAME",
        date_drilled_field="DATE_DRILLED",
        drill_type_field="DRILL_TYPE",
        commodity_of_interest_field="COMMODITY_OF_INTEREST",
        total_length_field="TOTAL_DH_LENGTH_M",
        inclination_field="DH_INCLINATION",
        azimuth_field="DH_AZIMUTH",
        collar_elevation_field="ORIGINAL_COLLAR_ELEVATION_M",
        core_availability_field="COREAVAILABILITY",
        core_storage_field="STORAGE_LOCATIONS",
        disposition_field="DISPOSITION",
        strat_depth_fields={
            "base_quaternary_m":  "BASE_OF_QUATERNARY_DEPTH_M",
            "base_phanerozoic_m": "BASE_OF_PHANEROZOIC_DEPTH_M",
            "base_athabasca_m":   "BASE_OF_ATHABASCA_SG_DEPTH_M",
            "top_basement_m":     "TOP_CRYSTALLINE_BSMT_DEPTH_M",
        },
        source_url_field="SOURCE",
    ),
    # BC — no province-wide drillhole compilation (plan §02a note: BC drill
    # data lives in per-assessment-report artifacts, not a unified feature
    # service). Deliberately no mapping entry; the Bronze registry has no
    # CA-BC-DRILLHOLE source either.
}


# Fallback used when a Bronze source has no registered mapping. Returns
# valid mapping objects so the extractor logs a warning and produces an
# effectively-empty canonical record rather than crashing. This is a safety
# rail for development — production sources must register a real mapping.
def _fallback_mineral_occurrence_mapping() -> MineralOccurrenceFieldMapping:
    return MineralOccurrenceFieldMapping(
        external_id_field=None,
        name_field="NAME",
        historic_names_field=None,
        status_field="STATUS",
        primary_commodities_field="COMMODITIES",
        associated_commodities_field=None,
        grouping_field=None,
        discovery_type_field=None,
        production_flag_field=None,
        reserves_resources_field=None,
        source_url_field=None,
    )


def _fallback_mine_mapping() -> MineFieldMapping:
    return MineFieldMapping(
        name_field="NAME",
        status_field="STATUS",
        commodities_field="COMMODITY",
        operator_field=None,
        source_url_field=None,
    )


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

class Reprojector:
    """Cached pyproj transformer + shapely wrapper for one source CRS."""

    def __init__(self, source_crs: int, target_crs: int = 4326) -> None:
        self.source_crs = int(source_crs)
        self.target_crs = int(target_crs)
        self._tx = Transformer.from_crs(
            self.source_crs, self.target_crs, always_xy=True
        )

    def transform(self, geom_json: dict[str, Any] | None) -> tuple[str | None, str | None]:
        """Return (source_crs_wkt, target_crs_wkt). Both None if geometry missing/invalid."""
        if not geom_json:
            return None, None
        try:
            src_shape = shapely.geometry.shape(geom_json)
        except Exception:
            return None, None
        if src_shape.is_empty:
            return None, None

        src_wkt = src_shape.wkt

        try:
            if self.source_crs == self.target_crs:
                tgt_shape = src_shape
            else:
                tgt_shape = shapely.ops.transform(self._tx.transform, src_shape)
        except Exception:
            return src_wkt, None

        return src_wkt, tgt_shape.wkt


# ---------------------------------------------------------------------------
# Feature-level utilities
# ---------------------------------------------------------------------------

_LIST_SPLIT_RE = re.compile(r"[,;/|]+")


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    return str(value)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _as_int(value: Any) -> int | None:
    f = _as_float(value)
    if f is None:
        return None
    try:
        return int(f)
    except (ValueError, TypeError):
        return None


def _split_list(value: Any) -> list[str]:
    """Split a delimiter-separated string into a trimmed list. Empty in → []."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    s = str(value).strip()
    if not s:
        return []
    parts = [p.strip() for p in _LIST_SPLIT_RE.split(s)]
    return [p for p in parts if p]


def _collect_commodity_values(
    props: dict[str, Any],
    spec: str | tuple[str, ...] | list[str] | None,
) -> list[str]:
    """Pull raw commodity values out of the feature's properties dict.

    The spec is either:
      - A single string — the property holds a delimiter-separated list
        (SK SMDI pattern: PRIMARYCOMMODITIES = "Au, Cu, Zn") → split on
        the shared delimiter set.
      - A tuple/list of strings — each property holds at most one value
        (BC MINFILE pattern: COMMODITY_CODE1..8) → collect non-empty
        values across all fields, preserving order.

    None spec returns []. Never raises on missing keys.
    """
    if spec is None:
        return []
    if isinstance(spec, str):
        return _split_list(props.get(spec))
    # tuple/list — merge single-valued fields.
    out: list[str] = []
    for name in spec:
        raw = props.get(name)
        if raw is None:
            continue
        s = str(raw).strip()
        if s:
            out.append(s)
    return out


_TRUTHY_FLAG_TOKENS = frozenset({
    "y", "yes", "true", "t", "1", "producer", "producing",
})
_FALSY_FLAG_TOKENS = frozenset({
    "n", "no", "none", "n/a", "na", "false", "f", "0", "",
})


def _parse_boolean_flag(raw: Any) -> bool:
    """Parse upstream boolean-ish strings into a Python bool.

    Known encodings across the jurisdictions we support:
      - BC MINFILE `PRODUCTION_IND`: 'Y' / 'N'
      - SK SMDI `PRODUCTION`: free text ("Yes", "Copper production 1962–1967", "")

    For free-text values that don't match either set, fall back to
    "non-empty string means true" — SK SMDI's historical descriptions
    should land on True ("Copper production 1962–1967" → True, "" → False).
    """
    if raw is None:
        return False
    s = str(raw).strip().lower()
    if s in _FALSY_FLAG_TOKENS:
        return False
    if s in _TRUTHY_FLAG_TOKENS:
        return True
    return bool(s)


def _canonicalize_commodities(raw: Iterable[str], crosswalks: CrosswalkSet) -> tuple[list[str], str | None]:
    """Map a raw commodity list to canonical codes + a dominant grouping.

    The dominant grouping is the most common grouping across resolved
    aliases; ties broken by first occurrence. Unresolved aliases are still
    returned as-is in the canonical_codes list (preserving the raw value is
    better than silently dropping).
    """
    codes: list[str] = []
    grouping_counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for i, raw_val in enumerate(raw):
        if not raw_val:
            continue
        match = crosswalks.resolve_commodity(raw_val)
        if match is None:
            codes.append(raw_val)
            continue
        codes.append(match.canonical_code)
        grouping_counts[match.commodity_grouping] = (
            grouping_counts.get(match.commodity_grouping, 0) + 1
        )
        first_seen.setdefault(match.commodity_grouping, i)

    if not grouping_counts:
        return codes, None

    best = max(
        grouping_counts.items(),
        key=lambda kv: (kv[1], -first_seen[kv[0]]),
    )
    return codes, best[0]


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    # ArcGIS FeatureServer emits dates as ms-since-epoch integers.
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).date()
        except (OSError, ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # Try common ISO forms.
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_core_availability(value: Any) -> str:
    v = (_as_str(value) or "").lower()
    if not v:
        return "unknown"
    if "partial" in v:
        return "partial"
    if "unavailable" in v or "no core" in v or "lost" in v or v == "no":
        return "unavailable"
    if "available" in v or v == "yes":
        return "available"
    return "unknown"


def _compute_feature_checksum(
    source_id: str,
    source_feature_id: str,
    source_attrs: dict[str, Any],
    source_geom_wkt: str | None,
) -> str:
    blob = {
        "source_id": source_id,
        "source_feature_id": source_feature_id,
        "attrs": source_attrs,
        "geom": source_geom_wkt or "",
    }
    raw = json.dumps(blob, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _derive_source_feature_id(props: dict[str, Any]) -> str | None:
    """Return a stable feature-scoped id.

    Tries Esri-style identifier fields in order. The SK Resource_Map
    layers publish a `OBJECTID` field that's set to 0 for most rows
    (upstream bug) while keeping the real unique id in `OBJECTID_1` —
    we detect this by preferring OBJECTID_1 whenever OBJECTID is 0 or
    missing.
    """
    def _clean(val: Any) -> str | None:
        if val is None:
            return None
        s = str(val).strip()
        return s or None

    oid = _clean(props.get("OBJECTID"))
    oid1 = _clean(props.get("OBJECTID_1"))

    # Prefer the unique alt-id when OBJECTID is the degenerate literal "0"
    # or missing. Otherwise trust OBJECTID as it's the canonical Esri PK.
    if oid is not None and oid != "0":
        return oid
    if oid1 is not None and oid1 != "0":
        return oid1

    for key in ("FID", "ObjectID", "objectid"):
        v = _clean(props.get(key))
        if v is not None:
            return v

    # Last resort — both OBJECTIDs degenerate; fall back to OBJECTID_1 even
    # if it's zero (still more stable than None since we'd lose the row).
    return oid1 or oid


# ---------------------------------------------------------------------------
# Per-canonical-type extractors
# ---------------------------------------------------------------------------

def _extract_mine(
    source_id: str,
    props: dict[str, Any],
    geom_json: dict[str, Any] | None,
    ctx: dict[str, Any],
    crosswalks: CrosswalkSet,
) -> CanonicalRecord | None:
    feature_id = _derive_source_feature_id(props)
    if feature_id is None:
        return None

    mapping = MINE_FIELD_MAPPINGS.get(source_id) or _fallback_mine_mapping()

    reprojector: Reprojector = ctx["reprojector"]
    source_crs: int = ctx["source_crs"]
    jurisdiction: str = ctx["jurisdiction_code"]

    src_wkt, tgt_wkt = reprojector.transform(geom_json)

    raw_commodities = _split_list(props.get(mapping.commodities_field))
    commodity_codes, grouping = _canonicalize_commodities(raw_commodities, crosswalks)
    status = crosswalks.resolve_status(
        jurisdiction=jurisdiction,
        canonical_type="mine",
        raw=_as_str(props.get(mapping.status_field)),
    )

    canonical_attrs: dict[str, Any] = {
        "name": _as_str(props.get(mapping.name_field)),
        "status": status,
        "commodities": commodity_codes,
        "commodity_grouping": grouping,
        "operator": _as_str(props.get(mapping.operator_field)) if mapping.operator_field else None,
    }

    checksum = _compute_feature_checksum(source_id, feature_id, props, src_wkt)

    return CanonicalRecord(
        source_id=source_id,
        source_feature_id=feature_id,
        jurisdiction_code=jurisdiction,
        source_crs=source_crs,
        source_geom_wkt=src_wkt,
        geom_wkt_4326=tgt_wkt,
        source_url=_as_str(props.get(mapping.source_url_field)) if mapping.source_url_field else None,
        source_attributes=props,
        checksum=checksum,
        canonical_attrs=canonical_attrs,
    )


def _extract_mineral_occurrence(
    source_id: str,
    props: dict[str, Any],
    geom_json: dict[str, Any] | None,
    ctx: dict[str, Any],
    crosswalks: CrosswalkSet,
) -> CanonicalRecord | None:
    feature_id = _derive_source_feature_id(props)
    if feature_id is None:
        return None

    mapping = MINERAL_OCCURRENCE_FIELD_MAPPINGS.get(source_id) or _fallback_mineral_occurrence_mapping()

    reprojector: Reprojector = ctx["reprojector"]
    source_crs: int = ctx["source_crs"]
    jurisdiction: str = ctx["jurisdiction_code"]

    src_wkt, tgt_wkt = reprojector.transform(geom_json)

    primary_raw = _collect_commodity_values(props, mapping.primary_commodities_field)
    assoc_raw = (
        _collect_commodity_values(props, mapping.associated_commodities_field)
        if mapping.associated_commodities_field
        else []
    )
    primary_codes, primary_grouping = _canonicalize_commodities(primary_raw, crosswalks)
    assoc_codes, _ = _canonicalize_commodities(assoc_raw, crosswalks)

    # Prefer GROUPING over heuristic when present (applies to SMDI; BC MINFILE
    # has no grouping field so we fall through to the commodity-derived one).
    if mapping.grouping_field:
        grouping_raw = _as_str(props.get(mapping.grouping_field))
        if grouping_raw:
            grouping_match = crosswalks.resolve_commodity(grouping_raw)
            if grouping_match is not None:
                primary_grouping = grouping_match.commodity_grouping
            else:
                slug = _group_string_to_enum(grouping_raw)
                if slug:
                    primary_grouping = slug

    status = crosswalks.resolve_status(
        jurisdiction=jurisdiction,
        canonical_type="mineral_occurrence",
        raw=_as_str(props.get(mapping.status_field)),
    )

    # Production flag: parse from upstream field if present, else derive
    # from canonical status (producer / past-producer both imply historical
    # production). The explicit-field path accepts the common boolean-ish
    # encodings ('Y'/'N', 'Yes'/'No', 'True'/'False'); unrecognised truthy
    # values still flip the flag to True (SMDI's free-text production
    # descriptions behave this way).
    if mapping.production_flag_field:
        production_raw = _as_str(props.get(mapping.production_flag_field))
        production_flag = _parse_boolean_flag(production_raw)
    else:
        production_flag = status in {"producer", "past-producer"}

    # Historic names: SK lists them comma-separated in HISTORICNAMES; BC uses
    # a single "alternate name" in NAME2. `_split_list` handles both via
    # its delimiter regex.
    historic_names = (
        _split_list(props.get(mapping.historic_names_field))
        if mapping.historic_names_field
        else []
    )

    canonical_attrs: dict[str, Any] = {
        "external_id": (
            _as_str(props.get(mapping.external_id_field))
            if mapping.external_id_field
            else None
        ),
        "name": _as_str(props.get(mapping.name_field)),
        "historic_names": historic_names,
        "status": status,
        "primary_commodities": primary_codes,
        "associated_commodities": assoc_codes,
        "commodity_grouping": primary_grouping,
        "discovery_type": (
            _as_str(props.get(mapping.discovery_type_field))
            if mapping.discovery_type_field
            else None
        ),
        "production_flag": production_flag,
        "reserves_resources": (
            _as_str(props.get(mapping.reserves_resources_field))
            if mapping.reserves_resources_field
            else None
        ),
    }

    checksum = _compute_feature_checksum(source_id, feature_id, props, src_wkt)

    return CanonicalRecord(
        source_id=source_id,
        source_feature_id=feature_id,
        jurisdiction_code=jurisdiction,
        source_crs=source_crs,
        source_geom_wkt=src_wkt,
        geom_wkt_4326=tgt_wkt,
        source_url=(
            _as_str(props.get(mapping.source_url_field))
            if mapping.source_url_field
            else None
        ),
        source_attributes=props,
        checksum=checksum,
        canonical_attrs=canonical_attrs,
    )


def _group_string_to_enum(raw: str) -> str | None:
    """Map SMDI GROUPING strings to our commodity_grouping enum values."""
    s = raw.strip().lower()
    mapping = {
        "base metals": "base_metals",
        "precious metals": "precious_metals",
        "uranium": "uranium",
        "potash-salt": "potash_salt",
        "potash": "potash_salt",
        "industrial materials": "industrial_materials",
        "industrial minerals": "industrial_materials",
        "gemstones": "gemstones",
        "lithium": "lithium",
        "ree": "ree",
        "rare earth elements": "ree",
        "coal": "coal",
        "other": "other",
    }
    return mapping.get(s)


def _extract_drillhole_collar(
    source_id: str,
    props: dict[str, Any],
    geom_json: dict[str, Any] | None,
    ctx: dict[str, Any],
    crosswalks: CrosswalkSet,
) -> CanonicalRecord | None:
    feature_id = _derive_source_feature_id(props)
    if feature_id is None:
        return None

    mapping = DRILLHOLE_FIELD_MAPPINGS.get(source_id)
    if mapping is None:
        # No fallback — drillhole sources with no mapping are ingestion
        # bugs, not graceful-degradation cases. Log once per source_id so
        # a misspelled/unregistered mapping shows up in ops dashboards
        # instead of silently producing a zero-rows Silver run.
        if source_id not in _warned_missing_drillhole_mappings:
            logger.warning(
                "No DrillholeFieldMapping registered for source_id=%s — "
                "all features will be skipped. Register a mapping in "
                "DRILLHOLE_FIELD_MAPPINGS to fix.",
                source_id,
            )
            _warned_missing_drillhole_mappings.add(source_id)
        return None

    reprojector: Reprojector = ctx["reprojector"]
    source_crs: int = ctx["source_crs"]
    jurisdiction: str = ctx["jurisdiction_code"]

    src_wkt, tgt_wkt = reprojector.transform(geom_json)

    strat_depths: dict[str, Any] = {}
    for canonical_key, raw_key in mapping.strat_depth_fields.items():
        v = _as_float(props.get(raw_key))
        if v is not None:
            strat_depths[canonical_key] = v

    canonical_attrs: dict[str, Any] = {
        "drillhole_id": _as_str(props.get(mapping.drillhole_id_field)),
        "drillhole_name": (
            _as_str(props.get(mapping.drillhole_name_field))
            if mapping.drillhole_name_field
            else None
        ),
        "company": (
            _as_str(props.get(mapping.company_field))
            if mapping.company_field
            else None
        ),
        "project_name": (
            _as_str(props.get(mapping.project_name_field))
            if mapping.project_name_field
            else None
        ),
        "date_drilled": (
            _parse_date(props.get(mapping.date_drilled_field))
            if mapping.date_drilled_field
            else None
        ),
        "drill_type": (
            _as_str(props.get(mapping.drill_type_field))
            if mapping.drill_type_field
            else None
        ),
        "commodity_of_interest": (
            _split_list(props.get(mapping.commodity_of_interest_field))
            if mapping.commodity_of_interest_field
            else []
        ),
        "total_length_m": (
            _as_float(props.get(mapping.total_length_field))
            if mapping.total_length_field
            else None
        ),
        "inclination_deg": (
            _as_float(props.get(mapping.inclination_field))
            if mapping.inclination_field
            else None
        ),
        "azimuth_deg": (
            _as_float(props.get(mapping.azimuth_field))
            if mapping.azimuth_field
            else None
        ),
        "collar_elevation_m": (
            _as_float(props.get(mapping.collar_elevation_field))
            if mapping.collar_elevation_field
            else None
        ),
        "stratigraphic_depths": strat_depths,
        "core_availability": (
            _parse_core_availability(props.get(mapping.core_availability_field))
            if mapping.core_availability_field
            else "unknown"
        ),
        "core_storage": (
            _as_str(props.get(mapping.core_storage_field))
            if mapping.core_storage_field
            else None
        ),
        "disposition": (
            _as_str(props.get(mapping.disposition_field))
            if mapping.disposition_field
            else None
        ),
    }

    checksum = _compute_feature_checksum(source_id, feature_id, props, src_wkt)

    return CanonicalRecord(
        source_id=source_id,
        source_feature_id=feature_id,
        jurisdiction_code=jurisdiction,
        source_crs=source_crs,
        source_geom_wkt=src_wkt,
        geom_wkt_4326=tgt_wkt,
        source_url=(
            _as_str(props.get(mapping.source_url_field))
            if mapping.source_url_field
            else None
        ),
        source_attributes=props,
        checksum=checksum,
        canonical_attrs=canonical_attrs,
    )


def _extract_rock_sample(
    source_id: str,
    props: dict[str, Any],
    geom_json: dict[str, Any] | None,
    ctx: dict[str, Any],
    crosswalks: CrosswalkSet,
) -> CanonicalRecord | None:
    feature_id = _derive_source_feature_id(props)
    if feature_id is None:
        return None

    reprojector: Reprojector = ctx["reprojector"]
    source_crs: int = ctx["source_crs"]
    jurisdiction: str = ctx["jurisdiction_code"]
    src_wkt, tgt_wkt = reprojector.transform(geom_json)

    canonical_attrs: dict[str, Any] = {
        "station": _as_str(props.get("STATION")),
        "sample_number": _as_str(props.get("SAMPLE_NUM")),
        "geologist": _as_str(props.get("GEOLOGIST")),
        "geographic_area": _as_str(props.get("GEOG_AREA")),
        "report_number": _as_str(props.get("REPORT_NUM")),
        "map_number": _as_str(props.get("MAP_NUM")),
        "map_scale": _as_str(props.get("MAP_SCALE")),
        "nts_250k": _as_str(props.get("NTS_250K")),
        "nts_50k": _as_str(props.get("NTS_50K")),
        "date_collected": _parse_date(props.get("DATE_")),
    }

    checksum = _compute_feature_checksum(source_id, feature_id, props, src_wkt)

    return CanonicalRecord(
        source_id=source_id,
        source_feature_id=feature_id,
        jurisdiction_code=jurisdiction,
        source_crs=source_crs,
        source_geom_wkt=src_wkt,
        geom_wkt_4326=tgt_wkt,
        source_url=None,
        source_attributes=props,
        checksum=checksum,
        canonical_attrs=canonical_attrs,
    )


def _extract_mineral_disposition(
    source_id: str,
    props: dict[str, Any],
    geom_json: dict[str, Any] | None,
    ctx: dict[str, Any],
    crosswalks: CrosswalkSet,
) -> CanonicalRecord | None:
    """Tier 2 Mineral Tenure extractor.

    Two source schemas need to land in ONE canonical table:

      1. SK Mining layers 0–4 (legacy cryptic field names):
         DISPOSITIO (int), DISPOSIT_1 (disposition number str), OWNERS,
         EFFECTIVED (issue date), GOODSTANDI (expiry date), WORKWAITIN.

      2. SK Mining layers 5–8 + Crown layer 8 (modern clean names):
         DISPOSITION / DISPID, STATUS / DISPSTATUS, HOLDER / LESSEES,
         ANNIVERSARYDATE / ISSUEDATE, HECTARES / PARCELHECT.

    Rather than branch on source_id, we probe both name sets and pick
    whichever is present — MVT feeds are stable so the first non-null
    hit wins. The per-layer (disposition_type, status, commodity_type)
    tuple comes from `ctx["disposition_hint"]` injected by the caller.
    """
    feature_id = _derive_source_feature_id(props)
    if feature_id is None:
        return None

    reprojector: Reprojector = ctx["reprojector"]
    source_crs: int = ctx["source_crs"]
    jurisdiction: str = ctx["jurisdiction_code"]
    hint: dict[str, Any] = ctx.get("disposition_hint", {}) or {}
    src_wkt, tgt_wkt = reprojector.transform(geom_json)

    # Field probing — modern first (cleaner), legacy fallback.
    disposition_number = (
        _as_str(props.get("DISPOSITION"))
        or _as_str(props.get("DISPID"))
        or _as_str(props.get("DISPOSIT_1"))
    )
    holder_name = (
        _as_str(props.get("HOLDER"))
        or _as_str(props.get("LESSEES"))
        or _as_str(props.get("OWNERS"))
    )
    issue_date = _parse_date(
        props.get("ISSUEDATE") or props.get("EFFECTIVED") or props.get("ANNIVERSARYDATE")
    )
    expiry_date = _parse_date(
        props.get("GOODSTANDI") or props.get("EXPIRYDATE")
    )
    area_ha = _as_float(props.get("HECTARES") or props.get("PARCELHECT"))
    geographic_area = _as_str(props.get("GEOAREA"))

    # Status: explicit STATUS / DISPSTATUS column wins; otherwise use the
    # hint status (per-layer constant like 'active' / 'legacy' / 'pending').
    status = (
        _as_str(props.get("STATUS"))
        or _as_str(props.get("DISPSTATUS"))
        or hint.get("status")
        or "unknown"
    )
    # Normalise to the CHECK constraint's allowed set.
    status_lower = status.lower() if isinstance(status, str) else "unknown"
    status_mapped = {
        "active": "active",
        "pending": "pending",
        "legacy": "legacy",
        "lapsed": "lapsed",
        "reopening": "reopening",
        "precluded": "precluded",
        "good standing": "active",
    }.get(status_lower, "unknown")

    # Commodity codes — crown's Oil & Gas disposition has DSTRATRGHT as a
    # free-text strata-rights description; legacy mining layers don't carry
    # a commodity column (type inferred from layer). Use the hint if
    # nothing else is present.
    commodity_codes: list[str] = []
    hint_commodity = hint.get("commodity_type")
    if hint_commodity:
        commodity_codes = [hint_commodity]
    raw_commodity_text = _as_str(props.get("COMMODITY"))
    if raw_commodity_text:
        extra = [c.strip() for c in _LIST_SPLIT_RE.split(raw_commodity_text) if c.strip()]
        commodity_codes = list(dict.fromkeys(commodity_codes + extra))  # dedup, preserve order

    canonical_attrs: dict[str, Any] = {
        "disposition_number": disposition_number,
        "disposition_type":   hint.get("disposition_type") or "mineral",
        "status":             status_mapped,
        "holder_name":        holder_name,
        "issue_date":         issue_date,
        "expiry_date":        expiry_date,
        "area_ha":            area_ha,
        "commodity_codes":    commodity_codes,
        "geographic_area":    geographic_area,
    }

    checksum = _compute_feature_checksum(source_id, feature_id, props, src_wkt)

    return CanonicalRecord(
        source_id=source_id,
        source_feature_id=feature_id,
        jurisdiction_code=jurisdiction,
        source_crs=source_crs,
        source_geom_wkt=src_wkt,
        geom_wkt_4326=tgt_wkt,
        source_url=None,
        source_attributes=props,
        checksum=checksum,
        canonical_attrs=canonical_attrs,
    )


def _extract_bedrock_geology(
    source_id: str,
    props: dict[str, Any],
    geom_json: dict[str, Any] | None,
    ctx: dict[str, Any],
    crosswalks: CrosswalkSet,
) -> CanonicalRecord | None:
    """Silver extractor for SK Bedrock Geology 250K (MapServer layer 10).

    Produces a single canonical row per polygon feature with the full
    stratigraphic hierarchy (eon/era/period/group/formation/member) plus
    structural_domain and lithology.  scale is always '250K'.
    """
    feature_id = _derive_source_feature_id(props)
    if feature_id is None:
        return None

    reprojector: Reprojector = ctx["reprojector"]
    source_crs: int = ctx["source_crs"]
    jurisdiction: str = ctx["jurisdiction_code"]
    src_wkt, tgt_wkt = reprojector.transform(geom_json)

    unit_code = _as_str(props.get("ROCK_CODE"))
    if unit_code is None:
        return None  # NOT NULL constraint — skip rather than insert a blank row

    formation = _as_str(props.get("FORMATION"))
    member = _as_str(props.get("MEMBER"))

    # unit_name: NAME → formation[/member] → unit_code
    name_raw = _as_str(props.get("NAME"))
    if name_raw:
        unit_name: str = name_raw
    elif formation:
        unit_name = f"{formation} / {member}" if member else formation
    else:
        unit_name = unit_code

    canonical_attrs: dict[str, Any] = {
        "unit_code":         unit_code,
        "unit_name":         unit_name,
        "eon":               _as_str(props.get("EON")),
        "era":               _as_str(props.get("ERA")),
        "period":            _as_str(props.get("PERIOD")),
        "group_name":        _as_str(props.get("GROUP_")),
        "formation":         formation,
        "member":            member,
        "structural_domain": _as_str(props.get("DOMAIN")),
        "lithology":         _as_str(props.get("LITHOLOGY")),
        "scale":             "250K",
    }

    checksum = _compute_feature_checksum(source_id, feature_id, props, src_wkt)

    return CanonicalRecord(
        source_id=source_id,
        source_feature_id=feature_id,
        jurisdiction_code=jurisdiction,
        source_crs=source_crs,
        source_geom_wkt=src_wkt,
        geom_wkt_4326=tgt_wkt,
        source_url=None,
        source_attributes=props,
        checksum=checksum,
        canonical_attrs=canonical_attrs,
    )


def _extract_assessment_survey(
    source_id: str,
    props: dict[str, Any],
    geom_json: dict[str, Any] | None,
    ctx: dict[str, Any],
    crosswalks: CrosswalkSet,
) -> CanonicalRecord | None:
    feature_id = _derive_source_feature_id(props)
    if feature_id is None:
        return None

    reprojector: Reprojector = ctx["reprojector"]
    source_crs: int = ctx["source_crs"]
    jurisdiction: str = ctx["jurisdiction_code"]
    src_wkt, tgt_wkt = reprojector.transform(geom_json)

    # Derive survey_type from the source_id suffix.
    survey_type = "unknown"
    sid_lower = source_id.lower()
    if "underground" in sid_lower:
        survey_type = "underground"
    elif "ground" in sid_lower:
        survey_type = "ground"
    elif "airborne" in sid_lower:
        survey_type = "airborne"

    canonical_attrs: dict[str, Any] = {
        "survey_type": survey_type,
    }

    checksum = _compute_feature_checksum(source_id, feature_id, props, src_wkt)

    return CanonicalRecord(
        source_id=source_id,
        source_feature_id=feature_id,
        jurisdiction_code=jurisdiction,
        source_crs=source_crs,
        source_geom_wkt=src_wkt,
        geom_wkt_4326=tgt_wkt,
        source_url=None,
        source_attributes=props,
        checksum=checksum,
        canonical_attrs=canonical_attrs,
    )


def _extract_resource_potential_zone(
    source_id: str,
    props: dict[str, Any],
    geom_json: dict[str, Any] | None,
    ctx: dict[str, Any],
    crosswalks: CrosswalkSet,
) -> CanonicalRecord | None:
    feature_id = _derive_source_feature_id(props)
    if feature_id is None:
        return None

    reprojector: Reprojector = ctx["reprojector"]
    source_crs: int = ctx["source_crs"]
    jurisdiction: str = ctx["jurisdiction_code"]

    commodity_code: str = ctx["commodity_code"]
    match = crosswalks.resolve_commodity(commodity_code)
    commodity_grouping = match.commodity_grouping if match else None
    commodity_canonical = match.canonical_code if match else commodity_code

    src_wkt, tgt_wkt = reprojector.transform(geom_json)

    # Common Esri/Saskatchewan rank field names. We're permissive and take
    # the first one that parses cleanly.
    rank_candidates = ("POTENTIAL", "POTENTIAL_RANK", "RANK", "Potential")
    potential_rank: int | None = None
    for key in rank_candidates:
        if key in props:
            potential_rank = _as_int(props[key])
            if potential_rank is not None:
                break

    canonical_attrs: dict[str, Any] = {
        "commodity": commodity_canonical,
        "commodity_grouping": commodity_grouping,
        "potential_rank": potential_rank,
        "methodology_ref": _as_str(props.get("SOURCE") or props.get("METHODOLOGY")),
    }

    checksum = _compute_feature_checksum(source_id, feature_id, props, src_wkt)

    return CanonicalRecord(
        source_id=source_id,
        source_feature_id=feature_id,
        jurisdiction_code=jurisdiction,
        source_crs=source_crs,
        source_geom_wkt=src_wkt,
        geom_wkt_4326=tgt_wkt,
        source_url=None,
        source_attributes=props,
        checksum=checksum,
        canonical_attrs=canonical_attrs,
    )


# ---------------------------------------------------------------------------
# Staging / UPSERT / history
# ---------------------------------------------------------------------------

@dataclass
class UpsertStats:
    total_features: int = 0
    skipped_no_feature_id: int = 0
    skipped_no_geometry: int = 0
    inserted_new: int = 0
    updated_changed: int = 0
    touched_unchanged: int = 0


def _upsert_batch(
    *,
    postgres: PostgresResource,
    spec: CanonicalSpec,
    records: list[CanonicalRecord],
    context: AssetExecutionContext,
    batch_size: int,
) -> UpsertStats:
    """Stage → history → upsert pipeline for one source_id's Silver run."""
    stats = UpsertStats(total_features=len(records))
    if not records:
        return stats

    # Compose column list. Staging has the same shape as the target canonical
    # table minus the PG-managed bookkeeping columns.
    columns = [
        "id",                   # Python-generated UUID; discarded on conflict.
        "source_id",
        "source_feature_id",
        "jurisdiction_code",
        *spec.canonical_columns,
        "source_crs",
        "source_geom_wkt",
        "source_url",
        "source_attributes",
        "geom_wkt_4326",
        "checksum",
    ]

    columns_sql = ", ".join(columns)
    placeholders = ", ".join(f"%({c})s" for c in columns)

    # STAGING TABLE: flat types only (geometry carried as WKT text).
    stage_def_cols = ", ".join(
        f"{c} {_stage_column_type(c, spec)}" for c in columns
    )

    with postgres.get_connection() as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            # Note: PG default temp_buffers (8 MB) overflows on bulk
            # ST_GeomFromText over large MULTIPOLYGON corpora (bedrock_geology
            # = 9,596 polygons, mineral_disposition = 19,001 across 10 layers).
            # Mitigated by raising the server-level default to 256 MB via
            # ALTER SYSTEM on 2026-05-25; we can't SET it per-session here
            # because pgbouncer-recycled PG backends may have already touched
            # temp tables in earlier transactions (PG errors out on a change
            # after first temp-table use).
            cur.execute(
                f"""
                CREATE TEMP TABLE stage_pg (
                    {stage_def_cols},
                    PRIMARY KEY (source_id, source_feature_id)
                ) ON COMMIT DROP
                """
            )

            # Bulk-insert records into staging.
            insert_stage_sql = (
                f"INSERT INTO stage_pg ({columns_sql}) VALUES ({placeholders})"
            )
            params_seq = [_record_to_params(r, spec) for r in records]
            psycopg2.extras.execute_batch(
                cur, insert_stage_sql, params_seq, page_size=batch_size
            )
            context.log.info(
                "Silver[%s]: staged %d records", spec.target_table, len(records),
            )

            # 1) History insert for changed features (old live row → history,
            #    superseded_at = NOW).
            history_select_cols = [
                "id",
                "jurisdiction_code",
                "source_id",
                "source_feature_id",
                *spec.canonical_columns,
                "source_crs",
                "source_geom_wkt",
                "source_url",
                "source_attributes",
                "geom",
                "checksum",
            ]
            cur.execute(
                f"""
                INSERT INTO {spec.history_table} (
                    {', '.join(history_select_cols)}, superseded_at
                )
                SELECT {', '.join('l.' + c for c in history_select_cols)}, NOW()
                  FROM {spec.target_table} l
                  JOIN stage_pg s
                    ON s.source_id = l.source_id
                   AND s.source_feature_id = l.source_feature_id
                 WHERE l.checksum IS DISTINCT FROM s.checksum
                """
            )
            history_rows = cur.rowcount
            context.log.info(
                "Silver[%s]: appended %d history rows", spec.target_table, history_rows,
            )

            # 2) Stats pre-upsert — how many live rows match on PK, and how
            #    many of those have an unchanged checksum? The enclosing
            #    cursor is the default tuple cursor (we need psycopg2's
            #    standard cursor for execute_batch on the INSERT above, not
            #    RealDictCursor), so the probe reads by column index.
            cur.execute(
                f"""
                SELECT
                  COUNT(*) FILTER (WHERE l.checksum IS NULL) AS new_cnt,
                  COUNT(*) FILTER (WHERE l.checksum IS NOT NULL AND l.checksum = s.checksum) AS unchanged_cnt,
                  COUNT(*) FILTER (WHERE l.checksum IS NOT NULL AND l.checksum IS DISTINCT FROM s.checksum) AS changed_cnt
                FROM stage_pg s
                LEFT JOIN {spec.target_table} l
                       ON l.source_id = s.source_id
                      AND l.source_feature_id = s.source_feature_id
                """
            )
            new_cnt, unchanged_cnt, changed_cnt = cur.fetchone() or (0, 0, 0)
            stats.inserted_new = int(new_cnt or 0)
            stats.updated_changed = int(changed_cnt or 0)
            stats.touched_unchanged = int(unchanged_cnt or 0)

            # 3) UPSERT live. Geometry constructed here via
            #    ST_SetSRID(ST_GeomFromText(wkt), 4326).
            upsert_update_setters = ",\n                ".join(
                [
                    f"{col} = EXCLUDED.{col}" for col in [*spec.canonical_columns,
                                                           "source_crs", "source_geom_wkt",
                                                           "source_url", "source_attributes",
                                                           "geom", "checksum"]
                ]
                + [
                    "last_seen_at = NOW()",
                    (
                        f"updated_at = CASE WHEN {spec.target_table}.checksum IS DISTINCT FROM EXCLUDED.checksum "
                        "THEN NOW() ELSE " + spec.target_table + ".updated_at END"
                    ),
                ]
            )
            insert_col_list = [
                "id",
                "source_id",
                "source_feature_id",
                "jurisdiction_code",
                *spec.canonical_columns,
                "source_crs",
                "source_geom_wkt",
                "source_url",
                "source_attributes",
                "geom",
                "checksum",
                "first_seen_at",
                "last_seen_at",
                "created_at",
                "updated_at",
            ]
            select_exprs = [
                "s.id",
                "s.source_id",
                "s.source_feature_id",
                "s.jurisdiction_code",
                *[f"s.{c}" for c in spec.canonical_columns],
                "s.source_crs",
                "s.source_geom_wkt",
                "s.source_url",
                "s.source_attributes",
                "CASE WHEN s.geom_wkt_4326 IS NULL THEN NULL "
                "ELSE ST_SetSRID(ST_GeomFromText(s.geom_wkt_4326), 4326) END AS geom",
                "s.checksum",
                "NOW()",
                "NOW()",
                "NOW()",
                "NOW()",
            ]

            cur.execute(
                f"""
                INSERT INTO {spec.target_table} ({', '.join(insert_col_list)})
                SELECT {', '.join(select_exprs)}
                  FROM stage_pg s
                ON CONFLICT (source_id, source_feature_id) DO UPDATE SET
                    {upsert_update_setters}
                """
            )
            context.log.info(
                "Silver[%s]: upsert complete (new=%d changed=%d unchanged=%d)",
                spec.target_table,
                stats.inserted_new,
                stats.updated_changed,
                stats.touched_unchanged,
            )

        conn.commit()

    return stats


def _stage_column_type(col: str, spec: CanonicalSpec) -> str:
    """Return a permissive PG type for each staging column.

    All canonical columns use TEXT / JSONB / TEXT[] / NUMERIC / DATE — we let
    PG implicit-cast on INSERT INTO target … SELECT FROM stage.
    """
    generic = {
        "id": "UUID NOT NULL",
        "source_id": "VARCHAR(64) NOT NULL",
        "source_feature_id": "VARCHAR(128) NOT NULL",
        "jurisdiction_code": "VARCHAR(16) NOT NULL",
        "source_crs": "INT",
        "source_geom_wkt": "TEXT",
        "source_url": "TEXT",
        "source_attributes": "JSONB NOT NULL DEFAULT '{}'::jsonb",
        "geom_wkt_4326": "TEXT",
        "checksum": "CHAR(64) NOT NULL",
    }
    if col in generic:
        return generic[col]

    # Per-entity canonical columns.
    per_table: dict[tuple[str, str], str] = {
        ("public_geo.pg_mine", "name"): "VARCHAR(512)",
        ("public_geo.pg_mine", "status"): "VARCHAR(32)",
        ("public_geo.pg_mine", "commodities"): "TEXT[]",
        ("public_geo.pg_mine", "commodity_grouping"): "VARCHAR(32)",
        ("public_geo.pg_mine", "operator"): "VARCHAR(512)",

        ("public_geo.pg_mineral_occurrence", "external_id"): "VARCHAR(64)",
        ("public_geo.pg_mineral_occurrence", "name"): "VARCHAR(512)",
        ("public_geo.pg_mineral_occurrence", "historic_names"): "TEXT[]",
        ("public_geo.pg_mineral_occurrence", "status"): "VARCHAR(32)",
        ("public_geo.pg_mineral_occurrence", "primary_commodities"): "TEXT[]",
        ("public_geo.pg_mineral_occurrence", "associated_commodities"): "TEXT[]",
        ("public_geo.pg_mineral_occurrence", "commodity_grouping"): "VARCHAR(32)",
        ("public_geo.pg_mineral_occurrence", "discovery_type"): "VARCHAR(128)",
        ("public_geo.pg_mineral_occurrence", "production_flag"): "BOOLEAN",
        ("public_geo.pg_mineral_occurrence", "reserves_resources"): "TEXT",

        ("public_geo.pg_drillhole_collar", "drillhole_id"): "VARCHAR(128)",
        ("public_geo.pg_drillhole_collar", "drillhole_name"): "VARCHAR(512)",
        ("public_geo.pg_drillhole_collar", "company"): "VARCHAR(512)",
        ("public_geo.pg_drillhole_collar", "project_name"): "VARCHAR(512)",
        ("public_geo.pg_drillhole_collar", "date_drilled"): "DATE",
        ("public_geo.pg_drillhole_collar", "drill_type"): "VARCHAR(128)",
        ("public_geo.pg_drillhole_collar", "commodity_of_interest"): "TEXT[]",
        ("public_geo.pg_drillhole_collar", "total_length_m"): "NUMERIC(10,2)",
        ("public_geo.pg_drillhole_collar", "inclination_deg"): "NUMERIC(6,2)",
        ("public_geo.pg_drillhole_collar", "azimuth_deg"): "NUMERIC(6,2)",
        ("public_geo.pg_drillhole_collar", "collar_elevation_m"): "NUMERIC(10,2)",
        ("public_geo.pg_drillhole_collar", "stratigraphic_depths"): "JSONB",
        ("public_geo.pg_drillhole_collar", "core_availability"): "VARCHAR(32)",
        ("public_geo.pg_drillhole_collar", "core_storage"): "VARCHAR(512)",
        ("public_geo.pg_drillhole_collar", "disposition"): "VARCHAR(128)",

        ("public_geo.pg_rock_sample", "station"): "VARCHAR(128)",
        ("public_geo.pg_rock_sample", "sample_number"): "VARCHAR(128)",
        ("public_geo.pg_rock_sample", "geologist"): "VARCHAR(255)",
        ("public_geo.pg_rock_sample", "geographic_area"): "VARCHAR(255)",
        ("public_geo.pg_rock_sample", "report_number"): "VARCHAR(128)",
        ("public_geo.pg_rock_sample", "map_number"): "VARCHAR(128)",
        ("public_geo.pg_rock_sample", "map_scale"): "VARCHAR(64)",
        ("public_geo.pg_rock_sample", "nts_250k"): "VARCHAR(16)",
        ("public_geo.pg_rock_sample", "nts_50k"): "VARCHAR(128)",
        ("public_geo.pg_rock_sample", "date_collected"): "DATE",

        ("public_geo.pg_assessment_survey", "survey_type"): "VARCHAR(32)",

        ("public_geo.pg_resource_potential_zone", "commodity"): "VARCHAR(64)",
        ("public_geo.pg_resource_potential_zone", "commodity_grouping"): "VARCHAR(32)",
        ("public_geo.pg_resource_potential_zone", "potential_rank"): "SMALLINT",
        ("public_geo.pg_resource_potential_zone", "methodology_ref"): "TEXT",

        # Tier 2 — Bedrock Geology
        ("public_geo.pg_bedrock_geology", "unit_code"):         "VARCHAR(16)",
        ("public_geo.pg_bedrock_geology", "unit_name"):         "VARCHAR(128)",
        ("public_geo.pg_bedrock_geology", "eon"):               "VARCHAR(32)",
        ("public_geo.pg_bedrock_geology", "era"):               "VARCHAR(64)",
        ("public_geo.pg_bedrock_geology", "period"):            "VARCHAR(64)",
        ("public_geo.pg_bedrock_geology", "group_name"):        "VARCHAR(64)",
        ("public_geo.pg_bedrock_geology", "formation"):         "VARCHAR(64)",
        ("public_geo.pg_bedrock_geology", "member"):            "VARCHAR(64)",
        ("public_geo.pg_bedrock_geology", "structural_domain"): "VARCHAR(64)",
        ("public_geo.pg_bedrock_geology", "lithology"):         "VARCHAR(256)",
        ("public_geo.pg_bedrock_geology", "scale"):             "VARCHAR(8)",

        # Tier 2 — Mineral Tenure / Dispositions
        ("public_geo.pg_mineral_disposition", "disposition_number"): "VARCHAR(64)",
        ("public_geo.pg_mineral_disposition", "disposition_type"): "VARCHAR(32)",
        ("public_geo.pg_mineral_disposition", "status"): "VARCHAR(32)",
        ("public_geo.pg_mineral_disposition", "holder_name"): "VARCHAR(512)",
        ("public_geo.pg_mineral_disposition", "issue_date"): "DATE",
        ("public_geo.pg_mineral_disposition", "expiry_date"): "DATE",
        ("public_geo.pg_mineral_disposition", "area_ha"): "NUMERIC(14,2)",
        ("public_geo.pg_mineral_disposition", "commodity_codes"): "TEXT[]",
        ("public_geo.pg_mineral_disposition", "geographic_area"): "VARCHAR(128)",
    }
    return per_table.get((spec.target_table, col), "TEXT")


def _record_to_params(r: CanonicalRecord, spec: CanonicalSpec) -> dict[str, Any]:
    params: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "source_id": r.source_id,
        "source_feature_id": r.source_feature_id,
        "jurisdiction_code": r.jurisdiction_code,
        "source_crs": r.source_crs,
        "source_geom_wkt": r.source_geom_wkt,
        "source_url": r.source_url,
        "source_attributes": psycopg2.extras.Json(r.source_attributes),
        "geom_wkt_4326": r.geom_wkt_4326,
        "checksum": r.checksum,
    }
    for col in spec.canonical_columns:
        val = r.canonical_attrs.get(col)
        # psycopg2 handles list→array automatically via adapt; JSONB columns
        # need an explicit Json wrap.
        if isinstance(val, dict):
            params[col] = psycopg2.extras.Json(val)
        else:
            params[col] = val
    return params


# ---------------------------------------------------------------------------
# Driver — one source_id worth of Silver work
# ---------------------------------------------------------------------------

def _run_silver_for_source(
    *,
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
    source_id: str,
    spec: CanonicalSpec,
    crosswalks: CrosswalkSet,
    extra_ctx: dict[str, Any] | None = None,
) -> UpsertStats:
    """Load latest Bronze, extract, upsert. Returns per-source stats."""
    # Source row → CRS + jurisdiction.
    with postgres.get_cursor() as cur:
        cur.execute(
            """
            SELECT s.source_id, s.jurisdiction_code, s.canonical_type,
                   s.source_crs,
                   j.default_source_crs AS jurisdiction_default_crs
              FROM public_geo.sources s
              JOIN public_geo.jurisdictions j
                   ON j.jurisdiction_code = s.jurisdiction_code
             WHERE s.source_id = %s
            """,
            (source_id,),
        )
        src_row = cur.fetchone()
    if src_row is None:
        raise RuntimeError(f"No registry row for source_id={source_id!r}.")

    jurisdiction_code: str = src_row["jurisdiction_code"]
    source_crs = int(src_row["source_crs"] or src_row["jurisdiction_default_crs"] or 4326)

    # Locate latest Bronze object for this source.
    object_name = (
        config.override_object_name
        or _find_latest_bronze(
            minio, jurisdiction_code=jurisdiction_code, source_id=source_id,
        )
    )
    if not object_name:
        context.log.warning(
            "Silver: no Bronze object found for source_id=%s (jurisdiction=%s). "
            "Skipping — run the Bronze asset first.",
            source_id, jurisdiction_code,
        )
        return UpsertStats(total_features=0)

    context.log.info(
        "Silver[%s]: loading Bronze %s", source_id, object_name,
    )
    fc = _download_feature_collection(minio, object_name=object_name)
    features = fc.get("features") or []

    reprojector = Reprojector(source_crs=source_crs, target_crs=4326)
    base_ctx: dict[str, Any] = {
        "reprojector": reprojector,
        "source_crs": source_crs,
        "jurisdiction_code": jurisdiction_code,
    }
    if extra_ctx:
        base_ctx.update(extra_ctx)

    records: list[CanonicalRecord] = []
    skipped_no_id = 0
    skipped_no_geom = 0

    for feat in features:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        geom_json = feat.get("geometry")
        rec = spec.extractor(source_id, props, geom_json, base_ctx, crosswalks)
        if rec is None:
            skipped_no_id += 1
            continue
        if rec.geom_wkt_4326 is None:
            # No geometry — canonical tables permit NULL geom, but for
            # spatial-first entities (mine/occurrence/drillhole) a NULL
            # collar point is unusable for retrieval. Keep the row but log.
            skipped_no_geom += 1
        records.append(rec)

    stats = _upsert_batch(
        postgres=postgres,
        spec=spec,
        records=records,
        context=context,
        batch_size=config.batch_size,
    )
    stats.skipped_no_feature_id = skipped_no_id
    stats.skipped_no_geometry = skipped_no_geom
    return stats


# ---------------------------------------------------------------------------
# CanonicalSpec instances (one per target table)
# ---------------------------------------------------------------------------

SPEC_MINE = CanonicalSpec(
    target_table="public_geo.pg_mine",
    history_table="public_geo.pg_mine_history",
    geometry_type="POINT",
    canonical_columns=[
        "name",
        "status",
        "commodities",
        "commodity_grouping",
        "operator",
    ],
    extractor=_extract_mine,
)

SPEC_MINERAL_OCCURRENCE = CanonicalSpec(
    target_table="public_geo.pg_mineral_occurrence",
    history_table="public_geo.pg_mineral_occurrence_history",
    geometry_type="POINT",
    canonical_columns=[
        "external_id",
        "name",
        "historic_names",
        "status",
        "primary_commodities",
        "associated_commodities",
        "commodity_grouping",
        "discovery_type",
        "production_flag",
        "reserves_resources",
    ],
    extractor=_extract_mineral_occurrence,
)

SPEC_DRILLHOLE_COLLAR = CanonicalSpec(
    target_table="public_geo.pg_drillhole_collar",
    history_table="public_geo.pg_drillhole_collar_history",
    geometry_type="POINT",
    canonical_columns=[
        "drillhole_id",
        "drillhole_name",
        "company",
        "project_name",
        "date_drilled",
        "drill_type",
        "commodity_of_interest",
        "total_length_m",
        "inclination_deg",
        "azimuth_deg",
        "collar_elevation_m",
        "stratigraphic_depths",
        "core_availability",
        "core_storage",
        "disposition",
    ],
    extractor=_extract_drillhole_collar,
)

SPEC_BEDROCK_GEOLOGY = CanonicalSpec(
    target_table="public_geo.pg_bedrock_geology",
    history_table="public_geo.pg_bedrock_geology_history",
    geometry_type="MULTIPOLYGON",
    canonical_columns=[
        "unit_code",
        "unit_name",
        "eon",
        "era",
        "period",
        "group_name",
        "formation",
        "member",
        "structural_domain",
        "lithology",
        "scale",
    ],
    extractor=lambda source_id, props, geom_json, ctx, crosswalks: _extract_bedrock_geology(
        source_id, props, geom_json, ctx, crosswalks
    ),
)

SPEC_MINERAL_DISPOSITION = CanonicalSpec(
    target_table="public_geo.pg_mineral_disposition",
    history_table="public_geo.pg_mineral_disposition_history",
    geometry_type="MULTIPOLYGON",
    canonical_columns=[
        "disposition_number",
        "disposition_type",
        "status",
        "holder_name",
        "issue_date",
        "expiry_date",
        "area_ha",
        "commodity_codes",
        "geographic_area",
    ],
    extractor=lambda source_id, props, geom_json, ctx, crosswalks: _extract_mineral_disposition(
        source_id, props, geom_json, ctx, crosswalks
    ),
)

SPEC_ROCK_SAMPLE = CanonicalSpec(
    target_table="public_geo.pg_rock_sample",
    history_table="public_geo.pg_rock_sample_history",
    geometry_type="POINT",
    canonical_columns=[
        "station",
        "sample_number",
        "geologist",
        "geographic_area",
        "report_number",
        "map_number",
        "map_scale",
        "nts_250k",
        "nts_50k",
        "date_collected",
    ],
    extractor=lambda source_id, props, geom_json, ctx, crosswalks: _extract_rock_sample(
        source_id, props, geom_json, ctx, crosswalks
    ),
)

SPEC_ASSESSMENT_SURVEY = CanonicalSpec(
    target_table="public_geo.pg_assessment_survey",
    history_table="public_geo.pg_assessment_survey_history",
    geometry_type="MULTIPOLYGON",
    canonical_columns=[
        "survey_type",
    ],
    extractor=lambda source_id, props, geom_json, ctx, crosswalks: _extract_assessment_survey(
        source_id, props, geom_json, ctx, crosswalks
    ),
)

SPEC_RESOURCE_POTENTIAL_ZONE = CanonicalSpec(
    target_table="public_geo.pg_resource_potential_zone",
    history_table="public_geo.pg_resource_potential_zone_history",
    geometry_type="MULTIPOLYGON",
    canonical_columns=[
        "commodity",
        "commodity_grouping",
        "potential_rank",
        "methodology_ref",
    ],
    extractor=_extract_resource_potential_zone,
)


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

def _metadata_from_stats(
    stats: UpsertStats, *, source_id: str, canonical_type: str
) -> dict[str, Any]:
    return {
        "source_id":            MetadataValue.text(source_id),
        "canonical_type":       MetadataValue.text(canonical_type),
        "total_features":       MetadataValue.int(stats.total_features),
        "inserted_new":         MetadataValue.int(stats.inserted_new),
        "updated_changed":      MetadataValue.int(stats.updated_changed),
        "touched_unchanged":    MetadataValue.int(stats.touched_unchanged),
        "skipped_no_feature_id": MetadataValue.int(stats.skipped_no_feature_id),
        "skipped_no_geometry":  MetadataValue.int(stats.skipped_no_geometry),
    }


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_sk_mine_loc"],
    description=(
        "Reproject + crosswalk + upsert Saskatchewan Mine Locations into "
        "public_geo.pg_mine. Writes history rows on checksum change; "
        "missing records are NOT hard-deleted (last_seen_at drift is the "
        "staleness signal, plan §05b)."
    ),
)
def silver_pg_ca_sk_mine_loc(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)
    stats = _run_silver_for_source(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-MINE-LOC",
        spec=SPEC_MINE,
        crosswalks=crosswalks,
    )
    return MaterializeResult(
        metadata=_metadata_from_stats(stats, source_id="CA-SK-MINE-LOC", canonical_type="mine"),
    )


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_sk_smdi"],
    description=(
        "Reproject + crosswalk + upsert the Saskatchewan Mineral Deposits "
        "Index (SMDI) into public_geo.pg_mineral_occurrence."
    ),
)
def silver_pg_ca_sk_smdi(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)
    stats = _run_silver_for_source(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-SMDI",
        spec=SPEC_MINERAL_OCCURRENCE,
        crosswalks=crosswalks,
    )
    return MaterializeResult(
        metadata=_metadata_from_stats(
            stats, source_id="CA-SK-SMDI", canonical_type="mineral_occurrence",
        ),
    )


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_sk_drillhole"],
    description=(
        "Reproject + normalize + upsert the Saskatchewan public drillhole "
        "compilation into public_geo.pg_drillhole_collar. Stratigraphic "
        "depths are packed into a JSONB column; downhole assays/lithology "
        "arrive separately via SMAD documents (cross-corpus linker §07)."
    ),
)
def silver_pg_ca_sk_drillhole(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)
    stats = _run_silver_for_source(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-DRILLHOLE",
        spec=SPEC_DRILLHOLE_COLLAR,
        crosswalks=crosswalks,
    )
    return MaterializeResult(
        metadata=_metadata_from_stats(
            stats, source_id="CA-SK-DRILLHOLE", canonical_type="drillhole_collar",
        ),
    )


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_sk_rock_samples"],
    description="Reproject + upsert Saskatchewan Government Rock Samples into pg_rock_sample.",
)
def silver_pg_ca_sk_rock_samples(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)
    stats = _run_silver_for_source(
        context=context, config=config, postgres=postgres, minio=minio,
        source_id="CA-SK-ROCK-SAMPLES", spec=SPEC_ROCK_SAMPLE, crosswalks=crosswalks,
    )
    return MaterializeResult(
        metadata=_metadata_from_stats(stats, source_id="CA-SK-ROCK-SAMPLES", canonical_type="rock_sample"),
    )


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_sk_assessment_underground"],
    description="Reproject + upsert SMAD underground survey footprints into pg_assessment_survey.",
)
def silver_pg_ca_sk_assessment_underground(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)
    stats = _run_silver_for_source(
        context=context, config=config, postgres=postgres, minio=minio,
        source_id="CA-SK-ASSESSMENT-UNDERGROUND", spec=SPEC_ASSESSMENT_SURVEY, crosswalks=crosswalks,
    )
    return MaterializeResult(
        metadata=_metadata_from_stats(stats, source_id="CA-SK-ASSESSMENT-UNDERGROUND", canonical_type="assessment_survey"),
    )


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_sk_assessment_ground"],
    description="Reproject + upsert SMAD ground survey footprints into pg_assessment_survey.",
)
def silver_pg_ca_sk_assessment_ground(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)
    stats = _run_silver_for_source(
        context=context, config=config, postgres=postgres, minio=minio,
        source_id="CA-SK-ASSESSMENT-GROUND", spec=SPEC_ASSESSMENT_SURVEY, crosswalks=crosswalks,
    )
    return MaterializeResult(
        metadata=_metadata_from_stats(stats, source_id="CA-SK-ASSESSMENT-GROUND", canonical_type="assessment_survey"),
    )


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_sk_assessment_airborne"],
    description="Reproject + upsert SMAD airborne survey footprints into pg_assessment_survey.",
)
def silver_pg_ca_sk_assessment_airborne(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)
    stats = _run_silver_for_source(
        context=context, config=config, postgres=postgres, minio=minio,
        source_id="CA-SK-ASSESSMENT-AIRBORNE", spec=SPEC_ASSESSMENT_SURVEY, crosswalks=crosswalks,
    )
    return MaterializeResult(
        metadata=_metadata_from_stats(stats, source_id="CA-SK-ASSESSMENT-AIRBORNE", canonical_type="assessment_survey"),
    )


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_bc_minfile"],
    description=(
        "Reproject + crosswalk + upsert BC MINFILE into "
        "public_geo.pg_mineral_occurrence. Uses the "
        "CA-BC-MINFILE FieldMapping (MINFILE_NUMBER → external_id, MINFILE_NAME1 → name, "
        "STATUS_DESC → status, COMMODITIES → primary_commodities). "
        "Reprojects from EPSG:3005 (BC Albers) → EPSG:4326."
    ),
)
def silver_pg_ca_bc_minfile(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)
    stats = _run_silver_for_source(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-BC-MINFILE",
        spec=SPEC_MINERAL_OCCURRENCE,
        crosswalks=crosswalks,
    )
    return MaterializeResult(
        metadata=_metadata_from_stats(
            stats, source_id="CA-BC-MINFILE", canonical_type="mineral_occurrence",
        ),
    )


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_sk_resource_potential"],
    description=(
        "Iterate over the per-commodity CA-SK-RESOURCE-POTENTIAL-* source_ids "
        "auto-registered by Bronze. Reproject polygon rings 2957→4326 and "
        "upsert into the unified public_geo.pg_resource_potential_zone "
        "canonical table with `commodity` populated from the source slug "
        "(plan §11 item 3)."
    ),
)
def silver_pg_ca_sk_resource_potential(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)

    with postgres.get_cursor() as cur:
        cur.execute(
            """
            SELECT source_id
              FROM public_geo.sources
             WHERE jurisdiction_code = 'CA-SK'
               AND canonical_type    = 'resource_potential_zone'
               AND layer_index       IS NOT NULL
             ORDER BY source_id
            """,
        )
        rows = cur.fetchall()

    if not rows:
        context.log.warning(
            "Silver resource_potential: no per-commodity sources registered. "
            "Run bronze_pg_ca_sk_resource_potential first.",
        )
        return MaterializeResult(
            metadata={
                "source_id": MetadataValue.text("CA-SK-RESOURCE-POTENTIAL"),
                "canonical_type": MetadataValue.text("resource_potential_zone"),
                "sources_processed": MetadataValue.int(0),
                "total_features": MetadataValue.int(0),
            }
        )

    per_source: list[dict[str, Any]] = []
    totals = UpsertStats()

    for row in rows:
        source_id: str = row["source_id"]
        commodity_code = source_id.rsplit("-", 1)[-1].lower()

        stats = _run_silver_for_source(
            context=context,
            config=config,
            postgres=postgres,
            minio=minio,
            source_id=source_id,
            spec=SPEC_RESOURCE_POTENTIAL_ZONE,
            crosswalks=crosswalks,
            extra_ctx={"commodity_code": commodity_code},
        )
        totals.total_features += stats.total_features
        totals.inserted_new += stats.inserted_new
        totals.updated_changed += stats.updated_changed
        totals.touched_unchanged += stats.touched_unchanged
        totals.skipped_no_feature_id += stats.skipped_no_feature_id
        totals.skipped_no_geometry += stats.skipped_no_geometry
        per_source.append(
            {
                "source_id": source_id,
                "commodity": commodity_code,
                "total_features": stats.total_features,
                "inserted_new": stats.inserted_new,
                "updated_changed": stats.updated_changed,
                "touched_unchanged": stats.touched_unchanged,
            }
        )

    meta = _metadata_from_stats(
        totals,
        source_id="CA-SK-RESOURCE-POTENTIAL",
        canonical_type="resource_potential_zone",
    )
    meta["sources_processed"] = MetadataValue.int(len(per_source))
    meta["per_source"] = MetadataValue.json(per_source)
    return MaterializeResult(metadata=meta)


# ---------------------------------------------------------------------------
# Tier 2 — Mineral Tenure / Dispositions (plan Phase 2)
# ---------------------------------------------------------------------------
#
# Mirrors the resource_potential multi-source pattern: Bronze auto-registers
# one source_id per layer (CA-SK-MINERAL-DISPOSITION-MINING-0 through -8
# plus -CROWN-OIL-GAS), then this Silver asset sweeps them all and unifies
# into the single pg_mineral_disposition table. The (disposition_type,
# status, commodity_type) tuple per layer is read from the source row's
# notes / the Bronze-injected extra_ctx (disposition_hint), since those
# attributes aren't in the raw ArcGIS feature properties for legacy layers.

# Hint dict per derived source_id suffix. Bronze writes this into the
# source row's `notes` column; Silver reads it back without touching
# Bronze. If someone adds a new layer, extend this map.
_MINERAL_DISPOSITION_LAYER_HINTS: dict[str, dict[str, str]] = {
    "MINING-0": {"disposition_type": "mineral", "status": "active"},
    "MINING-1": {"disposition_type": "mineral", "status": "legacy"},
    "MINING-2": {"disposition_type": "mineral", "status": "pending"},
    "MINING-3": {"disposition_type": "mineral", "status": "reopening"},
    "MINING-4": {"disposition_type": "mineral", "status": "lapsed"},
    "MINING-5": {"disposition_type": "potash",  "status": "active", "commodity_type": "potash"},
    "MINING-6": {"disposition_type": "alkali",  "status": "active", "commodity_type": "alkali"},
    "MINING-7": {"disposition_type": "coal",    "status": "active", "commodity_type": "coal"},
    "MINING-8": {"disposition_type": "quarry",  "status": "active", "commodity_type": "quarry"},
    "CROWN-OIL-GAS": {"disposition_type": "oil_gas", "status": "active", "commodity_type": "oil_gas"},
}


def _hint_for_source_id(source_id: str) -> dict[str, str]:
    """Map derived source_id → (disposition_type, status, [commodity_type]).

    Extracts the suffix after `CA-SK-MINERAL-DISPOSITION-` and looks it up
    in the hint table. Returns an empty dict if the suffix is unknown so
    the extractor falls back to `disposition_type='mineral', status='unknown'`.
    """
    prefix = "CA-SK-MINERAL-DISPOSITION-"
    if not source_id.startswith(prefix):
        return {}
    return _MINERAL_DISPOSITION_LAYER_HINTS.get(source_id[len(prefix):], {})


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_sk_mineral_disposition"],
    description=(
        "Iterate over the per-layer CA-SK-MINERAL-DISPOSITION-* source_ids "
        "auto-registered by Bronze. Reproject polygon rings 2957→4326 and "
        "upsert into the unified public_geo.pg_mineral_disposition "
        "canonical table. (disposition_type, status, commodity_type) comes "
        "from the per-layer hint map — Mining layers 0–4 share the legacy "
        "cryptic field schema; layers 5–8 + Crown/8 use the clean schema. "
        "The extractor probes both. Plan Phase 2."
    ),
)
def silver_pg_ca_sk_mineral_disposition(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)

    with postgres.get_cursor() as cur:
        cur.execute(
            """
            SELECT source_id
              FROM public_geo.sources
             WHERE jurisdiction_code = 'CA-SK'
               AND canonical_type    = 'mineral_disposition'
               AND layer_index       IS NOT NULL
             ORDER BY source_id
            """,
        )
        rows = cur.fetchall()

    if not rows:
        context.log.warning(
            "Silver mineral_disposition: no per-layer sources registered. "
            "Run bronze_pg_ca_sk_mineral_disposition first.",
        )
        return MaterializeResult(
            metadata={
                "source_id": MetadataValue.text("CA-SK-MINERAL-DISPOSITION"),
                "canonical_type": MetadataValue.text("mineral_disposition"),
                "sources_processed": MetadataValue.int(0),
                "total_features": MetadataValue.int(0),
            }
        )

    per_source: list[dict[str, Any]] = []
    totals = UpsertStats()

    for row in rows:
        source_id: str = row["source_id"]
        hint = _hint_for_source_id(source_id)

        stats = _run_silver_for_source(
            context=context,
            config=config,
            postgres=postgres,
            minio=minio,
            source_id=source_id,
            spec=SPEC_MINERAL_DISPOSITION,
            crosswalks=crosswalks,
            extra_ctx={"disposition_hint": hint},
        )
        totals.total_features += stats.total_features
        totals.inserted_new += stats.inserted_new
        totals.updated_changed += stats.updated_changed
        totals.touched_unchanged += stats.touched_unchanged
        totals.skipped_no_feature_id += stats.skipped_no_feature_id
        totals.skipped_no_geometry += stats.skipped_no_geometry
        per_source.append(
            {
                "source_id": source_id,
                "disposition_type": hint.get("disposition_type", "mineral"),
                "status": hint.get("status", "unknown"),
                "total_features": stats.total_features,
                "inserted_new": stats.inserted_new,
                "updated_changed": stats.updated_changed,
                "touched_unchanged": stats.touched_unchanged,
            }
        )

    meta = _metadata_from_stats(
        totals,
        source_id="CA-SK-MINERAL-DISPOSITION",
        canonical_type="mineral_disposition",
    )
    meta["sources_processed"] = MetadataValue.int(len(per_source))
    meta["per_source"] = MetadataValue.json(per_source)
    return MaterializeResult(metadata=meta)


@asset(
    group_name="silver",
    deps=["bronze_pg_ca_sk_bedrock_geology"],
    description=(
        "Silver layer for SK Bedrock Geology 250K. Upserts into "
        "public_geo.pg_bedrock_geology with full stratigraphic hierarchy "
        "(eon/era/period/group/formation/member). scale='250K' constant."
    ),
)
def silver_pg_ca_sk_bedrock_geology(
    context: AssetExecutionContext,
    config: SilverPublicGeoscienceConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    crosswalks = _load_crosswalks(postgres)

    stats = _run_silver_for_source(
        context=context,
        config=config,
        postgres=postgres,
        minio=minio,
        source_id="CA-SK-GEOLOGY-BEDROCK-250K",
        spec=SPEC_BEDROCK_GEOLOGY,
        crosswalks=crosswalks,
    )

    meta = _metadata_from_stats(
        stats,
        source_id="CA-SK-GEOLOGY-BEDROCK-250K",
        canonical_type="bedrock_geology",
    )
    return MaterializeResult(metadata=meta)
