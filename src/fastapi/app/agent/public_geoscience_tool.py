"""Public Geoscience agent tool — Qdrant semantic search + PostGIS hydration.

Phase 3.4. One tool exposed to the deterministic orchestrator:

    search_public_geoscience(
        ctx,
        jurisdiction_codes,   # e.g. ["CA-SK"]
        canonical_types,      # subset of {"mine","mineral_occurrence",
                              #            "drillhole_collar","resource_potential_zone",
                              #            "rock_sample","assessment_survey"}
        commodities,          # canonical codes — ["Au", "U"]
        bbox,                 # [minLon, minLat, maxLon, maxLat] or None
        text_query,           # free-text semantic query
        limit,
    ) -> PublicGeoscienceSearchResult

Retrieval pipeline (plan §05d + §08):

  1. For each requested canonical_type, run a filtered Qdrant semantic
     search against the matching `pg_*` collection (populated by the
     Phase-3.2 index_public_geoscience_qdrant asset).
  2. Hydrate each hit from the Phase-3.2 Qdrant payload — that payload
     already carries `jurisdiction_code`, `source_id`, `commodities`,
     `status`, `geom_bbox`, `source_url`, and a pre-built `summary_text`
     so we can return useful records *without* a PostGIS round-trip in
     the hot path.
  3. Join against `public_geo.sources` + `public_geo.jurisdictions`
     once per run (small reference tables, cached in-memory for the call)
     to add license attribution + staleness metadata. This is the "evidence
     binding" stage of plan §08's two-stage citation model.

The tool is side-effect-free; it owns no LLM calls, no agent reasoning,
and no text generation. Just retrieval + hydration.

Timeout + graceful degradation: matches the existing tool conventions in
tools.py — each I/O is wrapped in `asyncio.wait_for`, and any failure
returns an empty result set with an audit-friendly `data_source` label
rather than raising.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.agent.deps import AgentDeps
from app.agent.tools import _metered  # P1 #16 — per-tool latency metric
from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Map canonical_type → Qdrant collection name (matches index_public_geoscience.py).
# Tier 1 expansion added rock_sample + assessment_survey collections; this
# dict is the single gate that opens them to the chat tool.
_COLLECTION_FOR_TYPE: dict[str, str] = {
    "mine":                    "pg_mine",
    "mineral_occurrence":      "pg_mineral_occurrence",
    "drillhole_collar":        "pg_drillhole_collar",
    "resource_potential_zone": "pg_resource_potential_zone",
    "rock_sample":             "pg_rock_sample",
    "assessment_survey":       "pg_assessment_survey",
    # Tier 2 Mineral Tenure. The collection only carries rows once the
    # paired index_public_geoscience_qdrant asset has been extended to
    # populate it — until then queries here return empty gracefully.
    "mineral_disposition":     "pg_mineral_disposition",
}

_ALL_CANONICAL_TYPES = tuple(_COLLECTION_FOR_TYPE.keys())

# Default limits. Lower than the internal search_documents cap because
# payload summaries are short and the chat model doesn't need more than
# ~10 per surface to reason over a jurisdiction.
_DEFAULT_LIMIT_PER_TYPE = 6
_MAX_LIMIT_PER_TYPE = 25


# ---------------------------------------------------------------------------
# Dataclasses (match the style of tools.py — plain dataclasses, not Pydantic)
# ---------------------------------------------------------------------------

@dataclass
class PublicGeoscienceRecord:
    """One canonical entity retrieved from a Public Geoscience Qdrant collection.

    Fields mirror the Qdrant payload built by the Phase-3.2 index asset
    plus post-hoc license/staleness metadata joined from PostGIS.

    All fields are either primitive types or dict/list so the response
    assembler can serialize them into the GeoRAGResponse without additional
    coercion.
    """

    pg_id: str
    canonical_type: str                   # "mine" | "mineral_occurrence" | …
    jurisdiction_code: str
    jurisdiction_name: str | None
    source_id: str
    source_feature_id: str | None
    name: str                             # display title derived from summary
    summary_text: str                     # structured NL summary from Qdrant
    commodities: list[str] = field(default_factory=list)
    commodity_grouping: str | None = None
    status: str | None = None
    geom_bbox: list[float] | None = None  # [minLon, minLat, maxLon, maxLat]
    source_url: str | None = None         # deep link back to upstream
    license_summary: str | None = None
    license_url: str | None = None
    staleness_seconds: int | None = None  # None if last_refreshed_at is NULL
    relevance_score: float = 0.0          # raw Qdrant cosine (no reranker here)


@dataclass
class PublicGeoscienceSearchResult:
    """Return type for search_public_geoscience."""

    records: list[PublicGeoscienceRecord]
    count: int
    jurisdictions_queried: list[str]
    canonical_types_queried: list[str]
    # Audit-only: supports hallucination Layer 5 (chunk provenance).
    data_source: str = "Qdrant public_geo.* collections"


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

@_metered("search_public_geoscience")
async def search_public_geoscience(
    ctx: Any,   # ToolContext | RunContext[AgentDeps] — keeping duck-typed like the other tools
    *,
    jurisdiction_codes: list[str] | None = None,
    canonical_types: list[str] | None = None,
    commodities: list[str] | None = None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    text_query: str | None = None,
    limit_per_type: int = _DEFAULT_LIMIT_PER_TYPE,
) -> PublicGeoscienceSearchResult:
    """Search the Public Geoscience corpus across one or more canonical types.

    Use this tool when the user asks about:
      - Government-published mineral occurrences, mines, drillholes, or
        resource potential zones.
      - Saskatchewan SMDI records (or, later, BC MINFILE / Ontario MDI / …).
      - Any "what's around X, Y" question that hits external geological-
        survey data rather than the internal project archive.

    When the user's intent is ambiguous between internal archive and Public
    Geoscience, the orchestrator is expected to call BOTH `search_documents`
    and this tool and let the response assembler label results by source
    surface.

    Args:
        jurisdiction_codes: Whitelist of jurisdictions to query. If None or
            empty, all active jurisdictions registered in PostGIS are searched.
        canonical_types: Subset of {"mine","mineral_occurrence",
            "drillhole_collar","resource_potential_zone","rock_sample",
            "assessment_survey"}. If None or empty, all six are queried.
            Restricting reduces latency proportionally.
        commodities: Canonical commodity codes (e.g. "Au", "U", "Li"). Matches
            any hit whose `commodities` payload list contains any of these.
        bbox: [minLon, minLat, maxLon, maxLat] or None. When provided we
            post-filter the Qdrant hits by overlap with this bbox (Qdrant
            doesn't natively index our geom_bbox arrays as a spatial type).
        text_query: Free-text semantic query. When provided we embed it and
            use the result as the query vector; when None we fall back to
            an empty-string-embedded "match all" vector so filter-only
            retrieval still works (useful for "list all producing uranium
            mines in Saskatchewan" style queries).
        limit_per_type: Max hits per canonical_type per Qdrant call. Hard-
            capped at 25 so a misbehaving caller can't DOS the LLM prompt.

    Returns:
        PublicGeoscienceSearchResult with hydrated records and audit metadata.
        On timeout or Qdrant error, returns an empty result with a descriptive
        data_source label rather than raising.
    """
    deps: AgentDeps = ctx.deps  # type: ignore[attr-defined]
    t0 = time.monotonic()

    juris_list = _normalize_strings(jurisdiction_codes) or []
    types_to_query = _normalize_strings(canonical_types) or list(_ALL_CANONICAL_TYPES)
    types_to_query = [t for t in types_to_query if t in _COLLECTION_FOR_TYPE]
    commodity_list = _normalize_strings(commodities) or []
    effective_limit = max(1, min(int(limit_per_type or _DEFAULT_LIMIT_PER_TYPE), _MAX_LIMIT_PER_TYPE))
    bbox_tuple = _normalize_bbox(bbox)

    if not types_to_query:
        logger.info("search_public_geoscience: empty canonical_types, nothing to do")
        return PublicGeoscienceSearchResult(
            records=[], count=0,
            jurisdictions_queried=juris_list,
            canonical_types_queried=[],
            data_source="Qdrant public_geo.* collections (no types requested)",
        )

    # ── Embed the query once, reuse across collections. ────────────────
    query_vector = await _embed_query(deps, text_query)
    if query_vector is None:
        # No embedding model at all → cannot run semantic search. Rather
        # than returning nothing we could build a payload-only filter path,
        # but the response assembler needs *some* ranking signal and the
        # internal tools would also degrade here, so just log and exit.
        logger.info("search_public_geoscience: embedding model unavailable")
        return PublicGeoscienceSearchResult(
            records=[], count=0,
            jurisdictions_queried=juris_list,
            canonical_types_queried=types_to_query,
            data_source="Qdrant public_geo.* (embedding model not loaded)",
        )

    # ── Fan out across canonical types in parallel. ─────────────────────
    try:
        fetched = await asyncio.wait_for(
            asyncio.gather(
                *[
                    _query_collection(
                        deps=deps,
                        canonical_type=ct,
                        query_vector=query_vector,
                        jurisdictions=juris_list,
                        commodities=commodity_list,
                        limit=effective_limit,
                    )
                    for ct in types_to_query
                ],
                return_exceptions=True,
            ),
            timeout=settings.TIMEOUT_QDRANT_S * 2,  # 2× because we fan out
        )
    except TimeoutError:
        logger.warning("search_public_geoscience: Qdrant fan-out timed out")
        return PublicGeoscienceSearchResult(
            records=[], count=0,
            jurisdictions_queried=juris_list,
            canonical_types_queried=types_to_query,
            data_source="Qdrant public_geo.* (timeout)",
        )

    records: list[PublicGeoscienceRecord] = []
    for ct, result in zip(types_to_query, fetched, strict=False):
        if isinstance(result, Exception):
            logger.warning(
                "search_public_geoscience: %s collection failed: %s",
                ct, result,
            )
            continue
        for rec in result:
            if _passes_bbox(rec.geom_bbox, bbox_tuple):
                records.append(rec)

    # ── Hydrate license + staleness from PostGIS. ──────────────────────
    # One query per run, not per record.
    await _hydrate_registry_metadata(deps, records)

    # Sort hits across all collections by score descending so the top-k
    # spans surfaces naturally (occurrences can outrank mines if the query
    # semantically prefers them).
    records.sort(key=lambda r: r.relevance_score, reverse=True)

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    logger.info(
        "search_public_geoscience: %d hits across %d types in %.1fms "
        "(jurisdictions=%s, commodities=%s, bbox=%s)",
        len(records),
        len(types_to_query),
        elapsed_ms,
        juris_list or "all",
        commodity_list or "all",
        "yes" if bbox_tuple else "no",
    )

    return PublicGeoscienceSearchResult(
        records=records,
        count=len(records),
        jurisdictions_queried=juris_list,
        canonical_types_queried=types_to_query,
        data_source=f"Qdrant public_geo.* ({len(records)} hits, {elapsed_ms}ms)",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

async def _embed_query(deps: AgentDeps, text_query: str | None) -> list[float] | None:
    model = deps.embedding_model
    if model is None:
        return None
    loop = asyncio.get_event_loop()
    # Empty/None text_query → use a neutral sentinel so the vector has
    # consistent dimensionality and filters still work. The embedding is
    # then effectively just "Canadian geological-survey records" noise.
    q = text_query if text_query and text_query.strip() else "Canadian public geological-survey records"
    # Qwen3-Embedding query template (matches tools.search_documents). When
    # EMBEDDING_QUERY_PROMPT_NAME is unset we fall back to raw encoding.
    from app.config import settings as _settings  # noqa: PLC0415
    _prompt_name = _settings.EMBEDDING_QUERY_PROMPT_NAME or None
    try:
        vec = await loop.run_in_executor(
            None,
            lambda: model.encode(
                q, normalize_embeddings=True, prompt_name=_prompt_name,
            ).tolist(),
        )
        return vec
    except Exception:
        logger.exception("search_public_geoscience: embedding failed for '%.80s'", q)
        return None


def _qdrant_filter(
    *,
    jurisdictions: list[str],
    commodities: list[str],
) -> Any | None:
    """Build a Qdrant Filter from whitelisted jurisdictions + commodities.

    None = no filter (all points eligible). Both arrays are OR-within,
    AND-across (standard Qdrant must/should semantics).
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    must: list[Any] = []
    if jurisdictions:
        must.append(
            FieldCondition(
                key="jurisdiction_code",
                match=MatchAny(any=list(jurisdictions)),
            )
        )
    if commodities:
        # `commodities` is a keyword array in the payload; MatchAny works
        # because Qdrant treats array fields as "set of keywords".
        must.append(
            FieldCondition(
                key="commodities",
                match=MatchAny(any=list(commodities)),
            )
        )
    if not must:
        return None
    return Filter(must=must)


async def _query_collection(
    *,
    deps: AgentDeps,
    canonical_type: str,
    query_vector: list[float],
    jurisdictions: list[str],
    commodities: list[str],
    limit: int,
) -> list[PublicGeoscienceRecord]:
    collection = _COLLECTION_FOR_TYPE[canonical_type]
    query_filter = _qdrant_filter(jurisdictions=jurisdictions, commodities=commodities)

    response = await deps.qdrant_client.query_points(
        collection_name=collection,
        query=query_vector,
        limit=limit,
        with_payload=True,
        query_filter=query_filter,
    )

    out: list[PublicGeoscienceRecord] = []
    for point in response.points:
        payload = point.payload or {}
        name = _derive_name(payload, canonical_type)
        out.append(
            PublicGeoscienceRecord(
                pg_id=str(payload.get("pg_id") or point.id),
                canonical_type=str(payload.get("canonical_type") or canonical_type),
                jurisdiction_code=str(payload.get("jurisdiction_code") or ""),
                jurisdiction_name=None,  # hydrated below
                source_id=str(payload.get("source_id") or ""),
                source_feature_id=_maybe_str(payload.get("source_feature_id")),
                name=name,
                summary_text=str(payload.get("summary_text") or ""),
                commodities=list(payload.get("commodities") or []),
                commodity_grouping=_maybe_str(payload.get("commodity_grouping")),
                status=_maybe_str(payload.get("status")),
                geom_bbox=_maybe_bbox(payload.get("geom_bbox")),
                source_url=_maybe_str(payload.get("source_url")),
                license_summary=None,     # hydrated below
                license_url=None,         # hydrated below
                staleness_seconds=None,   # hydrated below
                relevance_score=float(point.score or 0.0),
            )
        )
    return out


def _derive_name(payload: dict[str, Any], canonical_type: str) -> str:
    """Return a short display title. We avoid fetching the canonical table
    again by using the payload summary's first clause; most summaries are
    'STATUS NAME in JURISDICTION…' so the first 80 chars are meaningful.
    """
    summary = str(payload.get("summary_text") or "").strip()
    if summary:
        # Clip at the first period for a clean one-liner.
        first = summary.split(".", 1)[0]
        return first[:120]
    # Fall back to a generic type-qualified label.
    pretty = canonical_type.replace("_", " ").title()
    return f"{pretty} record"


async def _hydrate_registry_metadata(
    deps: AgentDeps,
    records: list[PublicGeoscienceRecord],
) -> None:
    """Attach jurisdiction_name + license + staleness_seconds to each record.

    One query per unique (jurisdiction, source) pair, not per record.
    """
    if not records:
        return

    source_ids = sorted({r.source_id for r in records if r.source_id})
    jurisdiction_codes = sorted({r.jurisdiction_code for r in records if r.jurisdiction_code})

    if not source_ids or not jurisdiction_codes:
        return

    sql = """
    SELECT
        s.source_id,
        s.jurisdiction_code,
        s.license_summary,
        s.license_url,
        s.last_refreshed_at,
        EXTRACT(EPOCH FROM (NOW() - s.last_refreshed_at))::BIGINT AS staleness_seconds,
        j.display_name AS jurisdiction_name
      FROM public_geo.sources s
      JOIN public_geo.jurisdictions j
           ON j.jurisdiction_code = s.jurisdiction_code
     WHERE s.source_id = ANY($1::text[])
       AND s.jurisdiction_code = ANY($2::text[])
    """

    async def _run() -> list[dict[str, Any]]:
        async with deps.pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, source_ids, jurisdiction_codes)
        return [dict(r) for r in rows]

    try:
        rows = await asyncio.wait_for(_run(), timeout=settings.TIMEOUT_POSTGIS_S)
    except TimeoutError:
        logger.warning("search_public_geoscience: registry hydration timed out")
        return
    except Exception:
        logger.exception("search_public_geoscience: registry hydration failed")
        return

    # Build a lookup keyed on (source_id, jurisdiction_code).
    lookup: dict[tuple[str, str], dict[str, Any]] = {
        (r["source_id"], r["jurisdiction_code"]): r for r in rows
    }

    for rec in records:
        ref = lookup.get((rec.source_id, rec.jurisdiction_code))
        if not ref:
            continue
        rec.jurisdiction_name = ref.get("jurisdiction_name")
        rec.license_summary = ref.get("license_summary")
        rec.license_url = ref.get("license_url")
        staleness = ref.get("staleness_seconds")
        rec.staleness_seconds = int(staleness) if staleness is not None else None


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _normalize_strings(values: Iterable[str] | None) -> list[str]:
    if not values:
        return []
    return [str(v).strip() for v in values if v is not None and str(v).strip()]


def _normalize_bbox(bbox: Any) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    try:
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    except (TypeError, ValueError):
        return None
    return None


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _maybe_bbox(value: Any) -> list[float] | None:
    if value is None:
        return None
    try:
        if isinstance(value, (list, tuple)) and len(value) == 4:
            return [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    return None


def _passes_bbox(
    hit: list[float] | None,
    requested: tuple[float, float, float, float] | None,
) -> bool:
    """Return True if the hit's bbox overlaps the requested bbox.

    No requested bbox → everything passes.
    Missing hit bbox → we can't tell; fail open (keep the record).
    """
    if requested is None or hit is None:
        return True
    try:
        h_min_lon, h_min_lat, h_max_lon, h_max_lat = hit
        r_min_lon, r_min_lat, r_max_lon, r_max_lat = requested
    except (TypeError, ValueError):
        return True
    # Standard AABB overlap test.
    return not (
        h_max_lon < r_min_lon
        or h_min_lon > r_max_lon
        or h_max_lat < r_min_lat
        or h_min_lat > r_max_lat
    )
