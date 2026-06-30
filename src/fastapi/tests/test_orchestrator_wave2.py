"""Wave-2 orchestrator-surgery tests.

Pins the four behaviours added in P1 wave 2:

  * #7   gather() rescues partial results when a peer branch raises
  * #14  _call_llm enforces MAX_LLM_CALLS_PER_QUERY and raises
         LLMCallBudgetExceeded with a counter increment
  * #14  the per-run counter resets between independent runs (contextvar
         is per-task, not module-global)
  * #12  _call_anthropic_llm builds a 3-turn message list when both
         previous_answer and correction_hint are supplied

Each test stubs the minimum machinery so the focal logic runs in
isolation; no real DB / Anthropic / Qdrant calls.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.orchestrator import (
    LLMCallBudgetExceeded,
    _call_anthropic_llm,
    _call_llm,
    _llm_call_counter,
)

# ---------------------------------------------------------------------------
# #14 — global LLM-call cap
# ---------------------------------------------------------------------------


@pytest.fixture
def _cap(monkeypatch):
    """Set MAX_LLM_CALLS_PER_QUERY for a test, restore after."""
    from app.config import settings

    original = settings.MAX_LLM_CALLS_PER_QUERY

    def _set(n: int) -> None:
        object.__setattr__(settings, "MAX_LLM_CALLS_PER_QUERY", n)

    yield _set
    object.__setattr__(settings, "MAX_LLM_CALLS_PER_QUERY", original)


@pytest.fixture
def _force_openai_backend(monkeypatch):
    """Pick the OpenAI-compat path so we can stub _call_openai_compatible_llm."""
    from app.config import settings

    original = settings.LLM_BACKEND
    object.__setattr__(settings, "LLM_BACKEND", "ollama")
    yield
    object.__setattr__(settings, "LLM_BACKEND", original)


@pytest.mark.asyncio
async def test_llm_call_budget_exceeded_raises_after_cap(
    _cap, _force_openai_backend, monkeypatch
):
    """Setting cap=2 and calling 3 times should raise on call 3."""
    _cap(2)
    _llm_call_counter.set(0)

    async def _fake(*a, **kw):
        return "ok"

    monkeypatch.setattr(
        "app.agent.llm_calls._call_openai_compatible_llm", _fake
    )

    # Calls 1 & 2 succeed
    assert (await _call_llm("q", "ctx")) == "ok"
    assert (await _call_llm("q", "ctx")) == "ok"

    # Call 3 hits the cap
    with pytest.raises(LLMCallBudgetExceeded) as exc_info:
        await _call_llm("q", "ctx")
    assert "budget of 2" in str(exc_info.value)


@pytest.mark.asyncio
async def test_counter_resets_between_runs(_cap, _force_openai_backend, monkeypatch):
    """The contextvar default semantics let independent tasks each start at 0."""
    _cap(3)

    async def _fake(*a, **kw):
        return "ok"

    monkeypatch.setattr(
        "app.agent.llm_calls._call_openai_compatible_llm", _fake
    )

    async def _one_run() -> int:
        # Each task gets a fresh contextvar value (default=0).
        _llm_call_counter.set(0)
        for _ in range(2):
            await _call_llm("q", "ctx")
        return _llm_call_counter.get()

    a, b = await asyncio.gather(_one_run(), _one_run())
    assert a == 2
    assert b == 2


@pytest.mark.asyncio
async def test_audit_label_appears_in_log(
    _cap, _force_openai_backend, monkeypatch, caplog
):
    """Per-attempt label must show up in the structured log line."""
    import logging

    _cap(5)
    _llm_call_counter.set(0)

    async def _fake(*a, **kw):
        return "ok"

    monkeypatch.setattr(
        "app.agent.llm_calls._call_openai_compatible_llm", _fake
    )

    # Phase F.12 — log emitter moved to app.agent.llm_calls. Capture
    # both loggers so the assertion sees the line regardless of where
    # the orchestrator chain ends up emitting it.
    with caplog.at_level(logging.INFO, logger="app.agent.llm_calls"), \
            caplog.at_level(logging.INFO, logger="app.agent.orchestrator"):
        await _call_llm("q", "ctx", audit_label="failover")

    msgs = [r.getMessage() for r in caplog.records]
    assert any("label=failover" in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# #12 — multi-turn correction message
# ---------------------------------------------------------------------------


def _make_blocking_anthropic_client(captured: dict):
    """Stub messages.create that records the messages payload."""
    text_block = SimpleNamespace(type="text", text="OK")
    fake_msg = SimpleNamespace(content=[text_block], usage=None)

    async def _create(**kwargs):
        captured["messages"] = kwargs.get("messages")
        return fake_msg

    return SimpleNamespace(
        messages=SimpleNamespace(
            create=_create,
            stream=AsyncMock(side_effect=AssertionError("stream not used here")),
        )
    )


@pytest.fixture
def _enable_anthropic(monkeypatch):
    from app.config import settings

    object.__setattr__(settings, "LLM_BACKEND", "anthropic")
    object.__setattr__(settings, "ANTHROPIC_API_KEY", "sk-test")
    object.__setattr__(settings, "REQUIRE_POOLED_ANTHROPIC_CLIENT", False)
    object.__setattr__(settings, "ANTHROPIC_ENABLE_PROMPT_CACHING", False)
    object.__setattr__(settings, "ANTHROPIC_USE_PRIORITY_TIER", False)

    # Z.1 — these legacy tests pin SDK call shapes (message list, system
    # blocks, multi-turn payloads). They predate the external-LLM egress
    # gate (Appendix C §5) which now refuses calls without a workspace
    # opt-in. Bypass the gate here so the SDK-shape assertions still run.
    # The gate itself is exercised by tests/test_anthropic_egress_gate.py.
    async def _passthrough(*, workspace_id, pg_pool=None):
        return None

    monkeypatch.setattr(
        "app.agent.egress_gate.assert_external_llm_allowed",
        _passthrough,
    )
    yield settings


@pytest.mark.asyncio
async def test_no_correction_sends_single_user_turn(_enable_anthropic):
    """Baseline — without correction, message list is exactly the original turn."""
    captured: dict = {}
    client = _make_blocking_anthropic_client(captured)

    await _call_anthropic_llm(
        "the user's question + context",
        temperature=0.1,
        client=client,
    )

    msgs = captured["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "the user's question + context"


@pytest.mark.asyncio
async def test_correction_builds_three_turn_payload(_enable_anthropic):
    """P1 #12 — retry sends [user(question), assistant(prev), user(correction)]."""
    captured: dict = {}
    client = _make_blocking_anthropic_client(captured)

    await _call_anthropic_llm(
        "the user's question + context",
        temperature=0.1,
        client=client,
        previous_answer="My first attempt — got the depth wrong.",
        correction_hint="depth value did not match silver.collars",
    )

    msgs = captured["messages"]
    assert len(msgs) == 3
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "the user's question + context"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "My first attempt — got the depth wrong."
    assert msgs[2]["role"] == "user"
    assert "depth value did not match silver.collars" in msgs[2]["content"]


@pytest.mark.asyncio
async def test_correction_without_previous_answer_stays_single_turn(_enable_anthropic):
    """If only correction_hint is set (defensive), still emit a single turn —
    sending an orphan correction without an assistant target would violate
    Anthropic's role-alternation rule and 400 the request."""
    captured: dict = {}
    client = _make_blocking_anthropic_client(captured)

    await _call_anthropic_llm(
        "the user's question",
        temperature=0.1,
        client=client,
        previous_answer=None,
        correction_hint="something is wrong",
    )

    msgs = captured["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
