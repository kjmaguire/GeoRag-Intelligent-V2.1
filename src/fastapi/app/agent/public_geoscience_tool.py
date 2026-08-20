"""Public Geoscience agent tool — attribute + spatial search over public_geo.*.

    search_public_geoscience(
        ctx,
        jurisdiction_codes,   # e.g. ["CA-SK"]
        canonical_types,      # subset of the seven canonical types
        commodities,          # commodity names or codes — ["Au", "uranium"]
        bbox,                 # [minLon, minLat, maxLon, maxLat] or None
        text_query,           # free-text name filter
        limit_per_type,
    ) -> PublicGeoscienceSearchResult

Where the data comes from
-------------------------
``public_geo.*``, kept fresh by the ``public_geo_sync`` workflow (03:30 UTC
Sundays), which pulls each survey's live ArcGIS service and upserts it. This
tool reads that mirror rather than calling the surveys itself, for two
reasons:

1. **Latency.** Querying live meant one HTTP request per feed — twenty-eight
   of them, fanned out concurrently but still bounded only by a 20-second
   ceiling. That is not a budget a chat turn can afford. The same search over
   the synced tables is one indexed query.
2. **Agreement.** The map layers and all eight citation resolvers read
   ``public_geo.*``. A chat answer sourced from somewhere else could cite a
   feature the map cannot draw and the citation panel cannot resolve.

The cost is staleness bounded by the sync cadence, which every record reports
honestly in ``staleness_seconds`` (computed from ``last_seen_at``) rather than
implying it is current.

What this is NOT
----------------
**Semantic search.** The previous implementation ranked against six Qdrant
collections holding 182,826 embedded points, fed by a Dagster pipeline dormant
since 2026-07-28. That corpus never reached Azure and its 384-dim vectors
could not be read by a 1024-dim reader (``HTTP 400: expected dim: 384, got
1024``). It is gone and is not being rebuilt: embedding a copy of someone
else's structured feature service buys ranking-by-meaning over records that
are already precisely filterable by commodity, status, name and geometry.

Two consequences for anyone reading results:

* ``relevance_score`` is 0.0 on every record. The field is kept so the
  response assembler's shape is unchanged, but it carries no information and
  must never be rendered as confidence.
* ``text_query`` is a name filter, not a query. "uranium deposits near Key
  Lake" will not work; "Key Lake" will, and the rest of that sentence belongs
  in the ``commodities`` and ``bbox`` arguments.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.agent.tools import _metered  # P1 #16 — per-tool latency metric
from app.services.public_geo.registry import (
    CANONICAL_TYPES,
    jurisdiction_for,
    source_by_id,
)

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT_PER_TYPE = 6
_MAX_LIMIT_PER_TYPE = 25

# Whole-query ceiling. Generous for an indexed read; it exists so a lock or a
# bad plan cannot hold a chat turn open.
_QUERY_TIMEOUT_S = 6.0


# ---------------------------------------------------------------------------
# Per-type projections
# ---------------------------------------------------------------------------
# The seven canonical tables do not share a schema, so each contributes its own
# SELECT normalised to a common shape, UNION ALL'd into one statement — one
# round trip regardless of how many types the caller asked for.
#
# Every branch takes the SAME parameter list ($1..$8), which is what makes a
# single bind for the whole union possible.

_TABLES: dict[str, str] = {
    "mine": "public_geo.pg_mine",
    "mineral_occurrence": "public_geo.pg_mineral_occurrence",
    "drillhole_collar": "public_geo.pg_drillhole_collar",
    "resource_potential_zone": "public_geo.pg_resource_potential_zone",
    "rock_sample": "public_geo.pg_rock_sample",
    "assessment_survey": "public_geo.pg_assessment_survey",
    "mineral_disposition": "public_geo.pg_mineral_disposition",
}

# Name expression per type. Referenced from both the SELECT list and the WHERE
# clause (the filter runs before the output alias exists), so it is declared
# once here rather than written twice.
_NAME_EXPR: dict[str, str] = {
    "mine": "name",
    "mineral_occurrence": "name",
    "drillhole_collar": "COALESCE(drillhole_name, drillhole_id, project_name)",
    # These zones have no name — the commodity IS the identity.
    "resource_potential_zone": "commodity",
    "rock_sample": "COALESCE(sample_number, station)",
    # The assessment FILE NUMBER is how these are cited and asked for; it lives
    # in source_attributes because the table keeps only survey_type as a typed
    # column.
    "assessment_survey": "source_attributes->>'FILENUMBER'",
    "mineral_disposition": "disposition_number",
}

_COMMODITY_EXPR: dict[str, str] = {
    "mine": "commodities",
    # Both lists are searchable: a geologist asking for copper wants the
    # occurrence where copper is associated, not only the ones where it is the
    # headline commodity.
    "mineral_occurrence": "(primary_commodities || associated_commodities)",
    "drillhole_collar": "commodity_of_interest",
    "resource_potential_zone": "ARRAY[commodity]",
    "rock_sample": "'{}'::text[]",
    "assessment_survey": "'{}'::text[]",
    "mineral_disposition": "COALESCE(commodity_codes, '{}'::text[])",
}

_GROUPING_EXPR: dict[str, str] = {
    "mine": "commodity_grouping",
    "mineral_occurrence": "commodity_grouping",
    "drillhole_collar": "NULL::varchar",
    "resource_potential_zone": "commodity_grouping",
    "rock_sample": "NULL::varchar",
    "assessment_survey": "NULL::varchar",
    "mineral_disposition": "NULL::varchar",
}

_STATUS_EXPR: dict[str, str] = {
    "mine": "status",
    "mineral_occurrence": "status",
    "drillhole_collar": "core_availability",
    "resource_potential_zone": "NULL::varchar",
    "rock_sample": "NULL::varchar",
    "assessment_survey": "survey_type",
    "mineral_disposition": "status",
}

# Commodity match, deliberately NOT a substring test.
#
# Commodity codes are one or two letters, so `LIKE '%U%'` is catastrophically
# loose: searching "U" matched "Ind*u*strial Mineral" and returned 24 of 25
# Saskatchewan mines, including chloride and salt deposits (verified against
# CA-SK-MINE-LOC, 2026-08-20). This compares whole values, and separately
# whole WORDS inside multi-word values, so "uranium" matches "Uranium Mine"
# while "U" still does not match "Industrial".
_COMMODITY_MATCH = """
        EXISTS (
            SELECT 1 FROM unnest({expr}) AS c
             WHERE lower(trim(c)) = ANY($3::text[])
                OR string_to_array(
                       regexp_replace(lower(c), '[^a-z0-9]+', ' ', 'g'), ' '
                   ) && $3::text[]
        )"""


@dataclass
class PublicGeoscienceRecord:
    """One entity from the public-geoscience mirror.

    Field set is unchanged from the stored-corpus era so the response
    assembler and citation layer need no edits.

    ``pg_id`` is ``"<source_id>:<source_feature_id>"`` — a composite of two
    upstream-stable identifiers rather than our internal UUID, so a citation
    stays resolvable against the survey itself even if our row is rebuilt.
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
    #: Seconds since the sync last confirmed this feature upstream. Real, not
    #: a placeholder zero.
    staleness_seconds: int | None = None
    #: Always 0.0 — nothing here is semantically ranked. See module docstring.
    relevance_score: float = 0.0


