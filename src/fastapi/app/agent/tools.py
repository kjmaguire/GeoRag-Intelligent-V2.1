"""Pydantic AI tool implementations for the GeoRAG agent.

Each tool in this module is a grounded data-access function registered on
``geo_agent`` via ``@geo_agent.tool``.  The agent calls these tools to fetch
real numbers, spatial records, and knowledge-graph relationships — the LLM
synthesises and explains; it never generates numerical values itself
(hallucination prevention Layer 3).

Timeout discipline (Section 06e)
---------------------------------
Every database call is wrapped with ``asyncio.wait_for`` using the timeout
constants from ``app.config.settings``:

  PostGIS   5 s   TIMEOUT_POSTGIS_S
  Neo4j     3 s   TIMEOUT_NEO4J_S
  Qdrant    2 s   TIMEOUT_QDRANT_S

Partial results under timeout are surfaced as empty lists with a warning log
rather than propagating an exception — partial data is always preferable to a
hard failure when the agent has already started assembling a response.

Parallel fan-out
----------------
The caller (queries.py / geo_agent.py) is expected to dispatch the three
primary retrieval tools via asyncio.gather() per the critical pattern in
Section 05c.  The tools themselves are single-responsibility; they do not
know about each other.

Milestone notes
---------------
search_documents and traverse_knowledge_graph return empty results until the
embedding pipeline (Milestone 2) and graph population (Milestone 3) are
complete.  The agent is wired to handle empty tool results gracefully and will
report "insufficient information" rather than fabricating data.
"""

from __future__ import annotations

import asyncio

# ---------------------------------------------------------------------------
# P1 #16 — per-tool latency + result-count metrics.
# ---------------------------------------------------------------------------
# Decorator that wraps every tool function. Records two histograms:
#   - georag_tool_duration_seconds{tool, outcome="ok|timeout|error"}
#   - georag_tool_result_count{tool}
# Outcome label distinguishes a slow-but-successful tool from a slow-because-
# crashed one. The decorator is generic so adding a new tool is one line.
import functools as _functools  # noqa: E402
import logging
import re
import time as _metric_time  # noqa: E402
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext

from app.agent.deps import AgentDeps
from app.agent.hallucination.layer1_retrieval import filter_by_quality
from app.agent.log_safe import query_hash
from app.config import settings

# ---------------------------------------------------------------------------
# P2 #28 — Cypher identifier allowlist.
# ---------------------------------------------------------------------------
# `traverse_knowledge_graph` and `query_graph_by_label` interpolate the
# LLM-supplied `relationship_type` and `label` parameters DIRECTLY into the
# Cypher query string — Neo4j's parameterised-query API does not support
# parameterising labels or relationship types (this is a Cypher language
# limit, not a driver one). That makes those parameters the Cypher
# equivalent of P0 #2's SQL-injection vector: an LLM-supplied
# `relationship_type="HOSTS_IN] WITH count(*) AS x MATCH (n) DETACH DELETE n //"`
# would execute as a destructive query against the graph.
#
# Mirror the SQL allowlist pattern: maintain a hard-coded set of valid
# labels + relationship types pulled from the graph schema (silver/gold
# Dagster assets + scripts/populate_neo4j.py). Any value not on the
# allowlist is logged and the function returns an empty result instead
# of running the dangerous Cypher.

# Node labels — keep in sync with:
#   - src/fastapi/scripts/populate_neo4j.py (Project, DrillHole, Formation, ...)
#   - src/fastapi/app/agent/orchestrator.py:_LABEL_KEYWORDS
#   - src/fastapi/app/agent/viz_builder.py type_colors
#   - src/dagster/.../gold_public_geoscience.py (Public-Geoscience labels)
#   - docker/neo4j/warmup.cypher (warmup traversals reference labels)
_ALLOWED_GRAPH_LABELS: frozenset[str] = frozenset({
    # Internal project graph. Canonical drill-hole label is `DrillHole`
    # (PascalCase, §04f Global Invariant 4). The 2026-04-27 migration
    # (`ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher`) renamed
    # all live nodes from the legacy `:Drillhole` (lowercase h) form.
    "Project",
    "DrillHole",
    "Formation",
    "Report",
    "QualifiedPerson",
    "Deposit",
    "MineralOccurrence",
    "Commodity",
    "PublicGeoSource",  # observed in live data — single PG-source registry node
    "Document",
    # Public Geoscience graph (PG-side ingester labels)
    "Source",
    "Mine",
    "MineralDisposition",
    "Jurisdiction",
    "ResourcePotentialZone",
    "RockSample",
    "AssessmentSurvey",
    "GeophysicalSurvey",
    "Publication",
})

# Relationship types — pulled from MERGE statements in populate_neo4j.py
# and gold_public_geoscience.py. Adding a new relationship to the indexer
# REQUIRES adding it here too, or the LLM will silently lose the ability
# to filter by it.
_ALLOWED_GRAPH_RELATIONSHIPS: frozenset[str] = frozenset({
    # Project ↔ DrillHole
    "HAS_HOLE", "LOCATED_IN",
    # DrillHole ↔ Formation / Lithology
    "HAS_LITHOLOGY", "INTERSECTS",
    # Project ↔ Report ↔ QualifiedPerson
    "HAS_REPORT", "AUTHORED_BY",
    # Project ↔ Deposit ↔ Formation
    "HOSTS", "DESCRIBES", "HOSTED_BY",
    # Deposit ↔ MineralOccurrence ↔ DrillHole
    "HAS_MINERALIZATION", "TARGETS",
    # Formation hierarchy
    "PART_OF",
    # QP ↔ Project
    "WORKS_ON",
    # Public-Geoscience graph
    "PUBLISHED_BY", "SOURCED_FROM",
    "HAS_COMMODITY", "HAS_PRIMARY_COMMODITY", "HAS_ASSOCIATED_COMMODITY",
    "COVERS_AREA_FOR",
    # Neo4j review — observed in live data, were missing from allowlist:
    "MENTIONS",          # Document → entity references
    "REFERENCES",        # Report → cross-references
    "ANALOGOUS_TO",      # Deposit ↔ Deposit comparable links
})

# Defensive regex — used as a SECOND gate before the allowlist check so
# an LLM that fabricates a never-before-seen identifier with valid-looking
# casing (e.g. "Deposit_drop_table") gets rejected fast even if a future
# allowlist update accidentally widens the set. Cypher identifiers are
# [A-Za-z_][A-Za-z0-9_]* with a 64-char ceiling we apply for sanity.
_CYPHER_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _validate_cypher_label(label: str | None) -> str | None:
    """Return ``label`` if it's safe to interpolate into Cypher, else None.

    Two-stage gate:
      1. Regex match — must be a syntactically valid Cypher identifier.
         Catches any payload with `]`, `(`, ` `, `;`, or other Cypher
         metacharacters BEFORE we test the allowlist.
      2. Allowlist membership — must be on `_ALLOWED_GRAPH_LABELS`.

    Returns None on any failure with a structured warning log so operators
    can spot LLM probing attempts in Loki.
    """
    if not label:
        return None
    label = label.strip()
    if not _CYPHER_IDENTIFIER_RE.match(label):
        logger.warning(
            "_validate_cypher_label: rejected label=%r (regex fail)",
            label[:80],
        )
        return None
    if label not in _ALLOWED_GRAPH_LABELS:
        logger.warning(
            "_validate_cypher_label: rejected label=%r (not on allowlist)",
            label[:80],
        )
        return None
    return label


def _validate_cypher_relationship(rel_type: str | None) -> str | None:
    """Return ``rel_type`` if safe to interpolate, else None.

    Same two-stage gate as `_validate_cypher_label` but against the
    relationship-type allowlist. Returning None lets the caller fall
    back to the unfiltered `[r]` traversal — preserves the user's
    intent (find any related entity) without honouring the unsafe
    type filter.
    """
    if not rel_type:
        return None
    rel_type = rel_type.strip()
    if not _CYPHER_IDENTIFIER_RE.match(rel_type):
        logger.warning(
            "_validate_cypher_relationship: rejected rel_type=%r (regex fail)",
            rel_type[:80],
        )
        return None
    if rel_type not in _ALLOWED_GRAPH_RELATIONSHIPS:
        logger.warning(
            "_validate_cypher_relationship: rejected rel_type=%r (not on allowlist)",
            rel_type[:80],
        )
        return None
    return rel_type


def _metered(tool_name: str):
    """Wrap an agent tool with per-call latency + result-count metrics.

    Outcome buckets:
      ok        — tool returned normally (the metric records duration AND
                  result.count when the result has a `.count` attribute)
      timeout   — asyncio.TimeoutError reached the wrapper (tool's own
                  asyncio.wait_for guards usually catch this internally,
                  but the wrapper records anything that propagates)
      error     — any other exception. The exception is re-raised so the
                  orchestrator's partial-rescue path (P1 #7) still sees it.
    """
    def _decorate(fn):
        @_functools.wraps(fn)
        async def _wrapped(*args, **kwargs):
            t0 = _metric_time.monotonic()
            outcome = "ok"
            result = None
            try:
                result = await fn(*args, **kwargs)
                return result
            except TimeoutError:
                outcome = "timeout"
                raise
            except Exception:
                outcome = "error"
                raise
            finally:
                elapsed = _metric_time.monotonic() - t0
                try:
                    from app.metrics import (  # noqa: PLC0415
                        TOOL_DURATION,
                        TOOL_RESULT_COUNT,
                    )
                    TOOL_DURATION.labels(
                        tool=tool_name, outcome=outcome
                    ).observe(elapsed)
                    # Only record result_count on success — recording 0 for
                    # the error path would make the histogram lie about
                    # retrieval thinness.
                    if outcome == "ok" and result is not None:
                        n = getattr(result, "count", None)
                        if isinstance(n, int):
                            TOOL_RESULT_COUNT.labels(tool=tool_name).observe(n)
                except ImportError:
                    pass

        return _wrapped

    return _decorate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool return types
# ---------------------------------------------------------------------------


@dataclass
class CollarRecord:
    """Single drill-hole collar returned from the PostGIS spatial query.

    All numeric fields are sourced directly from the database; the agent must
    not alter or round these values (Layer 3: numerical claim verification).

    longitude / latitude are computed by PostGIS via ST_Transform(geom, 4326)
    so the orchestrator can build GeoJSON map payloads without duplicating
    projection logic. They are nullable because pre-M2 rows may not have
    valid geometries.
    """

    hole_id: str
    collar_id: str
    easting: float
    northing: float
    elevation: float
    total_depth: float
    hole_type: str
    azimuth: float
    dip: float
    status: str
    drill_date: str | None
    longitude: float | None = None
    latitude: float | None = None


@dataclass
class SpatialQueryResult:
    """Return type for query_spatial_collars."""

    collars: list[CollarRecord]
    count: int
    data_source: str  # always "PostGIS silver.collars" — supports provenance Layer 5


@dataclass
class DocumentChunk:
    """Single document chunk returned from Qdrant semantic search.

    Fields match the georag_reports collection payload schema produced by the
    Dagster ``index_reports`` asset.  The ``section_number`` and
    ``section_title`` fields allow precise citation (hallucination Layer 2).
    """

    chunk_id: str
    text: str
    source_document_id: str
    document_title: str
    section_number: str | None   # e.g. "14.1", "14.2" — NI 43-101 section numbering
    section_title: str | None    # e.g. "Mineral Resource Estimate"
    section: str | None          # backwards-compat: section_title or section_number
    page: int | None
    document_type: str           # "NI43", "PUB", or "DATA"
    report_id: str               # bronze.reports FK — enables cross-referencing
    relevance_score: float       # raw Qdrant score before cross-encoder reranking
    # Phase 3 (2026-05-22) — OCR provenance per chunk. NULL means the
    # passage was extracted from the PDF text layer (no OCR involved).
    # 0.0–1.0 = mean OCR engine confidence. ocr_method records which
    # engine produced the text: fitz_native | pdfplumber_native |
    # docling_rapidocr | tesseract. Phase 3 exposes these in
    # search_documents results WITHOUT filtering or weighting — the
    # Phase 6 OCR Quality Agent will set thresholds.
    ocr_confidence: float | None = None
    ocr_method: str | None = None


@dataclass
class DocumentSearchResult:
    """Return type for search_documents."""

    chunks: list[DocumentChunk]
    count: int
    data_source: str  # "Qdrant" — supports provenance Layer 5


@dataclass
class ProjectOverviewResult:
    """Return type for query_project_overview — exposes project-level
    metadata + dataset capabilities so the LLM can answer questions like
    "what company drilled this?", "what county and state?", "what
    measurements were collected?" without relying on document retrieval
    surfacing them.

    Phase F.9 (retrieval-routing gap fix): the deterministic classifier
    in Phase E.2.4 routes structured questions to spatial/document
    tools, which surface drillhole rows + chunks but not the project's
    own metadata. This result type lives alongside SpatialQueryResult
    so the same _is_empty_tool_result + citation_binding wiring picks
    it up automatically.
    """

    project_name: str
    company: str | None
    commodity: str | None
    region: str | None       # often "<county>, <state>" e.g. "CARBON, WY"
    slug: str | None
    collar_count: int        # total drillhole count in this project
    distinct_curves: list[str]  # log-curve names available in well_log_curves
    report_count: int        # total NI 43-101 + scanned-log reports in
                             #   silver.reports for this project. Surfaced so
                             #   doc-count questions ("how many reports do we
                             #   have?", "how many PDFs are indexed?") can be
                             #   answered without falling through to refusal.
    parser_breakdown: dict[str, int]  # parser_used → count over silver.reports
                             #   (pdfplumber / pdfminer.six / openpyxl /
                             #   cameco_log_binary / ocr / ...). Lets the LLM
                             #   answer "what file types are indexed?" too.
    count: int               # = collar_count + len(distinct_curves) +
                             #   report_count, for the generic
                             #   _is_empty_tool_result/_build_retrieval_summary
                             #   path which expects a `count` attribute
    data_source: str = "PostGIS silver.projects + silver.well_log_curves + silver.reports"


@dataclass
class GraphEntity:
    """Single entity returned from the Neo4j knowledge graph."""

    entity_id: str
    entity_type: str
    name: str
    properties: dict[str, str]
    relationship_type: str
    relationship_direction: str  # "INBOUND" | "OUTBOUND"


@dataclass
class GraphTraversalResult:
    """Return type for traverse_knowledge_graph."""

    entities: list[GraphEntity]
    count: int
    data_source: str  # "Neo4j" — supports provenance Layer 5


@dataclass
class NumericalClaimVerification:
    """Return type for verify_numerical_claim."""

    claim_value: float
    db_value: float | None
    verified: bool
    tolerance_used: float
    verification_query: str  # the SQL used — supports audit trail


@dataclass
class LithologyInterval:
    """Single lithology interval row returned from silver.lithology_logs.

    All numeric fields come directly from the database; the LLM must quote
    them verbatim (hallucination Layer 3 — numerical claim verification).
    """

    log_id: str
    collar_id: str
    hole_id: str
    from_depth: float
    to_depth: float
    lithology_code: str | None
    lithology_description: str | None
    grain_size: str | None
    color: str | None
    hardness: str | None
    rqd: float | None
    recovery: float | None
    weathering: str | None


@dataclass
class DownholeLogsResult:
    """Return type for query_downhole_logs.

    Scoped to a single hole_id + project_id. ``intervals`` is ordered by
    from_depth ASC so the LLM can walk the column top-to-bottom. ``collar``
    carries the same CollarRecord that query_spatial_collars would return
    so the response_assembler can build a proper [DATA-X] citation without
    re-querying.
    """

    collar: CollarRecord | None
    intervals: list[LithologyInterval]
    count: int
    data_source: str  # "PostGIS silver.lithology_logs"


@dataclass
class AssaySample:
    """Single assay sample with a resolved element value."""

    hole_id: str
    collar_id: str
    from_depth: float
    to_depth: float
    element: str       # e.g. "U3O8_ppm", "Au_ppb", "Cu_pct"
    value: float
    sample_type: str


@dataclass
class AssayDataResult:
    """Return type for query_assay_data.

    Contains both raw sample points (for Plotly traces) and pre-computed
    aggregates (for the LLM to quote verbatim).
    """

    samples: list[AssaySample]
    count: int
    element: str                    # the element that was queried
    available_elements: list[str]   # all elements in this project
    # Pre-computed aggregates so the LLM doesn't do arithmetic.
    min_value: float | None
    max_value: float | None
    mean_value: float | None
    median_value: float | None
    data_source: str  # "PostGIS silver.samples"


