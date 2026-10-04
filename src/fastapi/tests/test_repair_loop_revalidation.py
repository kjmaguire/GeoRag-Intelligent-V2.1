"""Audit AGT-3 / RAG-19 (2026-09-29): a repair-loop re-issue must pass
validate -> demote before it can replace the answer, and is never streamed.

Guard classification: this STRENGTHENS Layers 2/3/4/5/6 on the (default-off)
LOWCOST / FULL repair paths, which previously bypassed all of them.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.agentic_retrieval import nodes as _nodes_mod
from app.agent.agentic_retrieval.nodes import repair_shadow_node
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.config import settings as _settings


class _FakeDeps:
    project_id = "p"
    workspace_id = "ws-1"
    pg_pool = None
    openai_http_client = None
    anthropic_client = None


def _response(text: str):
    from app.models.rag import Citation, GeoRAGResponse

    return GeoRAGResponse(
        text=text,
        citations=[Citation(
            citation_id="[DATA-1]",
            source_chunk_id="00000000-0000-0000-0000-000000000001",
            document_title="T", relevance_score=0.9, citation_type="DATA",
        )],
        confidence=0.7,
        sources_used=["00000000-0000-0000-0000-000000000001"],
    )


async def _token_cb(_chunk: str) -> None:  # pragma: no cover — must not be called
    raise AssertionError("a repair re-issue streamed a token")


def _state(**overrides: Any) -> AgenticRetrievalState:
    base = AgenticRetrievalState(
        query="q", deps=_FakeDeps(), intent="synthesis", effective_intent="synthesis",
        tool_results=[("query_project_overview", {"count": 1})],
        response=_response("original validated answer [DATA-1]"),
        validation_warnings=["layer 3: ungrounded number 5.0"],
        token_callback=_token_cb,
    )
    return base.model_copy(update=overrides)


def _lowcost(monkeypatch) -> None:
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_FULL_ENABLED", False, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_TERMINAL_ENABLED", False, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_MAX_ATTEMPTS", 1, raising=False)


@pytest.mark.asyncio
async def test_stage3_reissue_goes_through_validate_and_demote(monkeypatch):
    _lowcost(monkeypatch)
    llm_kwargs: list[dict[str, Any]] = []

    async def fake_call_llm(*args, **kwargs):
        llm_kwargs.append(kwargs)
        return "re-issued answer [DATA-1]"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    seen_by_validate: list[str] = []

    async def spy_validate(state):
        seen_by_validate.append(state.response.text)
        flagged = state.response.model_copy(update={
            "text": "BANNER " + state.response.text, "validation_state": "flagged",
        })
        return {"response": flagged, "validation_warnings": ["fresh L3 warning"]}

    async def spy_demote(state):
        return {"response": state.response.model_copy(update={"confidence": 0.2}),
                "demotion_reasons": ["demoted"]}

    monkeypatch.setattr(_nodes_mod, "validate_node", spy_validate)
    monkeypatch.setattr(_nodes_mod, "demote_node", spy_demote)

    update = await repair_shadow_node(_state())

    assert seen_by_validate == ["re-issued answer [DATA-1]"]
    assert update["response"].text == "BANNER re-issued answer [DATA-1]"
    assert update["response"].validation_state == "flagged"
    assert update["response"].confidence == 0.2
    assert update["validation_warnings"] == ["fresh L3 warning"]
    assert update["demotion_reasons"] == ["demoted"]
    # Never streamed.
    assert all(k.get("token_callback") is None for k in llm_kwargs)


@pytest.mark.asyncio
async def test_failed_revalidation_keeps_the_validated_original(monkeypatch):
    _lowcost(monkeypatch)

    async def fake_call_llm(*args, **kwargs):
        return "unchecked re-issue [DATA-1]"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)

    async def broken_validate(state):
        raise RuntimeError("validate blew up")

    monkeypatch.setattr(_nodes_mod, "validate_node", broken_validate)

    state = _state()
    update = await repair_shadow_node(state)

    assert "response" not in update
    assert state.response.text == "original validated answer [DATA-1]"


@pytest.mark.asyncio
async def test_stage4_reissue_does_not_stream(monkeypatch):
    monkeypatch.setattr(_settings, "REPAIR_LOOP_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_FULL_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_LOWCOST_ENABLED", False, raising=False)
    monkeypatch.setattr(_settings, "REPAIR_LOOP_MAX_ATTEMPTS", 1, raising=False)

    from app.agent.agentic_retrieval import preprocessor as _pp_mod
    from app.agent.agentic_retrieval import retrieval_profile as _rp_mod

    callbacks_seen: list[Any] = []

    async def fake_execute(s):
        callbacks_seen.append((s.token_callback, s.status_callback))
        return {"tool_results": [("query_project_overview", {"count": 2})],
                "evidence_packet": None}

    async def fake_assemble(s):
        callbacks_seen.append((s.token_callback, s.status_callback))
        return {"response": _response("loosened answer [DATA-1]")}

    async def passthrough_validate(state):
        return {"response": state.response, "validation_warnings": []}

    monkeypatch.setattr(_nodes_mod, "execute_node", fake_execute)
    monkeypatch.setattr(_nodes_mod, "assemble_node", fake_assemble)
    monkeypatch.setattr(_nodes_mod, "validate_node", passthrough_validate)

    state = _state(
        validation_warnings=["over-filtered query — relaxing filter set"],
        retrieval_profile=_rp_mod.profile_for_intent("synthesis"),
        retrieval_filters=_pp_mod.preprocess_envelope(None),
        status_callback=_token_cb,
    )
    update = await repair_shadow_node(state)

    assert callbacks_seen and all(cb == (None, None) for cb in callbacks_seen)
    assert update["response"].text == "loosened answer [DATA-1]"


# ---------------------------------------------------------------------------
# Audit 2026-10-04 item 25: own budget, and no stale retrieval_failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reissue_retrieval_replaces_the_stale_failure_list(monkeypatch):
    """The first retrieval's failures described searches that no longer apply."""
    from app.agent.agentic_retrieval import preprocessor as _pp_mod
    from app.agent.agentic_retrieval import retrieval_profile as _rp_mod

    async def fake_execute(s):
        return {
            "tool_results": [("query_project_overview", {"count": 2})],
            "evidence_packet": None,
            "retrieval_failures": [],  # the re-issue succeeded
        }

    seen_by_assemble: list[list[str]] = []

    async def fake_assemble(s):
        seen_by_assemble.append(list(s.retrieval_failures))
        return {"response": _response("loosened answer [DATA-1]")}

    monkeypatch.setattr(_nodes_mod, "execute_node", fake_execute)
    monkeypatch.setattr(_nodes_mod, "assemble_node", fake_assemble)

    state = _state(
        retrieval_failures=["PostGIS silver.collars (timeout) via query_spatial_collars"],
        retrieval_profile=_rp_mod.profile_for_intent("synthesis"),
        retrieval_filters=_pp_mod.preprocess_envelope(None),
    )
    await _nodes_mod._reissue_retrieval(state, {})
    assert state.retrieval_failures == []
    # assemble_node saw the FRESH list too, not the stale one.
    assert seen_by_assemble == [[]]


@pytest.mark.asyncio
async def test_a_slow_reissue_is_rolled_back_at_the_loop_budget(monkeypatch):
    _lowcost(monkeypatch)
    monkeypatch.setattr(_nodes_mod, "_REPAIR_LOOP_BUDGET_S", 0.05)

    async def never_returns(*args, **kwargs):
        import asyncio

        await asyncio.sleep(30)
        return "too late [DATA-1]"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", never_returns)

    state = _state()
    started = __import__("time").monotonic()
    update = await repair_shadow_node(state)

    assert __import__("time").monotonic() - started < 5
    assert "response" not in update
    assert state.response.text == "original validated answer [DATA-1]"
    assert state.validation_warnings == ["layer 3: ungrounded number 5.0"]


@pytest.mark.asyncio
async def test_an_exhausted_budget_skips_further_attempts(monkeypatch):
    _lowcost(monkeypatch)
    monkeypatch.setattr(_nodes_mod, "_REPAIR_LOOP_BUDGET_S", 0.0)
    called = []

    async def fake_call_llm(*args, **kwargs):  # pragma: no cover - must not run
        called.append(1)
        return "x [DATA-1]"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)
    state = _state()
    update = await repair_shadow_node(state)
    assert called == []
    assert "response" not in update
