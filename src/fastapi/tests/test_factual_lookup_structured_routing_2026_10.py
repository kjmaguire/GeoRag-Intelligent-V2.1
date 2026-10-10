"""Audit 2026-10 finding 22: structured-data questions no longer get a documents-only profile.

The classifier files every "what is / what are / what does" question, and any
query that names a hole, under factual_lookup, and factual_lookup's profile was
``["search_documents"]``. So "What is the deepest hole in the project?", "What
are the top gold assays?" and "Show the gold assays for PLS-22-08" never ran
the collar / assay / downhole tools: the answer to a number that lives in
PostGIS was looked for in report text.

The classifier's triggers are NOT narrowed (the first test records why the
profile has to be). The profile is widened where it is applied, by what the
question is about.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agent.agentic_retrieval import classify_intent_sync
from app.agent.agentic_retrieval.nodes import classify_node, execute_node, route_node
from app.agent.agentic_retrieval.retrieval_profile import (
    _PROFILES,
    profile_for_intent,
    profile_for_query,
    structured_tools_for_factual_lookup,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState

WORKSPACE = "a0000000-0000-0000-0000-000000000001"
PROJECT = "762b147e-af53-4593-b569-04ee46f31d97"

DEEPEST = "What is the deepest hole in the project?"
TOP_ASSAYS = "What are the top gold assays?"
HOLE_ASSAYS = "Show the gold assays for PLS-22-08"
HOLE_LITHOLOGY = "What lithology is logged for PLS-22-08?"
BARE_HOLE = "What is PLS-22-08?"
DEFINITION = "What is the NI 43-101 definition of an indicated resource?"
CRIRSCO = "What does the CRIRSCO template require for classification?"


# ---------------------------------------------------------------------------
# Why: the classifier calls all of these factual_lookup
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query", [DEEPEST, TOP_ASSAYS, HOLE_ASSAYS, HOLE_LITHOLOGY, BARE_HOLE, DEFINITION, CRIRSCO])
def test_the_classifier_files_all_of_them_under_factual_lookup(query: str) -> None:
    assert classify_intent_sync(query).intent == "factual_lookup"


# ---------------------------------------------------------------------------
# The tool selection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("query", "hole_ids", "expected"),
    [
        # project-level questions
        (DEEPEST, [], ["query_spatial_collars"]),
        ("How many drill holes are there?", [], ["query_spatial_collars"]),
        ("Which collar has the greatest depth?", [], ["query_spatial_collars"]),
        (TOP_ASSAYS, [], ["query_assay_data"]),
        ("What is the highest grade intercept?", [], ["query_assay_data"]),
        ("What are the best intercepts in g/t?", [], ["query_assay_data"]),
        # a named hole: the collar record is the pre-pass's job, not a 50-row sample
        (HOLE_ASSAYS, ["PLS-22-08"], ["query_assay_data"]),
        (HOLE_LITHOLOGY, ["PLS-22-08"], ["query_downhole_logs"]),
        ("What is the depth of hole PLS-22-08?", ["PLS-22-08"], []),
        ("Assays and lithology for PLS-22-08", ["PLS-22-08"], ["query_assay_data", "query_downhole_logs"]),
        # a named hole and nothing else to go on: what we hold on that hole
        (BARE_HOLE, ["PLS-22-08"], ["query_assay_data", "query_downhole_logs"]),
        # log words without a hole: the logs tool needs one, so it is not listed
        ("What lithologies occur in the project?", [], []),
        # pure document questions
        (DEFINITION, [], []),
        (CRIRSCO, [], []),
        ("", [], []),
    ],
)
def test_the_structured_tools_follow_what_the_question_is_about(
    query: str, hole_ids: list[str], expected: list[str]
) -> None:
    assert structured_tools_for_factual_lookup(query, hole_ids) == expected


def test_a_depth_question_does_not_drag_in_assay_statistics() -> None:
    """Statistics the question did not ask for are statistics the model quotes."""
    assert "query_assay_data" not in structured_tools_for_factual_lookup(DEEPEST, [])


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------
def test_a_structured_question_gets_the_structured_tools_after_the_documents_leg() -> None:
    profile = profile_for_query("factual_lookup", DEEPEST)

    assert profile.primary_tools == ["search_documents", "query_spatial_collars"]
    assert profile.secondary_tools == ["search_public_geoscience"]
    assert profile.answer_emphasis == "exact_citation"


def test_a_named_hole_scopes_the_assay_tool_and_skips_the_project_listing() -> None:
    profile = profile_for_query("factual_lookup", HOLE_ASSAYS, hole_ids=["PLS-22-08"])

    assert profile.primary_tools == ["search_documents", "query_assay_data"]


@pytest.mark.parametrize("query", [DEFINITION, CRIRSCO])
def test_a_pure_document_question_keeps_the_documents_only_profile(query: str) -> None:
    profile = profile_for_query("factual_lookup", query)

    assert profile.primary_tools == ["search_documents"]
    assert profile == profile_for_intent("factual_lookup")


@pytest.mark.parametrize("intent", [i for i in _PROFILES if i != "factual_lookup"])
def test_no_other_intent_is_touched(intent: Any) -> None:
    assert profile_for_query(intent, DEEPEST, hole_ids=["PLS-22-08"]) == profile_for_intent(intent)


def test_the_shared_base_profile_is_never_mutated() -> None:
    profile_for_query("factual_lookup", HOLE_ASSAYS, hole_ids=["PLS-22-08"])
    profile_for_query("factual_lookup", DEEPEST)

    assert _PROFILES["factual_lookup"].primary_tools == ["search_documents"]
    assert profile_for_intent("factual_lookup").primary_tools == ["search_documents"]


def test_a_tool_is_never_listed_twice() -> None:
    profile = profile_for_query("factual_lookup", "assays and grades and intercepts", hole_ids=["H-1"])

    assert len(profile.primary_tools) == len(set(profile.primary_tools))


# ---------------------------------------------------------------------------
# The graph: classify -> route -> execute
# ---------------------------------------------------------------------------
class _FakePool:
    @asynccontextmanager
    async def acquire(self):
        raise RuntimeError("no database in this test: the tool layer is patched")
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


async def _routed(query: str) -> AgenticRetrievalState:
    state = AgenticRetrievalState(query=query, deps=_FakeDeps())
    state = state.model_copy(update=await classify_node(state))
    return state.model_copy(update=await route_node(state))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_primary"),
    [
        (DEEPEST, ["search_documents", "query_spatial_collars"]),
        (TOP_ASSAYS, ["search_documents", "query_assay_data"]),
        (HOLE_ASSAYS, ["search_documents", "query_assay_data"]),
        (HOLE_LITHOLOGY, ["search_documents", "query_downhole_logs"]),
        (DEFINITION, ["search_documents"]),
        (CRIRSCO, ["search_documents"]),
    ],
)
async def test_route_node_applies_the_widened_profile(query: str, expected_primary: list[str]) -> None:
    state = await _routed(query)

    assert state.effective_intent == "factual_lookup"
    assert state.retrieval_profile is not None
    assert state.retrieval_profile.primary_tools == expected_primary


@pytest.mark.asyncio
async def test_execute_node_runs_the_assay_tool_with_the_hole_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent.tools as tools_mod

    assay_calls: list[dict[str, Any]] = []
    collar_calls: list[Any] = []

    async def fake_assays(ctx: Any, project_id: str, **kwargs: Any) -> None:
        assay_calls.append({"project_id": project_id, **kwargs})

    async def fake_collars(ctx: Any, project_id: str, **kwargs: Any) -> None:
        collar_calls.append(project_id)

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(tools_mod, "query_assay_data", fake_assays)
    monkeypatch.setattr(tools_mod, "query_spatial_collars", fake_collars)
    monkeypatch.setattr(tools_mod, "query_collar_details", noop)
    monkeypatch.setattr(tools_mod, "search_documents", noop)

    state = await _routed(HOLE_ASSAYS)
    await execute_node(state)

    assert len(assay_calls) == 1, "the assay tool never ran for an assay question"
    assert assay_calls[0]["project_id"] == PROJECT
    assert assay_calls[0]["hole_ids"] == ["PLS-22-08"]
    assert assay_calls[0]["commodity"] == "gold"
    assert collar_calls == [], "a named hole is not answered from a project-wide collar sample"


@pytest.mark.asyncio
async def test_execute_node_runs_the_collar_tool_for_a_project_level_depth_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent.tools as tools_mod

    collar_calls: list[str] = []
    assay_calls: list[Any] = []

    async def fake_collars(ctx: Any, project_id: str, **kwargs: Any) -> None:
        collar_calls.append(project_id)

    async def fake_assays(*_a: Any, **_k: Any) -> None:
        assay_calls.append(1)

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(tools_mod, "query_spatial_collars", fake_collars)
    monkeypatch.setattr(tools_mod, "query_assay_data", fake_assays)
    monkeypatch.setattr(tools_mod, "search_documents", noop)

    state = await _routed(DEEPEST)
    await execute_node(state)

    assert collar_calls == [PROJECT]
    assert assay_calls == []


@pytest.mark.asyncio
async def test_a_document_question_still_dispatches_only_the_documents_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent.tools as tools_mod

    seen: list[str] = []

    def recorder(name: str) -> Any:
        async def _tool(*_a: Any, **_k: Any) -> None:
            seen.append(name)

        return _tool

    for name in ("search_documents", "query_spatial_collars", "query_assay_data", "query_downhole_logs"):
        monkeypatch.setattr(tools_mod, name, recorder(name))

    state = await _routed(DEFINITION)
    await execute_node(state)

    assert seen == ["search_documents"]