@dataclass
class CollarDetailsResult:
    """Return type for query_collar_details — structured "tell me about
    hole X" lookup.

    Combines a silver.collars row with aggregate counts and headline
    grades from silver.assays_v2 / silver.lithology(_logs) / silver.samples
    / silver.structure so the LLM can answer factual hole-level questions
    without re-querying. Every numeric field comes directly from the
    database; the LLM must cite them verbatim (Layer 3 — numerical claim
    verification).

    ``count`` is 1 when a hole is found and 0 when not, so the
    ``_is_empty_tool_result`` filter in the orchestrator drops misses
    cleanly. ``source_row_ids`` is the [collar_id] for §04i citation
    binding through ``_extract_source_id`` in response_assembler.
    """

    collar_id: str | None
    hole_id: str | None
    hole_id_canonical: str | None
    project_id: str
    workspace_id: str
    total_depth: float | None
    drill_type: str | None
    hole_type: str | None
    drill_date: str | None
    easting: float | None
    northing: float | None
    elevation: float | None
    azimuth: float | None
    dip: float | None
    geologist: str | None
    # Aggregates over downstream tables.
    assay_count: int
    lithology_count: int
    sample_count: int
    structure_count: int
    max_assay_value: dict | None  # {element, value, unit, depth_from, depth_to}
    lithology_summary: list[dict]  # top-N {rock_code, total_metres}
    # Citation.
    source_row_ids: list[str]
    count: int  # 1 if found, 0 if not — drives _is_empty_tool_result
    data_source: str = (
        "PostGIS silver.collars + silver.assays_v2 + silver.lithology_logs "
        "+ silver.samples + silver.structure"
    )


# ---------------------------------------------------------------------------
# Lazy import of geo_agent to avoid circular imports.
# The tools module is imported by geo_agent.py; registering tools here via
# @geo_agent.tool would create a circular import.  Instead, geo_agent.py
# imports and registers all tools explicitly after creating the agent.
# The tool functions below are plain async functions that accept a RunContext;
# geo_agent.py wraps them with @geo_agent.tool.
# ---------------------------------------------------------------------------


@_metered("query_spatial_collars")
async def query_spatial_collars(
    ctx: RunContext[AgentDeps],
    project_id: str,
    center_easting: float | None = None,
    center_northing: float | None = None,
    radius_m: float | None = None,
    hole_type: str | None = None,
    status_filter: str | None = None,
    limit: int = 50,
) -> SpatialQueryResult:
    """Query PostGIS for drill-hole collar records within a project scope.

    Use this tool when the user asks about:
    - Which drill holes exist in a project or area
    - Collar locations, depths, orientations, or statuses
    - Spatial distribution of drilling (how many holes, where)
    - Specific hole IDs, eastings, northings, or total depths
    - Filtering holes by type (Diamond, RC, RAB) or status (Active, Completed)

    When center_easting, center_northing, and radius_m are all provided the
    query is spatially filtered using PostGIS ST_DWithin.  Without spatial
    parameters all collars for the project are returned (up to limit).

    All numeric values in the returned records come directly from the database
    and must be cited verbatim — do not round or paraphrase grade/depth values.

    Args:
        project_id: UUID of the project to scope the query.
        center_easting: UTM easting of the search centre (metres).
        center_northing: UTM northing of the search centre (metres).
        radius_m: Search radius in metres around the centre point.
        hole_type: Optional filter — "Diamond", "RC", "RAB", "Rotary", or "Percussion".
        status_filter: Optional filter — "Active", "Completed", or "Abandoned".
        limit: Maximum number of collar records to return (default 50, max 200).

    Returns:
        SpatialQueryResult with collars list and count.
    """
    limit = min(limit, 200)

    spatial_filter = ""
    bind_args: list = [project_id]
    param_idx = 2  # $1 already used for project_id

    if center_easting is not None and center_northing is not None and radius_m is not None:
        # ST_DWithin operates on the geometry column; the CRS must match the
        # project's SRID.  Using the point geometry constructor directly with
        # the stored SRID avoids a unit mismatch between geographic and
        # projected CRS.
        spatial_filter = (
            f" AND ST_DWithin(geom, ST_SetSRID(ST_MakePoint(${param_idx}, ${param_idx + 1}), "
            f"Find_SRID('silver', 'collars', 'geom')), ${param_idx + 2})"
        )
        bind_args.extend([center_easting, center_northing, radius_m])
        param_idx += 3

    type_filter = ""
    if hole_type:
        type_filter = f" AND hole_type = ${param_idx}"
        bind_args.append(hole_type)
        param_idx += 1

    status_clause = ""
    if status_filter:
        status_clause = f" AND status = ${param_idx}"
        bind_args.append(status_filter)
        param_idx += 1

    limit_clause = f" LIMIT ${param_idx}"
    bind_args.append(limit)

    sql = (
        "SELECT collar_id::text, hole_id, project_id::text, "
        "ST_X(geom) AS easting, ST_Y(geom) AS northing, elevation, "
        "total_depth, hole_type, azimuth, dip, status, "
        "drill_date::text, "
        "ST_X(ST_Transform(geom, 4326)) AS longitude, "
        "ST_Y(ST_Transform(geom, 4326)) AS latitude "
        "FROM silver.collars "
        f"WHERE project_id = $1{spatial_filter}{type_filter}{status_clause}"
        f" ORDER BY hole_id{limit_clause}"
    )

    logger.info(
        "query_spatial_collars: project=%s radius=%s hole_type=%s status=%s limit=%s",
        project_id,
        radius_m,
        hole_type,
        status_filter,
        limit,
    )

    async def _run_query() -> list[CollarRecord]:
        async with ctx.deps.pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, *bind_args)
            return [
                CollarRecord(
                    hole_id=row["hole_id"],
                    collar_id=row["collar_id"],
                    easting=row["easting"],
                    northing=row["northing"],
                    elevation=row["elevation"],
                    total_depth=row["total_depth"],
                    hole_type=row["hole_type"],
                    azimuth=row["azimuth"],
                    dip=row["dip"],
                    status=row["status"],
                    drill_date=row["drill_date"],
                    longitude=row["longitude"],
                    latitude=row["latitude"],
                )
                for row in rows
            ]

    try:
        collars = await asyncio.wait_for(_run_query(), timeout=settings.TIMEOUT_POSTGIS_S)
    except TimeoutError:
        logger.warning(
            "query_spatial_collars timed out after %.1fs for project=%s",
            settings.TIMEOUT_POSTGIS_S,
            project_id,
        )
        collars = []
    except Exception:
        logger.exception("query_spatial_collars failed for project=%s", project_id)
        collars = []

    return SpatialQueryResult(
        collars=collars,
        count=len(collars),
        data_source="PostGIS silver.collars",
    )


@_metered("query_project_overview")
async def query_project_overview(
    ctx: RunContext[AgentDeps],
    project_id: str,
) -> ProjectOverviewResult:
    """Fetch project metadata + dataset capabilities for the active project.

    Phase F.9 wiring: surfaces the project's company / commodity / region
    columns plus the distinct log-curve names available in
    silver.well_log_curves, so the LLM can answer questions like:

      * "What company drilled the holes?"
      * "What county and state is the project in?"
      * "What geophysical measurements were collected?"
      * "Does the dataset include uranium grade measurements?"

    Without this tool the orchestrator's deterministic classifier routes
    those questions to spatial+documents, which surface drillhole rows
    + chunks but not the metadata answering the question. The model then
    refuses ("evidence does not include information about ...") even
    though the answer is one column away.

    Args:
        project_id: UUID of the active project.

    Returns:
        ProjectOverviewResult with project_name, company, commodity,
        region, slug, collar_count, and distinct_curves. ``count`` is the
        sum (collar_count + len(distinct_curves)) so the orchestrator's
        empty-tool-result filter (Phase F.4) drops the result only when
        BOTH halves are empty.
    """
    project_sql = (
        "SELECT project_name, company, commodity, region, slug "
        "FROM silver.projects WHERE project_id = $1"
    )
    collar_count_sql = (
        "SELECT count(*) AS n FROM silver.collars WHERE project_id = $1"
    )
    curves_sql = (
        "SELECT DISTINCT wlc.curve_name "
        "FROM silver.well_log_curves wlc "
        "JOIN silver.collars c ON c.collar_id = wlc.collar_id "
        "WHERE c.project_id = $1 "
        "ORDER BY wlc.curve_name"
    )
    # Doc-count wiring: report rollup over silver.reports for the active
    # project. Empty / NULL parser values bucketed as 'unknown' so the
    # breakdown sums to the same total as the count query.
    reports_count_sql = (
        "SELECT count(*) AS n FROM silver.reports WHERE project_id = $1"
    )
    reports_breakdown_sql = (
        "SELECT COALESCE(parser_used, 'unknown') AS parser, count(*) AS n "
        "FROM silver.reports WHERE project_id = $1 "
        "GROUP BY parser ORDER BY n DESC"
    )

    project_name = ""
    company = commodity = region = slug = None
    collar_count = 0
    distinct_curves: list[str] = []
    report_count = 0
    parser_breakdown: dict[str, int] = {}

    async def _run() -> None:
        nonlocal project_name, company, commodity, region, slug
        nonlocal collar_count, distinct_curves, report_count, parser_breakdown
        async with ctx.deps.pg_pool.acquire() as conn:
            row = await conn.fetchrow(project_sql, project_id)
            if row is not None:
                project_name = row["project_name"] or ""
                company = row["company"]
                commodity = row["commodity"]
                region = row["region"]
                slug = row["slug"]
            count_row = await conn.fetchrow(collar_count_sql, project_id)
            collar_count = int(count_row["n"]) if count_row else 0
            curve_rows = await conn.fetch(curves_sql, project_id)
            distinct_curves = [r["curve_name"] for r in curve_rows]
            reports_row = await conn.fetchrow(reports_count_sql, project_id)
            report_count = int(reports_row["n"]) if reports_row else 0
            breakdown_rows = await conn.fetch(reports_breakdown_sql, project_id)
            parser_breakdown = {r["parser"]: int(r["n"]) for r in breakdown_rows}

    logger.info("query_project_overview: project=%s", project_id)
    try:
        await asyncio.wait_for(_run(), timeout=settings.TIMEOUT_POSTGIS_S)
    except TimeoutError:
        logger.warning(
            "query_project_overview timed out after %.1fs for project=%s",
            settings.TIMEOUT_POSTGIS_S,
            project_id,
        )
    except Exception:
        logger.exception(
            "query_project_overview failed for project=%s", project_id
        )

    # `count` is the union signal for the empty-result filter — if the
    # project genuinely has no metadata AND no curves AND no reports, the
    # result is empty and Phase F.4's filter drops it. Otherwise we ship.
    count = collar_count + len(distinct_curves) + report_count
    return ProjectOverviewResult(
        project_name=project_name,
        company=company,
        commodity=commodity,
        region=region,
        slug=slug,
        collar_count=collar_count,
        distinct_curves=distinct_curves,
        report_count=report_count,
        parser_breakdown=parser_breakdown,
        count=count,
    )


@_metered("query_downhole_logs")
async def query_downhole_logs(
    ctx: RunContext[AgentDeps],
    project_id: str,
    hole_id: str,
) -> DownholeLogsResult:
    """Fetch the full lithology column (and collar header) for a single hole.

    This tool is invoked by the orchestrator whenever the user names a
    specific drill hole AND their query has strip-log / downhole /
    lithology / interval intent. It joins silver.collars to
    silver.lithology_logs and returns every interval ordered by from_depth.

    Returned data is authoritative — the LLM must quote depths, RQD and
    recovery values verbatim (Layer 3). Empty results are returned as a
    valid DownholeLogsResult with ``count=0`` so the orchestrator can
    surface a structured "no logs for this hole" context block rather
    than silently refusing.

    Args:
        project_id: UUID of the active project. Required for row-level
            scoping — never trust a hole_id in isolation because hole
            names can repeat across projects.
        hole_id: Exact hole identifier as stored in silver.collars
            (e.g. "PLS-20-01"). Matching is case-insensitive.

    Returns:
        DownholeLogsResult with the collar record and an ordered list of
        LithologyInterval rows. On timeout or database error we log and
        return an empty result rather than raising — partial data is
        always preferable to a hard failure mid-stream.
    """
    logger.info(
        "query_downhole_logs: project=%s hole_id=%s",
        project_id,
        hole_id,
    )

    # Pull the collar header in the same query round-trip so the assembler
    # can cite it without issuing a second fetch. ST_Transform gives us
    # WGS84 coords for free in case a future tool wants to draw a map
    # pinned to this single hole.
    collar_sql = (
        "SELECT collar_id::text, hole_id, project_id::text, "
        "ST_X(geom) AS easting, ST_Y(geom) AS northing, elevation, "
        "total_depth, hole_type, azimuth, dip, status, drill_date::text, "
        "ST_X(ST_Transform(geom, 4326)) AS longitude, "
        "ST_Y(ST_Transform(geom, 4326)) AS latitude "
        "FROM silver.collars "
        "WHERE project_id = $1 AND UPPER(hole_id) = UPPER($2) "
        "LIMIT 1"
    )

    logs_sql = (
        "SELECT l.log_id::text, l.collar_id::text, c.hole_id, "
        "l.from_depth, l.to_depth, l.lithology_code, "
        "l.lithology_description, l.grain_size, l.color, l.hardness, "
        "l.rqd, l.recovery, l.weathering "
        "FROM silver.lithology_logs l "
        "JOIN silver.collars c ON c.collar_id = l.collar_id "
        "WHERE c.project_id = $1 AND UPPER(c.hole_id) = UPPER($2) "
        "ORDER BY l.from_depth ASC"
    )

    async def _run() -> tuple[CollarRecord | None, list[LithologyInterval]]:
        async with ctx.deps.pg_pool.acquire() as conn:
            collar_row = await conn.fetchrow(collar_sql, project_id, hole_id)
            log_rows = await conn.fetch(logs_sql, project_id, hole_id)

        collar_rec: CollarRecord | None = None
        if collar_row is not None:
            collar_rec = CollarRecord(
                hole_id=collar_row["hole_id"],
                collar_id=collar_row["collar_id"],
                easting=collar_row["easting"],
                northing=collar_row["northing"],
                elevation=collar_row["elevation"],
                total_depth=collar_row["total_depth"],
                hole_type=collar_row["hole_type"],
                azimuth=collar_row["azimuth"],
                dip=collar_row["dip"],
                status=collar_row["status"],
                drill_date=collar_row["drill_date"],
                longitude=collar_row["longitude"],
                latitude=collar_row["latitude"],
            )

        intervals = [
            LithologyInterval(
                log_id=row["log_id"],
                collar_id=row["collar_id"],
                hole_id=row["hole_id"],
                from_depth=row["from_depth"],
                to_depth=row["to_depth"],
                lithology_code=row["lithology_code"],
                lithology_description=row["lithology_description"],
                grain_size=row["grain_size"],
                color=row["color"],
                hardness=row["hardness"],
                rqd=row["rqd"],
                recovery=row["recovery"],
                weathering=row["weathering"],
            )
            for row in log_rows
        ]
        return collar_rec, intervals

    try:
        collar, intervals = await asyncio.wait_for(
            _run(),
            timeout=settings.TIMEOUT_POSTGIS_S,
        )
    except TimeoutError:
        logger.warning(
            "query_downhole_logs timed out after %.1fs project=%s hole=%s",
            settings.TIMEOUT_POSTGIS_S,
            project_id,
            hole_id,
        )
        collar, intervals = None, []
    except Exception:
        logger.exception(
            "query_downhole_logs failed project=%s hole=%s",
            project_id,
            hole_id,
        )
        collar, intervals = None, []

    logger.info(
        "query_downhole_logs: project=%s hole=%s intervals=%d collar_found=%s",
        project_id,
        hole_id,
        len(intervals),
        collar is not None,
    )

    return DownholeLogsResult(
        collar=collar,
        intervals=intervals,
        count=len(intervals),
        data_source="PostGIS silver.lithology_logs",
    )


