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


async def _search(reranker: Any, backend: str = "bedrock") -> DocumentSearchResult:
    with patch("app.agent.tools.settings") as s, patch(
        "app.agent.tools.RERANKER_BACKEND", backend
    ), patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
        s.TIMEOUT_QDRANT_S = 5.0
        s.TIMEOUT_RERANKER_S = 0.2
        s.RETRIEVAL_TOP_N = 20
        s.RERANKER_SCORE_THRESHOLD = 0.0
        s.RERANKER_SCORE_THRESHOLD_HOSTED = 0.2
        s.RERANKER_SCORE_THRESHOLD_PROBABILITY = 0.2
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
async def test_hosted_backend_with_no_reranker_fails_closed() -> None:
    """Audit item B (2026-10-04): RERANKER_BACKEND=bedrock with an empty
    BEDROCK_RERANK_MODEL_ID (or a swallowed lifespan failure) leaves the
    reranker None. That used to return unfiltered RRF order with no floor."""
    result = await _search(None, backend="bedrock")
    assert result.chunks == []
    assert result.count == 0
    assert result.retrieval_failure == "reranker_unavailable"
    assert result.rerank_degraded is False
    assert "reranker unavailable" in result.data_source


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["cross_encoder", "qwen3_causal"])
async def test_local_backend_with_no_reranker_keeps_the_rrf_degraded_mode(
    backend: str,
) -> None:
    result = await _search(None, backend=backend)
    assert result.rerank_degraded is True
    assert result.retrieval_failure is None
    assert result.count == 2


@pytest.mark.asyncio
async def test_hosted_no_reranker_failure_reaches_the_node_as_an_outage(monkeypatch) -> None:
    result = await _search(None, backend="bedrock")

    async def fake_call(tool_name, query, deps):
        return result if tool_name == "search_documents" else None

    monkeypatch.setattr(_nodes_mod, "_call_tool_safely", fake_call)
    state = AgenticRetrievalState(
        query="what is the resource estimate?", deps=_NodeDeps(),
        intent="factual_lookup", effective_intent="factual_lookup",
        retrieval_profile=profile_for_intent("factual_lookup"),
    )
    with pytest.raises(RetrievalBackendUnavailable) as exc_info:
        await execute_node(state)
    assert exc_info.value.reason == "reranker_unavailable"


async def _qwen3_search(scores: list[float], threshold: float = 0.2) -> DocumentSearchResult:
    reranker = MagicMock()
    reranker.predict = MagicMock(return_value=scores)
    with patch("app.agent.tools.RERANKER_BACKEND", "qwen3_causal"), patch(
        "app.agent.tools.settings"
    ) as s, patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
        s.TIMEOUT_QDRANT_S = 5.0
        s.TIMEOUT_RERANKER_S = 5.0
        s.RETRIEVAL_TOP_N = 20
        # The logit floor is a no-op on a probability; the probability floor
        # is what must apply.
        s.RERANKER_SCORE_THRESHOLD = 0.0
        s.RERANKER_SCORE_THRESHOLD_HOSTED = 0.99
        s.RERANKER_SCORE_THRESHOLD_PROBABILITY = threshold
        s.RERANKER_INPUT_CHAR_BUDGET = 2000
        s.RERANKER_TOP_K = 5
        return await search_documents(
            _Ctx(deps=_deps(reranker)),  # type: ignore[arg-type]
            query_text="indicated resource",
            project_id="proj",
        )


@pytest.mark.asyncio
async def test_qwen3_causal_probabilities_are_not_sigmoided() -> None:
    result = await _qwen3_search([0.9, 0.5])
    assert [c.relevance_score for c in result.chunks] == pytest.approx([0.9, 0.5])


@pytest.mark.asyncio
async def test_qwen3_causal_uses_its_own_probability_floor() -> None:
    """Audit item G: P(yes)=0.05 is irrelevant but cleared the 0.0 logit floor."""
    result = await _qwen3_search([0.9, 0.05])
    assert [c.chunk_id for c in result.chunks] == ["chunk-1"]
    # ...and the floor is its own setting, neither the hosted nor the logit one.
    loose = await _qwen3_search([0.9, 0.05], threshold=0.01)
    assert [c.chunk_id for c in loose.chunks] == ["chunk-1", "chunk-2"]


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


# ---------------------------------------------------------------------------
# 2026-10-10 audit, finding 10: only the literal "bedrock" failed closed
# ---------------------------------------------------------------------------
#
# Any other RERANKER_BACKEND value ("cohere", a typo) took the lenient local
# path: no reranker could be built for it, so search_documents returned 12
# RRF-ordered chunks with no relevance floor. "Explicitly local" is now a
# closed set; everything else is held to the hosted contract.


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["cohere", "bedrok", "Bedrock", "foundry", "", "local"])
async def test_an_unrecognised_backend_with_no_reranker_fails_closed(backend: str) -> None:
    result = await _search(None, backend=backend)
    assert result.chunks == []
    assert result.count == 0
    assert result.retrieval_failure == "reranker_unavailable"
    assert result.rerank_degraded is False


@pytest.mark.asyncio
async def test_an_unrecognised_backend_is_held_to_the_hosted_floor_not_the_logit_one() -> None:
    """With a reranker object in hand but a backend value nobody defined, the
    scores are not assumed to be logits: 0.1 sits under the hosted 0.2 floor
    (the logit floor, 0.0, would have kept it) and 0.9 is not sigmoided."""
    reranker = MagicMock()
    reranker.predict = MagicMock(return_value=[0.9, 0.1])
    result = await _search(reranker, backend="cohere")
    assert [c.chunk_id for c in result.chunks] == ["chunk-1"]
    assert [c.relevance_score for c in result.chunks] == pytest.approx([0.9])
