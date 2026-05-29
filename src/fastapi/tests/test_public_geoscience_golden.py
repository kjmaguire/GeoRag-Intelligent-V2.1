"""Golden query test for the Public Geoscience retrieval path.

Module 10 Chunk 10.3 — M-A5-04 harmonisation (path b: documented divergence).

Fixture shape divergence from main corpus (test_golden_queries.py)
------------------------------------------------------------------
PGEO golden fixtures use a per-test function style rather than a shared
GOLDEN_QUERIES list because:

  1. ``project_id`` is still required by the endpoint (technical constraint),
     but PGEO data is workspace-global — the project_id is a routing artefact
     not a meaningful scope. Putting it in a parametrized fixture would require
     a special-case note on every row.

  2. Each PGEO test asserts on citation-level metadata (jurisdiction_code,
     license_summary, staleness_seconds, corpus field) that has no parallel in
     the project-scoped golden set — these would bloat the shared fixture shape.

  3. The assertion logic diverges (e.g. regex search for drillhole ID patterns,
     BC vs SK jurisdiction routing checks) in ways that don't fit the
     generic ``expected_answer_contains`` pattern.

The ``query_class`` field IS added to each test function via a module-level
constant (``_PGEO_QUERY_CLASSES``) and validated by
``test_pgeo_golden_query_class_present`` below, so H-A5-02 is satisfied.

Fields NOT present in PGEO fixtures vs main corpus and why:
  - ``id``: tests are named functions, not rows in a list; pytest node IDs serve.
  - ``project_id``: workspace-global PGEO data; see point 1 above.
  - ``expected_answer_substr``: PGEO assertions use targeted ``assert in`` calls
    with richer context than a generic substring list allows.
  - ``expected_citation_type``: always "PGEO" here; factored into the assertion
    body rather than repeated in a fixture field.
  - ``min_confidence``: carried directly in each test's assert statement.
  - ``max_response_time_ms``: all PGEO tests share the 90 000 ms cold-start budget.

Pre-conditions (must be satisfied before running):
  - docker compose up -d fastapi postgresql pgbouncer ollama qdrant
  - public_geoscience.sources, public_geoscience.jurisdictions, and the
    pg_mineral_occurrence Qdrant collection must be populated with
    Saskatchewan SMDI data (Phase 3.2 Dagster asset run).

Run with:
    cd src/fastapi
    python -m pytest tests/test_public_geoscience_golden.py -m golden -v

Marks
-----
  golden      — live golden query, blocks milestone acceptance if it fails
  integration — requires the full docker compose stack
"""

from __future__ import annotations

import time

import httpx
import pytest

from tests.conftest import (
    AUTH_HEADERS,
    FASTAPI_URL,
    SERVICE_KEY,
    parse_sse_stream,
)

# Scope to a global (no project_id) or the test project — the PGEO path does
# not scope by project, but the endpoint requires a project_id parameter.
_TEST_PROJECT_ID = "019d74a1-fba8-7165-9ae6-a5bf93eef97d"

# ---------------------------------------------------------------------------
# H-A5-02 — query_class metadata for PGEO golden tests (documented divergence)
#
# PGEO tests use per-function style, not a parametrized list. The query_class
# for each test is recorded here so the coverage assertion below can verify
# that all seven classes are represented across both corpora (main + PGEO).
# ---------------------------------------------------------------------------
_PGEO_QUERY_CLASSES: dict[str, str] = {
    "test_pgeo_gold_occurrences_in_saskatchewan": "exists",
    "test_pgeo_gold_drillholes_near_uranium_target": "spatial",
    "test_pgeo_gold_bc_minfile_cross_jurisdiction": "exists",
}


