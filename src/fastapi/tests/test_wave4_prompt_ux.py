"""Wave-4 prompt + UX tests.

Pins four behaviours added in P1 wave 4:

  * #18 GRAPH variant — _select_system_prompt routes graph-only queries
        to the new GRAPH prompt; mixed queries fall back to DEFAULT.
  * #19 Refusal example baked into each variant — verified by string
        presence in the constants (cheap canary).
  * #20 Third cache block — _build_project_facts assembles a non-empty
        summary block from the materialized view, and
        _call_anthropic_llm wires it into system_blocks with
        cache_control on every block when caching is enabled.
  * #15 LLM-classifier all-False short-circuits to a refusal GeoRAGResponse
        without touching tools, the LLM, or the Redis cache.

Drift fix (10.1, 2026-04-26): Module 6 Chunk 3.6 introduced a colon-form
citation variant (CITATION_SPAN_RESOLVER_ENABLED=True). The orchestrator now
ships both dash-form (_SYSTEM_PROMPT_GRAPH etc.) and colon-form
(_SYSTEM_PROMPT_GRAPH_COLON etc.) constants and _select_system_prompt returns
the colon form when CITATION_SPAN_RESOLVER_ENABLED=True. The production
FastAPI container has this flag enabled.

The routing tests previously asserted `_select_system_prompt(cats) == _SYSTEM_PROMPT_GRAPH`
which fails when colon mode is active because a different object is returned
(same task profile, different citation syntax). Fixed: routing tests now assert
the correct TASK PROFILE: substring is present, not the exact object identity.
The refusal canary tests now parametrize over both dash and colon variants.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.orchestrator import (
    _SYSTEM_PROMPT_DEFAULT,
    _SYSTEM_PROMPT_DEFAULT_COLON,
    _SYSTEM_PROMPT_GRAPH,
    _SYSTEM_PROMPT_GRAPH_COLON,
    _SYSTEM_PROMPT_NARRATIVE,
    _SYSTEM_PROMPT_NARRATIVE_COLON,
    _SYSTEM_PROMPT_NUMERIC,
    _SYSTEM_PROMPT_NUMERIC_COLON,
    _build_project_facts,
    _call_anthropic_llm,
    _select_system_prompt,
)

# Task profile substrings unique to each prompt variant (present in both
# dash and colon forms — these lines are identical in both variants).
_TASK_PROFILE_GRAPH = "TASK PROFILE: knowledge-graph traversal."
_TASK_PROFILE_NUMERIC = "TASK PROFILE: numerical / factoid."
_TASK_PROFILE_NARRATIVE = "TASK PROFILE: document-anchored narrative."

# DEFAULT prompt has no TASK PROFILE line (it's the generic fallback).
# We identify it by absence of any named task profile.
_NAMED_TASK_PROFILES = (
    _TASK_PROFILE_GRAPH,
    _TASK_PROFILE_NUMERIC,
    _TASK_PROFILE_NARRATIVE,
)


# ---------------------------------------------------------------------------
# #18 — GRAPH variant routing
# Drift fix (10.1): assert TASK PROFILE substring, not prompt object identity,
# because _select_system_prompt returns the colon form when
# CITATION_SPAN_RESOLVER_ENABLED=True.
# ---------------------------------------------------------------------------


def test_graph_only_query_picks_graph_variant():
    """Pure graph-traversal query — no docs, no spatial — should go to GRAPH."""
    cats = {"graph": True, "documents": False, "spatial": False, "assay": False, "downhole": False, "public_geoscience": False}
    result = _select_system_prompt(cats)
    assert _TASK_PROFILE_GRAPH in result, (
        f"Expected GRAPH task profile in selected prompt, got a prompt without it. "
        f"Prompt starts with: {result[:120]!r}"
    )


def test_graph_plus_documents_routes_to_narrative():
    """Graph + docs → NARRATIVE: citation discipline of NARRATIVE wins when
    document chunks corroborate graph entities. GRAPH fires only for pure
    graph-traversal questions."""
    cats = {"graph": True, "documents": True, "spatial": False, "assay": False, "downhole": False, "public_geoscience": False}
    result = _select_system_prompt(cats)
    assert _TASK_PROFILE_NARRATIVE in result, (
        f"Expected NARRATIVE task profile for graph+docs query, prompt starts with: {result[:120]!r}"
    )


def test_graph_plus_spatial_falls_back_to_default():
    """Graph + structured numeric — DEFAULT (no named task profile)."""
    cats = {"graph": True, "spatial": True, "documents": False, "assay": False, "downhole": False, "public_geoscience": False}
    result = _select_system_prompt(cats)
    for named in _NAMED_TASK_PROFILES:
        assert named not in result, (
            f"graph+spatial should fall through to DEFAULT (no named task profile). "
            f"Found {named!r} in selected prompt."
        )


def test_pure_numeric_still_routes_to_numeric():
    """Regression — adding GRAPH must not steal NUMERIC routing."""
    cats = {"spatial": True, "graph": False, "documents": False, "assay": False, "downhole": False, "public_geoscience": False}
    result = _select_system_prompt(cats)
    assert _TASK_PROFILE_NUMERIC in result, (
        f"Expected NUMERIC task profile for spatial-only query, prompt starts with: {result[:120]!r}"
    )


def test_pure_documents_still_routes_to_narrative():
    """Regression — adding GRAPH must not steal NARRATIVE routing."""
    cats = {"documents": True, "graph": False, "spatial": False, "assay": False, "downhole": False, "public_geoscience": False}
    result = _select_system_prompt(cats)
    assert _TASK_PROFILE_NARRATIVE in result, (
        f"Expected NARRATIVE task profile for document-only query, prompt starts with: {result[:120]!r}"
    )


# ---------------------------------------------------------------------------
# #19 — refusal example present in every variant (both dash + colon forms)
# Drift fix (10.1): parametrize over all 8 constants (4 dash + 4 colon).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        _SYSTEM_PROMPT_DEFAULT,
        _SYSTEM_PROMPT_NUMERIC,
        _SYSTEM_PROMPT_NARRATIVE,
        _SYSTEM_PROMPT_GRAPH,
        _SYSTEM_PROMPT_DEFAULT_COLON,
        _SYSTEM_PROMPT_NUMERIC_COLON,
        _SYSTEM_PROMPT_NARRATIVE_COLON,
        _SYSTEM_PROMPT_GRAPH_COLON,
    ],
    ids=[
        "default-dash", "numeric-dash", "narrative-dash", "graph-dash",
        "default-colon", "numeric-colon", "narrative-colon", "graph-colon",
    ],
)
def test_every_variant_has_refusal_example(variant: str):
    """Each variant must include a refusal-style few-shot answer so the
    model has an anchor for out-of-scope queries (P1 #19)."""
    assert "I can only answer geological questions" in variant


# ---------------------------------------------------------------------------
# #20 — _build_project_facts + third cache block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_project_facts_returns_block_when_mv_has_row():
    """Materialised view returns counts → builder emits a HIGH-CONFIDENCE
    SUMMARIES block with quotable values."""
    fetched_row = {
        "total_collars": 20,
        "avg_depth": 215.4,
        "min_depth": 50.0,
        "max_depth": 510.5,
        "hole_type_count": 3,
        "earliest_drill": "2018-06-01",
        "latest_drill": "2024-09-15",
        "total_samples": 1248,
        "total_litho_intervals": 4612,
    }

    class _Conn:
        async def fetchrow(self, *_a):
            return fetched_row

    class _AcquireCM:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_a):
            return False

    pool = SimpleNamespace(acquire=lambda: _AcquireCM())
    block = await _build_project_facts("proj-uuid", pool)

    assert block is not None
    assert "HIGH-CONFIDENCE SUMMARIES" in block
    assert "20" in block
    assert "215.1" in block or "215.4" in block  # mean depth
    assert "1248" in block  # samples
    assert "2018-06-01" in block