@dataclass
class PublicGeoscienceSearchResult:
    """Return type for search_public_geoscience."""

    records: list[PublicGeoscienceRecord]
    count: int
    jurisdictions_queried: list[str]
    canonical_types_queried: list[str]
    data_source: str = "public_geo.* (synced from provincial + federal survey APIs)"


def _build_query(types: list[str]) -> str:
    """UNION ALL one independently-limited SELECT per requested type.

    Each branch is limited on its own so a type with a million rows cannot
    crowd out a type with fifty. The caller asked for ``limit_per_type``, and
    a single outer LIMIT would not deliver that.
    """
    branches = [
        f"""(
    SELECT '{t}'::text AS canonical_type,
           jurisdiction_code,
           source_id,
           source_feature_id,
           {_NAME_EXPR[t]} AS name,
           {_COMMODITY_EXPR[t]} AS commodities,
           {_GROUPING_EXPR[t]} AS commodity_grouping,
           {_STATUS_EXPR[t]} AS status,
           last_seen_at,
           CASE WHEN geom IS NULL THEN NULL ELSE ARRAY[
               ST_XMin(geom), ST_YMin(geom), ST_XMax(geom), ST_YMax(geom)
           ] END AS bbox
      FROM {_TABLES[t]}
     WHERE ($1::text[] IS NULL OR jurisdiction_code = ANY($1::text[]))
       AND ($2::text   IS NULL OR {_NAME_EXPR[t]} ILIKE '%' || $2::text || '%')
       AND ($3::text[] IS NULL OR{_COMMODITY_MATCH.format(expr=_COMMODITY_EXPR[t])})
       AND ($4::float8 IS NULL
            OR (geom IS NOT NULL
                AND geom && ST_MakeEnvelope($4, $5, $6, $7, 4326)))
     -- Most recently reaffirmed upstream first: if the caller will see only
     -- six of four hundred, they should be the six the survey most recently
     -- confirmed still exist.
     ORDER BY last_seen_at DESC
     LIMIT $8::int
)"""
        for t in types
    ]
    return "\nUNION ALL\n".join(branches)


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
    """Search government-published geoscience records.

    Use this when the user asks about:
      - Government-published mineral occurrences, mines, drillholes, resource
        potential zones, rock samples, assessment surveys or mineral tenure.
      - Saskatchewan SMDI or BC MINFILE records.
      - "What's around X, Y" questions that hit external survey data rather
        than the internal project archive.

    When intent is ambiguous between internal archive and public geoscience,
    the orchestrator calls BOTH this and `search_documents`, and the response
    assembler labels results by surface.

    Args:
        jurisdiction_codes: Restrict to these jurisdictions ("CA-SK", "CA-BC").
            None/empty means every jurisdiction.
        canonical_types: Subset of the seven canonical types. None/empty means
            all of them.
        commodities: Commodity names or codes ("Au", "uranium"). Matched
            whole-value or whole-word — never as a substring.
        bbox: [minLon, minLat, maxLon, maxLat] in WGS84. The most selective
            filter available; runs against the GIST index on geom.
        text_query: Case-insensitive contains-match against the type's name
            column. NOT semantic search; see the module docstring.
        limit_per_type: Max records per canonical type. Capped at 25 so a bad
            caller cannot flood the prompt.

    Returns:
        PublicGeoscienceSearchResult. Any failure degrades to an empty result
        rather than raising, matching the convention in `app.agent.tools`.
    """
    t0 = time.monotonic()

    juris_list = _normalize_strings(jurisdiction_codes)
    types_to_query = [
        t for t in (_normalize_strings(canonical_types) or list(CANONICAL_TYPES))
        if t in _TABLES
    ]
    commodity_tokens = _commodity_tokens(commodities)
    effective_limit = max(
        1, min(int(limit_per_type or _DEFAULT_LIMIT_PER_TYPE), _MAX_LIMIT_PER_TYPE)
    )
    bbox_tuple = _normalize_bbox(bbox)

    empty = PublicGeoscienceSearchResult(
        records=[], count=0,
        jurisdictions_queried=juris_list,
        canonical_types_queried=types_to_query,
    )

    if not types_to_query:
        logger.info("search_public_geoscience: no valid canonical_types requested")
        return empty

    pool = getattr(getattr(ctx, "deps", None), "pg_pool", None)
    if pool is None:
        logger.warning("search_public_geoscience: deps.pg_pool is None — empty result")
        return empty

    sql = _build_query(types_to_query)
    args = (
        juris_list or None,
        (text_query or "").strip() or None,
        commodity_tokens or None,
        bbox_tuple[0] if bbox_tuple else None,
        bbox_tuple[1] if bbox_tuple else None,
        bbox_tuple[2] if bbox_tuple else None,
        bbox_tuple[3] if bbox_tuple else None,
        effective_limit,
    )

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args, timeout=_QUERY_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — degrade, never fail an answer
        logger.exception("search_public_geoscience: query failed")
        return empty

    records = [_to_record(r) for r in rows]

    logger.info(
        "search_public_geoscience: %d record(s) across %d type(s) in %.0fms "
        "(juris=%s bbox=%s text=%s commodities=%s)",
        len(records), len(types_to_query), (time.monotonic() - t0) * 1000,
        juris_list or "all", bool(bbox_tuple), bool(text_query),
        commodity_tokens or "any",
    )

    return PublicGeoscienceSearchResult(
        records=records,
        count=len(records),
        jurisdictions_queried=juris_list or sorted({r.jurisdiction_code for r in records}),
        canonical_types_queried=types_to_query,
    )


