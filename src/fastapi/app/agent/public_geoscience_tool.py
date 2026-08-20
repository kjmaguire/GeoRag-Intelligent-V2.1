"""Public Geoscience agent tool — LIVE queries against provincial surveys.

    search_public_geoscience(
        ctx,
        jurisdiction_codes,   # e.g. ["CA-SK"]
        canonical_types,      # subset of the seven canonical types
        commodities,          # canonical commodity codes — ["Au", "U"]
        bbox,                 # [minLon, minLat, maxLon, maxLat] or None
        text_query,           # free-text name filter
        limit_per_type,
    ) -> PublicGeoscienceSearchResult

What changed, and why it matters when reading results
-----------------------------------------------------
This used to search a stored, embedded copy: six Qdrant collections holding
182,826 points, fed by a Dagster pipeline dormant since 2026-07-28. Public
geoscience is a look-through onto what surveys already publish, so the copy
was the wrong shape — it went stale, it never reached Azure at all, and its
384-dim vectors could not be queried by a 1024-dim reader
(``HTTP 400: expected dim: 384, got 1024``). It is gone.

Queries now hit the survey's own ArcGIS REST service and return what it is
serving at that moment. Three consequences worth knowing:

1. **No semantic ranking.** You cannot rank by meaning over data you never
   embedded. ``relevance_score`` is therefore 0.0 on every record — the field
   is kept so the response assembler's shape is unchanged, but it carries no
   information and must not be presented as confidence. Ordering is whatever
   the survey returns.
2. **``text_query`` is a name filter, not a search.** It becomes a
   case-insensitive LIKE against that layer's name-bearing columns. "uranium
   deposits near Key Lake" will not work; "Key Lake" will.
3. **Spatial filtering got better.** ``bbox`` is now an indexed envelope query
   executed by the survey's own database, instead of a post-filter over
   whatever the vector search happened to return.

Latency is a network call per feed rather than a local index hit. Feeds are
queried concurrently and every failure degrades to "no results from that
feed", so one unreachable survey never fails an answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.agent.tools import _metered  # P1 #16 — per-tool latency metric
from app.services.public_geo import arcgis
from app.services.public_geo.registry import (
    CANONICAL_TYPES,
    PublicGeoSource,
    jurisdiction_for,
    sources_for,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-type field mappings
# ---------------------------------------------------------------------------
# ArcGIS layers do not share a schema, so nothing may assume one. These
# candidate lists were read off the live services (2026-08-20) rather than
# guessed; `arcgis.first_present` walks them case-insensitively and takes the
# first non-empty hit, so a layer that spells a column differently degrades to
# "no name" instead of raising.
_NAME_FIELDS: dict[str, list[str]] = {
    "mine": ["NAME", "PROPERTY", "MINE_NAME", "SITE_NAME"],
    "mineral_occurrence": [
        "MINFILE_NAME", "NAME", "OCCURRENCE_NAME", "SHOWING_NAME", "PROPERTY",
    ],
    "drillhole_collar": [
        "DRILLHOLE_NAME", "HOLE_ID", "HOLE_NAME", "DRILLHOLE_ID", "NAME", "COMPANY",
    ],
    "resource_potential_zone": ["COMMODITY", "MAPKEY", "NAME", "ZONE_NAME"],
    "rock_sample": ["SAMPLE_ID", "SAMPLE_NUMBER", "NAME", "STATION"],
    "assessment_survey": ["TITLE", "SURVEY_NAME", "NAME", "FILE_NAME", "ASSESSMENT_FILE"],
    "mineral_disposition": ["DISPOSITION", "DISPOSITION_NUMBER", "NAME", "CLIENT_NAME"],
}

_COMMODITY_FIELDS: list[str] = [
    "COMMODITY",
    "COMMODITY_OF_INTEREST",
    "COMMODITIES",
    "COMMODITY_DESCRIPTION1",
    "COMMODITY_CODE1",
]

_STATUS_FIELDS: list[str] = ["STATUS", "DEP_CLASS", "DEPOSIT_STATUS", "OPERATING_STATUS"]

_DEFAULT_LIMIT_PER_TYPE = 6
_MAX_LIMIT_PER_TYPE = 25

# Whole-call ceiling. Individual feeds have their own per-request timeout; this
# bounds the fan-out so a slow survey cannot hold an answer open indefinitely.
_TOTAL_TIMEOUT_S = 20.0


@dataclass
class PublicGeoscienceRecord:
    """One entity retrieved live from a public survey.

    Field set is unchanged from the stored-corpus era so the response
    assembler and citation layer need no edits, but two fields changed meaning:

    ``pg_id`` was a UUID minted when we stored the row. Nothing is stored now,
    so it is ``"<source_id>:<OBJECTID>"`` — a composite of two upstream-stable
    identifiers, which is what makes a citation re-resolvable later.

    ``staleness_seconds`` is 0 by construction: the record was fetched during
    this request. It is retained because the citation envelope renders it.
    """

    pg_id: str
    canonical_type: str
    jurisdiction_code: str
    jurisdiction_name: str | None
    source_id: str
    source_feature_id: str | None
    name: str
    summary_text: str
    commodities: list[str] = field(default_factory=list)
    commodity_grouping: str | None = None
    status: str | None = None
    geom_bbox: list[float] | None = None
    source_url: str | None = None
    license_summary: str | None = None
    license_url: str | None = None
    staleness_seconds: int | None = 0
    # Always 0.0 — live results carry no semantic score. See module docstring.
    relevance_score: float = 0.0


@dataclass
class PublicGeoscienceSearchResult:
    """Return type for search_public_geoscience."""

    records: list[PublicGeoscienceRecord]
    count: int
    jurisdictions_queried: list[str]
    canonical_types_queried: list[str]
    data_source: str = "Live ArcGIS REST (provincial + federal surveys)"


@_metered("search_public_geoscience")
async def search_public_geoscience(
    ctx: Any,   # ToolContext | RunContext[AgentDeps] — duck-typed like the other tools
    *,
    jurisdiction_codes: list[str] | None = None,
    canonical_types: list[str] | None = None,
    commodities: list[str] | None = None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    text_query: str | None = None,
    limit_per_type: int = _DEFAULT_LIMIT_PER_TYPE,
) -> PublicGeoscienceSearchResult:
    """Query government-published geoscience records live.

    Use this when the user asks about:
      - Government-published mineral occurrences, mines, drillholes, or
        resource potential zones.
      - Saskatchewan SMDI or BC MINFILE records.
      - "What's around X, Y" questions that hit external survey data rather
        than the internal project archive.

    When intent is ambiguous between internal archive and public geoscience,
    the orchestrator calls BOTH this and `search_documents`, and the response
    assembler labels results by surface.

    Args:
        jurisdiction_codes: Restrict to these jurisdictions ("CA-SK", "CA-BC").
            None/empty means every registered jurisdiction.
        canonical_types: Subset of the seven canonical types. None/empty means
            all of them. Restricting cuts latency roughly proportionally,
            because each type is one or more live HTTP calls.
        commodities: Canonical commodity codes ("Au", "U", "Li"). Applied as a
            case-insensitive substring match against whichever commodity
            column the layer exposes.
        bbox: [minLon, minLat, maxLon, maxLat]. Executed as a native spatial
            query by the survey — the most selective filter available here.
        text_query: Case-insensitive LIKE against the layer's name columns.
            NOT semantic search; see the module docstring.
        limit_per_type: Max records per feed. Capped at 25 so a bad caller
            cannot flood the prompt.

    Returns:
        PublicGeoscienceSearchResult. Any failure degrades to fewer (or zero)
        records rather than raising.
    """
    t0 = time.monotonic()

    juris_list = _normalize_strings(jurisdiction_codes) or []
    types_to_query = _normalize_strings(canonical_types) or list(CANONICAL_TYPES)
    types_to_query = [t for t in types_to_query if t in CANONICAL_TYPES]
    commodity_list = _normalize_strings(commodities) or []
    effective_limit = max(
        1, min(int(limit_per_type or _DEFAULT_LIMIT_PER_TYPE), _MAX_LIMIT_PER_TYPE)
    )
    bbox_tuple = _normalize_bbox(bbox)

    if not types_to_query:
        logger.info("search_public_geoscience: empty canonical_types, nothing to do")
        return PublicGeoscienceSearchResult(
            records=[], count=0,
            jurisdictions_queried=juris_list,
            canonical_types_queried=[],
        )

    feeds = sources_for(canonical_types=types_to_query, jurisdiction_codes=juris_list)
    if not feeds:
        logger.info(
            "search_public_geoscience: no queryable feed for types=%s juris=%s",
            types_to_query, juris_list,
        )
        return PublicGeoscienceSearchResult(
            records=[], count=0,
            jurisdictions_queried=juris_list,
            canonical_types_queried=types_to_query,
        )

    async def _one(src: PublicGeoSource) -> list[PublicGeoscienceRecord]:
        try:
            features = await arcgis.query_features(
                src,
                text=text_query,
                text_fields=_NAME_FIELDS.get(src.canonical_type),
                bbox=bbox_tuple,
                limit=effective_limit,
            )
        except Exception:  # noqa: BLE001 — one feed must never sink the fan-out
            logger.exception("search_public_geoscience: %s failed", src.source_id)
            return []
        return [_to_record(src, f) for f in features]

    try:
        batches = await asyncio.wait_for(
            asyncio.gather(*(_one(s) for s in feeds), return_exceptions=True),
            timeout=_TOTAL_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warning(
            "search_public_geoscience: fan-out exceeded %.0fs across %d feed(s)",
            _TOTAL_TIMEOUT_S, len(feeds),
        )
        batches = []

    records: list[PublicGeoscienceRecord] = []
    for b in batches:
        if isinstance(b, list):
            records.extend(b)

    if commodity_list:
        records = [r for r in records if _matches_commodity(r, commodity_list)]

    logger.info(
        "search_public_geoscience: %d record(s) from %d feed(s) in %.0fms "
        "(types=%s juris=%s bbox=%s text=%s)",
        len(records), len(feeds), (time.monotonic() - t0) * 1000,
        types_to_query, juris_list or "all", bool(bbox_tuple), bool(text_query),
    )

    return PublicGeoscienceSearchResult(
        records=records,
        count=len(records),
        jurisdictions_queried=sorted({s.jurisdiction_code for s in feeds}),
        canonical_types_queried=types_to_query,
    )


# ---------------------------------------------------------------------------
# Feature -> record
# ---------------------------------------------------------------------------

def _to_record(src: PublicGeoSource, feature: dict[str, Any]) -> PublicGeoscienceRecord:
    props = feature.get("properties") or {}
    oid = arcgis.object_id_of(feature)
    juris = jurisdiction_for(src)

    name = (
        arcgis.first_present(props, _NAME_FIELDS.get(src.canonical_type, ["NAME"]))
        or f"{src.canonical_type.replace('_', ' ').title()} {oid or ''}".strip()
    )
    commodities = _commodities_of(props)
    status = arcgis.first_present(props, _STATUS_FIELDS)

    return PublicGeoscienceRecord(
        # Composite of two upstream-stable identifiers — this is what lets a
        # citation be re-resolved by fetching that OBJECTID again later.
        pg_id=f"{src.source_id}:{oid}" if oid else src.source_id,
        canonical_type=src.canonical_type,
        jurisdiction_code=src.jurisdiction_code,
        jurisdiction_name=juris.display_name if juris else None,
        source_id=src.source_id,
        source_feature_id=oid,
        name=name,
        summary_text=_summarize(src, name, commodities, status, juris),
        commodities=commodities,
        commodity_grouping=arcgis.first_present(props, ["COMMODITY_GROUPING"]),
        status=status,
        geom_bbox=_bbox_of(feature),
        source_url=src.service_url,
        license_summary=(juris.license_summary if juris else None) or src.license_summary,
        license_url=(juris.license_url if juris else None) or src.license_url,
        staleness_seconds=0,  # fetched during this request
        relevance_score=0.0,  # no semantic score exists — see module docstring
    )


def _summarize(
    src: PublicGeoSource,
    name: str,
    commodities: list[str],
    status: str | None,
    juris: Any,
) -> str:
    """Human-readable one-liner — what the LLM actually reads.

    Mirrors the shape the stored pipeline used to bake into `summary_text`, so
    prompts and the response assembler see a familiar sentence.
    """
    where = juris.display_name if juris else src.jurisdiction_code
    bits = [f"{name} ({src.canonical_type.replace('_', ' ')}) in {where}"]
    if commodities:
        bits.append(f"Commodities: {', '.join(commodities)}")
    if status:
        bits.append(f"Status: {status}")
    bits.append(f"Source: {src.name}")
    return ". ".join(bits) + "."


def _commodities_of(props: dict[str, Any]) -> list[str]:
    """Collect commodity values across the several spellings layers use.

    BC MINFILE splits them across COMMODITY_DESCRIPTION1..8; Saskatchewan uses
    a single delimited COMMODITY. Handle both without assuming either.
    """
    found: list[str] = []
    lowered = {str(k).lower(): v for k, v in props.items()}

    for key, val in lowered.items():
        if not key.startswith(("commodity", "commodities")):
            continue
        if val in (None, "", " "):
            continue
        for part in str(val).replace(";", ",").split(","):
            p = part.strip()
            if p and p not in found:
                found.append(p)
    return found[:12]


def _matches_commodity(rec: PublicGeoscienceRecord, wanted: list[str]) -> bool:
    """Token match against the record's commodity values.

    Deliberately NOT a substring match. Commodity codes are one or two letters,
    so substring matching is catastrophically loose: searching "U" matched
    "Ind*u*strial Mineral" and returned 24 of 25 mines including chloride and
    salt deposits. Verified against CA-SK-MINE-LOC, 2026-08-20.

    Compares whole values case-insensitively, and also accepts a full-word hit
    inside a multi-word value so "uranium" matches "Uranium Mine" while "U"
    still does not match "Industrial".
    """
    wanted_l = {w.lower() for w in wanted}
    for value in rec.commodities:
        v = value.strip().lower()
        if v in wanted_l:
            return True
        if any(w in v.split() for w in wanted_l):
            return True
    return False


def _bbox_of(feature: dict[str, Any]) -> list[float] | None:
    """Derive [minLon, minLat, maxLon, maxLat] from GeoJSON geometry."""
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates")
    if coords is None:
        return None

    xs: list[float] = []
    ys: list[float] = []

    def _walk(node: Any) -> None:
        if isinstance(node, (int, float)):
            return
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and all(isinstance(v, (int, float)) for v in node[:2])
        ):
            xs.append(float(node[0]))
            ys.append(float(node[1]))
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                _walk(child)

    _walk(coords)
    if not xs or not ys:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalize_strings(values: Iterable[str] | None) -> list[str]:
    if not values:
        return []
    return [str(v).strip() for v in values if str(v).strip()]


def _normalize_bbox(bbox: Any) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    try:
        vals = [float(v) for v in list(bbox)[:4]]
    except (TypeError, ValueError):
        return None
    if len(vals) != 4:
        return None
    return (vals[0], vals[1], vals[2], vals[3])