@_metered("query_collar_details")
async def query_collar_details(
    deps: AgentDeps,
    workspace_id: str,
    project_id: str,
    hole_id: str,
) -> CollarDetailsResult:
    """Fetch a single drill-hole's structured profile (collar + aggregates).

    Used by the agentic-retrieval ``factual_lookup`` path when the user
    names a specific hole ("tell me about hole 36-1085"). Returns one
    CollarDetailsResult with collar header + downstream-table aggregates
    so the LLM can answer hole-level questions in one tool call.

    Hole matching is intentionally lenient: matches against ``hole_id``
    case-insensitively, then against ``hole_id_canonical`` (NULL on 282 of
    567 collars as of 2026-05-25 — the data-engineer's backfill is
    parallel work, this tool must work today), then as a substring of
    ``hole_id``. The first match wins.

    Args:
        deps: AgentDeps with pg_pool, workspace_id, project_id.
        workspace_id: Tenancy scope — always passed to the WHERE clause.
        project_id: Project scope — always passed to the WHERE clause.
        hole_id: Exact or partial hole identifier (e.g. "36-1085").

    Returns:
        CollarDetailsResult. ``count`` is 1 when a hole is found and 0
        when the lookup misses; on database error or timeout we log and
        return count=0 with all fields None so the response_assembler can
        refuse cleanly.
    """
    logger.info(
        "query_collar_details: workspace=%s project=%s hole_id=%s",
        workspace_id,
        project_id,
        hole_id,
    )

    empty = CollarDetailsResult(
        collar_id=None,
        hole_id=None,
        hole_id_canonical=None,
        project_id=project_id,
        workspace_id=workspace_id,
        total_depth=None,
        drill_type=None,
        hole_type=None,
        drill_date=None,
        easting=None,
        northing=None,
        elevation=None,
        azimuth=None,
        dip=None,
        geologist=None,
        assay_count=0,
        lithology_count=0,
        sample_count=0,
        structure_count=0,
        max_assay_value=None,
        lithology_summary=[],
        source_row_ids=[],
        count=0,
    )

    # ── Collar header. Match in priority order: exact hole_id, canonical,
    # substring. The single SELECT walks each branch via UNION ALL so we
    # get one round-trip and the LIMIT 1 + ORDER BY priority chooses the
    # best match. workspace_id + project_id are ALWAYS in the WHERE.
    collar_sql = (
        "SELECT collar_id::text, hole_id, hole_id_canonical, project_id::text, "
        "ST_X(geom) AS easting, ST_Y(geom) AS northing, elevation, "
        "total_depth, drill_type, hole_type, azimuth, dip, "
        "drill_date::text, geologist, match_priority "
        "FROM ("
        "  SELECT *, 1 AS match_priority FROM silver.collars "
        "  WHERE workspace_id = $1::uuid AND project_id = $2::uuid "
        "    AND UPPER(hole_id) = UPPER($3) "
        "  UNION ALL "
        "  SELECT *, 2 AS match_priority FROM silver.collars "
        "  WHERE workspace_id = $1::uuid AND project_id = $2::uuid "
        "    AND hole_id_canonical IS NOT NULL "
        "    AND UPPER(hole_id_canonical) = UPPER($3) "
        "  UNION ALL "
        "  SELECT *, 3 AS match_priority FROM silver.collars "
        "  WHERE workspace_id = $1::uuid AND project_id = $2::uuid "
        "    AND hole_id ILIKE '%' || $3 || '%' "
        "    AND UPPER(hole_id) <> UPPER($3) "
        ") matches "
        "ORDER BY match_priority ASC, hole_id ASC "
        "LIMIT 1"
    )

    # ── Aggregates. Cheap COUNT()s + one MAX() per element bucket.
    # silver.assays_v2 (collar_id-keyed) is preferred; we also count the
    # legacy silver.samples join. silver.lithology_logs is the
    # always-populated legacy table; silver.lithology is the v2 sibling
    # we sum metres on when present.
    assay_counts_sql = (
        "SELECT COUNT(*) AS n FROM silver.assays_v2 "
        "WHERE workspace_id = $1::uuid AND collar_id = $2::uuid"
    )
    sample_counts_sql = (
        "SELECT COUNT(*) AS n FROM silver.samples "
        "WHERE collar_id = $1::uuid"
    )
    litho_counts_sql = (
        "SELECT COUNT(*) AS n FROM silver.lithology_logs "
        "WHERE collar_id = $1::uuid"
    )
    structure_counts_sql = (
        "SELECT COUNT(*) AS n FROM silver.structure "
        "WHERE workspace_id = $1::uuid AND collar_id = $2::uuid"
    )
    max_assay_sql = (
        "SELECT element, value, unit, from_depth, to_depth "
        "FROM silver.assays_v2 "
        "WHERE workspace_id = $1::uuid AND collar_id = $2::uuid "
        "  AND value IS NOT NULL "
        "ORDER BY value DESC NULLS LAST "
        "LIMIT 1"
    )
    # Lithology summary — sum metres per rock_code from the populated
    # lithology_logs table (which has lithology_code, not rock_code).
    litho_summary_sql = (
        "SELECT lithology_code AS code, "
        "       SUM(to_depth - from_depth) AS total_m "
        "FROM silver.lithology_logs "
        "WHERE collar_id = $1::uuid "
        "  AND lithology_code IS NOT NULL "
        "GROUP BY lithology_code "
        "ORDER BY total_m DESC NULLS LAST "
        "LIMIT 5"
    )

    async def _run() -> CollarDetailsResult:
        async with deps.pg_pool.acquire() as conn:
            collar_row = await conn.fetchrow(
                collar_sql, workspace_id, project_id, hole_id
            )
            if collar_row is None:
                return empty

            collar_id = collar_row["collar_id"]

            assay_count = 0
            sample_count = 0
            litho_count = 0
            structure_count = 0
            max_assay: dict | None = None
            litho_summary: list[dict] = []

            try:
                row = await conn.fetchrow(
                    assay_counts_sql, workspace_id, collar_id
                )
                assay_count = int(row["n"]) if row and row["n"] is not None else 0
            except Exception:
                logger.exception(
                    "query_collar_details: assay_counts failed collar=%s",
                    collar_id,
                )

            try:
                row = await conn.fetchrow(sample_counts_sql, collar_id)
                sample_count = int(row["n"]) if row and row["n"] is not None else 0
            except Exception:
                logger.exception(
                    "query_collar_details: sample_counts failed collar=%s",
                    collar_id,
                )

            try:
                row = await conn.fetchrow(litho_counts_sql, collar_id)
                litho_count = int(row["n"]) if row and row["n"] is not None else 0
            except Exception:
                logger.exception(
                    "query_collar_details: litho_counts failed collar=%s",
                    collar_id,
                )

            try:
                row = await conn.fetchrow(
                    structure_counts_sql, workspace_id, collar_id
                )
                structure_count = (
                    int(row["n"]) if row and row["n"] is not None else 0
                )
            except Exception:
                logger.exception(
                    "query_collar_details: structure_counts failed collar=%s",
                    collar_id,
                )

            if assay_count > 0:
                try:
                    row = await conn.fetchrow(
                        max_assay_sql, workspace_id, collar_id
                    )
                    if row is not None:
                        max_assay = {
                            "element": row["element"],
                            "value": float(row["value"]) if row["value"] is not None else None,
                            "unit": row["unit"],
                            "depth_from": float(row["from_depth"]) if row["from_depth"] is not None else None,
                            "depth_to": float(row["to_depth"]) if row["to_depth"] is not None else None,
                        }
                except Exception:
                    logger.exception(
                        "query_collar_details: max_assay failed collar=%s",
                        collar_id,
                    )

            if litho_count > 0:
                try:
                    rows = await conn.fetch(litho_summary_sql, collar_id)
                    litho_summary = [
                        {
                            "rock_code": r["code"],
                            "total_metres": float(r["total_m"]) if r["total_m"] is not None else 0.0,
                        }
                        for r in rows
                    ]
                except Exception:
                    logger.exception(
                        "query_collar_details: litho_summary failed collar=%s",
                        collar_id,
                    )

            return CollarDetailsResult(
                collar_id=collar_id,
                hole_id=collar_row["hole_id"],
                hole_id_canonical=collar_row["hole_id_canonical"],
                project_id=project_id,
                workspace_id=workspace_id,
                total_depth=(
                    float(collar_row["total_depth"])
                    if collar_row["total_depth"] is not None
                    else None
                ),
                drill_type=collar_row["drill_type"],
                hole_type=collar_row["hole_type"],
                drill_date=collar_row["drill_date"],
                easting=(
                    float(collar_row["easting"])
                    if collar_row["easting"] is not None
                    else None
                ),
                northing=(
                    float(collar_row["northing"])
                    if collar_row["northing"] is not None
                    else None
                ),
                elevation=(
                    float(collar_row["elevation"])
                    if collar_row["elevation"] is not None
                    else None
                ),
                azimuth=(
                    float(collar_row["azimuth"])
                    if collar_row["azimuth"] is not None
                    else None
                ),
                dip=(
                    float(collar_row["dip"])
                    if collar_row["dip"] is not None
                    else None
                ),
                geologist=collar_row["geologist"],
                assay_count=assay_count,
                lithology_count=litho_count,
                sample_count=sample_count,
                structure_count=structure_count,
                max_assay_value=max_assay,
                lithology_summary=litho_summary,
                source_row_ids=[collar_id],
                count=1,
            )

    try:
        result = await asyncio.wait_for(
            _run(), timeout=settings.TIMEOUT_POSTGIS_S
        )
    except TimeoutError:
        logger.warning(
            "query_collar_details timed out after %.1fs hole=%s",
            settings.TIMEOUT_POSTGIS_S,
            hole_id,
        )
        return empty
    except Exception:
        logger.exception(
            "query_collar_details failed workspace=%s project=%s hole=%s",
            workspace_id,
            project_id,
            hole_id,
        )
        return empty

    logger.info(
        "query_collar_details: hole=%s match=%s assays=%d litho=%d samples=%d "
        "structure=%d",
        hole_id,
        result.hole_id,
        result.assay_count,
        result.lithology_count,
        result.sample_count,
        result.structure_count,
    )
    return result


@_metered("query_assay_data")
async def query_assay_data(
    ctx: RunContext[AgentDeps],
    project_id: str,
    element: str | None = None,
    hole_id: str | None = None,
    limit: int = 5000,
) -> AssayDataResult:
    """Fetch assay sample values from silver.samples for plotting + narration.

    Returns raw sample points (for Plotly histogram/scatter traces) and
    pre-computed aggregates (min, max, mean, median) so the LLM can quote
    statistics verbatim without doing arithmetic.

    The ``commodity_assays`` column is JSONB with keys like ``U3O8_ppm``,
    ``Au_ppb``, ``Cu_pct``.  If ``element`` is not specified, we detect the
    first available element in the project.

    P1 #29 — LIMIT cap. A real exploration project can have 50k+ assays;
    pulling all of them and serialising to the LLM context is wasteful and
    risks blowing the token budget. We cap raw sample rows to ``limit``
    (default 5000) but compute aggregates (min/max/mean/median, sample
    count) over the FULL unfiltered set in SQL — so statistics stay
    correct even when the rendered sample list is truncated.

    Args:
        project_id: UUID scope.
        element: JSONB key name (e.g. "U3O8_ppm"). None → auto-detect primary.
        hole_id: Optional filter to a single hole.
        limit: Max raw sample rows to return (1 to 20000). Aggregates are
            ALWAYS computed over the full unfiltered set, so capping
            ``limit`` only affects what's available for plotting, not the
            stats the LLM cites.

    Returns:
        AssayDataResult with raw samples (≤ limit), aggregates over the
        full set, and available elements.
    """
    # Guard the cap. The agent can ask for arbitrarily large limits via
    # tool calls; clamp here so a misrouted query can't OOM the worker.
    limit = max(1, min(int(limit or 5000), 20000))
    logger.info(
        "query_assay_data: project=%s element=%s hole=%s",
        project_id,
        element,
        hole_id,
    )

    async def _run() -> AssayDataResult:
        async with ctx.deps.pg_pool.acquire() as conn:
            # Step 1: discover available elements in this project.
            avail_sql = (
                "SELECT DISTINCT jsonb_object_keys(s.commodity_assays) AS elem "
                "FROM silver.samples s "
                "JOIN silver.collars c ON c.collar_id = s.collar_id "
                "WHERE c.project_id = $1 "
                "ORDER BY elem"
            )
            avail_rows = await conn.fetch(avail_sql, project_id)
            available = [r["elem"] for r in avail_rows]

            if not available:
                return AssayDataResult(
                    samples=[],
                    count=0,
                    element=element or "",
                    available_elements=[],
                    min_value=None,
                    max_value=None,
                    mean_value=None,
                    median_value=None,
                    data_source="PostGIS silver.samples",
                )

            # Auto-detect primary element. Prefer derived composites
            # (`U3O8_pct_e` / `Au_ppb_e` / `Cu_pct_e`) when they're the
            # populated path, because for some projects the only
            # populated grade column is the derived effective grade
            # (e.g. Cameco Shirley Basin: 2,332 derived composites vs.
            # 4 Core U3O8_ppm rows). Falling through to U3O8_ppm there
            # produces a 4-row "assay" answer that the LLM correctly
            # refuses, instead of the genuine project-wide grade.
            chosen = element
            if chosen is None:
                preferred_order = (
                    "U3O8_pct_e", "U3O8_ppm",
                    "Au_ppb_e", "Au_ppb",
                    "Cu_pct_e", "Cu_pct",
                )
                for preferred in preferred_order:
                    if preferred in available:
                        chosen = preferred
                        break
                if chosen is None:
                    chosen = available[0]
            elif chosen not in available:
                # Caller asked for an element that doesn't exist in this
                # project's samples (e.g. orchestrator routed "uranium
                # grade" to "U3O8_ppm" but only the derived "U3O8_pct_e"
                # is populated). Substitute the closest available related
                # key rather than returning an empty result.
                alias_map = {
                    "U3O8_ppm":  "U3O8_pct_e",
                    "U3O8_pct":  "U3O8_pct_e",
                    "U3O8_pct_e": "U3O8_ppm",
                    "Au_ppb":    "Au_ppb_e",
                    "Au_ppb_e":  "Au_ppb",
                    "Cu_pct":    "Cu_pct_e",
                    "Cu_pct_e":  "Cu_pct",
                }
                alt = alias_map.get(chosen)
                if alt and alt in available:
                    logger.info(
                        "query_assay_data: substituting element %s -> %s (caller-requested key absent, alias populated)",
                        chosen, alt,
                    )
                    chosen = alt

            # Step 2: aggregates computed over the FULL unfiltered set in
            # SQL so they don't degrade when we cap raw rows for context
            # budget. PERCENTILE_CONT gives us a true median (not the
            # discrete-row-pick that the old Python fallback used).
            hole_filter = ""
            bind = [project_id, chosen]
            param_idx = 3
            if hole_id:
                hole_filter = f" AND UPPER(c.hole_id) = UPPER(${param_idx})"
                bind.append(hole_id)
                param_idx += 1

            agg_sql = (
                "SELECT "
                "  MIN(val) AS min_v, MAX(val) AS max_v, "
                "  AVG(val) AS mean_v, "
                "  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY val) AS median_v, "
                "  COUNT(*) AS total_n "
                "FROM ("
                "  SELECT (s.commodity_assays->>$2)::double precision AS val "
                "  FROM silver.samples s "
                "  JOIN silver.collars c ON c.collar_id = s.collar_id "
                f"  WHERE c.project_id = $1 AND s.commodity_assays ? $2{hole_filter} "
                ") sub "
                "WHERE val IS NOT NULL"
            )
            agg_row = await conn.fetchrow(agg_sql, *bind)

            total_n = int(agg_row["total_n"]) if agg_row and agg_row["total_n"] else 0
            if total_n == 0:
                return AssayDataResult(
                    samples=[],
                    count=0,
                    element=chosen,
                    available_elements=available,
                    min_value=None,
                    max_value=None,
                    mean_value=None,
                    median_value=None,
                    data_source="PostGIS silver.samples",
                )

            # Step 3: fetch raw sample rows for plotting. P1 #29 — apply
            # LIMIT so a 50k-assay project doesn't blow the LLM context.
            # Aggregates above already used the full set, so capping the
            # row list does NOT distort statistics the LLM will cite.
            limit_idx = param_idx
            data_sql = (
                "SELECT c.hole_id, s.collar_id::text, s.from_depth, s.to_depth, "
                "       s.sample_type, "
                "      (s.commodity_assays->>$2)::double precision AS val "
                "FROM silver.samples s "
                "JOIN silver.collars c ON c.collar_id = s.collar_id "
                f"WHERE c.project_id = $1 AND s.commodity_assays ? $2{hole_filter} "
                "  AND (s.commodity_assays->>$2) IS NOT NULL "
                f"ORDER BY c.hole_id, s.from_depth "
                f"LIMIT ${limit_idx}"
            )
            rows = await conn.fetch(data_sql, *bind, limit)

            samples = [
                AssaySample(
                    hole_id=r["hole_id"],
                    collar_id=r["collar_id"],
                    from_depth=r["from_depth"],
                    to_depth=r["to_depth"],
                    element=chosen,
                    value=r["val"],
                    sample_type=r["sample_type"],
                )
                for r in rows
                if r["val"] is not None
            ]

            if total_n > len(samples):
                logger.info(
                    "query_assay_data: capped raw rows at %d of %d total "
                    "(aggregates still cover the full set) project=%s element=%s",
                    len(samples), total_n, project_id, chosen,
                )

            return AssayDataResult(
                samples=samples,
                # `count` reflects the FULL set so the LLM doesn't cite a
                # truncated count. The samples list is the plottable subset.
                count=total_n,
                element=chosen,
                available_elements=available,
                min_value=float(agg_row["min_v"]) if agg_row["min_v"] is not None else None,
                max_value=float(agg_row["max_v"]) if agg_row["max_v"] is not None else None,
                mean_value=float(agg_row["mean_v"]) if agg_row["mean_v"] is not None else None,
                median_value=float(agg_row["median_v"]) if agg_row["median_v"] is not None else None,
                data_source="PostGIS silver.samples",
            )

    try:
        return await asyncio.wait_for(_run(), timeout=settings.TIMEOUT_POSTGIS_S)
    except TimeoutError:
        logger.warning("query_assay_data timed out project=%s", project_id)
    except Exception:
        logger.exception("query_assay_data failed project=%s", project_id)

    return AssayDataResult(
        samples=[],
        count=0,
        element=element or "",
        available_elements=[],
        min_value=None,
        max_value=None,
        mean_value=None,
        median_value=None,
        data_source="PostGIS silver.samples",
    )


