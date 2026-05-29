"""End-to-end dispatch test — hole-id query routes to query_collar_details.

Drives the agentic-retrieval graph nodes (classify → route → execute) with
a mocked tool layer to prove that "tell me about hole 36-1085" actually
invokes ``query_collar_details`` with the right hole_id, instead of
falling through to search_documents alone.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from app.agent.agentic_retrieval import classify_intent_sync
from app.agent.agentic_retrieval.nodes import (
    classify_node,
    execute_node,
    route_node,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.tools import CollarDetailsResult

WORKSPACE = "a0000000-0000-0000-0000-000000000001"
PROJECT = "762b147e-af53-4593-b569-04ee46f31d97"
COLLAR_ID = "6e5144c7-55f3-48a5-96cb-245aafb06ace"


class _FakePool:
    @asynccontextmanager
    async def acquire(self):
        # No real DB in this test — the tool layer is monkeypatched below.
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


# ---------------------------------------------------------------------------
# 1. Classify proves the short-circuit fires for the failing query.
# ---------------------------------------------------------------------------


def test_classifier_routes_hole_query_to_factual_lookup() -> None:
    got = classify_intent_sync("tell me about hole 36-1085")
    assert got.intent == "factual_lookup"
    assert got.matched_triggers == ("hole_id_detected",)


# ---------------------------------------------------------------------------
# 2. End-to-end: classify → route → execute. The execute node must call
#    query_collar_details with hole_id='36-1085'.
# ---------------------------------------------------------------------------


async def test_execute_node_calls_query_collar_details(monkeypatch) -> None:
    calls: list[tuple[Any, str, str, str]] = []

    async def fake_collar_details(
        deps: Any, workspace_id: str, project_id: str, hole_id: str
    ) -> CollarDetailsResult:
        calls.append((deps, workspace_id, project_id, hole_id))
        return CollarDetailsResult(
            collar_id=COLLAR_ID,
            hole_id=hole_id,
            hole_id_canonical=None,
            project_id=project_id,
            workspace_id=workspace_id,
            total_depth=372.3,
            drill_type="DDH",
            hole_type="Diamond",
            drill_date="1985-06-15",
            easting=421000.0,
            northing=4630000.0,
            elevation=2100.0,
            azimuth=90.0,
            dip=-60.0,
            geologist="J. Smith",
            assay_count=42,
            lithology_count=18,
            sample_count=30,
            structure_count=3,
            max_assay_value={
                "element": "U3O8",
                "value": 0.342,
                "unit": "pct",
                "depth_from": 145.2,
                "depth_to": 146.7,
            },
            lithology_summary=[{"rock_code": "SS", "total_metres": 180.0}],
            source_row_ids=[COLLAR_ID],
            count=1,
        )

    # Patch query_collar_details where it's imported in BOTH possible
    # call sites (nodes.execute_node + the tool dispatcher).
    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "query_collar_details", fake_collar_details)

    # Make sure search_documents (the factual_lookup profile's primary
    # tool) is a no-op so this test only checks the hole-id pre-pass.
    async def noop_search(*args, **kwargs):
        return None

    monkeypatch.setattr(_tools_mod, "search_documents", noop_search)

    state = AgenticRetrievalState(
        query="tell me about hole 36-1085",
        deps=_FakeDeps(),
    )
    # classify
    update = await classify_node(state)
    state = state.model_copy(update=update)
    assert state.intent == "factual_lookup"

    # route
    update = await route_node(state)
    state = state.model_copy(update=update)
    assert state.retrieval_profile is not None

    # execute
    update = await execute_node(state)
    state = state.model_copy(update=update)

    # Assert query_collar_details was invoked once with hole_id='36-1085'
    assert len(calls) == 1
    _deps, ws, proj, hid = calls[0]
    assert ws == WORKSPACE
    assert proj == PROJECT
    assert hid == "36-1085"

    # tool_results contains the CollarDetailsResult
    collar_results = [
        r for name, r in state.tool_results if name == "query_collar_details"
    ]
    assert len(collar_results) == 1
    assert isinstance(collar_results[0], CollarDetailsResult)
    assert collar_results[0].hole_id == "36-1085"
    assert collar_results[0].collar_id == COLLAR_ID


async def test_no_collar_details_call_for_non_hole_query(monkeypatch) -> None:
    """Queries that don't name a hole must NOT invoke query_collar_details."""
    calls: list[Any] = []

    async def fake_collar_details(*args, **kwargs):
        calls.append(args)
        return None

    async def noop(*args, **kwargs):
        return None

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "query_collar_details", fake_collar_details)
    monkeypatch.setattr(_tools_mod, "search_documents", noop)
    monkeypatch.setattr(_tools_mod, "query_spatial_collars", noop)
    monkeypatch.setattr(_tools_mod, "query_assay_data", noop)
    monkeypatch.setattr(_tools_mod, "query_downhole_logs", noop)
    monkeypatch.setattr(_tools_mod, "traverse_knowledge_graph", noop)
    monkeypatch.setattr(_tools_mod, "query_project_overview", noop)

    state = AgenticRetrievalState(
        query="Summarise the alteration assemblage across the deposit.",
        deps=_FakeDeps(),
    )
    state = state.model_copy(update=await classify_node(state))
    state = state.model_copy(update=await route_node(state))
    await execute_node(state)

    assert calls == []