@pytest.mark.asyncio
async def test_build_project_facts_returns_none_when_mv_has_no_row():
    """Fresh project / no ingestion yet → no block emitted."""
    class _Conn:
        async def fetchrow(self, *_a):
            return None

    class _AcquireCM:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_a):
            return False

    pool = SimpleNamespace(acquire=lambda: _AcquireCM())
    block = await _build_project_facts("proj-uuid", pool)
    assert block is None


@pytest.mark.asyncio
async def test_anthropic_call_includes_third_cache_block(monkeypatch):
    """When project_facts is supplied, system_blocks contains 3 entries
    each with a cache_control ephemeral marker."""
    from app.config import settings

    object.__setattr__(settings, "LLM_BACKEND", "anthropic")
    object.__setattr__(settings, "ANTHROPIC_API_KEY", "sk-test")
    object.__setattr__(settings, "REQUIRE_POOLED_ANTHROPIC_CLIENT", False)
    object.__setattr__(settings, "ANTHROPIC_ENABLE_PROMPT_CACHING", True)
    object.__setattr__(settings, "ANTHROPIC_USE_PRIORITY_TIER", False)

    captured: dict = {}

    async def _create(**kwargs):
        captured["system"] = kwargs.get("system")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=None,
        )

    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=_create,
            stream=AsyncMock(side_effect=AssertionError("not used")),
        )
    )

    await _call_anthropic_llm(
        "user msg",
        temperature=0.1,
        client=client,
        project_preamble="=== PROJECT CONTEXT ===\nProject: TEST\n=== END ===",
        project_facts="=== HIGH-CONFIDENCE SUMMARIES ===\nTotal: 20\n=== END ===",
    )

    blocks = captured["system"]
    assert len(blocks) == 3, f"expected 3 cached blocks, got {len(blocks)}: {blocks}"
    # Every block should have its own cache_control marker so a change
    # to project_facts only invalidates THAT block, not the preamble.
    assert all(
        b.get("cache_control") == {"type": "ephemeral"} for b in blocks
    ), blocks


