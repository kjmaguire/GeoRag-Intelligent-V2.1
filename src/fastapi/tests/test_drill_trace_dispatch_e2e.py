"""End-to-end dispatch test — ADR-0007 PR-4.

Drives the agentic-retrieval graph nodes (classify → route → execute)
with a mocked tool layer to prove that:

  * "show me 3D view of hole 36-1085" actually invokes
    ``query_drill_traces_3d`` with ``hole_id='36-1085'``
  * a project-wide "3D view of drill traces for this project" call
    invokes the tool with ``hole_id=None``
  * a query with no 3-D keywords does NOT invoke the tool

Companion to ``test_hole_dispatch_e2e.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from app.agent.agentic_retrieval.nodes import (
    classify_node,
    execute_node,
    route_node,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.tools import DrillTrace3DResult

WORKSPACE = "a0000000-0000-0000-0000-000000000001"
PROJECT = "762b147e-af53-4593-b569-04ee46f31d97"


class _FakePool:
    @asynccontextmanager
    async def acquire(self):
        raise RuntimeError("FakePool.acquire should not be invoked")
        yield  # pragma: no cover


class _FakeDeps:
    def __init__(self) -> None:
        self.pg_pool = _FakePool()
        self.qdrant_client = None
        self.neo4j_driver = None
        self.project_id = PROJECT
        self.workspace_id = WORKSPACE
        self.openai_http_client = None
        self.anthropic_client = None
        self.redis_client = None
        self.embedding_model = None
        self.reranker = None
        self.user_id = "1"
        self.user_roles = ()


def _empty_drill_result(hole_id: str | None) -> DrillTrace3DResult:
    return DrillTrace3DResult(
        collars=[],
        intervals=[],
        structures=[],
        project_id=PROJECT,
        workspace_id=WORKSPACE,
        count=0,
        hole_id_filter=hole_id,
        source_row_ids=[],
    )


async def test_dispatch_single_hole_3d_view(monkeypatch) -> None:
    """'show me 3D view of hole 36-1085' must call query_drill_traces_3d."""
    calls: list[tuple[str, str, str | None]] = []

    async def fake_drill_traces(deps, workspace_id, project_id, hole_id):
        calls.append((workspace_id, project_id, hole_id))
        # Return a non-empty result so the result list captures the call.
        return DrillTrace3DResult(
            collars=[],
            intervals=[],
            structures=[],
            project_id=project_id,
            workspace_id=workspace_id,
            count=1,  # >0 so the response_assembler treats it as cited
            hole_id_filter=hole_id,
            source_row_ids=["collar-fake"],
        )

    # All other tools no-op so we don't trip on missing infra.
    async def noop(*args, **kwargs):
        return None

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "query_drill_traces_3d", fake_drill_traces)
    monkeypatch.setattr(_tools_mod, "search_documents", noop)
    monkeypatch.setattr(_tools_mod, "query_collar_details", noop)
    monkeypatch.setattr(_tools_mod, "query_spatial_collars", noop)
    monkeypatch.setattr(_tools_mod, "query_assay_data", noop)
    monkeypatch.setattr(_tools_mod, "query_downhole_logs", noop)
    monkeypatch.setattr(_tools_mod, "query_project_overview", noop)
    monkeypatch.setattr(_tools_mod, "query_stereonet", noop)

    state = AgenticRetrievalState(
        query="show me a 3D view of hole 36-1085",
        deps=_FakeDeps(),
    )
    state = state.model_copy(update=await classify_node(state))
    state = state.model_copy(update=await route_node(state))
    state = state.model_copy(update=await execute_node(state))

    # query_drill_traces_3d must have been invoked with hole_id='36-1085'.
    assert calls, "query_drill_traces_3d was not invoked"
    ws, proj, hid = calls[0]
    assert ws == WORKSPACE
    assert proj == PROJECT
    assert hid == "36-1085"

    # And the result must appear in tool_results so the assembler can
    # emit the viz_payload.
    matched = [r for name, r in state.tool_results if name == "query_drill_traces_3d"]
    assert len(matched) == 1
    assert isinstance(matched[0], DrillTrace3DResult)
    assert matched[0].hole_id_filter == "36-1085"


async def test_dispatch_project_wide_3d_view(monkeypatch) -> None:
    """'3D view of drill traces for this project' invokes with hole_id=None."""
    calls: list[tuple[str, str, str | None]] = []

    async def fake_drill_traces(deps, workspace_id, project_id, hole_id):
        calls.append((workspace_id, project_id, hole_id))
        return DrillTrace3DResult(
            collars=[],
            intervals=[],
            structures=[],
            project_id=project_id,
            workspace_id=workspace_id,
            count=66,
            hole_id_filter=hole_id,
            source_row_ids=[],
        )

    async def noop(*args, **kwargs):
        return None

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "query_drill_traces_3d", fake_drill_traces)
    monkeypatch.setattr(_tools_mod, "search_documents", noop)
    monkeypatch.setattr(_tools_mod, "query_collar_details", noop)
    monkeypatch.setattr(_tools_mod, "query_spatial_collars", noop)
    monkeypatch.setattr(_tools_mod, "query_assay_data", noop)
    monkeypatch.setattr(_tools_mod, "query_downhole_logs", noop)
    monkeypatch.setattr(_tools_mod, "query_project_overview", noop)
    monkeypatch.setattr(_tools_mod, "query_stereonet", noop)

    state = AgenticRetrievalState(
        query="show me the 3D view of drill traces for this project",
        deps=_FakeDeps(),
    )
    state = state.model_copy(update=await classify_node(state))
    state = state.model_copy(update=await route_node(state))
    state = state.model_copy(update=await execute_node(state))

    assert calls, "query_drill_traces_3d was not invoked"
    _ws, _proj, hid = calls[0]
    assert hid is None


async def test_no_dispatch_when_no_3d_keywords(monkeypatch) -> None:
    """Synthesis without 3D / trace keywords must NOT invoke the tool."""
    calls: list[Any] = []

    async def fake_drill_traces(*args, **kwargs):
        calls.append(args)
        return None

    async def noop(*args, **kwargs):
        return None

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "query_drill_traces_3d", fake_drill_traces)
    monkeypatch.setattr(_tools_mod, "search_documents", noop)
    monkeypatch.setattr(_tools_mod, "query_collar_details", noop)
    monkeypatch.setattr(_tools_mod, "query_spatial_collars", noop)
    monkeypatch.setattr(_tools_mod, "query_assay_data", noop)
    monkeypatch.setattr(_tools_mod, "query_downhole_logs", noop)
    monkeypatch.setattr(_tools_mod, "query_project_overview", noop)
    monkeypatch.setattr(_tools_mod, "query_stereonet", noop)

    state = AgenticRetrievalState(
        query="Summarise the alteration assemblage across the deposit.",
        deps=_FakeDeps(),
    )
    state = state.model_copy(update=await classify_node(state))
    state = state.model_copy(update=await route_node(state))
    await execute_node(state)

    assert calls == []