def _build_document_scope_filter(project_id: str):
    """Build a Qdrant ``Filter`` for ``search_documents`` per project-scope policy.

    Returns ``None`` when the policy is ``cross_project`` (historical
    default — no filter). For the other policies returns a
    ``qdrant_client.models.Filter`` instance.

    The import is lazy so unit tests that don't have qdrant-client installed
    can still import the module. Any unexpected policy value is treated as
    ``cross_project`` (fail-open rather than drop all traffic) and logged
    once per call.
    """
    mode = getattr(settings, "QDRANT_DOCUMENT_PROJECT_SCOPE", "cross_project")
    if mode == "cross_project":
        return None

    try:
        from qdrant_client.models import (  # noqa: PLC0415
            FieldCondition,
            Filter,
            IsEmptyCondition,
            MatchValue,
            PayloadField,
        )
    except Exception:  # pragma: no cover - qdrant-client missing in some envs
        logger.warning(
            "search_documents: qdrant_client.models unavailable — "
            "falling back to cross_project mode"
        )
        return None

    project_match = FieldCondition(
        key="project_id", match=MatchValue(value=project_id)
    )

    if mode == "strict":
        return Filter(must=[project_match])

    if mode == "project_or_public":
        # Admit: project_id == caller, OR payload.project_id is empty
        # (legacy public-report rows), OR project_id == "public".
        return Filter(
            should=[
                project_match,
                IsEmptyCondition(is_empty=PayloadField(key="project_id")),
                FieldCondition(key="project_id", match=MatchValue(value="public")),
            ]
        )

    logger.warning(
        "search_documents: unknown QDRANT_DOCUMENT_PROJECT_SCOPE=%r — "
        "falling back to cross_project",
        mode,
    )
    return None


@_metered("search_documents")
async def search_documents(
    ctx: RunContext[AgentDeps],
    query_text: str,
    project_id: str,
    limit: int = 10,
    score_threshold: float | None = None,
    sparse_boost_factor: float = 1.0,
) -> DocumentSearchResult:
    """Search the Qdrant vector store for document chunks relevant to the query.

    Use this tool when the user asks about:
    - Technical reports, NI 43-101 filings, JORC reports, or publications
    - Resource estimates, geological interpretations, or study conclusions
    - Historical exploration results described in documents
    - Any question where the answer likely comes from a written report rather
      than structured drill-hole data

    Retrieval uses a two-stage pipeline for Layer 1 hallucination prevention:

    Stage 1 — Coarse vector search:
      The query_text is embedded with the configured dense embedder
      (settings.EMBEDDING_MODEL_NAME — live default Qwen/Qwen3-Embedding-0.6B,
      1024-dim, swapped from bge-small 2026-06-03) and used to retrieve up to
      RETRIEVAL_TOP_N (20) candidates from Qdrant with a permissive cosine
      threshold (RETRIEVAL_QUALITY_THRESHOLD = 0.3). NOTE (audit 2026-06-27,
      T1 open): the query-side instruction prefix below is still the bge-era
      string — a known query/corpus asymmetry pending an eval-gated fix.

    Stage 2 — Cross-encoder reranking (when reranker is available):
      Each (query, chunk.text) pair is scored by the configured cross-encoder
      (settings.RERANKER_MODEL_PATH — live default bge-reranker-base; set the
      env to Qwen/Qwen3-Reranker-0.6B to use the Qwen3 reranker), which
      produces raw logits.  Candidates are sorted by logit descending; only the
      top RERANKER_TOP_K (5) with logit >= RERANKER_SCORE_THRESHOLD (0.0) are
      returned.  The reranker score replaces the raw Qdrant cosine in the
      returned relevance_score field.

    When no reranker is available the tool falls back to Layer 1 quality gate
    filtering via filter_by_quality using RETRIEVAL_QUALITY_THRESHOLD.

    The tool targets the ``georag_reports`` Qdrant collection which is
    populated by the Dagster ``index_reports`` asset.  Whether a project_id
    filter is applied depends on ``settings.QDRANT_DOCUMENT_PROJECT_SCOPE``:

      * ``cross_project``     — no filter (historical default; safe only when
                                the collection holds public NI 43-101 filings)
      * ``project_or_public`` — admit chunks whose payload ``project_id``
                                equals the caller's project, OR is missing /
                                equal to ``"public"`` (legacy rows)
      * ``strict``            — admit only chunks whose payload ``project_id``
                                equals the caller's project

    Args:
        query_text: Natural-language query to embed and search against.
        project_id: UUID of the requesting project. Used both for audit
            logging and — when ``QDRANT_DOCUMENT_PROJECT_SCOPE`` is not
            ``cross_project`` — as a Qdrant payload filter.
        limit: Ignored when reranker is active (RERANKER_TOP_K governs
            final count).  Caps initial Qdrant fetch when reranker is absent.
        score_threshold: Minimum Qdrant cosine score for the coarse pass.
            Defaults to RETRIEVAL_QUALITY_THRESHOLD.

    Returns:
        DocumentSearchResult with chunk list, count, and data source label.
        When reranker is active, data_source ends with "(reranked)".
    """
    # Guard: embedding model not yet available.
    if ctx.deps.embedding_model is None:
        logger.info(
            "search_documents: embedding model not loaded — returning empty results"
        )
        return DocumentSearchResult(chunks=[], count=0, data_source="Qdrant (model not loaded)")


    # ADR-0010 — hard flag flip between the legacy and canonical document
    # collections. Lifted out of the inner closure so error / timeout /
    # success data_source strings carry the right collection name.
    _doc_collection = (
        "georag_chunks" if settings.RETRIEVAL_USE_DOCUMENT_PASSAGES
        else "georag_reports"
    )

    # Stage 1: coarse Qdrant retrieval.
    # When a reranker is present we fetch a wider candidate set (RETRIEVAL_TOP_N)
    # before the cross-encoder narrows it.  Without a reranker we respect the
    # caller-provided limit (capped to 50).
    initial_limit = settings.RETRIEVAL_TOP_N if ctx.deps.reranker is not None else min(limit, 50)

    # Resolve workspace_id for the mandatory GI-9 tenant filter BEFORE running
    # the search. Audit 2026-06-27 (C3): prefer the authenticated workspace on
    # the agent deps (JWT-sourced, Module 9); else derive it from the project
    # row. CRITICALLY never fall back to a hardcoded default tenant — a
    # failed/missing resolution FAILS CLOSED (returns no documents) rather than
    # serving the default workspace's chunks. Hoisted to search_documents scope
    # (NOT the _run_search helper) so the refusal returns from the tool — a
    # return from _run_search would make `chunks` a DocumentSearchResult and
    # break the reranker / quality-gate path (len() on a non-list).
    _workspace_id = getattr(ctx.deps, "workspace_id", None)
    if not _workspace_id:
        try:
            if ctx.deps.pg_pool is not None:
                async with ctx.deps.pg_pool.acquire() as _conn:
                    _wid_row = await _conn.fetchrow(
                        "SELECT workspace_id::text FROM silver.projects WHERE project_id = $1::uuid",
                        project_id,
                    )
                if _wid_row and _wid_row["workspace_id"]:
                    _workspace_id = _wid_row["workspace_id"]
        except Exception as _wid_exc:
            logger.warning(
                "search_documents: workspace_id lookup failed: %s", _wid_exc
            )
    if not _workspace_id:
        logger.error(
            "search_documents: could not resolve workspace_id for project "
            "%s — failing closed (returning no documents) rather than "
            "querying a default tenant.",
            project_id,
        )
        return DocumentSearchResult(
            chunks=[], count=0,
            data_source="Qdrant (workspace unresolved — refused)",
        )
    _workspace_id = str(_workspace_id)

    async def _run_search() -> list[DocumentChunk]:
        loop = asyncio.get_event_loop()
        # Eval 15 R3 — geological query expansion. Annotate symbols
        # (Au→gold, g/t→grams per tonne, DDH→diamond drillhole) so the
        # dense embedder sees BOTH the abbreviation and the full term
        # in one input. Sparse retrieval gets the bonus exact-token
        # matches against passages using either form.
        from app.services.geological_query_expansion import (  # noqa: PLC0415
            expand_query as _expand_geo_query,
        )
        _expanded_query = _expand_geo_query(query_text)

        # Audit 2026-06-28 (T1): Qwen3-Embedding-0.6B is the live dense embedder
        # (swapped from bge-small 2026-06-03). The bge-era instruction prefix
        # ("Represent this geological query …") is WRONG for Qwen3 — the model
        # was not trained on it, so it embeds the prefix words as query noise and
        # degrades query/passage symmetry. Embed the raw expanded query instead.
        # (Optimal Qwen3 query-instruction wiring via EMBEDDING_QUERY_PROMPT_NAME
        # is a follow-up that needs a golden-eval pass.)
        # NOTE: applied WITHOUT a golden-eval pass — flag for eval validation
        # before treating retrieval-quality numbers as final.
        _embed_input = _expanded_query
        # Dense query embedding (sync inference, off the event loop).
        query_vector: list[float] = await loop.run_in_executor(
            None,
            lambda: ctx.deps.embedding_model.encode(
                _embed_input, normalize_embeddings=True
            ).tolist(),
        )
        # Sparse query encoding via SPLADE++ (sync inference, off the event loop).
        # GI-11: if SPLADE fails, this raises and the outer wait_for propagates
        # the exception -- no silent dense-only fallback.
        from app.services.sparse_encoder import encode_sparse  # noqa: PLC0415
        query_sparse: dict[int, float] = await loop.run_in_executor(
            None,
            lambda: encode_sparse(query_text),
        )

        # _workspace_id was resolved (and the fail-closed refusal returned) in
        # search_documents scope above — see the C3 note. It is captured here by
        # closure and passed to hybrid_query as the mandatory tenant filter.

        # Query the active document collection via the Qdrant 1.17 Query API.
        # ADR-0010 flip — RETRIEVAL_USE_DOCUMENT_PASSAGES selects between the
        # legacy georag_reports (sections_text) and the canonical georag_chunks
        # (silver.document_passages). Payload shape is kept compatible via
        # report_id/page aliases written by index_document_passages._build_payload.
        # Uses hybrid dense+sparse Prefetch with server-side RRF fusion (GI-11).
        from app.services.qdrant_service import hybrid_query  # noqa: PLC0415
        scope_filter = _build_document_scope_filter(project_id)

        # score_threshold is not natively supported in the FusionQuery path;
        # Qdrant RRF scores are relative (not cosine) so threshold filtering
        # is done post-retrieval by the reranker (Layer 1).
        points = await hybrid_query(
            client=ctx.deps.qdrant_client,
            collection=_doc_collection,
            query_dense=query_vector,
            query_sparse=query_sparse,
            workspace_id=_workspace_id,
            limit=initial_limit,
            additional_filter=scope_filter,
            sparse_boost_factor=sparse_boost_factor,
        )

        candidates: list[DocumentChunk] = []
        for point in points:
            payload = point.payload or {}
            section_number = payload.get("section_number")
            section_title = payload.get("section_title")
            # Provide a backwards-compatible ``section`` field using the most
            # informative available value.
            section_label = section_title or section_number
            # Phase 3 — surface OCR provenance straight from the qdrant
            # payload. Cast to float for ocr_confidence in case the
            # payload stored a numeric type that isn't already float.
            _ocr_conf_raw = payload.get("ocr_confidence")
            candidates.append(
                DocumentChunk(
                    chunk_id=str(point.id),
                    text=payload.get("text", ""),
                    source_document_id=payload.get("report_id", payload.get("document_id", "")),
                    document_title=payload.get("document_title", "Unknown"),
                    section_number=section_number,
                    section_title=section_title,
                    section=section_label,
                    page=payload.get("page"),
                    document_type=payload.get("document_type", "NI43"),
                    report_id=payload.get("report_id", ""),
                    relevance_score=float(point.score),  # overwritten by reranker below
                    ocr_confidence=(
                        float(_ocr_conf_raw) if _ocr_conf_raw is not None else None
                    ),
                    ocr_method=payload.get("ocr_method"),
                )
            )
        return candidates

    try:
        chunks = await asyncio.wait_for(_run_search(), timeout=settings.TIMEOUT_QDRANT_S)
    except TimeoutError:
        logger.warning(
            "search_documents timed out after %.1fs for project=%s query_hash=%s",
            settings.TIMEOUT_QDRANT_S,
            project_id,
            query_hash(query_text),
        )
        return DocumentSearchResult(chunks=[], count=0, data_source=f"Qdrant {_doc_collection} (timeout)")
    except Exception:
        logger.exception("search_documents failed for project=%s", project_id)
        return DocumentSearchResult(chunks=[], count=0, data_source=f"Qdrant {_doc_collection} (error)")

    if not chunks:
        return DocumentSearchResult(chunks=[], count=0, data_source=f"Qdrant {_doc_collection}")

    # Stage 2: cross-encoder reranking (Layer 1 precision gate).
    if ctx.deps.reranker is not None:
        logger.info(
            "search_documents: reranking %d candidates for project=%s query_hash=%s",
            len(chunks),
            project_id,
            query_hash(query_text),
        )
        loop = asyncio.get_event_loop()
        # Latency-fix follow-up — pre-truncate body text so the reranker's
        # tokeniser doesn't walk a 5 kB chunk just to discard everything
        # past the model's 512-token max. ~2000 chars (~500 tokens) is the
        # safe budget under bge-reranker-base.
        _budget = settings.RERANKER_INPUT_CHAR_BUDGET
        pairs = [(query_text, (c.text or "")[:_budget]) for c in chunks]
        try:
            # CrossEncoder.predict is synchronous and CPU-bound; run in executor
            # to avoid blocking the asyncio event loop. Wrap in an inner
            # wait_for so a wedged reranker doesn't blow the outer
            # search_documents branch budget — the orchestrator's per-branch
            # wait_for catches the longer cases (latency-fix follow-up).
            scores: list[float] = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: list(ctx.deps.reranker.predict(pairs)),
                ),
                timeout=settings.TIMEOUT_RERANKER_S,
            )
        except TimeoutError:
            logger.warning(
                "search_documents: reranker timed out after %.1fs on %d pairs "
                "for project=%s — falling back to Qdrant cosine ordering",
                settings.TIMEOUT_RERANKER_S,
                len(pairs),
                project_id,
            )
            scores = []
        except Exception:
            logger.exception(
                "search_documents: reranker.predict failed for project=%s — "
                "falling back to Qdrant cosine ordering",
                project_id,
            )
            scores = []

        if scores:
            # Cross-encoder outputs raw logits (unbounded real numbers). We
            # (a) threshold and sort using the raw logit — preserving the
            # semantic that RERANKER_SCORE_THRESHOLD=0.0 means "any positive
            # logit" — and (b) sigmoid-transform to [0, 1] before storing on
            # the chunk so downstream Citation.relevance_score (Pydantic
            # constrained float, 0..1) accepts the value.
            import math

            raw_logits: list[float] = [float(s) for s in scores]

            # Pair chunks with raw logits, threshold, sort, top-K.
            pre_threshold_count = len(chunks)
            min_score = settings.RERANKER_SCORE_THRESHOLD
            paired = [
                (chunk, logit)
                for chunk, logit in zip(chunks, raw_logits, strict=False)
                if logit >= min_score
            ]
            paired.sort(key=lambda p: p[1], reverse=True)

            # Sigmoid-transform logit -> [0, 1] for the Citation model.
            for chunk, logit in paired:
                chunk.relevance_score = 1.0 / (1.0 + math.exp(-logit))

            chunks = [chunk for chunk, _ in paired]

            if pre_threshold_count > 0 and len(chunks) == 0:
                logger.warning(
                    "search_documents: all %d candidates dropped by reranker "
                    "threshold=%.2f for project=%s query_hash=%s",
                    pre_threshold_count,
                    min_score,
                    project_id,
                    query_hash(query_text),
                )

            # Take top-K after threshold filtering.
            chunks = chunks[: settings.RERANKER_TOP_K]

            logger.info(
                "search_documents: reranking complete — top %d chunks "
                "(best score=%.4f) for project=%s",
                len(chunks),
                chunks[0].relevance_score if chunks else 0.0,
                project_id,
            )

        return DocumentSearchResult(
            chunks=chunks,
            count=len(chunks),
            data_source=f"qdrant:{_doc_collection} (reranked)",
        )

    # No reranker: fall back to Layer 1 quality gate on raw Qdrant cosine scores.
    pre_filter_count = len(chunks)
    chunks = filter_by_quality(chunks, settings.RETRIEVAL_QUALITY_THRESHOLD)
    if pre_filter_count > 0 and len(chunks) == 0:
        logger.warning(
            "search_documents: all %d chunks dropped by Layer 1 quality gate "
            "(threshold=%.2f) for project=%s query_hash=%s",
            pre_filter_count,
            settings.RETRIEVAL_QUALITY_THRESHOLD,
            project_id,
            query_hash(query_text),
        )

    return DocumentSearchResult(
        chunks=chunks,
        count=len(chunks),
        data_source=f"Qdrant {_doc_collection}",
    )


