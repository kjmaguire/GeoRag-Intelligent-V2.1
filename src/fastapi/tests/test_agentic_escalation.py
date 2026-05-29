"""Unit tests for R9-full — Pydantic AI agentic escalation.

Focuses on the wiring and failure modes; the agent's internal decisions
are the LLM's responsibility, not ours.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.agentic_escalation import run_agentic_escalation
from app.agent.deps import AgentDeps


def _deps_with_client(client):
    """Minimal AgentDeps for tests — only the client matters for gating."""
    return AgentDeps(
        pg_pool=None,
        qdrant_client=None,
        neo4j_driver=None,
        project_id="proj-test",
        anthropic_client=client,
    )


@pytest.mark.asyncio
async def test_returns_empty_when_flag_off(monkeypatch):
    """Default state: AGENTIC_FULL_ESCALATION_ENABLED=False → no agent run."""
    from app.agent import agentic_escalation as ae

    monkeypatch.setattr(
        ae.settings, "AGENTIC_FULL_ESCALATION_ENABLED", False, raising=False
    )
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
    deps = _deps_with_client(client)
    result = await run_agentic_escalation("any query", deps)
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_when_no_anthropic_client(monkeypatch):
    """AGENTIC_FULL_ESCALATION_ENABLED=True but no client → graceful no-op."""
    from app.agent import agentic_escalation as ae

    monkeypatch.setattr(
        ae.settings, "AGENTIC_FULL_ESCALATION_ENABLED", True, raising=False
    )
    deps = _deps_with_client(client=None)
    result = await run_agentic_escalation("any query", deps)
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_on_agent_build_failure(monkeypatch):
    """If _build_agent raises (e.g. library incompatibility), return []."""
    from app.agent import agentic_escalation as ae

    monkeypatch.setattr(
        ae.settings, "AGENTIC_FULL_ESCALATION_ENABLED", True, raising=False
    )
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
    deps = _deps_with_client(client)

    async def _blow_up(_deps):
        raise RuntimeError("agent construction failed")

    with patch.object(ae, "_build_agent", _blow_up):
        # Agent construction error should bubble up as a non-fatal empty
        # list so the main orchestrator can fall through to its own
        # "no results" path.
        try:
            result = await run_agentic_escalation("q", deps)
        except RuntimeError:
            pytest.fail(
                "run_agentic_escalation should swallow agent-construction "
                "errors and return [] rather than letting them escape."
            )
        assert result == []


@pytest.mark.asyncio
async def test_extracts_tool_results_from_agent_run(monkeypatch):
    """Happy path: mock the agent; verify the extractor returns normalised
    (tool_name, content) tuples with the _tool suffix stripped."""
    from app.agent import agentic_escalation as ae

    monkeypatch.setattr(
        ae.settings, "AGENTIC_FULL_ESCALATION_ENABLED", True, raising=False
    )

    # Fake tool return parts — mirror Pydantic AI's ToolReturnPart shape.
    fake_parts = [
        SimpleNamespace(
            tool_name="search_documents_tool",
            content=SimpleNamespace(count=4, chunks=["c1", "c2", "c3", "c4"]),
        ),
        SimpleNamespace(
            tool_name="traverse_knowledge_graph_tool",
            content=SimpleNamespace(count=2, entities=["e1", "e2"]),
        ),
    ]
    fake_msg = SimpleNamespace(parts=fake_parts)
    fake_result = SimpleNamespace(all_messages=lambda: [fake_msg])

    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))

    async def _fake_build(_deps):
        return fake_agent

    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
    deps = _deps_with_client(client)

    with patch.object(ae, "_build_agent", _fake_build):
        result = await run_agentic_escalation("compare deposits", deps)

    # Tool names must have the _tool suffix stripped so they match the
    # deterministic-dispatch names downstream consumers expect.
    assert [n for n, _ in result] == [
        "search_documents",
        "traverse_knowledge_graph",
    ]
    assert result[0][1].count == 4
    assert result[1][1].count == 2


@pytest.mark.asyncio
async def test_ignores_parts_without_tool_name(monkeypatch):
    """Text-only or system parts that lack tool_name must be filtered."""
    from app.agent import agentic_escalation as ae

    monkeypatch.setattr(
        ae.settings, "AGENTIC_FULL_ESCALATION_ENABLED", True, raising=False
    )

    fake_parts = [
        SimpleNamespace(
            content="agent intro text",
            # no tool_name attribute at all
        ),
        SimpleNamespace(
            tool_name="search_documents_tool",
            content=SimpleNamespace(count=1),
        ),
        SimpleNamespace(
            tool_name="orphan_tool",
            content=None,   # content=None is skipped
        ),
    ]
    fake_msg = SimpleNamespace(parts=fake_parts)
    fake_result = SimpleNamespace(all_messages=lambda: [fake_msg])
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))

    async def _fake_build(_deps):
        return fake_agent

    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
    deps = _deps_with_client(client)

    with patch.object(ae, "_build_agent", _fake_build):
        result = await run_agentic_escalation("q", deps)

    # Only the one real tool result survived; suffix stripped.
    assert [n for n, _ in result] == ["search_documents"]