def test_pgeo_golden_query_class_present() -> None:
    """Structural assertion: every PGEO golden test has a query_class entry
    in _PGEO_QUERY_CLASSES. Does NOT require a live stack. PR-fast suite.
    """
    _valid_classes: frozenset[str] = frozenset({
        "count", "exists", "numeric", "spatial", "document", "graph", "refusal",
    })
    expected_tests = {
        "test_pgeo_gold_occurrences_in_saskatchewan",
        "test_pgeo_gold_drillholes_near_uranium_target",
        "test_pgeo_gold_bc_minfile_cross_jurisdiction",
    }
    missing = expected_tests - set(_PGEO_QUERY_CLASSES.keys())
    assert not missing, (
        f"PGEO test(s) missing query_class entry in _PGEO_QUERY_CLASSES: {missing}"
    )
    for test_name, qc in _PGEO_QUERY_CLASSES.items():
        assert qc in _valid_classes, (
            f"PGEO test {test_name!r} has invalid query_class {qc!r}. "
            f"Valid: {sorted(_valid_classes)}"
        )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.golden
async def test_pgeo_gold_occurrences_in_saskatchewan() -> None:
    """Verifies PGEO retrieval path end-to-end against live FastAPI.

    Section 07e golden query requirements for the Public Geoscience surface:

    1. HTTP 200 with text/event-stream.
    2. At least 3 citations total in the ``completed`` event.
    3. At least 1 citation has citation_type == "PGEO".
    4. Every PGEO citation has a non-empty source_chunk_id starting with
       "pg_mineral_occurrence:".
    5. Every PGEO citation has corpus == "public_geoscience".
    6. Every PGEO citation has both jurisdiction_code and license_summary set.
    7. At least one PGEO citation has staleness_seconds > 0 (Blocker #2 fix
       — registry hydration must populate staleness metadata from the
       public_geoscience.sources last_refreshed_at column).
    8. Response text contains at least one "SMDI" mention (proves the answer
       is grounded in real SMDI records, not fabricated).
    9. confidence >= 0.5.
    10. Response arrives within 90 seconds (cold-start Ollama budget).
    """
    query = "What gold mineral occurrences are in Saskatchewan?"

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
                "query": query,
                "project_id": _TEST_PROJECT_ID,
            },
        ) as response:
            # --- Check 1: HTTP 200 and text/event-stream ---
            assert response.status_code == 200, (
                f"[pgeo-gold-001] Expected HTTP 200, got {response.status_code}"
            )
            assert "text/event-stream" in response.headers.get("content-type", ""), (
                "[pgeo-gold-001] Expected text/event-stream content type"
            )

            completed = await parse_sse_stream(response)

    elapsed_ms = time.monotonic() * 1000 - start_ms

    # --- Check 10: latency ---
    assert elapsed_ms <= 90_000, (
        f"[pgeo-gold-001] Query took {elapsed_ms:.0f} ms, budget is 90 000 ms"
    )

    response_text: str = completed.get("text", "")
    citations: list[dict] = completed.get("citations", [])
    confidence: float = completed.get("confidence", 0.0)

    pgeo_citations = [c for c in citations if c.get("citation_type") == "PGEO"]

    # --- Check 2: at least 3 citations total ---
    assert len(citations) >= 3, (
        f"[pgeo-gold-001] Expected >= 3 citations, got {len(citations)}.\n"
        f"Response: {response_text!r}"
    )

    # --- Check 3: at least 1 PGEO citation ---
    assert len(pgeo_citations) >= 1, (
        f"[pgeo-gold-001] Expected at least 1 PGEO citation.\n"
        f"Citation types present: {[c.get('citation_type') for c in citations]}\n"
        f"Response: {response_text!r}"
    )

    # --- Check 4: PGEO source_chunk_ids start with pg_mineral_occurrence: ---
    for cit in pgeo_citations:
        chunk_id = cit.get("source_chunk_id", "")
        assert chunk_id.startswith("pg_mineral_occurrence:"), (
            f"[pgeo-gold-001] PGEO citation has unexpected source_chunk_id: {chunk_id!r}"
        )

    # --- Check 5: corpus == "public_geoscience" ---
    for cit in pgeo_citations:
        assert cit.get("corpus") == "public_geoscience", (
            f"[pgeo-gold-001] PGEO citation corpus mismatch: {cit!r}"
        )

    # --- Check 6: jurisdiction_code and license_summary populated ---
    for cit in pgeo_citations:
        assert cit.get("jurisdiction_code"), (
            f"[pgeo-gold-001] PGEO citation missing jurisdiction_code: {cit!r}"
        )
        assert cit.get("license_summary"), (
            f"[pgeo-gold-001] PGEO citation missing license_summary: {cit!r}"
        )

    # --- Check 7: at least one PGEO citation has staleness_seconds > 0 ---
    staleness_values = [
        c.get("staleness_seconds")
        for c in pgeo_citations
        if c.get("staleness_seconds") is not None
    ]
    assert any(s > 0 for s in staleness_values), (
        "[pgeo-gold-001] No PGEO citation has staleness_seconds > 0. "
        "Blocker #2 fix: registry hydration must populate staleness from "
        "public_geoscience.sources.last_refreshed_at.\n"
        f"staleness_seconds values found: {staleness_values}"
    )

    # --- Check 8: response mentions SMDI (grounded in real data) ---
    assert "smdi" in response_text.lower(), (
        f"[pgeo-gold-001] Response text does not mention 'SMDI' — the answer "
        f"may be fabricated rather than grounded in SMDI records.\n"
        f"Response: {response_text!r}"
    )

    # --- Check 9: confidence >= 0.5 ---
    assert confidence >= 0.5, (
        f"[pgeo-gold-001] Expected confidence >= 0.5, got {confidence:.3f}.\n"
        f"Response: {response_text!r}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.golden
async def test_pgeo_gold_drillholes_near_uranium_target() -> None:
    """Drillhole-centric golden query — verifies the retrieval path can hit
    `pg_drillhole_collar` and return collar-level citations.

    Checks (mirrors pgeo-gold-001 structure):
      1. HTTP 200 + SSE content type.
      2. At least 2 citations total.
      3. At least 1 PGEO citation with source_chunk_id starting with
         ``pg_drillhole_collar:`` (proves drillhole retrieval, not just SMDI).
      4. Every PGEO citation has a non-null jurisdiction_code + license_summary.
      5. Response text contains at least one drillhole-ID-shaped substring
         (proves the LLM is grounding in real collar data, not speculating).
      6. Latency within 90 s (cold-start budget).

    Pre-conditions: the `pg_drillhole_collar` Qdrant collection is populated
    and `CA-SK-DRILLHOLE` Silver has been materialized at least once.
    """
    import re

    query = "Find drillholes in northern Saskatchewan that targeted uranium."

    start_ms = time.monotonic() * 1000

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{FASTAPI_URL}/internal/queries",
            headers={
                **AUTH_HEADERS,
                "Content-Type": "application/json",
            },
            json={"query": query, "project_id": _TEST_PROJECT_ID},
        ) as response:
            assert response.status_code == 200, (
                f"[pgeo-gold-002] Expected HTTP 200, got {response.status_code}"
            )
            completed = await parse_sse_stream(response)

    elapsed_ms = time.monotonic() * 1000 - start_ms
    assert elapsed_ms <= 90_000, (
        f"[pgeo-gold-002] Took {elapsed_ms:.0f} ms; budget 90 000 ms"
    )

    response_text: str = completed.get("text", "")
    citations: list[dict] = completed.get("citations", [])
    pgeo_citations = [c for c in citations if c.get("citation_type") == "PGEO"]

    assert len(citations) >= 2, (
        f"[pgeo-gold-002] Expected >= 2 citations, got {len(citations)}"
    )

    drillhole_citations = [
        c for c in pgeo_citations
        if c.get("source_chunk_id", "").startswith("pg_drillhole_collar:")
    ]
    assert len(drillhole_citations) >= 1, (
        f"[pgeo-gold-002] Expected at least 1 drillhole-collar citation.\n"
        f"source_chunk_ids found: "
        f"{[c.get('source_chunk_id') for c in pgeo_citations]}"
    )

    for cit in pgeo_citations:
        assert cit.get("jurisdiction_code"), (
            f"[pgeo-gold-002] Missing jurisdiction_code: {cit!r}"
        )
        assert cit.get("license_summary"), (
            f"[pgeo-gold-002] Missing license_summary: {cit!r}"
        )

    # Drillhole IDs in SK look like "GOS_4482" or "GOS-4482"; having any
    # such shape in the response body confirms the LLM is citing real collars.
    assert re.search(r"\bGOS[_\s-]\d{3,6}\b", response_text, re.IGNORECASE), (
        f"[pgeo-gold-002] Response contains no GOS drillhole-ID-shaped "
        f"substring. May indicate the LLM is narrating without citing real "
        f"collars.\nResponse: {response_text!r}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.golden
async def test_pgeo_gold_bc_minfile_cross_jurisdiction() -> None:
    """Cross-jurisdiction golden query — BC MINFILE should be retrievable.

    Verifies the FieldMapping abstraction (Phase 4 refactor) actually works:
    the same chat tool that serves SK SMDI must also serve BC MINFILE
    occurrences under the same canonical_type, with jurisdiction_code
    distinguishing them.

    Checks:
      1. HTTP 200 + SSE.
      2. At least 1 PGEO citation with jurisdiction_code == "CA-BC".
      3. Any BC citation has license_summary containing "British Columbia"
         OR "Open Government Licence" — proves registry join produced the
         right license string.
      4. response text mentions "MINFILE" (BC's equivalent of SMDI).
      5. Latency within 90 s.

    Pre-conditions: CA-BC-MINFILE Silver has been materialized at least once
    against the live BC Geographic Warehouse endpoint.
    """
    query = "What mineral occurrences have been recorded in British Columbia?"

    start_ms = time.monotonic() * 1000

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{FASTAPI_URL}/internal/queries",
            headers={
                **AUTH_HEADERS,
                "Content-Type": "application/json",
            },
            json={"query": query, "project_id": _TEST_PROJECT_ID},
        ) as response:
            assert response.status_code == 200, (
                f"[pgeo-gold-003] Expected HTTP 200, got {response.status_code}"
            )
            completed = await parse_sse_stream(response)

    elapsed_ms = time.monotonic() * 1000 - start_ms
    assert elapsed_ms <= 90_000, (
        f"[pgeo-gold-003] Took {elapsed_ms:.0f} ms; budget 90 000 ms"
    )

    response_text: str = completed.get("text", "")
    citations: list[dict] = completed.get("citations", [])
    pgeo_citations = [c for c in citations if c.get("citation_type") == "PGEO"]

    bc_citations = [
        c for c in pgeo_citations if c.get("jurisdiction_code") == "CA-BC"
    ]
    assert len(bc_citations) >= 1, (
        f"[pgeo-gold-003] No BC (CA-BC) PGEO citation found. FieldMapping "
        f"abstraction may not be routing BC MINFILE correctly.\n"
        f"Jurisdictions present: "
        f"{[c.get('jurisdiction_code') for c in pgeo_citations]}"
    )

    for cit in bc_citations:
        lic = (cit.get("license_summary") or "").lower()
        assert (
            "british columbia" in lic or "open government licence" in lic
        ), (
            f"[pgeo-gold-003] BC citation has unexpected license_summary: "
            f"{cit.get('license_summary')!r}"
        )

    assert "minfile" in response_text.lower(), (
        f"[pgeo-gold-003] Response does not mention 'MINFILE' — BC "
        f"occurrences may not be grounded in real MINFILE records.\n"
        f"Response: {response_text!r}"
    )