@_metered("traverse_knowledge_graph")
async def traverse_knowledge_graph(
    ctx: RunContext[AgentDeps],
    entity_name: str,
    project_id: str,
    relationship_type: str | None = None,
    depth: int = 1,
) -> GraphTraversalResult:
    """Traverse the Neo4j knowledge graph to find entities related to a named entity.

    Matching strategy (in order):
      1. Exact case-insensitive match on the ``name`` property.
      2. CONTAINS substring match (e.g. "Triple R" matches "Triple R Deposit").
      3. If no start node found, returns empty result gracefully.

    Scoping: only nodes with ``project_id = $project_id`` are considered
    as start nodes. Related nodes are NOT project-scoped so cross-project
    links (if any) are returned.

    Args:
        entity_name: Name of the starting entity node (fuzzy-matched).
        project_id: UUID to scope the start node to this project's subgraph.
        relationship_type: Optional Cypher relationship type to filter edges.
        depth: Traversal depth (1–3).

    Returns:
        GraphTraversalResult with related entities, count, and data source.
    """
    depth = min(max(depth, 1), 3)

    # P2 #28 — validate the LLM-supplied relationship_type before
    # interpolating it into the Cypher string. Invalid → None → fall
    # back to the unfiltered `[r]` form so we still honour the user's
    # discovery intent without executing dangerous Cypher.
    safe_rel_type = _validate_cypher_relationship(relationship_type)
    rel_filter = f"[r:{safe_rel_type}]" if safe_rel_type else "[r]"

    # Two-stage match: exact first, then CONTAINS. The UNION deduplicates.
    cypher = (
        "CALL { "
        "  MATCH (start) WHERE start.project_id = $project_id "
        "    AND toLower(start.name) = toLower($entity_name) "
        f"  MATCH (start)-{rel_filter}-(related) "
        "  RETURN start, related, r "
        "  UNION "
        "  MATCH (start) WHERE start.project_id = $project_id "
        "    AND toLower(start.name) CONTAINS toLower($entity_name) "
        f"  MATCH (start)-{rel_filter}-(related) "
        "  RETURN start, related, r "
        "} "
        "RETURN DISTINCT "
        "  elementId(related) AS entity_id, "
        "  labels(related)[0] AS entity_type, "
        "  related.name AS name, "
        "  properties(related) AS props, "
        "  type(r) AS rel_type, "
        "  CASE WHEN startNode(r) = start THEN 'OUTBOUND' ELSE 'INBOUND' END AS direction "
        "LIMIT 50"
    )

    logger.info(
        "traverse_knowledge_graph: entity='%s' project=%s rel_type=%s depth=%s",
        entity_name,
        project_id,
        relationship_type,
        depth,
    )

    async def _run_traversal() -> list[GraphEntity]:
        async with ctx.deps.neo4j_driver.session() as session:
            result = await session.run(
                cypher,
                entity_name=entity_name,
                project_id=project_id,
            )
            records = await result.data()

        entities: list[GraphEntity] = []
        for rec in records:
            raw_props: dict = rec.get("props") or {}
            str_props = {k: str(v) for k, v in raw_props.items()}
            entities.append(
                GraphEntity(
                    entity_id=str(rec.get("entity_id", "")),
                    entity_type=rec.get("entity_type", "Unknown"),
                    name=rec.get("name", ""),
                    properties=str_props,
                    relationship_type=rec.get("rel_type", ""),
                    relationship_direction=rec.get("direction", "OUTBOUND"),
                )
            )
        return entities

    try:
        entities = await asyncio.wait_for(_run_traversal(), timeout=settings.TIMEOUT_NEO4J_S)
    except TimeoutError:
        logger.warning(
            "traverse_knowledge_graph timed out after %.1fs for entity='%s' project=%s",
            settings.TIMEOUT_NEO4J_S,
            entity_name,
            project_id,
        )
        entities = []
    except Exception:
        logger.exception(
            "traverse_knowledge_graph failed for entity='%s' project=%s",
            entity_name,
            project_id,
        )
        entities = []

    return GraphTraversalResult(
        entities=entities,
        count=len(entities),
        data_source="Neo4j knowledge graph",
    )


@_metered("query_graph_by_label")
async def query_graph_by_label(
    ctx: RunContext[AgentDeps],
    label: str,
    project_id: str,
) -> GraphTraversalResult:
    """List all nodes of a given label in the project's subgraph.

    Used by the orchestrator as a fallback when traverse_knowledge_graph
    returns empty because the user asked about a *category* ("formations",
    "deposits") rather than a specific *entity* ("Triple R"). Returns
    nodes directly without traversal. Each returned GraphEntity carries
    relationship_type="" and direction="OUTBOUND" as placeholders.

    Args:
        label: Neo4j node label (e.g. "Formation", "Deposit", "DrillHole").
        project_id: UUID scope.

    Returns:
        GraphTraversalResult with all matching nodes (capped at 50).
        Returns an empty result (count=0) when ``label`` is not on the
        Cypher identifier allowlist (P2 #28) — rejecting an unsafe
        label is preferred over executing arbitrary Cypher.
    """
    # P2 #28 — validate the LLM-supplied label before interpolating it.
    # Unlike `relationship_type` in traverse_knowledge_graph (which has
    # an unfiltered `[r]` fallback), `label` has no safe-fallback because
    # MATCH (n) without a label scans every node in the database — wrong
    # result shape, dangerous query cost. Bail out empty instead.
    safe_label = _validate_cypher_label(label)
    if safe_label is None:
        logger.info(
            "query_graph_by_label: rejected label=%r — returning empty result",
            label[:80] if label else None,
        )
        return GraphTraversalResult(
            entities=[],
            count=0,
            data_source="Neo4j knowledge graph (label rejected)",
        )

    cypher = (
        f"MATCH (n:{safe_label}) "
        "WHERE n.project_id = $project_id "
        "OPTIONAL MATCH (n)-[r]-(m) "
        "RETURN "
        "  elementId(n) AS entity_id, "
        "  labels(n)[0] AS entity_type, "
        "  n.name AS name, "
        "  properties(n) AS props, "
        "  COLLECT(DISTINCT type(r) + ' → ' + COALESCE(m.name, '?'))[..5] AS rels "
        "LIMIT 50"
    )

    logger.info(
        "query_graph_by_label: label=%s project=%s",
        label,
        project_id,
    )

    async def _run() -> list[GraphEntity]:
        async with ctx.deps.neo4j_driver.session() as session:
            result = await session.run(cypher, project_id=project_id)
            records = await result.data()

        entities: list[GraphEntity] = []
        for rec in records:
            raw_props: dict = rec.get("props") or {}
            str_props = {k: str(v) for k, v in raw_props.items()}
            # Append relationship summary to properties
            rels = rec.get("rels") or []
            if rels:
                str_props["related_to"] = "; ".join(rels)
            entities.append(
                GraphEntity(
                    entity_id=str(rec.get("entity_id", "")),
                    entity_type=rec.get("entity_type", "Unknown"),
                    name=rec.get("name", ""),
                    properties=str_props,
                    relationship_type="",
                    relationship_direction="OUTBOUND",
                )
            )
        return entities

    try:
        entities = await asyncio.wait_for(_run(), timeout=settings.TIMEOUT_NEO4J_S)
    except TimeoutError:
        logger.warning(
            "query_graph_by_label timed out label=%s project=%s",
            label,
            project_id,
        )
        entities = []
    except Exception:
        logger.exception(
            "query_graph_by_label failed label=%s project=%s",
            label,
            project_id,
        )
        entities = []

    return GraphTraversalResult(
        entities=entities,
        count=len(entities),
        data_source="Neo4j knowledge graph",
    )


@_metered("verify_numerical_claim")
async def verify_numerical_claim(
    ctx: RunContext[AgentDeps],
    table: str,
    column: str,
    row_id: str,
    claimed_value: float,
    tolerance: float = 0.001,
) -> NumericalClaimVerification:
    """Verify a specific numerical value against the PostGIS database.

    Use this tool (hallucination prevention Layer 3) whenever the response
    includes a precise numerical claim sourced from structured data:
    - A depth value (from_depth, to_depth, total_depth)
    - A grade or assay result (commodity_assays values)
    - A coordinate (easting, northing, elevation)
    - A resource estimate figure (tonnage, grade, contained metal)

    The tool runs a direct SELECT against the specified table/column/row and
    checks that the LLM-generated value matches the database value within the
    specified tolerance.  If the values diverge, the agent must correct the
    claim before including it in the response.

    Allowed tables (to prevent SQL injection): silver.collars, silver.samples,
    silver.lithology_logs, silver.alteration, silver.structures,
    silver.geochemistry, silver.surveys, bronze.reports.

    Args:
        table: Schema-qualified table name (e.g. "silver.collars").
        column: Column name to check (e.g. "total_depth").
        row_id: UUID of the row to look up.
        claimed_value: The numerical value the LLM intends to state.
        tolerance: Absolute tolerance for floating-point comparison (default 0.001).

    Returns:
        NumericalClaimVerification with verified flag and the actual DB value.
    """
    # Per-table column allowlist (P0 #2). Previously `column` was
    # interpolated raw into the f-string below, letting an LLM-supplied
    # `column="total_depth, (SELECT current_user)"` exfiltrate row
    # contents. Now we whitelist the set of numeric columns any caller
    # could plausibly want to verify, keyed by table.
    #
    # Only columns that store NUMERIC values (not text, geometry, arrays,
    # or JSON) belong here — verify_numerical_claim returns a float.
    allowed_by_table: dict[str, tuple[str, set[str]]] = {
        # (primary_key_column, allowed_value_columns)
        "silver.collars": ("collar_id", {
            "total_depth", "azimuth", "dip", "easting", "northing", "elevation",
        }),
        "silver.samples": ("sample_id", {
            "from_depth", "to_depth", "sample_length", "recovery",
        }),
        "silver.lithology_logs": ("log_id", {
            "from_depth", "to_depth", "rqd", "recovery",
        }),
        "silver.alteration": ("alteration_id", {
            "from_depth", "to_depth", "intensity",
        }),
        "silver.structures": ("structure_id", {
            "depth", "true_dip", "dip_direction", "apparent_dip",
        }),
        "silver.geochemistry": ("geochem_id", {
            "from_depth", "to_depth", "value", "detection_limit",
        }),
        "silver.surveys": ("survey_id", {
            "depth", "azimuth", "dip",
        }),
        "bronze.reports": ("report_id", {
            "page_count", "version_number",
        }),
    }

    if table not in allowed_by_table:
        logger.error("verify_numerical_claim: disallowed table '%s'", table)
        return NumericalClaimVerification(
            claim_value=claimed_value,
            db_value=None,
            verified=False,
            tolerance_used=tolerance,
            verification_query=f"BLOCKED — table '{table}' not in allowlist",
        )

    pk_col, allowed_columns = allowed_by_table[table]

    if column not in allowed_columns:
        logger.error(
            "verify_numerical_claim: disallowed column '%s' on table '%s' "
            "(allowed: %s)",
            column,
            table,
            sorted(allowed_columns),
        )
        return NumericalClaimVerification(
            claim_value=claimed_value,
            db_value=None,
            verified=False,
            tolerance_used=tolerance,
            verification_query=(
                f"BLOCKED — column '{column}' not in allowlist for table '{table}'"
            ),
        )

    # Safe to interpolate — both identifiers are drawn from the static
    # allowlist. `row_id` is bound via asyncpg $1 parameter.
    sql = f"SELECT {column} FROM {table} WHERE {pk_col} = $1::uuid"

    logger.info(
        "verify_numerical_claim: %s.%s row=%s claimed=%.4f tol=%.4f",
        table,
        column,
        row_id,
        claimed_value,
        tolerance,
    )

    async def _run_verify() -> float | None:
        async with ctx.deps.pg_pool.acquire() as conn:
            row = await conn.fetchrow(sql, row_id)
            if row is None:
                return None
            return float(row[column])

    try:
        db_value = await asyncio.wait_for(_run_verify(), timeout=settings.TIMEOUT_POSTGIS_S)
    except TimeoutError:
        logger.warning("verify_numerical_claim timed out for %s.%s row=%s", table, column, row_id)
        db_value = None
    except Exception:
        logger.exception("verify_numerical_claim failed for %s.%s row=%s", table, column, row_id)
        db_value = None

    verified = False if db_value is None else abs(claimed_value - db_value) <= tolerance

    return NumericalClaimVerification(
        claim_value=claimed_value,
        db_value=db_value,
        verified=verified,
        tolerance_used=tolerance,
        verification_query=sql,
    )


# ---------------------------------------------------------------------------
# ADR-0007 PR-1 — project_summary + coverage_gap chat-card tools
# ---------------------------------------------------------------------------
#
# These two tools back the new `project_summary` and `coverage_gap` intents
# (intent_classifier.py + retrieval_profile.py). They are SQL-aggregate-first
# tools — no Qdrant, no embeddings — that return shape suitable for the
# frontend's TimelineCard / CoverageTableCard renderers.
#
# Calling convention differs from the legacy pydantic-ai tools above:
#   * Takes ``deps: AgentDeps`` directly (not a RunContext) so the agentic
#     LangGraph's ``_call_tool_safely`` can dispatch them without the
#     pydantic-ai RunContext shim
#   * Always filters by ``workspace_id`` (JWT-derived; pulled from deps)
#     AND ``project_id`` for tenancy + scope
#   * Returns ``source_row_ids: list[uuid]`` per breakdown / coverage row
#     so the §04i citation contract binds per-row, not per-tool
#
# Schema notes from the 2026-05-25 audit:
#   * silver.campaigns.contractor / .geologist are 0% populated → returned
#     as NULL with ``extraction_pending_fields=['contractor','geologist',
#     'lab_name']`` so the answer body can call out the gap honestly.
#   * silver.geophysics_surveys has 1 dev row — query still runs.
#   * silver.completeness_findings has 2 dev rows — surfaced as `findings`.


@dataclass
class TechniqueBreakdownRow:
    """One row of the technique × year breakdown.

    technique: ``drill_type`` (DDH/RC/...), survey_type for geophysics, or
        ``report`` for the documentation breakdown.
    source_table: which silver table the row was aggregated from.
    year: extracted from start_date / drill_date / acquisition_date / filing_date.
        None when the date column is NULL on every row in the group.
    count: number of distinct entities (campaigns / collars / surveys / reports)
        in this (technique, year) bucket.
    total_metres: sum of drilled metres for the group, when applicable.
        None for non-drilling technique rows.
    contractor / geologist: pulled from silver.campaigns when populated. NULL
        for non-campaign rows AND for campaign rows where the extractor hasn't
        backfilled the column yet (ADR-0007 PR-3 covers the backfill).
    source_row_ids: UUIDs of the underlying silver rows. Required per §04i
        so the LLM's claim "we drilled 4 RC holes in 2022" binds to a real
        set of rows the citation guard can verify.
    """

    technique: str
    source_table: str
    year: int | None
    count: int
    total_metres: float | None
    contractor: str | None
    geologist: str | None
    source_row_ids: list[str]


@dataclass
class ProjectSummaryResult:
    """Return type for ``query_project_summary``.

    technique_breakdown: rows ordered by (source_table, year DESC, technique).
        Empty when the project has no campaigns / collars / surveys / reports
        — caller should refuse cleanly rather than fabricate a summary.
    extraction_pending_fields: names of columns that are present in the
        schema but not yet populated by an extractor. ADR-0007 PR-1 ships
        with ``contractor``/``geologist``/``lab_name`` always listed (the
        2026-05-25 audit showed 0% population); PR-3 will narrow this.
    project_id: echoed back for the assembler.
    workspace_id: echoed back for audit.
    count: number of breakdown rows (= ``len(technique_breakdown)``).
        Aliased so the generic "_is_empty_tool_result" helper works.
    data_source: provenance label for §04i Layer 5.
    """

    technique_breakdown: list[TechniqueBreakdownRow]
    extraction_pending_fields: list[str]
    project_id: str
    workspace_id: str
    count: int
    data_source: str = (
        "PostGIS silver.campaigns + silver.collars + "
        "silver.geophysics_surveys + silver.reports"
    )


@dataclass
class IngestGapStats:
    """Bronze → silver ingest-stage coverage gap.

    indexed: count of rows in ``bronze.ingest_manifest`` for this workspace.
    processed: count of distinct manifest rows that have a downstream
        ``bronze.provenance`` entry pointing at a row in ``silver.reports``.
    gap_pct: 100 * (indexed - processed) / indexed when indexed > 0, else 0.
    """

    indexed: int
    processed: int
    gap_pct: float


