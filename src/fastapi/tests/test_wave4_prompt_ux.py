"""Wave-4 prompt + UX tests.

Pins the behaviours added in P1 wave 4 (the #18 GRAPH variant was removed
with the knowledge graph):

  * #19 Refusal example baked into each variant — verified by string
        presence in the constants (cheap canary).
  * #20 Third cache block — _call_anthropic_llm wires supplied project
        facts into system_blocks with cache_control on every block.

Drift fix (10.1, 2026-04-26): Module 6 Chunk 3.6 introduced a colon-form
citation variant (CITATION_SPAN_RESOLVER_ENABLED=True). The orchestrator now
ships both dash-form (_SYSTEM_PROMPT_NUMERIC etc.) and colon-form
(_SYSTEM_PROMPT_NUMERIC_COLON etc.) constants and _select_system_prompt returns
the colon form when CITATION_SPAN_RESOLVER_ENABLED=True. The production
FastAPI container has this flag enabled.

The routing tests previously asserted `_select_system_prompt(cats) == _SYSTEM_PROMPT_NUMERIC`
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
    _SYSTEM_PROMPT_NARRATIVE,
    _SYSTEM_PROMPT_NARRATIVE_COLON,
    _SYSTEM_PROMPT_NUMERIC,
    _SYSTEM_PROMPT_NUMERIC_COLON,
    _call_anthropic_llm,
    _select_system_prompt,
)

# Task profile substrings unique to each prompt variant (present in both
# dash and colon forms — these lines are identical in both variants).
_TASK_PROFILE_NUMERIC = "TASK PROFILE: numerical / factoid."
_TASK_PROFILE_NARRATIVE = "TASK PROFILE: document-anchored narrative."

# DEFAULT prompt has no TASK PROFILE line (it's the generic fallback).
# We identify it by absence of any named task profile.
_NAMED_TASK_PROFILES = (
    _TASK_PROFILE_NUMERIC,
    _TASK_PROFILE_NARRATIVE,
)


# ---------------------------------------------------------------------------
# Variant routing
# Drift fix (10.1): assert TASK PROFILE substring, not prompt object identity,
# because _select_system_prompt returns the colon form when
# CITATION_SPAN_RESOLVER_ENABLED=True.
# ---------------------------------------------------------------------------


def test_structured_plus_documents_falls_back_to_default():
    """Structured numeric + documents — DEFAULT (no named task profile)."""
    cats = {"spatial": True, "documents": True, "assay": False, "downhole": False, "public_geoscience": False}
    result = _select_system_prompt(cats)
    for named in _NAMED_TASK_PROFILES:
        assert named not in result, (
            f"structured+documents should fall through to DEFAULT (no named task profile). "
            f"Found {named!r} in selected prompt."
        )


def test_pure_numeric_still_routes_to_numeric():
    """Spatial-only evidence routes to NUMERIC."""
    cats = {"spatial": True, "documents": False, "assay": False, "downhole": False, "public_geoscience": False}
    result = _select_system_prompt(cats)
    assert _TASK_PROFILE_NUMERIC in result, (
        f"Expected NUMERIC task profile for spatial-only query, prompt starts with: {result[:120]!r}"
    )


def test_pure_documents_still_routes_to_narrative():
    """Document-only evidence routes to NARRATIVE."""
    cats = {"documents": True, "spatial": False, "assay": False, "downhole": False, "public_geoscience": False}
    result = _select_system_prompt(cats)
    assert _TASK_PROFILE_NARRATIVE in result, (
        f"Expected NARRATIVE task profile for document-only query, prompt starts with: {result[:120]!r}"
    )


# ---------------------------------------------------------------------------
# #19 — refusal example present in every variant (both dash + colon forms)
# Drift fix (10.1): parametrize over all 6 constants (3 dash + 3 colon).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        _SYSTEM_PROMPT_DEFAULT,
        _SYSTEM_PROMPT_NUMERIC,
        _SYSTEM_PROMPT_NARRATIVE,
        _SYSTEM_PROMPT_DEFAULT_COLON,
        _SYSTEM_PROMPT_NUMERIC_COLON,
        _SYSTEM_PROMPT_NARRATIVE_COLON,
    ],
    ids=[
        "default-dash", "numeric-dash", "narrative-dash",
        "default-colon", "numeric-colon", "narrative-colon",
    ],
)
def test_every_variant_has_refusal_example(variant: str):
    """Each variant must include a refusal-style few-shot answer so the
    model has an anchor for out-of-scope queries (P1 #19)."""
    assert "I can only answer geological questions" in variant


# ---------------------------------------------------------------------------
# #20 — third cache block
# ---------------------------------------------------------------------------


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

    # Z.1 — bypass the external-LLM egress gate; this test pins SDK-side
    # cache_control wiring, not workspace policy (covered by
    # tests/test_anthropic_egress_gate.py).
    async def _passthrough(*, workspace_id, pg_pool=None):
        return None
    monkeypatch.setattr(
        "app.agent.egress_gate.assert_external_llm_allowed",
        _passthrough,
    )

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

    # Z.1 — bypass the external-LLM egress gate (covered separately).
    async def _passthrough(*, workspace_id, pg_pool=None):
        return None
    monkeypatch.setattr(
        "app.agent.egress_gate.assert_external_llm_allowed",
        _passthrough,
    )

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