# ---------------------------------------------------------------------------
# Row -> record
# ---------------------------------------------------------------------------

def _to_record(row: Any) -> PublicGeoscienceRecord:
    canonical_type = row["canonical_type"]
    source_id = row["source_id"]
    feature_id = row["source_feature_id"]

    # Licence and display name come from the code registry rather than a join:
    # they are static addressing metadata, and keeping them out of the query
    # avoids two more table reads on every chat turn.
    src = source_by_id(source_id)
    juris = jurisdiction_for(src) if src else None

    commodities = [c for c in (row["commodities"] or []) if c]
    name = (
        row["name"]
        or f"{canonical_type.replace('_', ' ').title()} {feature_id or ''}".strip()
    )
    status = row["status"]

    return PublicGeoscienceRecord(
        pg_id=f"{source_id}:{feature_id}" if feature_id else source_id,
        canonical_type=canonical_type,
        jurisdiction_code=row["jurisdiction_code"],
        jurisdiction_name=juris.display_name if juris else None,
        source_id=source_id,
        source_feature_id=feature_id,
        name=name,
        summary_text=_summarize(
            name, canonical_type, commodities, status,
            (juris.display_name if juris else None) or row["jurisdiction_code"],
            src.name if src else source_id,
        ),
        commodities=commodities[:12],
        commodity_grouping=row["commodity_grouping"],
        status=status,
        geom_bbox=[float(v) for v in row["bbox"]] if row["bbox"] else None,
        source_url=src.service_url if src else None,
        license_summary=(juris.license_summary if juris else None)
        or (src.license_summary if src else None),
        license_url=(juris.license_url if juris else None)
        or (src.license_url if src else None),
        staleness_seconds=_staleness(row["last_seen_at"]),
        relevance_score=0.0,  # nothing here is semantically ranked
    )


