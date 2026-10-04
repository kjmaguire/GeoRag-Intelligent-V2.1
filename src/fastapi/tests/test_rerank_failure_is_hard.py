"""Audit 2026-10-04 item 1: a reranker outage must not bypass Layer 1.

A reranker timeout or exception used to return the top-K chunks in RRF order
with no score floor, so the hard refusal could never fire during a Bedrock
throttle. It is now a typed retrieval failure (``reranker_unavailable``,
retried once) that ``execute_node`` raises as ``RetrievalBackendUnavailable``,
the same path as a dead sparse leg.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from app.agent.agentic_retrieval import nodes as _nodes_mod
from app.agent.agentic_retrieval.nodes import execute_node
from app.agent.agentic_retrieval.retrieval_profile import profile_for_intent
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.deps import AgentDeps
from app.agent.errors import RetrievalBackendUnavailable
from app.agent.tools import DocumentSearchResult, search_documents

_WS = "a0000000-0000-0000-0000-000000000001"


@dataclass
class _Ctx:
    deps: AgentDeps


def _point(i: int, score: float) -> MagicMock:
    p = MagicMock()
    p.id = f"chunk-{i}"
    p.score = score
    p.payload = {
        "text": f"passage {i}",
        "document_title": "NI 43-101",
        "report_id": "rep-1",
        "document_type": "NI43",
    }
    return p


def _deps(reranker: Any) -> AgentDeps:
    response = MagicMock()
    response.points = [_point(1, 0.03), _point(2, 0.02)]
    qdrant = AsyncMock()
    qdrant.query_points = AsyncMock(return_value=response)
    model = MagicMock()
    model.encode = MagicMock(return_value=np.array([0.1] * 8, dtype="float32"))
    return AgentDeps(
        pg_pool=None,  # type: ignore[arg-type]
        qdrant_client=qdrant,  # type: ignore[arg-type]
        neo4j_driver=None,  # type: ignore[arg-type]
        project_id="00000000-0000-0000-0000-0000000000aa",
        embedding_model=model,
        reranker=reranker,
        workspace_id=_WS,
    )


async def _search(reranker: Any) -> DocumentSearchResult:
    with patch("app.agent.tools.settings") as s, patch(
        "app.agent.tools.RERANKER_BACKEND", "bedrock"
    ), patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
        s.TIMEOUT_QDRANT_S = 5.0
        s.TIMEOUT_RERANKER_S = 0.2
        s.RETRIEVAL_TOP_N = 20
        s.RERANKER_SCORE_THRESHOLD = 0.0
        s.RERANKER_SCORE_THRESHOLD_HOSTED = 0.2
        s.RERANKER_TOP_K = 5
        return await search_documents(
            _Ctx(deps=_deps(reranker)),  # type: ignore[arg-type]
            query_text="indicated resource",
            project_id="proj",
        )


@pytest.mark.asyncio
async def test_reranker_exception_is_retried_once_then_typed_failure() -> None:
    reranker = MagicMock()
    reranker.predict = MagicMock(side_effect=RuntimeError("ThrottlingException"))
    result = await _search(reranker)
    assert reranker.predict.call_count == 2
    assert result.chunks == []
    assert result.retrieval_failure == "reranker_unavailable"
    assert result.rerank_degraded is False
    assert "reranker unavailable" in result.data_source


@pytest.mark.asyncio
async def test_reranker_timeout_is_retried_once_then_typed_failure() -> None:
    import time

    reranker = MagicMock()
    reranker.predict = MagicMock(side_effect=lambda pairs: time.sleep(0.5) or [0.9, 0.9])
    result = await _search(reranker)
    assert reranker.predict.call_count == 2
    assert result.chunks == []
    assert result.retrieval_failure == "reranker_unavailable"


@pytest.mark.asyncio
async def test_transient_reranker_failure_recovers_on_retry() -> None:
    reranker = MagicMock()
    reranker.predict = MagicMock(side_effect=[RuntimeError("429"), [0.9, 0.5]])
    result = await _search(reranker)
    assert reranker.predict.call_count == 2
    assert result.retrieval_failure is None
    assert [c.chunk_id for c in result.chunks] == ["chunk-1", "chunk-2"]
    assert result.rerank_degraded is False


@pytest.mark.asyncio
async def test_short_score_list_is_a_failure_not_an_unfiltered_pass() -> None:
    reranker = MagicMock()
    reranker.predict = MagicMock(return_value=[])
    result = await _search(reranker)
    assert result.retrieval_failure == "reranker_unavailable"
    assert result.chunks == []


@pytest.mark.asyncio
async def test_no_reranker_configured_keeps_the_rrf_degraded_mode() -> None:
    result = await _search(None)
    assert result.rerank_degraded is True
    assert result.retrieval_failure is None
    assert result.count == 2


@pytest.mark.asyncio
async def test_qwen3_causal_probabilities_are_not_sigmoided() -> None:
    reranker = MagicMock()
    reranker.predict = MagicMock(return_value=[0.9, 0.5])
    with patch("app.agent.tools.RERANKER_BACKEND", "qwen3_causal"), patch(
        "app.agent.tools.settings"
    ) as s, patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
        s.TIMEOUT_QDRANT_S = 5.0
        s.TIMEOUT_RERANKER_S = 5.0
        s.RETRIEVAL_TOP_N = 20
        s.RERANKER_SCORE_THRESHOLD = 0.0
        s.RERANKER_INPUT_CHAR_BUDGET = 2000
        s.RERANKER_TOP_K = 5
        result = await search_documents(
            _Ctx(deps=_deps(reranker)),  # type: ignore[arg-type]
            query_text="indicated resource",
            project_id="proj",
        )
    assert [c.relevance_score for c in result.chunks] == pytest.approx([0.9, 0.5])


class _NodeDeps:
    project_id = "p-1"
    workspace_id = "ws-1"
    pg_pool = None
    redis_client = None


@pytest.mark.asyncio
async def test_node_raises_backend_unavailable_on_reranker_failure(monkeypatch) -> None:
    failed = DocumentSearchResult(
        chunks=[], count=0, data_source="Qdrant georag_chunks (reranker unavailable)",
        retrieval_failure="reranker_unavailable",
    )

    async def fake_call(tool_name, query, deps):
        return failed if tool_name == "search_documents" else None

    monkeypatch.setattr(_nodes_mod, "_call_tool_safely", fake_call)
    state = AgenticRetrievalState(
        query="what is the resource estimate?", deps=_NodeDeps(),
        intent="factual_lookup", effective_intent="factual_lookup",
        retrieval_profile=profile_for_intent("factual_lookup"),
    )
    with pytest.raises(RetrievalBackendUnavailable) as exc_info:
        await execute_node(state)
    assert exc_info.value.reason == "reranker_unavailable"
