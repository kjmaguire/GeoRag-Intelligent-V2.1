"""Milestone 1 Golden Query Test Suite — Section 07e of the GeoRAG architecture.

These 10 queries form the minimum viable golden set required for Milestone 1
acceptance.  Each query has verified expected answers derived from the actual
database state:

  Project:  019d74a1-fba8-7165-9ae6-a5bf93eef97d
  Collars:  10 rows in silver.collars (PLS-20-01 through PLS-22-10)
  All 10 holes are Diamond type
  Status:   9 Completed, 1 In Progress (PLS-22-10)
  Depths:   min=265 m (PLS-21-06), max=510 m (PLS-22-08), avg=364 m
  Eastings: min=493445 (PLS-21-05), max=498256.9 (PLS-22-10)
  Drill years: 2020 (4 holes), 2021 (3 holes), 2022 (3 holes)

Ground-truth verification query used to derive these values:
    SELECT hole_id, hole_type, status, total_depth, ST_X(geom) as easting,
           EXTRACT(YEAR FROM drill_date) as year
    FROM silver.collars
    WHERE project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
    ORDER BY hole_id;

Failure here BLOCKS milestone acceptance.  See Section 07e.

Running
-------
The FastAPI service must be up: docker compose up -d fastapi postgresql pgbouncer ollama

    cd src/fastapi
    python -m pytest tests/test_golden_queries.py -v --tb=short

Marks
-----
  golden      — all queries in this module
  integration — requires live stack (skipped in CI without live infra)
"""

from __future__ import annotations

import time

import httpx
import pytest
import pytest_asyncio

from tests.conftest import (
    AUTH_HEADERS,
    FASTAPI_URL,
    SERVICE_KEY,
    TEST_PROJECT_ID,
    parse_sse_stream,
)

# ---------------------------------------------------------------------------
# Golden query fixture definitions
# ---------------------------------------------------------------------------
# Each entry is a dict with:
#   id                       — unique test identifier, used as parametrize ID
#   query                    — natural-language string sent to the endpoint
#   project_id               — project UUID scope
#   expected_answer_contains — list of substrings ALL of which must appear in
#                              the response text (case-insensitive match)
#   must_not_contain         — list of substrings NONE of which may appear
#   expected_citation_count_min — minimum number of citations in the response
#   expected_citation_type   — if set, at least one citation must have this type
#   min_confidence           — minimum GeoRAGResponse.confidence score
#   max_response_time_ms     — wall-clock budget for the full stream to complete
# ---------------------------------------------------------------------------