def _staleness(last_seen_at: Any) -> int | None:
    """Seconds since the sync last confirmed this feature upstream."""
    if last_seen_at is None:
        return None
    seen = last_seen_at
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - seen).total_seconds()))


def _summarize(
    name: str,
    canonical_type: str,
    commodities: list[str],
    status: str | None,
    where: str,
    source_name: str,
) -> str:
    """Human-readable one-liner — what the LLM actually reads.

    Mirrors the sentence the stored pipeline used to bake into
    ``summary_text``, so prompts and the response assembler see a familiar
    shape.
    """
    bits = [f"{name} ({canonical_type.replace('_', ' ')}) in {where}"]
    if commodities:
        bits.append(f"Commodities: {', '.join(commodities[:8])}")
    if status:
        bits.append(f"Status: {status}")
    bits.append(f"Source: {source_name}")
    return ". ".join(bits) + "."


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalize_strings(values: Iterable[str] | None) -> list[str]:
    """Trim and drop blanks.

    ``None`` entries are dropped BEFORE stringification. Coercing first turns
    them into the literal ``"None"``, which is truthy and survives the filter,
    so a caller passing ``jurisdiction_codes=[None]`` would silently search for
    a jurisdiction named "None" and get an empty result that looks like
    "no data there".
    """
    if not values:
        return []
    out = []
    for v in values:
        if v is None:
            continue
        text = str(v).strip()
        if text:
            out.append(text)
    return out


def _commodity_tokens(values: Iterable[str] | None) -> list[str]:
    """Lowercase whole-value and whole-word forms to match against.

    A caller may pass "Rare Earth Elements"; the whole-value comparison
    handles that, and the individual words feed the array-overlap test for
    values the survey spells differently.
    """
    tokens: list[str] = []
    for raw in _normalize_strings(values):
        low = raw.lower()
        if low not in tokens:
            tokens.append(low)
        for word in re.split(r"[^a-z0-9]+", low):
            if word and word not in tokens:
                tokens.append(word)
    return tokens


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