@dataclass
class AttributeCoverageRow:
    """Per-attribute drillhole coverage for one project.

    attribute: which downstream silver table is being measured
        (``assays``, ``lithology_logs``, ``structure``, ``alteration``,
        ``samples``).
    collars_with_data: number of distinct collars that have at least one
        row in the target table.
    collars_total: total collars in the project (denominator).
    coverage_pct: 100 * collars_with_data / collars_total. 0 when there
        are no collars at all (avoids div/0 + signals nothing to measure).
    source_row_ids: collar UUIDs that DO have data. Empty list when none.
    """

    attribute: str
    collars_with_data: int
    collars_total: int
    coverage_pct: float
    source_row_ids: list[str]


@dataclass
class CoverageFindingRow:
    """One row from ``silver.completeness_findings`` surfaced as-is."""

    kind: str
    severity: str
    description: str
    source_row_ids: list[str]


@dataclass
class CoverageGapResult:
    """Return type for ``query_coverage_gap``.

    ingest_gap: bronze→silver coverage stats for the workspace.
    attribute_coverage: per-attribute coverage rows for the project.
    findings: rows from silver.completeness_findings for this project.
    gap_geojson: FeatureCollection of project collars with per-feature
        ``properties.has_data`` (any of the requested attributes) +
        ``properties.missing_attributes`` (list). None when the project
        has no collars or geometries are unavailable. The §6b
        ``InlineViz`` MapView consumes this directly to show
        spatially where coverage gaps cluster.
    project_id / workspace_id: echoed back.
    count: total rows surfaced (ingest_gap counts as 1 if non-trivial).
    data_source: provenance label.
    """

    ingest_gap: IngestGapStats
    attribute_coverage: list[AttributeCoverageRow]
    findings: list[CoverageFindingRow]
    project_id: str
    workspace_id: str
    count: int
    gap_geojson: dict | None = None
    data_source: str = (
        "PostGIS bronze.ingest_manifest + silver.reports + "
        "silver.collars + silver.assays_v2 + silver.lithology_logs + "
        "silver.completeness_findings"
    )


# Columns the §04e schema defines that may or may not be populated.
# ADR-0007 PR-3 is the dedicated backfill. Until PR-3 lands the rows the
# OIUR uncertainty block lists these verbatim; once the backfill runs the
# pending list shrinks dynamically (see ``_compute_pending_fields`` below)
# rather than this constant being the source of truth.
_PROJECT_SUMMARY_EXTRACTION_CANDIDATES: tuple[str, ...] = (
    "contractor",
    "geologist",
    "lab_name",
)

# Per-column "is this populated at all for this project?" probe. Each entry
# maps an extraction-pending candidate name to a workspace+project-scoped
# COUNT query. A non-zero count for ANY row drops the field off the pending
# list — the §04i OIUR uncertainty section only needs to call out fields
# the user genuinely won't see data for in this project.
_PENDING_FIELD_PROBE_SQL: dict[str, str] = {
    "contractor": (
        "SELECT 1 FROM silver.campaigns c "
        "JOIN silver.projects p ON p.project_id = c.project_id "
        "WHERE p.workspace_id = $1::uuid AND c.project_id = $2::uuid "
        "AND c.contractor IS NOT NULL LIMIT 1"
    ),
    "geologist": (
        "SELECT 1 FROM silver.campaigns c "
        "JOIN silver.projects p ON p.project_id = c.project_id "
        "WHERE p.workspace_id = $1::uuid AND c.project_id = $2::uuid "
        "AND c.geologist IS NOT NULL "
        "UNION ALL "
        "SELECT 1 FROM silver.collars co "
        "JOIN silver.projects p ON p.project_id = co.project_id "
        "WHERE p.workspace_id = $1::uuid AND co.project_id = $2::uuid "
        "AND co.geologist IS NOT NULL LIMIT 1"
    ),
    "lab_name": (
        "SELECT 1 FROM silver.assays_v2 a "
        "JOIN silver.collars co ON co.collar_id = a.collar_id "
        "JOIN silver.projects p ON p.project_id = co.project_id "
        "WHERE p.workspace_id = $1::uuid AND co.project_id = $2::uuid "
        "AND a.lab_name IS NOT NULL LIMIT 1"
    ),
}


async def _compute_pending_fields(
    deps: AgentDeps, workspace_id: str, project_id: str,
) -> list[str]:
    """Return the subset of extraction candidates with zero rows in this
    project. Cheap: each probe is ``LIMIT 1`` on indexed columns. Failures
    degrade gracefully — on any error we fall back to the full candidate
    list so the OIUR uncertainty block is never under-stated.
    """
    if deps.pg_pool is None:
        return list(_PROJECT_SUMMARY_EXTRACTION_CANDIDATES)
    pending: list[str] = []
    try:
        async with deps.pg_pool.acquire() as conn:
            for field, sql in _PENDING_FIELD_PROBE_SQL.items():
                row = await conn.fetchrow(sql, workspace_id, project_id)
                if row is None:
                    pending.append(field)
    except Exception:
        logger.exception(
            "_compute_pending_fields failed workspace=%s project=%s",
            workspace_id, project_id,
        )
        return list(_PROJECT_SUMMARY_EXTRACTION_CANDIDATES)
    return pending


@_metered("query_project_summary")
async def query_project_summary(
    deps: AgentDeps,
    workspace_id: str,
    project_id: str,
) -> ProjectSummaryResult:
    """Aggregate data-collection techniques for a project.

    Returns a per-(technique × year × source_table) breakdown over:
      * silver.campaigns       — grouped by ``drill_type`` + year(start_date)
      * silver.collars         — grouped by ``drill_type`` + year(drill_date)
      * silver.geophysics_surveys — grouped by ``survey_type`` + year(acquisition_date)
      * silver.reports         — grouped by year(filing_date) + parser_used

    All queries are workspace_id-scoped via ``silver.projects`` so cross-
    workspace leakage is impossible.

    Args:
        deps: Agent dependencies (asyncpg pool required).
        workspace_id: Caller's workspace UUID from the JWT.
        project_id: Project to summarise.

    Returns:
        :class:`ProjectSummaryResult` — empty breakdown on any database
        error so the agentic-retrieval graph keeps streaming.
    """
    if deps.pg_pool is None:
        logger.warning(
            "query_project_summary: deps.pg_pool is None — returning empty result"
        )
        return ProjectSummaryResult(
            technique_breakdown=[],
            extraction_pending_fields=list(_PROJECT_SUMMARY_EXTRACTION_CANDIDATES),
            project_id=project_id,
            workspace_id=workspace_id,
            count=0,
        )

    # ── Campaigns ──
    # contractor / geologist remain NULL in the SELECT until PR-3 backfills.
    # array_agg(id) carries the source_row_ids per group. silver.campaigns PK
    # is "id" (not "campaign_id" — that name is the FK shape used by callers).
    campaigns_sql = """
        SELECT
            COALESCE(c.drill_type, 'unknown') AS technique,
            EXTRACT(YEAR FROM c.start_date)::int AS year,
            COUNT(*)::int AS n,
            SUM(c.total_metres)::float AS total_metres,
            MAX(c.contractor) AS contractor,
            MAX(c.geologist) AS geologist,
            array_agg(c.id::text) AS source_row_ids
        FROM silver.campaigns c
        JOIN silver.projects p ON p.project_id = c.project_id
        WHERE p.workspace_id = $1::uuid
          AND c.project_id = $2::uuid
        GROUP BY technique, year
        ORDER BY year DESC NULLS LAST, technique
    """

    # ── Collars ──
    collars_sql = """
        SELECT
            COALESCE(co.drill_type, co.hole_type, 'unknown') AS technique,
            EXTRACT(YEAR FROM co.drill_date)::int AS year,
            COUNT(*)::int AS n,
            SUM(co.total_depth)::float AS total_metres,
            array_agg(co.collar_id::text) AS source_row_ids
        FROM silver.collars co
        JOIN silver.projects p ON p.project_id = co.project_id
        WHERE p.workspace_id = $1::uuid
          AND co.project_id = $2::uuid
        GROUP BY technique, year
        ORDER BY year DESC NULLS LAST, technique
    """

    # ── Geophysics surveys ──
    geophys_sql = """
        SELECT
            COALESCE(g.survey_type, 'unknown') AS technique,
            EXTRACT(YEAR FROM g.acquisition_date)::int AS year,
            COUNT(*)::int AS n,
            array_agg(g.survey_id::text) AS source_row_ids
        FROM silver.geophysics_surveys g
        JOIN silver.projects p ON p.project_id = g.project_id
        WHERE p.workspace_id = $1::uuid
          AND g.project_id = $2::uuid
        GROUP BY technique, year
        ORDER BY year DESC NULLS LAST, technique
    """

    # ── Reports ──
    # parser_used acts as the "technique" axis for documentation rollups.
    reports_sql = """
        SELECT
            COALESCE(r.parser_used, 'unknown') AS technique,
            EXTRACT(YEAR FROM r.filing_date)::int AS year,
            COUNT(*)::int AS n,
            array_agg(r.report_id::text) AS source_row_ids
        FROM silver.reports r
        JOIN silver.projects p ON p.project_id = r.project_id
        WHERE p.workspace_id = $1::uuid
          AND r.project_id = $2::uuid
        GROUP BY technique, year
        ORDER BY year DESC NULLS LAST, technique
    """

    async def _run() -> list[TechniqueBreakdownRow]:
        breakdown: list[TechniqueBreakdownRow] = []
        async with deps.pg_pool.acquire() as conn:
            campaign_rows = await conn.fetch(campaigns_sql, workspace_id, project_id)
            for row in campaign_rows:
                breakdown.append(
                    TechniqueBreakdownRow(
                        technique=row["technique"] or "unknown",
                        source_table="silver.campaigns",
                        year=row["year"],
                        count=int(row["n"]),
                        total_metres=(
                            float(row["total_metres"])
                            if row["total_metres"] is not None
                            else None
                        ),
                        contractor=row["contractor"],
                        geologist=row["geologist"],
                        source_row_ids=list(row["source_row_ids"] or []),
                    )
                )

            collar_rows = await conn.fetch(collars_sql, workspace_id, project_id)
            for row in collar_rows:
                breakdown.append(
                    TechniqueBreakdownRow(
                        technique=row["technique"] or "unknown",
                        source_table="silver.collars",
                        year=row["year"],
                        count=int(row["n"]),
                        total_metres=(
                            float(row["total_metres"])
                            if row["total_metres"] is not None
                            else None
                        ),
                        contractor=None,
                        geologist=None,
                        source_row_ids=list(row["source_row_ids"] or []),
                    )
                )

            geophys_rows = await conn.fetch(geophys_sql, workspace_id, project_id)
            for row in geophys_rows:
                breakdown.append(
                    TechniqueBreakdownRow(
                        technique=row["technique"] or "unknown",
                        source_table="silver.geophysics_surveys",
                        year=row["year"],
                        count=int(row["n"]),
                        total_metres=None,
                        contractor=None,
                        geologist=None,
                        source_row_ids=list(row["source_row_ids"] or []),
                    )
                )

            report_rows = await conn.fetch(reports_sql, workspace_id, project_id)
            for row in report_rows:
                breakdown.append(
                    TechniqueBreakdownRow(
                        technique=row["technique"] or "unknown",
                        source_table="silver.reports",
                        year=row["year"],
                        count=int(row["n"]),
                        total_metres=None,
                        contractor=None,
                        geologist=None,
                        source_row_ids=list(row["source_row_ids"] or []),
                    )
                )
        return breakdown

    logger.info(
        "query_project_summary: workspace=%s project=%s",
        workspace_id,
        project_id,
    )
    try:
        breakdown = await asyncio.wait_for(_run(), timeout=settings.TIMEOUT_POSTGIS_S)
    except TimeoutError:
        logger.warning(
            "query_project_summary timed out workspace=%s project=%s",
            workspace_id,
            project_id,
        )
        breakdown = []
    except Exception:
        logger.exception(
            "query_project_summary failed workspace=%s project=%s",
            workspace_id,
            project_id,
        )
        breakdown = []

    pending_fields = await _compute_pending_fields(deps, workspace_id, project_id)

    return ProjectSummaryResult(
        technique_breakdown=breakdown,
        extraction_pending_fields=pending_fields,
        project_id=project_id,
        workspace_id=workspace_id,
        count=len(breakdown),
    )


# Per-attribute coverage targets. Each tuple is
# (attribute_name, target_table, join_column_on_target). join_column is
# usually ``collar_id`` but some §04e tables use ``hole_id`` — keeping
# this declarative makes adding a new attribute one entry.
_COVERAGE_ATTRIBUTES: tuple[tuple[str, str, str], ...] = (
    ("assays", "silver.assays_v2", "collar_id"),
    ("lithology_logs", "silver.lithology_logs", "collar_id"),
    ("structure", "silver.structure", "collar_id"),
    ("alteration", "silver.alteration", "collar_id"),
    ("samples", "silver.samples", "collar_id"),
)