@pytest.mark.asyncio
async def test_anthropic_call_omits_third_block_when_facts_none(monkeypatch):
    """When project_facts is None, the call still works — only 1-2 blocks."""
    from app.config import settings

    object.__setattr__(settings, "LLM_BACKEND", "anthropic")
    object.__setattr__(settings, "REQUIRE_POOLED_ANTHROPIC_CLIENT", False)
    object.__setattr__(settings, "ANTHROPIC_ENABLE_PROMPT_CACHING", True)

    captured: dict = {}

    async def _create(**kwargs):
        captured["system"] = kwargs.get("system")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=None,
        )

    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=_create,
            stream=AsyncMock(side_effect=AssertionError("not used")),
        )
    )

    await _call_anthropic_llm(
        "user msg",
        temperature=0.1,
        client=client,
        project_preamble="=== PROJECT CONTEXT ===\nProject: TEST\n=== END ===",
        project_facts=None,
    )

    blocks = captured["system"]
    assert len(blocks) == 2  # static prompt + preamble; no facts


# ---------------------------------------------------------------------------
# #15 — All-False refusal short-circuits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_false_classifier_returns_refusal_response(monkeypatch):
    """When LLM classifier returns all-False, the run short-circuits to a
    polite refusal GeoRAGResponse without touching tools or the LLM
    synthesis path."""
    # Avoid real DB / Redis / tool wiring — we patch right at classify_via_llm
    # so the function returns all-False, and we patch the LLM pre-check + the
    # cache lookup so the function reaches the refusal branch.
    from app.agent import orchestrator
    from app.agent.deps import AgentDeps

    # Monkey-patch the LLM classifier to return all-False.
    async def _all_false(*_a, **_kw):
        return {
            "spatial": False, "documents": False, "graph": False,
            "assay": False, "downhole": False, "targeting": False,
            "public_geoscience": False,
        }

    monkeypatch.setattr(
        "app.agent.llm_classifier.classify_via_llm", _all_false
    )

    # Force the keyword classifier to flag classifier_fallback so the LLM
    # classifier branch fires.
    def _force_fallback(_q):
        return {
            "spatial": False, "documents": False, "graph": False,
            "assay": False, "downhole": False, "targeting": False,
            "public_geoscience": False, "classifier_fallback": True,
        }

    monkeypatch.setattr(orchestrator, "_classify_query", _force_fallback)

    # Bypass the LLM pre-check (only relevant when LLM_BACKEND != anthropic).
    from app.config import settings
    object.__setattr__(settings, "LLM_BACKEND", "anthropic")

    # Sentinels: if these are called we've failed the short-circuit.
    async def _sentinel_call_llm(*_a, **_kw):
        raise AssertionError("_call_llm must NOT be called on the refusal path")

    monkeypatch.setattr(orchestrator, "_call_llm", _sentinel_call_llm)

    # Build deps with an anthropic_client stub (the classifier reads it
    # from deps) and minimal others.
    deps = AgentDeps(
        pg_pool=None,
        qdrant_client=None,
        neo4j_driver=None,
        project_id="3a2c6f5e-9d11-4f8a-9b3e-1c2d4e5f6a7b",
        anthropic_client=SimpleNamespace(),  # truthy stub — not actually used
        redis_client=None,
    )

    result = await orchestrator.run_deterministic_rag(query="tell me a joke", deps=deps)

    assert "I can only answer geological questions" in result.text
    assert result.confidence == 0.0
    assert result.citations[0].source_chunk_id == "out-of-scope-refusal"
