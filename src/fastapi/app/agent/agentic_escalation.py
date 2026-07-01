"""R9-full — Pydantic AI agentic second-tier escalation.

Invoked only when BOTH the deterministic keyword-classifier path AND the
bounded rephrasing retry (R9-lite in escalation.py) return empty. The
agent gets bounded tool-call budget to figure out what the deterministic
dispatch missed.

When to expect this to fire
---------------------------
- First tier (deterministic): covers 99%+ of well-classified queries.
- Second tier (rephrasing): catches queries where the keywords didn't
  match but a rephrased version does.
- This third tier (agent): queries where neither the original nor any
  rephrasing matches a single tool's keyword classifier — e.g., the
  user asked about a geological concept that requires a multi-hop path
  (graph entity → related documents → spatial context) that no single
  tool catches on its own.

Bounded cost
------------
- `AGENTIC_MAX_TOOL_CALLS` (default 8, env-tunable) caps retrieval tool
  invocations per run.  Raised from 3 to 8 in §04p Phase 2.B-i to budget
  for PDF chaining (find_legends → crop_region → ocr_region →
  summarize_section needs 4 calls minimum).  Non-PDF queries pay nothing
  extra — the agent stops as soon as it has enough context.
  `AGENTIC_MAX_VERIFY_CALLS` (default 3) adds headroom for
  verify_numerical_claim calls, which do not consume the retrieval budget.
- A 10-second timeout wraps the whole agentic run.
- Returns structured tool_results in the same shape as the deterministic
  dispatch so the caller can swap in without other changes.
- On any failure the function logs + returns empty — strictly additive,
  never blocks.

This is explicitly a SECOND escalation, not a replacement of the
deterministic path. The signal that ought to trigger a swap
(deterministic-replaced-by-agent) would be sustained >50% escalation
rates, which we'll see on the Grafana dashboard.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai import Agent, RunContext

# Phase H — deferred import for AnthropicModel / AnthropicProvider.
# `pydantic_ai.models.anthropic` raises ImportError at import time when
# the installed anthropic SDK is older than the version pydantic-ai
# expects (today: anthropic 0.102.0 missing UserLocation symbol that
# pydantic-ai 1.38.0 expects from beta_web_search_tool_20250305_param).
# We don't need these symbols at module load — only when an escalation
# actually fires. Late-bind so the module is importable, and surface a
# clear error if escalation is invoked without a compatible SDK pair.
try:
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    _HAS_ANTHROPIC_MODEL = True
    _ANTHROPIC_MODEL_IMPORT_ERROR: ImportError | None = None
except ImportError as _anthropic_imp_err:
    AnthropicModel = None  # type: ignore[assignment,misc]
    AnthropicProvider = None  # type: ignore[assignment,misc]
    _HAS_ANTHROPIC_MODEL = False
    _ANTHROPIC_MODEL_IMPORT_ERROR = _anthropic_imp_err

from app.agent.deps import AgentDeps
from app.agent.pdf_tool_results import (
    PdfCropRegionToolResult,
    PdfExtractTextToolResult,
    PdfFindCoordinatesToolResult,
    PdfFindLegendsToolResult,
    PdfFindTablesToolResult,
    PdfLayoutRegionSummary,
    PdfOcrRegionToolResult,
    PdfRenderPageToolResult,
    PdfSummarizeSectionToolResult,
    PdfTableSummary,
    PdfTextBlockSummary,
    VlClaimSummary,
)
from app.agent.tools import (
    DocumentSearchResult,
    GraphTraversalResult,
    NumericalClaimVerification,
    SpatialQueryResult,
    query_spatial_collars,
    search_documents,
    traverse_knowledge_graph,
    verify_numerical_claim,
)
from app.config import settings
from app.metrics import TOOL_DURATION, TOOL_RESULT_COUNT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# §04p Phase 2.B-ii — PDF tool instrumentation helper
#
# The 4 pre-PDF tools (search_documents, query_spatial_collars,
# traverse_knowledge_graph, verify_numerical_claim) are NOT instrumented
# here — this is a §04p-only slice.  General tool instrumentation is
# tracked separately in the backlog.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _instrument_pdf_tool(tool_name: str):  # type: ignore[return]
    """Async context manager that fires TOOL_DURATION + TOOL_RESULT_COUNT.

    Usage::

        async with _instrument_pdf_tool("pdf_extract_text") as (outcome, count):
            ...
            count["count"] = len(blocks)

    Outcome defaults to "ok".  Set ``outcome["value"] = "error"`` before
    yielding or raise — any uncaught exception flips outcome to "error"
    automatically.  The finally block always fires so the metric is recorded
    even when the caller re-raises.
    """
    start = time.perf_counter()
    outcome: dict[str, str] = {"value": "ok"}
    count: dict[str, int] = {"count": 0}
    try:
        yield outcome, count
    except Exception:
        outcome["value"] = "error"
        raise
    finally:
        TOOL_DURATION.labels(tool=tool_name, outcome=outcome["value"]).observe(
            time.perf_counter() - start
        )
        TOOL_RESULT_COUNT.labels(tool=tool_name).observe(count["count"])


# ---------------------------------------------------------------------------
# Bronze-store helper (shared by all 8 PDF tool wrappers)
# ---------------------------------------------------------------------------


class _PdfNotFoundError(Exception):
    """Raised by _fetch_pdf_bytes_for_agent when the pdf_id is not in Bronze."""


class _WorkspaceRequiredError(Exception):
    """Raised when an agent PDF tool runs without a resolvable workspace_id."""


def _require_workspace_uuid(ctx: RunContext[AgentDeps]) -> uuid.UUID:
    """Resolve ctx.deps.workspace_id to uuid.UUID or raise.

    Agent PDF tools persist into workspace-scoped silver.pdf_* tables, all of
    which have NOT NULL workspace_id. Refuse to run when the agent context
    cannot supply one — the caller surfaces this as a tool error.
    """
    wid = getattr(ctx.deps, "workspace_id", None)
    if not wid:
        raise _WorkspaceRequiredError("workspace_id missing from AgentDeps")
    try:
        return uuid.UUID(str(wid))
    except (ValueError, AttributeError) as exc:
        raise _WorkspaceRequiredError(
            f"workspace_id {wid!r} is not a valid UUID"
        ) from exc


async def _fetch_pdf_bytes_for_agent(ctx: RunContext[AgentDeps], pdf_id: str) -> bytes:
    """Load normalised PDF bytes from the Bronze store by pdf_id.

    Mirrors the router's _fetch_pdf_bytes helper but reads from
    ctx.deps.bronze_store so the agent tools don't need HTTP round-trips
    through the FastAPI router layer.

    Raises _PdfNotFoundError when the pdf_id is absent from the store
    (translated to success=False in each tool wrapper).
    """
    store = ctx.deps.bronze_store
    if store is None:
        raise _PdfNotFoundError("bronze_store not available on AgentDeps")
    key = f"pdfs/{pdf_id}.pdf"
    pdf_bytes = await store.get(key)
    if pdf_bytes is None:
        raise _PdfNotFoundError(f"pdf_id {pdf_id[:16]}... not found in Bronze store")
    return pdf_bytes


# Phase 14 Step 1 (R-P12-more-prompts) — the agentic-escalation
# system prompt was previously a 67-line module-level triple-quoted
# string here. It now lives at the canonical Phase 11 Step 3 path;
# see prompts/agent_system.py.
from app.agent.prompts.agent_system import (  # noqa: E402
    SYSTEM_PROMPT as _AGENT_SYSTEM_PROMPT,
)


async def _build_agent(deps: AgentDeps) -> Agent | None:
    """Construct a Pydantic AI agent bound to the current Anthropic client.

    Returns None when no usable anthropic_client is attached to deps —
    the escalation is opportunistic, not a hard requirement.
    """
    client = getattr(deps, "anthropic_client", None)
    if client is None:
        return None

    # Phase H — fail clean when the late-bound AnthropicModel /
    # AnthropicProvider couldn't import. Today this trips when the
    # installed anthropic SDK lacks the symbols pydantic-ai expects
    # (anthropic 0.102 ↔ pydantic-ai 1.38 UserLocation mismatch).
    if not _HAS_ANTHROPIC_MODEL:
        logger.warning(
            "agentic_escalation: AnthropicModel import unavailable — "
            "escalation skipped. Bump anthropic SDK or pin pydantic-ai to "
            "a compatible version. Underlying error: %s",
            _ANTHROPIC_MODEL_IMPORT_ERROR,
        )
        return None

    # Pydantic AI's AnthropicProvider can be constructed from a pre-built
    # AsyncAnthropic instance so we reuse the pooled client from app.state
    # rather than paying a second TLS handshake.
    provider = AnthropicProvider(anthropic_client=client)
    model_name = getattr(settings, "MODEL_TIER_STANDARD", "claude-sonnet-4-6")
    model = AnthropicModel(model_name, provider=provider)

    agent: Agent[AgentDeps] = Agent(
        model=model,
        deps_type=AgentDeps,
        system_prompt=_AGENT_SYSTEM_PROMPT,
        retries=0,  # outer orchestrator owns retries
    )

    # ── Register the retrieval tools ────────────────────────────────────
    # Each wraps the deterministic implementation so the agent's choices
    # hit the same code paths (reranker, quality gate, graph fallback).

    @agent.tool
    async def search_documents_tool(
        ctx: RunContext[AgentDeps], query_text: str
    ) -> DocumentSearchResult:
        """Search NI 43-101 technical reports for passages relevant to a query.

        Returns up to 8 chunks ranked by a cross-encoder reranker, each
        attributed to a specific report + section. The Layer-1 quality
        gate (`RETRIEVAL_QUALITY_THRESHOLD`) drops irrelevant chunks
        before they reach this output, so an empty result genuinely means
        "no documents matched", not "search failed".

        Use this tool for geological interpretations, resource estimates,
        deposit descriptions, mineralisation models, structural settings —
        anything whose answer lives in written reports rather than
        structured drill-hole rows. Do NOT use it as a substitute for
        `query_spatial_collars_tool` when the user asks about the drill
        programme layout itself.

        Args:
            query_text: Natural-language search string. Specific keywords
                ("Athabasca basement unconformity", "Section 13 resource
                estimate") work better than full questions.
        """
        return await search_documents(
            ctx,
            query_text=query_text,
            project_id=ctx.deps.project_id,
            limit=8,
            score_threshold=settings.RETRIEVAL_QUALITY_THRESHOLD,
        )

    @agent.tool
    async def traverse_knowledge_graph_tool(
        ctx: RunContext[AgentDeps], entity_name: str
    ) -> GraphTraversalResult:
        """Look up a named entity + its relationships in the knowledge graph.

        Returns the matching Neo4j node plus 1-hop neighbours (typed
        relationships). Useful when the user names something specific:
        a deposit ("Cigar Lake"), formation ("Manitou Falls"), company,
        qualified person, or commodity. Returns an empty result when no
        node matches the supplied name — the graph only knows entities
        that have been extracted from ingested reports.

        Args:
            entity_name: Exact or near-exact name string. Case-insensitive
                match. Multi-word entity names work; partial-word probes
                ("Cigar") may miss because the graph indexes whole names.
        """
        return await traverse_knowledge_graph(
            ctx,
            entity_name=entity_name,
            project_id=ctx.deps.project_id,
        )

    @agent.tool
    async def query_spatial_collars_tool(
        ctx: RunContext[AgentDeps],
        hole_type: str | None = None,
        status_filter: str | None = None,
        radius_m: float | None = None,
        center_easting: float | None = None,
        center_northing: float | None = None,
        limit: int = 200,
    ) -> SpatialQueryResult:
        """Fetch drill-hole collar metadata for the active project.

        Returns collar rows (hole_id, easting, northing, elevation,
        total_depth, azimuth, dip, hole_type, status, drill_date,
        longitude, latitude). Use for questions about programme layout,
        hole counts, hole-type / status mix, or spatial extent.

        All filters are optional; omit them for a project-wide listing.
        Pass a center + radius to restrict to a spatial neighbourhood
        (uses ST_DWithin on the project's native UTM CRS).

        Args:
            hole_type: Restrict to a specific hole type
                ("DD", "RC", "AC", etc.). None for any.
            status_filter: Restrict to a specific status
                ("complete", "abandoned", "active"). None for any.
            radius_m: Spatial filter radius (metres). When set, also
                pass center_easting + center_northing.
            center_easting: UTM easting (project CRS) of the search
                centre. Required when radius_m is set.
            center_northing: UTM northing of the search centre.
                Required when radius_m is set.
            limit: Max rows to return (default 200, hard-capped server
                side). Aggregate counts in the result are over the
                full unfiltered set.
        """
        return await query_spatial_collars(
            ctx,
            project_id=ctx.deps.project_id,
            center_easting=center_easting,
            center_northing=center_northing,
            radius_m=radius_m,
            hole_type=hole_type,
            status_filter=status_filter,
            limit=limit,
        )

    @agent.tool
    async def verify_numerical_claim_tool(
        ctx: RunContext[AgentDeps],
        table: str,
        column: str,
        row_id: str,
        claimed_value: float,
        tolerance: float = 0.001,
    ) -> NumericalClaimVerification:
        """Verify a precise numerical value against the database (Layer 3
        hallucination prevention). Returns whether the claimed value
        matches the actual stored value within ``tolerance``.

        Use this AFTER a retrieval tool has surfaced a row whose number
        you intend to quote — never as a discovery tool. It is gated by
        a per-table column allowlist (P0 #2): asking for a column that
        isn't on the allowlist returns a BLOCKED result without touching
        the database.

        Allowed (table, column) combinations the LLM can verify:
          - silver.collars: total_depth, azimuth, dip, easting, northing, elevation
          - silver.samples: from_depth, to_depth, sample_length, recovery
          - silver.lithology_logs: from_depth, to_depth, rqd, recovery
          - silver.alteration: from_depth, to_depth, intensity
          - silver.structures: depth, true_dip, dip_direction, apparent_dip
          - silver.geochemistry: numeric element columns
          - silver.surveys: depth, azimuth, dip
          - bronze.reports: tonnage, grade values

        Args:
            table: Schema-qualified table name (e.g. "silver.collars").
            column: Numeric column to check.
            row_id: UUID of the row whose value you want to verify.
            claimed_value: The number you intend to state to the user.
            tolerance: Absolute tolerance for comparison (default 0.001).
        """
        return await verify_numerical_claim(
            ctx,
            table=table,
            column=column,
            row_id=row_id,
            claimed_value=claimed_value,
            tolerance=tolerance,
        )

    # ── §04p Phase 2.B-i — PDF subsystem tools ──────────────────────────────
    # Each tool checks for service availability on ctx.deps before calling
    # the underlying service method.  A None service returns success=False
    # with a structured error — the agent can decide whether to rephrase or
    # skip the PDF path entirely.

    @agent.tool
    async def pdf_render_page_tool(
        ctx: RunContext[AgentDeps],
        pdf_id: str,
        page: int,
        dpi: int = 200,
    ) -> PdfRenderPageToolResult:
        """Render a PDF page to a PNG image (returned as base64-encoded bytes).

        Use this when you need to visually inspect what is on a specific page
        before deciding which sub-region to crop or which layout regions to
        target with find_legends.  The base64 PNG can be large (300–800 KB for
        an A4 page at 200 DPI) — call this tool only when visual inspection
        genuinely guides the next step.

        For structured data (text, tables, coordinates), call the targeted
        tools (pdf_extract_text, pdf_find_tables, pdf_find_coordinates) instead
        to avoid burning token budget on image payloads.

        Args:
            pdf_id: SHA-256 hex of the normalised PDF (as returned by the
                /pdf/preflight endpoint and stored in the Bronze store).
            page: 1-indexed page number to render.
            dpi: Render resolution (72–300). Default 200 is suitable for
                visual inspection and VL input. Use 72–96 for thumbnails.
        """
        service = ctx.deps.pdf_render_service
        if service is None:
            async with _instrument_pdf_tool("pdf_render_page") as (outcome, _count):
                outcome["value"] = "error"
            return PdfRenderPageToolResult(
                success=False,
                error="pdf_render_service not available on AgentDeps",
            )

        async with _instrument_pdf_tool("pdf_render_page") as (outcome, count):
            try:
                pdf_bytes = await _fetch_pdf_bytes_for_agent(ctx, pdf_id)
            except _PdfNotFoundError as exc:
                outcome["value"] = "error"
                return PdfRenderPageToolResult(success=False, error=str(exc))

            try:
                png_bytes = await service.render_page(pdf_bytes, pdf_id, page, dpi)
            except Exception as exc:
                logger.warning("pdf_render_page_tool failed: %s", exc)
                outcome["value"] = "error"
                return PdfRenderPageToolResult(success=False, error=str(exc))

            count["count"] = 1
            return PdfRenderPageToolResult(
                success=True,
                pdf_id=pdf_id,
                page=page,
                dpi=dpi,
                png_base64=base64.b64encode(png_bytes).decode(),
            )

    @agent.tool
    async def pdf_crop_region_tool(
        ctx: RunContext[AgentDeps],
        pdf_id: str,
        page: int,
        bbox: tuple[float, float, float, float],
        dpi: int = 200,
    ) -> PdfCropRegionToolResult:
        """Render and crop a specific region of a PDF page to a PNG image.

        Use this after pdf_find_legends has returned a region bbox that you
        want to inspect visually — for example, to see what a figure contains
        before running OCR on it, or to confirm a table's content before
        calling pdf_find_tables.

        The base64 PNG payload is smaller than a full-page render but can
        still be large for wide regions.  Crop tightly to the bbox of interest.

        Args:
            pdf_id: SHA-256 hex of the normalised PDF.
            page: 1-indexed page number containing the region.
            bbox: (x0, y0, x1, y1) in PDF user-space points.
                Origin = bottom-left, y increases upward (PDF convention).
                Use the bbox values returned by pdf_find_legends directly.
            dpi: Render resolution (72–300). Default 200 is suitable for
                visual inspection. Use 300 for OCR input regions.
        """
        service = ctx.deps.pdf_render_service
        if service is None:
            async with _instrument_pdf_tool("pdf_crop_region") as (outcome, _count):
                outcome["value"] = "error"
            return PdfCropRegionToolResult(
                success=False,
                error="pdf_render_service not available on AgentDeps",
            )

        async with _instrument_pdf_tool("pdf_crop_region") as (outcome, count):
            try:
                pdf_bytes = await _fetch_pdf_bytes_for_agent(ctx, pdf_id)
            except _PdfNotFoundError as exc:
                outcome["value"] = "error"
                return PdfCropRegionToolResult(success=False, error=str(exc))

            try:
                png_bytes = await service.crop_region(pdf_bytes, pdf_id, page, bbox, dpi)
            except Exception as exc:
                logger.warning("pdf_crop_region_tool failed: %s", exc)
                outcome["value"] = "error"
                return PdfCropRegionToolResult(success=False, error=str(exc))

            count["count"] = 1
            return PdfCropRegionToolResult(
                success=True,
                pdf_id=pdf_id,
                page=page,
                bbox=bbox,
                dpi=dpi,
                png_base64=base64.b64encode(png_bytes).decode(),
            )

    @agent.tool
    async def pdf_extract_text_tool(
        ctx: RunContext[AgentDeps],
        pdf_id: str,
        page: int | None = None,
    ) -> PdfExtractTextToolResult:
        """Extract text blocks with bounding boxes from a PDF using pdfminer.six.

        This is the PRIMARY text extraction path for PDFs.  Results are cached
        in silver.pdf_text_blocks and reused by pdf_find_coordinates.  Call
        this tool FIRST for any pdf_id before calling pdf_find_coordinates.

        Each returned block carries a (page, bbox) provenance anchor so the
        agent can cite the source location in its GeoRAGResponse.

        Note: This tool extracts digitally-embedded text only.  For image-based
        content (scanned maps, figures with text overlays), use pdf_ocr_region
        after locating the region with pdf_find_legends.

        Args:
            pdf_id: SHA-256 hex of the normalised PDF.
            page: 1-indexed page to extract, or None for all pages.
                Single-page extraction is faster and cheaper for targeted
                queries; use None only when you need the full document text.
        """
        service = ctx.deps.pdf_extract_service
        if service is None:
            async with _instrument_pdf_tool("pdf_extract_text") as (outcome, _count):
                outcome["value"] = "error"
            return PdfExtractTextToolResult(
                success=False,
                error="pdf_extract_service not available on AgentDeps",
            )

        async with _instrument_pdf_tool("pdf_extract_text") as (outcome, count):
            try:
                workspace_id = _require_workspace_uuid(ctx)
            except _WorkspaceRequiredError as exc:
                outcome["value"] = "error"
                return PdfExtractTextToolResult(success=False, error=str(exc))

            try:
                pdf_bytes = await _fetch_pdf_bytes_for_agent(ctx, pdf_id)
            except _PdfNotFoundError as exc:
                outcome["value"] = "error"
                return PdfExtractTextToolResult(success=False, error=str(exc))

            try:
                blocks_raw, cache_hit = await service.extract_text(
                    pdf_bytes, pdf_id, workspace_id=workspace_id, page=page,
                )
            except Exception as exc:
                logger.warning("pdf_extract_text_tool failed: %s", exc)
                outcome["value"] = "error"
                return PdfExtractTextToolResult(success=False, error=str(exc))

            blocks = [
                PdfTextBlockSummary(
                    page=b["page"],
                    text=b["text"],
                    bbox=(b["bbox_x0"], b["bbox_y0"], b["bbox_x1"], b["bbox_y1"]),
                )
                for b in blocks_raw
            ]
            count["count"] = len(blocks)
            return PdfExtractTextToolResult(
                success=True,
                blocks=blocks,
                cache_hit=cache_hit,
            )

    @agent.tool
    async def pdf_find_tables_tool(
        ctx: RunContext[AgentDeps],
        pdf_id: str,
        page: int | None = None,
    ) -> PdfFindTablesToolResult:
        """Extract table matrices with cell-level bounding boxes using pdfplumber.

        Use this tool when the query asks about structured data in a PDF table:
        assay results, resource estimates, drill-hole summary tables, etc.
        Each returned table includes the full cell-text matrix so the agent
        can read values directly.

        The first-cell bbox (bbox_first_cell) serves as the location anchor
        for citing the table in a GeoRAGResponse.

        Note: For numerically critical values (grades, assays, depths), always
        cross-check with verify_numerical_claim against the Silver structured
        tables.  Table cells from this tool are the PDF's representation of
        those values, not the authoritative Silver row.

        Args:
            pdf_id: SHA-256 hex of the normalised PDF.
            page: 1-indexed page to extract, or None for all pages.
        """
        service = ctx.deps.pdf_extract_service
        if service is None:
            async with _instrument_pdf_tool("pdf_find_tables") as (outcome, _count):
                outcome["value"] = "error"
            return PdfFindTablesToolResult(
                success=False,
                error="pdf_extract_service not available on AgentDeps",
            )

        async with _instrument_pdf_tool("pdf_find_tables") as (outcome, count):
            try:
                workspace_id = _require_workspace_uuid(ctx)
            except _WorkspaceRequiredError as exc:
                outcome["value"] = "error"
                return PdfFindTablesToolResult(success=False, error=str(exc))

            try:
                pdf_bytes = await _fetch_pdf_bytes_for_agent(ctx, pdf_id)
            except _PdfNotFoundError as exc:
                outcome["value"] = "error"
                return PdfFindTablesToolResult(success=False, error=str(exc))

            try:
                tables_raw, cache_hit = await service.extract_tables(
                    pdf_bytes, pdf_id, workspace_id=workspace_id, page=page,
                )
            except Exception as exc:
                logger.warning("pdf_find_tables_tool failed: %s", exc)
                outcome["value"] = "error"
                return PdfFindTablesToolResult(success=False, error=str(exc))

            tables = []
            for t in tables_raw:
                first_bbox: tuple[float, float, float, float] | None = None
                cell_bboxes = t.get("cell_bboxes") or []
                if cell_bboxes and cell_bboxes[0]:
                    raw = cell_bboxes[0][0]
                    if raw and len(raw) == 4:
                        first_bbox = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
                tables.append(
                    PdfTableSummary(
                        page=t["page"],
                        table_index=t["table_index"],
                        rows=t["rows"],
                        bbox_first_cell=first_bbox,
                        total_cells=t.get("total_cells", 0),
                    )
                )
            count["count"] = len(tables)
            return PdfFindTablesToolResult(
                success=True,
                tables=tables,
                cache_hit=cache_hit,
            )

    @agent.tool
    async def pdf_find_legends_tool(
        ctx: RunContext[AgentDeps],
        pdf_id: str,
        page: int | None = None,
        region_type: str | None = None,
    ) -> PdfFindLegendsToolResult:
        """Detect layout regions (figures, tables, headers, titles etc.) via Docling.

        Use this tool to locate specific content types within a PDF page before
        targeting them with pdf_crop_region or pdf_ocr_region.  Returns regions
        ordered by page + position with bounding boxes in PDF user-space.

        Typical chaining pattern:
          find_legends(page=5, region_type='figure') → inspect bboxes →
          crop_region(bbox) → ocr_region or summarize_section

        Args:
            pdf_id: SHA-256 hex of the normalised PDF.
            page: 1-indexed page to detect, or None for all pages.
                Single-page detection is faster for targeted queries.
            region_type: Optional filter — return only regions of this type.
                Allowed values: text | figure | table | header | footer |
                formula | title | caption | footnote | list | page_number | unknown.
                None returns all region types.
        """
        service = ctx.deps.pdf_layout_service
        if service is None:
            async with _instrument_pdf_tool("pdf_find_legends") as (outcome, _count):
                outcome["value"] = "error"
            return PdfFindLegendsToolResult(
                success=False,
                error="pdf_layout_service not available on AgentDeps",
            )

        async with _instrument_pdf_tool("pdf_find_legends") as (outcome, count):
            try:
                workspace_id = _require_workspace_uuid(ctx)
            except _WorkspaceRequiredError as exc:
                outcome["value"] = "error"
                return PdfFindLegendsToolResult(success=False, error=str(exc))

            try:
                pdf_bytes = await _fetch_pdf_bytes_for_agent(ctx, pdf_id)
            except _PdfNotFoundError as exc:
                outcome["value"] = "error"
                return PdfFindLegendsToolResult(success=False, error=str(exc))

            try:
                regions_raw, cache_hit = await service.detect_layout(
                    pdf_bytes, pdf_id, workspace_id=workspace_id, page=page,
                )
            except Exception as exc:
                logger.warning("pdf_find_legends_tool failed: %s", exc)
                outcome["value"] = "error"
                return PdfFindLegendsToolResult(success=False, error=str(exc))

            regions = [
                PdfLayoutRegionSummary(
                    page=r["page"],
                    region_index=r["region_index"],
                    region_type=r["region_type"],
                    bbox=(r["bbox_x0"], r["bbox_y0"], r["bbox_x1"], r["bbox_y1"]),
                    region_confidence=r.get("region_confidence"),
                )
                for r in regions_raw
                if region_type is None or r.get("region_type") == region_type
            ]
            # result_count = regions after region_type filter (not raw regions_raw).
            # An empty list with outcome="ok" means no matching region type was found —
            # a normal empty-result case, distinct from "error".
            count["count"] = len(regions)
            return PdfFindLegendsToolResult(
                success=True,
                regions=regions,
                cache_hit=cache_hit,
            )

    @agent.tool
    async def pdf_ocr_region_tool(
        ctx: RunContext[AgentDeps],
        pdf_id: str,
        page: int,
        bbox: tuple[float, float, float, float],
        dpi: int = 300,
    ) -> PdfOcrRegionToolResult:
        """Run PaddleOCR PP-OCRv5 on a specific region of a PDF page.

        Use this when digitally-embedded text is absent or incomplete for a
        target region — for example, text inside a scanned map image, a figure
        label, or a table rendered as an image.

        The tool renders the region crop at the specified DPI (300 recommended
        for OCR accuracy) and passes the PNG bytes to PaddleOCR.  Results are
        cached in silver.pdf_ocr_results.

        Lines are collapsed to text_content + mean_confidence to minimise
        token usage.  mean_confidence of 1.0 with empty text_content means
        the model ran but found no recognisable text in the region.

        Args:
            pdf_id: SHA-256 hex of the normalised PDF.
            page: 1-indexed page number containing the region.
            bbox: (x0, y0, x1, y1) in PDF user-space points.
                Use the bbox from pdf_find_legends to target a specific region.
            dpi: Render resolution for the crop (default 300 — higher than
                the standard 200 because OCR accuracy degrades at low DPI).
        """
        service = ctx.deps.pdf_ocr_service
        if service is None:
            async with _instrument_pdf_tool("pdf_ocr_region") as (outcome, _count):
                outcome["value"] = "error"
            return PdfOcrRegionToolResult(
                success=False,
                error="pdf_ocr_service not available on AgentDeps",
            )

        async with _instrument_pdf_tool("pdf_ocr_region") as (outcome, count):
            try:
                workspace_id = _require_workspace_uuid(ctx)
            except _WorkspaceRequiredError as exc:
                outcome["value"] = "error"
                return PdfOcrRegionToolResult(success=False, error=str(exc))

            try:
                pdf_bytes = await _fetch_pdf_bytes_for_agent(ctx, pdf_id)
            except _PdfNotFoundError as exc:
                outcome["value"] = "error"
                return PdfOcrRegionToolResult(success=False, error=str(exc))

            try:
                result_raw, cache_hit = await service.ocr_region(
                    pdf_bytes=pdf_bytes,
                    pdf_id=pdf_id,
                    page=page,
                    bbox=bbox,
                    workspace_id=workspace_id,
                    dpi=dpi,
                )
            except Exception as exc:
                logger.warning("pdf_ocr_region_tool failed: %s", exc)
                outcome["value"] = "error"
                return PdfOcrRegionToolResult(success=False, error=str(exc))

            count["count"] = 1  # single OCR result per call
            return PdfOcrRegionToolResult(
                success=True,
                text_content=result_raw.get("text_content"),
                mean_confidence=result_raw.get("mean_confidence"),
                cache_hit=cache_hit,
            )

    @agent.tool
    async def pdf_summarize_section_tool(
        ctx: RunContext[AgentDeps],
        pdf_id: str,
        section_kind: str,
        page: int | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
        region_id: str | None = None,
    ) -> PdfSummarizeSectionToolResult:
        """Summarise a PDF section using Qwen-VL (vision-language model).

        Each returned claim carries a (page, bbox) provenance anchor — these
        MUST be cited in the agent's final GeoRAGResponse per §04i Citation
        completeness (Layer 2).

        §04p Determinism rule: do NOT use summary_text as a coordinate source.
        Always call pdf_find_coordinates for UTM / lat-lon values.  Do NOT
        quote assay or grade numbers from summary_text without cross-checking
        via verify_numerical_claim.

        section_kind selects the section_ref shape:
          - 'page': requires page argument (single page)
          - 'page_range': requires page_start + page_end arguments
          - 'layout_region': requires region_id argument (UUID from
            silver.pdf_layout_regions, as returned by pdf_find_legends)

        Args:
            pdf_id: SHA-256 hex of the normalised PDF.
            section_kind: 'page' | 'page_range' | 'layout_region'
            page: 1-indexed page (required when section_kind='page').
            page_start: Start of page range (required when section_kind='page_range').
            page_end: End of page range inclusive (required when section_kind='page_range').
            region_id: UUID string of the layout region (required when
                section_kind='layout_region').
        """
        service = ctx.deps.pdf_vl_service
        if service is None:
            async with _instrument_pdf_tool("pdf_summarize_section") as (outcome, _count):
                outcome["value"] = "error"
            return PdfSummarizeSectionToolResult(
                success=False,
                error="pdf_vl_service not available on AgentDeps",
            )

        # Build section_ref from kind + args.
        # Argument-validation failures are treated as errors for metric purposes
        # (caller passed an invalid section_kind — this is an agent reasoning error,
        # not a "successfully empty" result).
        if section_kind == "page":
            if page is None:
                async with _instrument_pdf_tool("pdf_summarize_section") as (outcome, _count):
                    outcome["value"] = "error"
                return PdfSummarizeSectionToolResult(
                    success=False,
                    error="section_kind='page' requires the page argument",
                )
            section_ref: dict[str, Any] = {"kind": "page", "page": page}
        elif section_kind == "page_range":
            if page_start is None or page_end is None:
                async with _instrument_pdf_tool("pdf_summarize_section") as (outcome, _count):
                    outcome["value"] = "error"
                return PdfSummarizeSectionToolResult(
                    success=False,
                    error="section_kind='page_range' requires page_start and page_end",
                )
            section_ref = {"kind": "page_range", "page_start": page_start, "page_end": page_end}
        elif section_kind == "layout_region":
            if region_id is None:
                async with _instrument_pdf_tool("pdf_summarize_section") as (outcome, _count):
                    outcome["value"] = "error"
                return PdfSummarizeSectionToolResult(
                    success=False,
                    error="section_kind='layout_region' requires region_id",
                )
            section_ref = {"kind": "layout_region", "region_id": region_id}
        else:
            async with _instrument_pdf_tool("pdf_summarize_section") as (outcome, _count):
                outcome["value"] = "error"
            return PdfSummarizeSectionToolResult(
                success=False,
                error=f"Unknown section_kind {section_kind!r}. Use 'page', 'page_range', or 'layout_region'.",
            )

        async with _instrument_pdf_tool("pdf_summarize_section") as (outcome, count):
            try:
                workspace_id = _require_workspace_uuid(ctx)
            except _WorkspaceRequiredError as exc:
                outcome["value"] = "error"
                return PdfSummarizeSectionToolResult(success=False, error=str(exc))

            try:
                pdf_bytes = await _fetch_pdf_bytes_for_agent(ctx, pdf_id)
            except _PdfNotFoundError as exc:
                outcome["value"] = "error"
                return PdfSummarizeSectionToolResult(success=False, error=str(exc))

            try:
                result_raw, cache_hit = await service.summarize_section(
                    pdf_bytes=pdf_bytes,
                    pdf_id=pdf_id,
                    section_ref=section_ref,
                    workspace_id=workspace_id,
                )
            except Exception as exc:
                logger.warning("pdf_summarize_section_tool failed: %s", exc)
                outcome["value"] = "error"
                return PdfSummarizeSectionToolResult(success=False, error=str(exc))

            claims = [
                VlClaimSummary(
                    claim_text=c["claim_text"],
                    page=c["page"],
                    bbox=tuple(c["bbox"]),  # type: ignore[arg-type]
                    confidence=c["confidence"],
                )
                for c in (result_raw.get("claims") or [])
            ]
            count["count"] = len(claims)
            return PdfSummarizeSectionToolResult(
                success=True,
                summary_text=result_raw.get("summary_text"),
                claims=claims,
                cache_hit=cache_hit,
            )

    @agent.tool
    async def pdf_find_coordinates_tool(
        ctx: RunContext[AgentDeps],
        pdf_id: str,
        page: int | None = None,
        coord_kind: str | None = None,
    ) -> PdfFindCoordinatesToolResult:
        """Extract geographic coordinates from PDF text blocks using deterministic regex.

        This is the AUTHORITATIVE coordinate source for a PDF.  The VL model
        must NEVER be used to read coordinates — call this tool instead.

        Supported coordinate types (coord_kind filter):
          - 'utm': Zone/hemisphere/easting/northing (full and terse forms)
          - 'latlon_decimal': Decimal degrees with optional N/S/E/W suffix
          - 'latlon_dms': Degrees/minutes/seconds with hemisphere

        IMPORTANT: call pdf_extract_text first for the target pdf_id + page
        combination.  This tool reads from silver.pdf_text_blocks — if no
        text blocks are cached yet, it returns an empty coordinates list with
        a message explaining the prerequisite.

        Args:
            pdf_id: SHA-256 hex of the normalised PDF.
            page: 1-indexed page to scan, or None for all pages.
            coord_kind: Optional filter — return only 'utm', 'latlon_decimal',
                or 'latlon_dms' coordinates.  None returns all kinds.
        """
        service = ctx.deps.pdf_coordinates_service
        if service is None:
            async with _instrument_pdf_tool("pdf_find_coordinates") as (outcome, _count):
                outcome["value"] = "error"
            return PdfFindCoordinatesToolResult(
                success=False,
                error="pdf_coordinates_service not available on AgentDeps",
            )

        async with _instrument_pdf_tool("pdf_find_coordinates") as (outcome, count):
            try:
                workspace_id = _require_workspace_uuid(ctx)
            except _WorkspaceRequiredError as exc:
                outcome["value"] = "error"
                return PdfFindCoordinatesToolResult(success=False, error=str(exc))

            try:
                coords_raw, cache_hit = await service.find_coordinates(
                    pdf_id, workspace_id=workspace_id, page=page,
                )
            except Exception as exc:
                logger.warning("pdf_find_coordinates_tool failed: %s", exc)
                outcome["value"] = "error"
                return PdfFindCoordinatesToolResult(success=False, error=str(exc))

            from app.agent.pdf_tool_results import PdfCoordinateSummary  # noqa: PLC0415

            coords = [
                PdfCoordinateSummary(
                    coord_kind=c["coord_kind"],
                    raw_match=c["raw_match"],
                    latitude=c.get("latitude"),
                    longitude=c.get("longitude"),
                    utm_zone=c.get("utm_zone"),
                    utm_easting=c.get("utm_easting"),
                    utm_northing=c.get("utm_northing"),
                    datum=c.get("datum"),
                    page=c.get("page"),
                )
                for c in coords_raw
                if coord_kind is None or c.get("coord_kind") == coord_kind
            ]
            # result_count = coords after coord_kind filter.
            count["count"] = len(coords)
            return PdfFindCoordinatesToolResult(
                success=True,
                coordinates=coords,
                cache_hit=cache_hit,
            )

    return agent


async def run_agentic_escalation(
    query: str,
    deps: AgentDeps,
) -> list[tuple[str, Any]]:
    """Run the bounded Pydantic AI agent escalation and return tool_results.

    The agent's own text output is discarded — we only care about what
    tools it invoked and what they returned. The caller folds those into
    the normal response-assembly path.

    Empty return means either:
      - agent disabled by config
      - no anthropic_client available
      - agent ran but found nothing
      - agent errored (logged; failure is non-fatal)
    """
    if not getattr(settings, "AGENTIC_FULL_ESCALATION_ENABLED", False):
        return []

    try:
        agent = await _build_agent(deps)
    except Exception as exc:
        logger.warning(
            "agentic_escalation: agent construction failed (non-fatal): %s",
            exc.__class__.__name__,
        )
        return []

    if agent is None:
        logger.info(
            "agentic_escalation: no anthropic client available; skipping full agent"
        )
        return []

    # Bound the whole thing. Pydantic AI 1.x exposes an overall timeout via
    # `UsageLimits(request_limit=...)`; we additionally wrap in asyncio
    # timeout as a hard ceiling.
    from pydantic_ai.usage import UsageLimits  # noqa: PLC0415

    # P1 #11 — verify_numerical_claim shares the tool_calls budget with
    # retrieval. If we left max_tools at 3 the agent could blow the
    # whole budget on verifications and never make a discovery call.
    # Add headroom proportional to the number of retrieval tools (3) so
    # a paranoid model can verify each retrieval result without being
    # starved of budget for discovery.
    max_retrieval_tools = int(getattr(settings, "AGENTIC_MAX_TOOL_CALLS", 3))
    max_verify_tools = int(getattr(settings, "AGENTIC_MAX_VERIFY_CALLS", 3))
    total_tool_budget = max_retrieval_tools + max_verify_tools
    limits = UsageLimits(tool_calls_limit=total_tool_budget)

    try:
        async with asyncio.timeout(
            float(getattr(settings, "AGENTIC_TIMEOUT_S", 10.0))
        ):
            result = await agent.run(
                query,
                deps=deps,
                usage_limits=limits,
            )
    except TimeoutError:
        logger.warning(
            "agentic_escalation: timed out after %.1fs",
            float(getattr(settings, "AGENTIC_TIMEOUT_S", 10.0)),
        )
        return []
    except Exception as exc:
        logger.warning(
            "agentic_escalation: agent error (non-fatal): %s", exc.__class__.__name__
        )
        return []

    # Extract tool results from the agent's message history. Pydantic AI
    # records tool returns on `result.all_messages()` as ToolReturnPart
    # entries; we fish the same shape the deterministic dispatch emits
    # (list of (tool_name, result_object)) so downstream code doesn't care
    # which path produced it.
    tool_results: list[tuple[str, Any]] = []
    try:
        for msg in result.all_messages():
            parts = getattr(msg, "parts", None)
            if not parts:
                continue
            for part in parts:
                if hasattr(part, "tool_name") and hasattr(part, "content"):
                    tool_name = getattr(part, "tool_name", None)
                    content = getattr(part, "content", None)
                    if not tool_name or content is None:
                        continue
                    # Strip the `_tool` suffix our wrappers added so the
                    # result keys match the deterministic dispatch (e.g.
                    # 'search_documents' not 'search_documents_tool').
                    if tool_name.endswith("_tool"):
                        tool_name = tool_name[: -len("_tool")]
                    tool_results.append((tool_name, content))
    except Exception:
        logger.exception(
            "agentic_escalation: failed to extract tool results; returning empty"
        )
        return []

    logger.info(
        "agentic_escalation: completed with %d tool_result(s)", len(tool_results)
    )
    return tool_results