def _build_coverage_geojson(
    collar_rows: list[Any],
    selected_dims: set[str] | None,
) -> dict:
    """Convert per-collar coverage rows into a GeoJSON FeatureCollection.

    Per-feature `properties` shape (consumed by ``InlineViz`` MapView):
        has_data:               bool  — true iff ANY requested attribute
                                        has a row for this collar
        missing_attributes:     list[str]  — dimension names lacking data
        attributes_with_data:   list[str]  — dimension names that have data
        hole_id:                str
        collar_id:              str (UUID)

    The MapView reads `has_data` for the green/red colour map and shows
    the missing list in a click popup so the user can see WHICH attribute
    is the gap for that specific hole.

    `collar_rows` shape per row: ``{collar_id, hole_id, longitude,
    latitude, has_assays?, has_lithology_logs?, has_structure?,
    has_alteration?, has_samples?}`` — only the has_* columns the SQL
    actually included (driven by selected_dims) are present.
    """
    # The set of dimension names actually queried for. Used to compute
    # missing_attributes per collar. When the caller passed dimensions=None
    # we want all 5; when they passed a subset, only those.
    if selected_dims is not None:
        active_dims = sorted(selected_dims)
    else:
        active_dims = [attr_name for attr_name, _, _ in _COVERAGE_ATTRIBUTES]

    features: list[dict] = []
    for row in collar_rows:
        long_ = row["longitude"]
        lat = row["latitude"]
        # Skip rows where the geometry came back null (shouldn't happen
        # given the WHERE clause but defensive).
        if long_ is None or lat is None:
            continue

        attributes_with_data: list[str] = []
        missing_attributes: list[str] = []
        for dim in active_dims:
            col = f"has_{dim}"
            # Defensive: if the EXISTS column wasn't included (e.g.
            # dimensions=[] stub path) treat as no data.
            if row.get(col, False):
                attributes_with_data.append(dim)
            else:
                missing_attributes.append(dim)

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [float(long_), float(lat)],
            },
            "properties": {
                "collar_id": row["collar_id"],
                "hole_id": row["hole_id"],
                "has_data": len(attributes_with_data) > 0,
                "attributes_with_data": attributes_with_data,
                "missing_attributes": missing_attributes,
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


@_metered("query_coverage_gap")
async def query_coverage_gap(
    deps: AgentDeps,
    workspace_id: str,
    project_id: str,
    dimensions: list[str] | None = None,
) -> CoverageGapResult:
    """Compute ingest-stage + per-attribute coverage gaps for a project.

    Three signals are surfaced:

      1. **Ingest gap**: bronze.ingest_manifest rows for the workspace that
         have no provenance pointer into silver.reports. The 2026-05-25
         audit observed 39,744 indexed vs 1,209 processed (~97% gap).

      2. **Attribute coverage**: for each known §04e detail table
         (assays / lithology / structure / alteration / samples), what
         fraction of the project's collars have at least one row?

      3. **Completeness findings**: rows from silver.completeness_findings
         scoped to the project. Surfaced as-is so the LLM can quote them.

    Args:
        deps: Agent dependencies.
        workspace_id: Caller's workspace UUID.
        project_id: Project to analyse.
        dimensions: Optional whitelist of attribute names to evaluate
            (e.g. ``['assays', 'structure']``). None = evaluate all
            declared in :data:`_COVERAGE_ATTRIBUTES`.

    Returns:
        :class:`CoverageGapResult`. Always returns a valid object — DB
        errors degrade to ``IngestGapStats(0,0,0.0)`` + empty lists so
        the graph keeps streaming.
    """
    if deps.pg_pool is None:
        logger.warning(
            "query_coverage_gap: deps.pg_pool is None — returning empty result"
        )
        return CoverageGapResult(
            ingest_gap=IngestGapStats(indexed=0, processed=0, gap_pct=0.0),
            attribute_coverage=[],
            findings=[],
            project_id=project_id,
            workspace_id=workspace_id,
            count=0,
        )

    selected_dims: set[str] | None = None
    if dimensions is not None:
        selected_dims = {d.lower() for d in dimensions if d}

    # ── Ingest stage ──
    # bronze.ingest_manifest is workspace-scoped (workspace_id column added
    # in the 2026-05-25 bronze tenancy migration). The "processed" count
    # is the distinct manifest_id values that appear in bronze.provenance
    # tied to a silver.reports row — the strongest available ingest signal.
    ingest_sql = """
        WITH indexed AS (
            SELECT COUNT(*)::int AS n
            FROM bronze.ingest_manifest
            WHERE workspace_id = $1::uuid
        ),
        processed AS (
            SELECT COUNT(DISTINCT bp.target_id)::int AS n
            FROM bronze.provenance bp
            JOIN silver.reports r ON r.report_id = bp.target_id::uuid
            WHERE bp.workspace_id = $1::uuid
              AND r.workspace_id = $1::uuid
        )
        SELECT indexed.n AS indexed_n, processed.n AS processed_n
        FROM indexed, processed
    """

    # ── Per-attribute coverage ──
    # Build one CTE-style query per attribute to keep the SQL simple and
    # readable. The denominator (collars_total) is a single shared query.
    collars_total_sql = """
        SELECT COUNT(*)::int AS n
        FROM silver.collars co
        JOIN silver.projects p ON p.project_id = co.project_id
        WHERE p.workspace_id = $1::uuid
          AND co.project_id = $2::uuid
    """

    def _attribute_sql(target_table: str, join_col: str) -> str:
        return f"""
            SELECT
                COUNT(DISTINCT co.collar_id)::int AS n,
                array_agg(DISTINCT co.collar_id::text) AS source_row_ids
            FROM silver.collars co
            JOIN silver.projects p ON p.project_id = co.project_id
            JOIN {target_table} t ON t.{join_col} = co.collar_id
            WHERE p.workspace_id = $1::uuid
              AND co.project_id = $2::uuid
        """

    findings_sql = """
        SELECT
            COALESCE(cf.finding_kind, 'unknown') AS kind,
            COALESCE(cf.severity, 'info') AS severity,
            COALESCE(cf.description, '') AS description,
            cf.finding_id::text AS finding_id
        FROM silver.completeness_findings cf
        WHERE cf.workspace_id = $1::uuid
          AND cf.project_id = $2::uuid
        ORDER BY cf.severity DESC, cf.finding_kind
        LIMIT 50
    """

    # Per-collar geometry + coverage signal (§6b P4). Returns ALL collars
    # in the project with their WGS84 location + a per-attribute has-data
    # flag computed via EXISTS subqueries (each is index-backed so the
    # whole query is cheap even on 1000-collar projects).
    #
    # The frontend MapView reads `properties.has_data` (true iff the
    # collar has at least one row in ANY of the requested attributes) +
    # `properties.missing_attributes` (the dimension names that lack
    # data on this collar) so it can colour-code green / red / partial.
    #
    # The geometry source is silver.collars.geom_4326 — the WGS84
    # column that pre-existed the §6b work; the SRID=32613 raw `geom`
    # column is UTM-specific and not suitable for GeoJSON.
    def _collar_geojson_sql(selected: set[str] | None) -> str:
        """Build the per-collar coverage SQL, including only the EXISTS
        subqueries for attributes the caller asked about. When dimensions
        is None we include all 5 §04e detail tables; the EXISTS column
        names are stable so the Python aggregator below can iterate."""
        exists_clauses = []
        # Map of (attr_name → (table, join_col)) — extracted from
        # _COVERAGE_ATTRIBUTES so any future addition to that tuple
        # automatically lands here too.
        for attr_name, target_table, join_col in _COVERAGE_ATTRIBUTES:
            if selected is not None and attr_name not in selected:
                continue
            exists_clauses.append(
                f"  EXISTS (SELECT 1 FROM {target_table} t "
                f"WHERE t.{join_col} = co.collar_id) AS has_{attr_name}"
            )
        if not exists_clauses:
            # Caller passed dimensions=[] (filter narrowed to nothing).
            # Still useful to surface collar locations; emit a stub
            # has_any column so the GeoJSON builder treats every
            # collar as "no data" without crashing on missing keys.
            exists_clauses.append("  FALSE AS has_any_stub")
        exists_block = ",\n".join(exists_clauses)
        return f"""
            SELECT
                co.collar_id::text  AS collar_id,
                co.hole_id          AS hole_id,
                ST_X(co.geom_4326::geometry) AS longitude,
                ST_Y(co.geom_4326::geometry) AS latitude,
{exists_block}
            FROM silver.collars co
            JOIN silver.projects p ON p.project_id = co.project_id
            WHERE p.workspace_id = $1::uuid
              AND co.project_id = $2::uuid
              AND co.geom_4326 IS NOT NULL
        """

    async def _run() -> tuple[
        IngestGapStats,
        list[AttributeCoverageRow],
        list[CoverageFindingRow],
        dict | None,
    ]:
        async with deps.pg_pool.acquire() as conn:
            # Ingest stage
            try:
                ingest_row = await conn.fetchrow(ingest_sql, workspace_id)
            except Exception:
                logger.exception(
                    "query_coverage_gap: ingest stage query failed workspace=%s",
                    workspace_id,
                )
                ingest_row = None

            if ingest_row is not None:
                indexed = int(ingest_row["indexed_n"] or 0)
                processed = int(ingest_row["processed_n"] or 0)
                gap_pct = (
                    100.0 * (indexed - processed) / indexed if indexed > 0 else 0.0
                )
                ingest_gap = IngestGapStats(
                    indexed=indexed, processed=processed, gap_pct=round(gap_pct, 2)
                )
            else:
                ingest_gap = IngestGapStats(indexed=0, processed=0, gap_pct=0.0)

            # Collars total
            collars_row = await conn.fetchrow(
                collars_total_sql, workspace_id, project_id
            )
            collars_total = int(collars_row["n"]) if collars_row else 0

            # Per-attribute coverage
            attribute_coverage: list[AttributeCoverageRow] = []
            for attr_name, target_table, join_col in _COVERAGE_ATTRIBUTES:
                if selected_dims is not None and attr_name not in selected_dims:
                    continue
                try:
                    row = await conn.fetchrow(
                        _attribute_sql(target_table, join_col),
                        workspace_id,
                        project_id,
                    )
                except Exception:
                    # Some §04e tables may not exist on every deployment
                    # (e.g. silver.structure pre-PR-2). Skip cleanly.
                    logger.info(
                        "query_coverage_gap: skipping attribute %s "
                        "(table %s unavailable)",
                        attr_name,
                        target_table,
                    )
                    continue
                with_data = int(row["n"]) if row else 0
                coverage_pct = (
                    100.0 * with_data / collars_total
                    if collars_total > 0
                    else 0.0
                )
                attribute_coverage.append(
                    AttributeCoverageRow(
                        attribute=attr_name,
                        collars_with_data=with_data,
                        collars_total=collars_total,
                        coverage_pct=round(coverage_pct, 2),
                        source_row_ids=(
                            list(row["source_row_ids"] or [])
                            if row and with_data > 0
                            else []
                        ),
                    )
                )

            # Findings
            try:
                finding_rows = await conn.fetch(
                    findings_sql, workspace_id, project_id
                )
            except Exception:
                logger.exception(
                    "query_coverage_gap: findings query failed workspace=%s "
                    "project=%s",
                    workspace_id,
                    project_id,
                )
                finding_rows = []
            findings = [
                CoverageFindingRow(
                    kind=row["kind"],
                    severity=row["severity"],
                    description=row["description"],
                    source_row_ids=[row["finding_id"]],
                )
                for row in finding_rows
            ]

            # §6b P4 — per-collar coverage map. Skip when no attribute
            # coverage was requested (would emit a meaningless map). Any
            # DB failure here logs + returns None so the rest of the
            # response (table + findings) still ships.
            gap_geojson: dict | None = None
            try:
                collar_rows = await conn.fetch(
                    _collar_geojson_sql(selected_dims), workspace_id, project_id,
                )
            except Exception:
                logger.info(
                    "query_coverage_gap: per-collar geojson fetch failed "
                    "workspace=%s project=%s — table missing or geom_4326 "
                    "unavailable; map will be omitted",
                    workspace_id, project_id,
                )
                collar_rows = []

            if collar_rows:
                gap_geojson = _build_coverage_geojson(
                    collar_rows, selected_dims,
                )

        return ingest_gap, attribute_coverage, findings, gap_geojson

    logger.info(
        "query_coverage_gap: workspace=%s project=%s dims=%s",
        workspace_id,
        project_id,
        sorted(selected_dims) if selected_dims else "(all)",
    )
    try:
        ingest_gap, attribute_coverage, findings, gap_geojson = await asyncio.wait_for(
            _run(), timeout=settings.TIMEOUT_POSTGIS_S
        )
    except TimeoutError:
        logger.warning(
            "query_coverage_gap timed out workspace=%s project=%s",
            workspace_id,
            project_id,
        )
        ingest_gap = IngestGapStats(indexed=0, processed=0, gap_pct=0.0)
        attribute_coverage = []
        findings = []
        gap_geojson = None
    except Exception:
        logger.exception(
            "query_coverage_gap failed workspace=%s project=%s",
            workspace_id,
            project_id,
        )
        ingest_gap = IngestGapStats(indexed=0, processed=0, gap_pct=0.0)
        attribute_coverage = []
        findings = []
        gap_geojson = None

    total_count = (
        (1 if ingest_gap.indexed > 0 else 0)
        + len(attribute_coverage)
        + len(findings)
    )

    return CoverageGapResult(
        ingest_gap=ingest_gap,
        attribute_coverage=attribute_coverage,
        findings=findings,
        project_id=project_id,
        workspace_id=workspace_id,
        count=total_count,
        gap_geojson=gap_geojson,
    )


# ---------------------------------------------------------------------------
# ADR-0007 PR-2 — query_stereonet (server-side mplstereonet render)
# ---------------------------------------------------------------------------


@dataclass
class StereonetPoint:
    """Single structural measurement projected onto a stereonet.

    Sourced 1:1 from ``gold.structure_measurements_visual``. The
    ``source_row_id`` is the gold visual_id (a UUID that anchors back to
    a silver.structure row via the same collar+depth+kind tuple — per
    §04i Layer 5 provenance).
    """

    depth: float | None
    structure_type: str
    strike_deg: float | None
    dip_deg: float | None
    dip_direction_deg: float | None
    plunge_deg: float | None
    trend_deg: float | None
    stereonet_x: float
    stereonet_y: float
    source_row_id: str


@dataclass
class StereonetResult:
    """Return type for ``query_stereonet``.

    image_base64 is a raw base64-encoded PNG (no ``data:`` prefix —
    the frontend prepends it). The PNG comes from a server-side
    mplstereonet equal-area lower-hemisphere render so the LLM never
    fabricates point coordinates (§04i Layer 3).
    """

    points: list[StereonetPoint]
    image_base64: str
    project_id: str
    workspace_id: str
    count: int
    projection: str = "Schmidt"
    data_source: str = (
        "PostGIS gold.structure_measurements_visual + mplstereonet server-render"
    )


# Cap render size so a project with thousands of measurements doesn't
# block the FastAPI worker for tens of seconds. Downsample is deterministic
# (sorted by visual_id, every Nth row) so the same query returns the same
# rendered subset on every call — keeps citation IDs stable.
_STEREONET_MAX_POINTS: int = 500


def _downsample_stereonet_points(
    rows: list[dict], max_points: int = _STEREONET_MAX_POINTS,
) -> list[dict]:
    """Stride-sample ``rows`` down to at most ``max_points`` items.

    Deterministic: ``rows`` is sorted by ``visual_id`` ASC by the caller
    so the same query returns the same subset across runs (citation
    stability per §04i).
    """
    if len(rows) <= max_points:
        return rows
    stride = len(rows) / max_points
    return [rows[int(i * stride)] for i in range(max_points)]


def _render_stereonet_png(points: list[StereonetPoint]) -> str:
    """Render an equal-area (Schmidt) lower-hemisphere stereonet PNG.

    Returns raw base64. Uses ``mplstereonet`` as the canonical renderer
    per §04g — do NOT substitute another lib without an ADR amendment.

    Renders poles to planes when (strike, dip) are available, and
    individual line measurements when (trend, plunge) are. Empty
    inputs return a blank-axes PNG so the card always renders an image.
    """
    import base64  # noqa: PLC0415
    import io  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    import mplstereonet  # noqa: PLC0415

    fig, ax = mplstereonet.subplots(figsize=(6, 6))
    ax.grid(True, linestyle=":", alpha=0.4)

    # Group by structure_type so each kind gets its own colour + legend entry.
    by_type: dict[str, list[StereonetPoint]] = {}
    for p in points:
        by_type.setdefault(p.structure_type or "other", []).append(p)

    _palette: dict[str, str] = {
        "bedding":   "#1f77b4",
        "foliation": "#2ca02c",
        "cleavage":  "#17becf",
        "joint":     "#ff7f0e",
        "fault":     "#d62728",
        "shear":     "#8c564b",
        "vein":      "#9467bd",
        "contact":   "#bcbd22",
        "fracture":  "#e377c2",
        "lineation": "#7f7f7f",
        "fold_axis": "#000000",
        "other":     "#aaaaaa",
    }

    for kind in sorted(by_type.keys()):
        pts = by_type[kind]
        colour = _palette.get(kind, "#aaaaaa")
        plane_strikes = [
            p.strike_deg for p in pts
            if p.strike_deg is not None and p.dip_deg is not None
        ]
        plane_dips = [
            p.dip_deg for p in pts
            if p.strike_deg is not None and p.dip_deg is not None
        ]
        if plane_strikes:
            ax.pole(
                plane_strikes, plane_dips,
                marker="o", markersize=4,
                color=colour,
                label=f"{kind} (n={len(plane_strikes)})",
            )
        line_trends = [
            p.trend_deg for p in pts
            if p.trend_deg is not None and p.plunge_deg is not None
        ]
        line_plunges = [
            p.plunge_deg for p in pts
            if p.trend_deg is not None and p.plunge_deg is not None
        ]
        if line_trends:
            ax.line(
                line_plunges, line_trends,
                marker="^", markersize=5,
                color=colour,
                label=f"{kind} lineation (n={len(line_trends)})",
            )

    if points:
        ax.legend(loc="lower right", fontsize=8, framealpha=0.7)
    ax.set_title(
        "Equal-area (Schmidt) lower-hemisphere",
        fontsize=10, pad=12,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight",
                facecolor="white", dpi=120)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@_metered("query_stereonet")
async def query_stereonet(
    deps: AgentDeps,
    workspace_id: str,
    project_id: str,
    structure_filter: str | list[str] | None = None,
) -> StereonetResult:
    """Aggregate gold.structure_measurements_visual and render a stereonet.

    Reads pre-projected stereonet points for the project (optionally
    filtered to one or more ``structure_type`` values), renders a
    server-side equal-area (Schmidt) lower-hemisphere projection via
    mplstereonet, and returns the base64 PNG alongside the underlying
    point list. Every point carries its ``silver.structure``-anchored
    ``source_row_id`` so the response_assembler can cite verbatim
    (§04i Layer 5).

    Args:
        deps: Agent dependencies (asyncpg pool required).
        workspace_id: Caller's workspace UUID from the JWT.
        project_id: Project to render.
        structure_filter: Optional single value or list of structure_type
            values to filter on (foliation / joint / fault / vein /
            contact / cleavage / bedding / shear / fracture / lineation /
            fold_axis / other). None → all kinds.

    Returns:
        :class:`StereonetResult` — empty points + blank PNG on any
        database error so the agentic graph keeps streaming.
    """
    if deps.pg_pool is None:
        logger.warning(
            "query_stereonet: deps.pg_pool is None — returning empty result"
        )
        return StereonetResult(
            points=[],
            image_base64=_render_stereonet_png([]),
            project_id=project_id,
            workspace_id=workspace_id,
            count=0,
        )

    if isinstance(structure_filter, str):
        filter_list: list[str] | None = [structure_filter]
    elif isinstance(structure_filter, list) and structure_filter:
        filter_list = [s for s in structure_filter if s]
    else:
        filter_list = None

    base_sql = """
        SELECT
            v.visual_id::text                       AS source_row_id,
            v.depth::float                          AS depth,
            v.structure_type                        AS structure_type,
            v.strike_deg::float                     AS strike_deg,
            v.dip_deg::float                        AS dip_deg,
            v.dip_direction_deg::float              AS dip_direction_deg,
            v.plunge_deg::float                     AS plunge_deg,
            v.trend_deg::float                      AS trend_deg,
            v.stereonet_x::float                    AS stereonet_x,
            v.stereonet_y::float                    AS stereonet_y
        FROM gold.structure_measurements_visual v
        WHERE v.workspace_id = $1::uuid
          AND v.project_id   = $2::uuid
    """

    bind: list = [workspace_id, project_id]
    if filter_list:
        bind.append(filter_list)
        sql = base_sql + " AND v.structure_type = ANY($3::text[]) ORDER BY v.visual_id"
    else:
        sql = base_sql + " ORDER BY v.visual_id"

    async def _run() -> list[dict]:
        async with deps.pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, *bind)
            return [dict(r) for r in rows]

    logger.info(
        "query_stereonet: workspace=%s project=%s filter=%s",
        workspace_id, project_id, filter_list,
    )
    try:
        raw_rows = await asyncio.wait_for(
            _run(), timeout=settings.TIMEOUT_POSTGIS_S,
        )
    except TimeoutError:
        logger.warning(
            "query_stereonet timed out workspace=%s project=%s",
            workspace_id, project_id,
        )
        raw_rows = []
    except Exception:
        logger.exception(
            "query_stereonet failed workspace=%s project=%s",
            workspace_id, project_id,
        )
        raw_rows = []

    sampled = _downsample_stereonet_points(raw_rows)

    points: list[StereonetPoint] = []
    for r in sampled:
        # Defensive: stereonet_x/_y are mandatory in the gold table for
        # planar measurements but lineation-only rows may carry NULLs.
        # Fall back to 0/0 so the dataclass shape is preserved; the
        # renderer uses strike/dip or trend/plunge directly anyway.
        points.append(StereonetPoint(
            depth=r.get("depth"),
            structure_type=r.get("structure_type") or "other",
            strike_deg=r.get("strike_deg"),
            dip_deg=r.get("dip_deg"),
            dip_direction_deg=r.get("dip_direction_deg"),
            plunge_deg=r.get("plunge_deg"),
            trend_deg=r.get("trend_deg"),
            stereonet_x=float(r.get("stereonet_x") or 0.0),
            stereonet_y=float(r.get("stereonet_y") or 0.0),
            source_row_id=str(r.get("source_row_id") or ""),
        ))

    # Render PNG off the asyncio loop — matplotlib is sync + CPU-bound.
    loop = asyncio.get_event_loop()
    try:
        image_b64 = await loop.run_in_executor(None, _render_stereonet_png, points)
    except Exception:
        logger.exception(
            "query_stereonet: PNG render failed workspace=%s project=%s",
            workspace_id, project_id,
        )
        image_b64 = ""

    return StereonetResult(
        points=points,
        image_base64=image_b64,
        project_id=project_id,
        workspace_id=workspace_id,
        count=len(points),
    )