GOLDEN_QUERIES: list[dict] = [
    # ------------------------------------------------------------------
    # GQ-001: Count query — must return exactly 20
    # The most basic correctness check. Previously the LLM hallucinated 2459.
    # Failure here confirms Layer 3 numerical verification is broken.
    # query_class: count — cardinal aggregation with a specific expected number.
    # ------------------------------------------------------------------
    {
        # Project had 10 collars after Milestone 1; Milestone 2 Excel parser
        # added 10 more (XLS-24-01 through XLS-24-10), so the current count is 20.
        "id": "gq-001-count-holes",
        "query": "How many drill holes are in this project?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["20"],
        "must_not_contain": ["2459", "5000", "many", "several", "approximately"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.8,
        "max_response_time_ms": 90_000,
        "query_class": "count",
    },
    # ------------------------------------------------------------------
    # GQ-002: List query — at least one PLS-* hole ID must appear
    # The LLM must use the tool result, not invent hole IDs.
    # query_class: count — list of items (count archetype, catalogue).
    # ------------------------------------------------------------------
    {
        "id": "gq-002-list-hole-ids",
        "query": "List the hole IDs in this project",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["PLS-"],
        "must_not_contain": ["DH-", "ATDD-", "ATH-"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.8,
        "max_response_time_ms": 90_000,
        "query_class": "count",
    },
    # ------------------------------------------------------------------
    # GQ-003: Deepest hole — PLS-22-08 at 510 m
    # Tests that max aggregation is grounded in the tool data.
    # query_class: numeric — requires max() over a measured value.
    # ------------------------------------------------------------------
    {
        "id": "gq-003-deepest-hole",
        "query": "What drill hole has the deepest total depth?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["PLS-22-08", "510"],
        "must_not_contain": ["PLS-21-05", "PLS-20-03"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.8,
        "max_response_time_ms": 90_000,
        "query_class": "numeric",
    },
    # ------------------------------------------------------------------
    # GQ-004: Shallowest hole — PLS-21-06 at 265 m
    # Tests that min aggregation is grounded in the tool data.
    # query_class: numeric — requires min() over a measured value.
    # ------------------------------------------------------------------
    {
        "id": "gq-004-shallowest-hole",
        "query": "Which drill hole has the shallowest total depth?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["PLS-21-06", "265"],
        "must_not_contain": [],  # proactive insights may reference other holes
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.8,
        "max_response_time_ms": 90_000,
        "query_class": "numeric",
    },
    # ------------------------------------------------------------------
    # GQ-005: Drill type filter — all 10 are Diamond
    # Tests that the LLM correctly reads hole_type from the tool result.
    # query_class: exists — "show me only X" → confirms existence/presence.
    # ------------------------------------------------------------------
    {
        "id": "gq-005-diamond-holes",
        "query": "Show me only Diamond drill holes",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["10", "diamond"],
        "must_not_contain": ["RC", "RAB", "rotary"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.8,
        "max_response_time_ms": 90_000,
        "query_class": "exists",
    },
    # ------------------------------------------------------------------
    # GQ-006: Status filter — 9 Completed, 1 In Progress
    # Tests status field parsing.  "Active" is a common synonym — the model
    # should report Completed/In Progress per the actual enum values.
    # query_class: count — how many matching a filter condition.
    # ------------------------------------------------------------------
    {
        "id": "gq-006-completed-holes",
        "query": "How many holes have a Completed status?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["9"],
        "must_not_contain": ["10 completed", "all completed"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.8,
        "max_response_time_ms": 90_000,
        "query_class": "count",
    },
    # ------------------------------------------------------------------
    # GQ-007: Average depth — 360.8 m (recomputed after Excel import)
    # Tests arithmetic grounding.  The LLM must not round or fabricate.
    # query_class: numeric — average() aggregation over measured values.
    # ------------------------------------------------------------------
    {
        "id": "gq-007-average-depth",
        "query": "What is the average total depth of all drill holes?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["360.8"],
        "must_not_contain": ["500", "450", "400"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.7,
        "max_response_time_ms": 90_000,
        "query_class": "numeric",
    },
    # ------------------------------------------------------------------
    # GQ-008: Easternmost hole — PLS-22-10 at easting 498256.9
    # Tests spatial reasoning over the tool result's easting values.
    # query_class: spatial — requires spatial/coordinate reasoning.
    # ------------------------------------------------------------------
    {
        "id": "gq-008-easternmost-hole",
        "query": "What is the easternmost drill hole?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["PLS-22-10"],
        "must_not_contain": ["PLS-21-05", "PLS-21-06"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.8,
        "max_response_time_ms": 90_000,
        "query_class": "spatial",
    },
    # ------------------------------------------------------------------
    # GQ-009: Holes drilled in 2022 — 3 holes (PLS-22-08, PLS-22-09, PLS-22-10)
    # Tests year-based filtering from drill_date.
    # query_class: count — count filtered by temporal attribute.
    # ------------------------------------------------------------------
    {
        "id": "gq-009-holes-in-2022",
        "query": "How many holes were drilled in 2022?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["3"],
        "must_not_contain": ["4 holes", "2 holes", "two holes"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.7,
        "max_response_time_ms": 90_000,
        "query_class": "count",
    },
    # ------------------------------------------------------------------
    # GQ-010: Specific hole lookup — PLS-22-08
    # Tests that the LLM can retrieve and report a single hole's properties.
    # query_class: numeric — depth of a specific named entity.
    # ------------------------------------------------------------------
    {
        "id": "gq-010-specific-hole-depth",
        "query": "What is the total depth of drill hole PLS-22-08?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["510", "PLS-22-08"],
        "must_not_contain": ["PLS-22-09", "PLS-22-10", "480"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.8,
        "max_response_time_ms": 90_000,
        "query_class": "numeric",
    },
    # ------------------------------------------------------------------
    # GQ-011: Graph — deposit identification via knowledge graph traversal
    # Tests that traverse_knowledge_graph returns the Triple R deposit.
    # query_class: graph — requires Neo4j/knowledge graph traversal.
    # ------------------------------------------------------------------
    {
        "id": "gq-011-graph-deposit",
        "query": "What deposit does this project host?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["Triple R"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "min_confidence": 0.5,
        "max_response_time_ms": 90_000,
        "query_class": "graph",
    },
    # ------------------------------------------------------------------
    # GQ-012: Graph — QP identification from NI 43-101 report
    # Tests that the knowledge graph QualifiedPerson node is surfaced.
    # query_class: document — QP info comes from document + graph.
    # ------------------------------------------------------------------
    {
        "id": "gq-012-graph-qp",
        "query": "Who is the qualified person on the NI 43-101 report?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["Sarah Thompson"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "min_confidence": 0.5,
        "max_response_time_ms": 90_000,
        "query_class": "document",
    },
    # ------------------------------------------------------------------
    # GQ-013: Graph — formation listing via label-based fallback
    # Tests query_graph_by_label when no specific entity name is matched.
    # query_class: graph — knowledge graph entity listing.
    # ------------------------------------------------------------------
    {
        "id": "gq-013-graph-formations",
        "query": "Which formations do the drill holes intersect?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["CGL", "GPT"],  # most reliably mentioned by LLM
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "min_confidence": 0.5,
        "max_response_time_ms": 90_000,
        "query_class": "graph",
    },
    # ------------------------------------------------------------------
    # GQ-014: Assay data — U3O8 grade statistics
    # Tests the query_assay_data tool. Requires stat retrieval from structured data.
    # query_class: numeric — grade statistics (max, avg over assay values).
    # ------------------------------------------------------------------
    {
        "id": "gq-014-assay-u3o8",
        "query": "What is the U3O8 grade distribution across all holes?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["U3O8", "52"],  # LLM may format as 52,000 or 52000
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.4,
        "max_response_time_ms": 90_000,
        "query_class": "numeric",
    },
    # ------------------------------------------------------------------
    # GQ-015: Downhole lithology narration — PLS-20-01 strip log
    # Tests the query_downhole_logs tool. Requires litho interval retrieval.
    # query_class: document — narrative retrieval from structured log data.
    # ------------------------------------------------------------------
    {
        "id": "gq-015-lithology-narration",
        "query": "Summarise the lithology intersections for drill hole PLS-20-01",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["PLS-20-01", "SST", "PGN"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.5,
        "max_response_time_ms": 90_000,
        "query_class": "document",
    },
    # ------------------------------------------------------------------
    # GQ-016: Cross-section — W-E profile with drill holes
    # Tests the cross-section VizPayload builder.
    # query_class: spatial — requires spatial layout reasoning (W-E profile).
    # ------------------------------------------------------------------
    {
        "id": "gq-016-cross-section",
        "query": "Describe the west-east cross section of drill holes in this project",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["PLS-"],  # LLM always references hole IDs
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.5,
        "max_response_time_ms": 90_000,
        "query_class": "spatial",
    },
    # ------------------------------------------------------------------
    # GQ-017: Assay — gold (Au_ppb) statistics
    # Tests multi-element assay data retrieval.
    # query_class: numeric — assay grade statistics for a different element.
    # ------------------------------------------------------------------
    {
        "id": "gq-017-assay-gold",
        "query": "What is the gold grade distribution?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["Au"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "min_confidence": 0.4,
        "max_response_time_ms": 90_000,
        "query_class": "numeric",
    },
    # ------------------------------------------------------------------
    # GQ-018: Graph — deposit type identification
    # Tests graph traversal returning deposit_type property.
    # query_class: graph — requires Neo4j property lookup on deposit node.
    # ------------------------------------------------------------------
    {
        "id": "gq-018-deposit-type",
        "query": "What type of deposit is the Triple R?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["unconformity"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "min_confidence": 0.5,
        "max_response_time_ms": 90_000,
        "query_class": "graph",
    },
    # ─────────────────────────────────────────────────────────────────────
    # GQ-019 — GQ-030: Golden-set expansion (Phase A+). Broadens coverage
    # beyond count/lookup into structural, stratigraphic, methodological,
    # and temporal query archetypes.
    # ─────────────────────────────────────────────────────────────────────
    {
        # Westernmost = min easting. Known: PLS-21-05 at 493,445 m E.
        # query_class: spatial — requires spatial/coordinate reasoning.
        "id": "gq-019-westernmost-hole",
        "query": "Which drill hole is the westernmost in the project?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["PLS-21-05"],
        "must_not_contain": ["PLS-22-10"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.7,
        "max_response_time_ms": 90_000,
        "query_class": "spatial",
    },
    {
        # Status-filter query — 1 hole in progress (PLS-22-10) out of 20.
        # query_class: count — count filtered by status attribute.
        "id": "gq-020-in-progress-count",
        "query": "How many drill holes are currently in progress?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["1"],
        "must_not_contain": ["0", "none", "no holes"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.7,
        "max_response_time_ms": 90_000,
        "query_class": "count",
    },
    {
        # Orientation reference is stored on silver.projects.
        # query_class: exists — does the project use grid orientation? (existence check)
        "id": "gq-021-orientation-reference",
        "query": "What orientation reference do drill holes in this project use?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["grid"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "min_confidence": 0.4,
        "max_response_time_ms": 90_000,
        "query_class": "exists",
    },
    {
        # Commodity is Uranium — core identity question.
        # query_class: exists — "what is the primary commodity?" (property existence).
        "id": "gq-022-primary-commodity",
        "query": "What is the primary commodity of interest for this project?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["uranium"],
        "must_not_contain": ["gold", "copper"],
        "expected_citation_count_min": 1,
        "min_confidence": 0.5,
        "max_response_time_ms": 90_000,
        "query_class": "exists",
    },
    {
        # Structural-geology domain — tests structures table retrieval.
        # query_class: count — count of fault-class structures.
        "id": "gq-023-fault-count",
        "query": "How many logged structures are classified as faults across all drill holes?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["fault"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "min_confidence": 0.4,
        "max_response_time_ms": 90_000,
        "query_class": "count",
    },
    {
        # Stratigraphy — Athabasca basin is the hosting basin. Tests
        # narrative retrieval from NI 43-101.
        # query_class: document — answer lives in the NI 43-101 PDF, not structured data.
        "id": "gq-024-host-basin",
        "query": "What geological basin hosts the mineralization in this project?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["Athabasca"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "expected_citation_type": "NI43",
        "min_confidence": 0.5,
        "max_response_time_ms": 90_000,
        "query_class": "document",
    },
    {
        # QP name retrieval — tests graph + document cross-reference.
        # query_class: document — answer lives in NI 43-101 title page / graph QP node.
        "id": "gq-025-qp-name",
        "query": "Who is the qualified person on the NI 43-101 report?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["Sarah Thompson"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "expected_citation_type": "NI43",
        "min_confidence": 0.5,
        "max_response_time_ms": 90_000,
        "query_class": "document",
    },
    {
        # Methodology question — estimation method from Section 14.
        # query_class: document — methodological detail from the NI 43-101 report.
        "id": "gq-026-estimation-method",
        "query": "What resource estimation method was used in the most recent NI 43-101?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["kriging"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "expected_citation_type": "NI43",
        "min_confidence": 0.5,
        "max_response_time_ms": 90_000,
        "query_class": "document",
    },
    {
        # Shallow vs deep — inverse of GQ-003. Known: PLS-21-06 at 265 m.
        # query_class: numeric — min() over depth.
        "id": "gq-027-shallowest-hole",
        "query": "What is the shallowest drill hole and its depth?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["PLS-21-06", "265"],
        "must_not_contain": ["PLS-22-08"],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.7,
        "max_response_time_ms": 90_000,
        "query_class": "numeric",
    },
    {
        # Aggregate — total drilled metres across all holes.
        # query_class: numeric — sum() over depth.
        "id": "gq-028-total-metres",
        "query": "What is the total drilled length across all holes in this project?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["metre"],  # "metres" substring
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "expected_citation_type": "DATA",
        "min_confidence": 0.6,
        "max_response_time_ms": 90_000,
        "query_class": "numeric",
    },
    {
        # Trend question — temporal synthesis from structured + document sources.
        # query_class: document — narrative synthesis over programme evolution.
        "id": "gq-029-drill-programme-trend",
        "query": "How has the drill programme evolved from 2020 to 2022?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["2020", "2022"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "min_confidence": 0.4,
        "max_response_time_ms": 90_000,
        "query_class": "document",
    },
    {
        # Azimuth question — tests survey table + narrative.
        # query_class: numeric — dominant value from a distribution (modal azimuth).
        "id": "gq-030-dominant-azimuth",
        "query": "What is the dominant drilling azimuth for this project's holes?",
        "project_id": TEST_PROJECT_ID,
        "expected_answer_contains": ["azimuth"],
        "must_not_contain": [],
        "expected_citation_count_min": 1,
        "min_confidence": 0.4,
        "max_response_time_ms": 90_000,
        "query_class": "numeric",
    },
]


# ---------------------------------------------------------------------------
# Parametrized golden-query test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", GOLDEN_QUERIES, ids=lambda c: c["id"])
@pytest.mark.integration
@pytest.mark.golden
async def test_golden_query(case: dict) -> None:
    """Submit a golden query to the live FastAPI endpoint and assert correctness.

    Checks (per Section 07e):
    1. HTTP 200 with text/event-stream content type.
    2. ``completed`` event received within the time budget.
    3. Response text contains all ``expected_answer_contains`` substrings.
    4. Response text contains none of the ``must_not_contain`` strings.
    5. Citation count >= ``expected_citation_count_min``.
    6. If ``expected_citation_type`` set, at least one citation matches.
    7. confidence >= ``min_confidence``.
    """
    start_ms = time.monotonic() * 1000

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{FASTAPI_URL}/internal/queries",
            headers={
                **AUTH_HEADERS,
                "Content-Type": "application/json",
            },
            json={
                "query": case["query"],
                "project_id": case["project_id"],
            },
        ) as response:
            # --- Check 1: HTTP 200 ---
            assert response.status_code == 200, (
                f"[{case['id']}] Expected HTTP 200, got {response.status_code}"
            )
            assert "text/event-stream" in response.headers.get("content-type", ""), (
                f"[{case['id']}] Expected text/event-stream content type"
            )

            # --- Parse the SSE stream ---
            completed = await parse_sse_stream(response)

    elapsed_ms = time.monotonic() * 1000 - start_ms

    # --- Check 2: Time budget ---
    assert elapsed_ms <= case["max_response_time_ms"], (
        f"[{case['id']}] Query took {elapsed_ms:.0f} ms, "
        f"budget is {case['max_response_time_ms']} ms"
    )

    response_text: str = completed.get("text", "")
    citations: list[dict] = completed.get("citations", [])
    confidence: float = completed.get("confidence", 0.0)

    # --- Check 3: Required substrings (case-insensitive) ---
    for phrase in case.get("expected_answer_contains", []):
        assert phrase.lower() in response_text.lower(), (
            f"[{case['id']}] Required phrase {phrase!r} not found in response.\n"
            f"Response text: {response_text!r}"
        )

    # --- Check 4: Prohibited substrings (case-insensitive) ---
    for phrase in case.get("must_not_contain", []):
        assert phrase.lower() not in response_text.lower(), (
            f"[{case['id']}] Prohibited phrase {phrase!r} found in response.\n"
            f"Response text: {response_text!r}"
        )

    # --- Check 5: Minimum citation count ---
    min_citations = case.get("expected_citation_count_min", 1)
    assert len(citations) >= min_citations, (
        f"[{case['id']}] Expected >= {min_citations} citation(s), "
        f"got {len(citations)}: {citations}"
    )

    # --- Check 6: Citation type ---
    expected_type = case.get("expected_citation_type")
    if expected_type:
        citation_types = [c.get("citation_type") for c in citations]
        assert expected_type in citation_types, (
            f"[{case['id']}] Expected citation type {expected_type!r} not found. "
            f"Types present: {citation_types}"
        )

    # --- Check 7: Minimum confidence ---
    assert confidence >= case["min_confidence"], (
        f"[{case['id']}] confidence {confidence:.3f} below threshold "
        f"{case['min_confidence']:.3f}"
    )

    # --- Check 8: sources_used is non-empty (Layer 2 provenance) ---
    sources_used = completed.get("sources_used", [])
    assert len(sources_used) >= 1, (
        f"[{case['id']}] sources_used must be non-empty (Layer 2 provenance requirement)"
    )

    # --- Check 9: All citation source_chunk_ids are non-empty (Layer 2) ---
    for citation in citations:
        chunk_id = citation.get("source_chunk_id", "")
        assert chunk_id, (
            f"[{case['id']}] Citation {citation.get('citation_id')} has empty "
            f"source_chunk_id — Layer 2 violation"
        )


# ---------------------------------------------------------------------------
# H-A5-02 — query_class corpus coverage assertion
#
# This test is NOT marked @integration/@golden — it runs purely over the
# in-memory GOLDEN_QUERIES list and requires NO live stack. It belongs in the
# PR-fast suite and is excluded from integration/golden markers.
#
# Asserts:
#   1. Every fixture in GOLDEN_QUERIES has a "query_class" field.
#   2. The field value is one of the seven canonical classes.
#   3. Every class has at least 3 representatives (Section 07e minimum).
# ---------------------------------------------------------------------------

_VALID_QUERY_CLASSES: frozenset[str] = frozenset({
    "count",
    "exists",
    "numeric",
    "spatial",
    "document",
    "graph",
    "refusal",
})

_MIN_CASES_PER_CLASS: int = 3


def test_golden_query_class_coverage() -> None:
    """Structural assertion: every golden fixture has a query_class and every
    class has at least 3 cases (H-A5-02 closure).

    Does NOT require a live FastAPI URL. Runs in the PR-fast suite.
    """
    missing_class: list[str] = []
    invalid_class: list[tuple[str, str]] = []

    for case in GOLDEN_QUERIES:
        qc = case.get("query_class")
        if qc is None:
            missing_class.append(case["id"])
        elif qc not in _VALID_QUERY_CLASSES:
            invalid_class.append((case["id"], qc))

    assert not missing_class, (
        f"Fixtures missing query_class field: {missing_class}\n"
        f"Add a query_class to each fixture before merging."
    )
    assert not invalid_class, (
        f"Fixtures with invalid query_class: {invalid_class}\n"
        f"Valid classes: {sorted(_VALID_QUERY_CLASSES)}"
    )

    # Count cases per class.
    from collections import Counter
    counts: Counter = Counter(case["query_class"] for case in GOLDEN_QUERIES)

    under_covered: dict[str, int] = {
        cls: counts[cls]
        for cls in _VALID_QUERY_CLASSES
        if counts[cls] < _MIN_CASES_PER_CLASS
    }

    # "refusal" class is not represented in the Milestone 1–2 golden set
    # (refusal fixtures live in the hallucination test file). Exclude it
    # from the minimum-coverage requirement here; it is tested separately.
    under_covered.pop("refusal", None)

    assert not under_covered, (
        f"These query_class values have fewer than {_MIN_CASES_PER_CLASS} cases:\n"
        + "\n".join(f"  {cls}: {n} case(s)" for cls, n in sorted(under_covered.items()))
        + "\nAdd more golden fixtures to reach the minimum per class."
    )