# ---------------------------------------------------------------------------
# ADR-0007 PR-4 — 3D drill-trace card data tool
# ---------------------------------------------------------------------------


@dataclass
class DrillTraceCollar:
    """One drill hole rendered as a 3-D trace.

    Sourced from ``silver.collars`` + ``silver.drill_traces``. The trace
    points come from the LINESTRINGZ geometry written by the
    silver_drill_traces Dagster asset — every coordinate the LLM ever
    cites traces back to a row in silver.collars (§04i Layer 5).
    """

    hole_id: str
    collar_id: str
    longitude: float
    latitude: float
    elevation: float
    total_depth: float
    hole_type: str
    status: str
    azimuth: float
    dip: float
    # 2 points (collar + toe) for straight-line traces; N points along
    # the LINESTRINGZ for surveyed deviation. Each carries depth_m so
    # the React layer can interpolate interval colouring along the trace.
    trace_points: list[dict]


@dataclass
class DrillTraceInterval:
    """One coloured downhole interval from ``gold.drillhole_intervals_visual``.

    Optional overlay on the 3-D trace — assay grade, lithology, alteration,
    or structure depending on ``interval_kind``. ``color_hint`` is the hex
    colour the visual asset pre-computed for the strip log; reusing it
    keeps the 3-D card and the strip-log card colour-consistent.
    """

    collar_id: str
    depth_from: float
    depth_to: float
    interval_kind: str
    color_hint: str
    label: str
    source_row_id: str


@dataclass
class DrillTraceStructure:
    """One structural measurement plotted as a pole along the trace.

    Sourced from ``gold.structure_measurements_visual``. Mirrors a subset
    of :class:`StereonetPoint` so the 3-D card can render a small pole
    glyph at the right downhole depth.
    """

    collar_id: str
    depth: float
    structure_type: str
    strike_deg: float | None
    dip_deg: float | None
    source_row_id: str


@dataclass
class DrillTrace3DResult:
    """Return type for :func:`query_drill_traces_3d`.

    Carries everything the React DrillTrace3D component needs to render
    the chat-inline 3-D card, plus the row-ids needed for §04i citations.

    ``source_row_ids`` aggregates collar_ids + interval visual_ids +
    structure visual_ids so the response_assembler can bind every
    quoted number back to a silver/gold row.
    """

    collars: list[DrillTraceCollar]
    intervals: list[DrillTraceInterval]
    structures: list[DrillTraceStructure]
    project_id: str
    workspace_id: str
    count: int
    hole_id_filter: str | None
    source_row_ids: list[str]
    data_source: str = (
        "PostGIS silver.collars + silver.drill_traces (+ gold.drillhole_intervals_visual)"
    )


# Per-call cap so a project with many holes can't blow the response
# size. The frontend renders all returned holes; 200 is plenty for any
# chat-inline view and matches the 3-D scatter's interactive ceiling.
_DRILL_TRACE_MAX_COLLARS: int = 200
# Per-trace point cap. Surveyed traces with N>2 points already get
# decimated client-side, but capping here keeps the JSON small.
_DRILL_TRACE_MAX_POINTS_PER_TRACE: int = 50


def _parse_linestring_z_points(wkt_or_text: str) -> list[tuple[float, float, float]]:
    """Parse a ``LINESTRING Z (lon lat z, lon lat z, ...)`` WKT.

    Returns a list of (lon, lat, z) tuples. Empty list when the input is
    malformed — the caller falls back to a 2-point straight line built
    from the collar coordinates so the card always renders something.
    """
    if not wkt_or_text:
        return []
    try:
        inside = wkt_or_text[wkt_or_text.index("(") + 1: wkt_or_text.rindex(")")]
    except ValueError:
        return []
    pts: list[tuple[float, float, float]] = []
    for chunk in inside.split(","):
        bits = chunk.strip().split()
        if len(bits) < 3:
            continue
        try:
            pts.append((float(bits[0]), float(bits[1]), float(bits[2])))
        except ValueError:
            continue
    return pts


def _downsample_trace_points(
    pts: list[tuple[float, float, float]],
    max_points: int = _DRILL_TRACE_MAX_POINTS_PER_TRACE,
) -> list[tuple[float, float, float]]:
    """Stride-sample a trace down to ``max_points`` items while keeping the toe.

    Deterministic: same input → same output. The first and last points
    are always preserved so the visible line still terminates at the toe.
    """
    if len(pts) <= max_points:
        return pts
    stride = (len(pts) - 1) / (max_points - 1)
    sampled = [pts[int(i * stride)] for i in range(max_points - 1)]
    sampled.append(pts[-1])
    return sampled


@_metered("query_drill_traces_3d")
async def query_drill_traces_3d(
    deps: AgentDeps,
    workspace_id: str,
    project_id: str,
    hole_id: str | None = None,
) -> DrillTrace3DResult:
    """Aggregate collars + drill_traces + (optional) intervals + structures for the 3-D card.

    Reads from ``silver.collars`` joined to ``silver.drill_traces``
    (LINESTRINGZ in EPSG:4326) for the trace geometry. When ``hole_id``
    is provided we narrow to that single hole; otherwise we return up to
    :data:`_DRILL_TRACE_MAX_COLLARS` collars for the project.

    Interval overlays come from ``gold.drillhole_intervals_visual`` and
    structural poles from ``gold.structure_measurements_visual``; both
    are best-effort — an error returns an empty list rather than
    failing the whole tool.

    Args:
        deps: Agent dependencies (asyncpg pool required).
        workspace_id: Caller's workspace UUID from the JWT.
        project_id: Project to render.
        hole_id: Optional single hole to scope to (e.g. ``"36-1085"``).
            Matches against ``silver.collars.hole_id`` AND
            ``hole_id_canonical`` so either form works from the user query.

    Returns:
        :class:`DrillTrace3DResult` — empty collars list on any database
        error so the agentic graph keeps streaming.
    """
    empty = DrillTrace3DResult(
        collars=[],
        intervals=[],
        structures=[],
        project_id=project_id,
        workspace_id=workspace_id,
        count=0,
        hole_id_filter=hole_id,
        source_row_ids=[],
    )

    if deps.pg_pool is None:
        logger.warning(
            "query_drill_traces_3d: deps.pg_pool is None — returning empty result"
        )
        return empty

    # Convert any straight-line / surveyed trace into a list of (lon, lat,
    # z) tuples by extracting the underlying coordinates as text. We then
    # parse client-side rather than calling ST_DumpPoints to avoid a
    # second round-trip per collar.
    collar_sql = """
        SELECT
            c.collar_id::text                                   AS collar_id,
            c.hole_id                                           AS hole_id,
            c.hole_type                                         AS hole_type,
            c.status                                            AS status,
            COALESCE(c.elevation, 0.0)::float                   AS elevation,
            COALESCE(c.total_depth, 0.0)::float                 AS total_depth,
            COALESCE(c.azimuth, 0.0)::float                     AS azimuth,
            COALESCE(c.dip, -90.0)::float                       AS dip,
            ST_X(ST_Transform(c.geom, 4326))::float             AS longitude,
            ST_Y(ST_Transform(c.geom, 4326))::float             AS latitude,
            ST_AsText(t.geom)                                   AS trace_wkt
        FROM silver.collars c
        LEFT JOIN silver.drill_traces t
               ON t.collar_id = c.collar_id
              AND t.workspace_id = $1::uuid
        WHERE c.workspace_id = $1::uuid
          AND c.project_id   = $2::uuid
    """
    bind: list = [workspace_id, project_id]
    if hole_id:
        bind.append(hole_id)
        collar_sql += (
            " AND (c.hole_id = $3 OR c.hole_id_canonical = $3)"
            " ORDER BY c.hole_id LIMIT 1"
        )
    else:
        collar_sql += f" ORDER BY c.hole_id LIMIT {_DRILL_TRACE_MAX_COLLARS}"

    async def _run_collars() -> list[dict]:
        async with deps.pg_pool.acquire() as conn:
            rows = await conn.fetch(collar_sql, *bind)
            return [dict(r) for r in rows]

    logger.info(
        "query_drill_traces_3d: workspace=%s project=%s hole_id=%s",
        workspace_id, project_id, hole_id,
    )
    try:
        collar_rows = await asyncio.wait_for(
            _run_collars(), timeout=settings.TIMEOUT_POSTGIS_S,
        )
    except TimeoutError:
        logger.warning(
            "query_drill_traces_3d: collar query timed out workspace=%s project=%s",
            workspace_id, project_id,
        )
        return empty
    except Exception:
        logger.exception(
            "query_drill_traces_3d: collar query failed workspace=%s project=%s",
            workspace_id, project_id,
        )
        return empty

    if not collar_rows:
        return empty

    collars: list[DrillTraceCollar] = []
    collar_ids: list[str] = []
    for r in collar_rows:
        cid = str(r.get("collar_id") or "")
        if not cid:
            continue
        lon = float(r.get("longitude") or 0.0)
        lat = float(r.get("latitude") or 0.0)
        elev = float(r.get("elevation") or 0.0)
        td = float(r.get("total_depth") or 0.0)
        az = float(r.get("azimuth") or 0.0)
        dip = float(r.get("dip") or -90.0)

        wkt_points = _parse_linestring_z_points(r.get("trace_wkt") or "")
        if wkt_points:
            wkt_points = _downsample_trace_points(wkt_points)
            # Approximate per-point depth by linear interpolation along
            # total_depth — the WKT itself doesn't carry MD. Sufficient
            # for the visual interval-overlay binding.
            n = max(len(wkt_points) - 1, 1)
            trace_points = [
                {
                    "x": float(p[0]),
                    "y": float(p[1]),
                    "z": float(p[2]),
                    "depth_m": float(td * (i / n)),
                }
                for i, p in enumerate(wkt_points)
            ]
        else:
            # Fallback when silver.drill_traces has no row for this
            # collar (e.g. unusable orientation). Emit a 2-point vertical
            # placeholder so the card still renders the hole position.
            trace_points = [
                {"x": lon, "y": lat, "z": elev, "depth_m": 0.0},
                {"x": lon, "y": lat, "z": elev - td, "depth_m": td},
            ]

        collars.append(DrillTraceCollar(
            hole_id=str(r.get("hole_id") or ""),
            collar_id=cid,
            longitude=lon,
            latitude=lat,
            elevation=elev,
            total_depth=td,
            hole_type=str(r.get("hole_type") or ""),
            status=str(r.get("status") or ""),
            azimuth=az,
            dip=dip,
            trace_points=trace_points,
        ))
        collar_ids.append(cid)

    if not collars:
        return empty

    # --- Intervals (best-effort) --------------------------------------------
    interval_sql = """
        SELECT
            v.collar_id::text                       AS collar_id,
            v.visual_id::text                       AS source_row_id,
            v.depth_from::float                     AS depth_from,
            v.depth_to::float                       AS depth_to,
            v.interval_kind                         AS interval_kind,
            v.color_hint                            AS color_hint,
            COALESCE(v.lithology_label, '')::text   AS label
        FROM gold.drillhole_intervals_visual v
        WHERE v.workspace_id = $1::uuid
          AND v.project_id   = $2::uuid
          AND v.collar_id    = ANY($3::uuid[])
        ORDER BY v.collar_id, v.depth_from
        LIMIT 1000
    """
    intervals: list[DrillTraceInterval] = []
    try:
        async def _run_intervals() -> list[dict]:
            async with deps.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    interval_sql, workspace_id, project_id, collar_ids,
                )
                return [dict(r) for r in rows]

        interval_rows = await asyncio.wait_for(
            _run_intervals(), timeout=settings.TIMEOUT_POSTGIS_S,
        )
    except TimeoutError:
        logger.warning(
            "query_drill_traces_3d: interval query timed out (continuing)"
        )
        interval_rows = []
    except Exception:
        # Schema differences across deployments (some columns may be
        # named differently). Log + continue with empty intervals — the
        # card still renders the trace lines.
        logger.warning(
            "query_drill_traces_3d: interval query failed (continuing): %s",
            "see logs",
            exc_info=True,
        )
        interval_rows = []

    for r in interval_rows:
        intervals.append(DrillTraceInterval(
            collar_id=str(r.get("collar_id") or ""),
            depth_from=float(r.get("depth_from") or 0.0),
            depth_to=float(r.get("depth_to") or 0.0),
            interval_kind=str(r.get("interval_kind") or ""),
            color_hint=str(r.get("color_hint") or "#888888"),
            label=str(r.get("label") or ""),
            source_row_id=str(r.get("source_row_id") or ""),
        ))

    # --- Structures (best-effort) ------------------------------------------
    structure_sql = """
        SELECT
            v.collar_id::text       AS collar_id,
            v.visual_id::text       AS source_row_id,
            v.depth::float          AS depth,
            v.structure_type        AS structure_type,
            v.strike_deg::float     AS strike_deg,
            v.dip_deg::float        AS dip_deg
        FROM gold.structure_measurements_visual v
        WHERE v.workspace_id = $1::uuid
          AND v.project_id   = $2::uuid
          AND v.collar_id    = ANY($3::uuid[])
        ORDER BY v.collar_id, v.depth
        LIMIT 500
    """
    structures: list[DrillTraceStructure] = []
    try:
        async def _run_structures() -> list[dict]:
            async with deps.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    structure_sql, workspace_id, project_id, collar_ids,
                )
                return [dict(r) for r in rows]

        structure_rows = await asyncio.wait_for(
            _run_structures(), timeout=settings.TIMEOUT_POSTGIS_S,
        )
    except TimeoutError:
        logger.warning(
            "query_drill_traces_3d: structure query timed out (continuing)"
        )
        structure_rows = []
    except Exception:
        logger.warning(
            "query_drill_traces_3d: structure query failed (continuing)",
            exc_info=True,
        )
        structure_rows = []

    for r in structure_rows:
        structures.append(DrillTraceStructure(
            collar_id=str(r.get("collar_id") or ""),
            depth=float(r.get("depth") or 0.0),
            structure_type=str(r.get("structure_type") or "other"),
            strike_deg=(
                float(r["strike_deg"]) if r.get("strike_deg") is not None else None
            ),
            dip_deg=(
                float(r["dip_deg"]) if r.get("dip_deg") is not None else None
            ),
            source_row_id=str(r.get("source_row_id") or ""),
        ))

    source_row_ids: list[str] = list(collar_ids)
    source_row_ids.extend(i.source_row_id for i in intervals if i.source_row_id)
    source_row_ids.extend(s.source_row_id for s in structures if s.source_row_id)

    return DrillTrace3DResult(
        collars=collars,
        intervals=intervals,
        structures=structures,
        project_id=project_id,
        workspace_id=workspace_id,
        count=len(collars),
        hole_id_filter=hole_id,
        source_row_ids=source_row_ids,
    )
