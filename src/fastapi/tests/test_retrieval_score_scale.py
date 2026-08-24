"""A calibrated [0,1] gate must never be applied to Qdrant RRF fusion scores.

`hybrid_query` fuses a dense and a sparse prefetch with server-side RRF, so
`point.score` is derived from a point's RANK in each branch. It is a small
number bounded by the fusion constant and it says nothing about similarity.
Two places treated it as a calibrated relevance:

  * `search_documents`, when `reranker is None`, ran a Layer 1 quality gate
    at RETRIEVAL_QUALITY_THRESHOLD (0.5) over those scores.
  * `_dispatch_document_passage_search` re-applied `min_relevance`
    (0.5-0.6, from the decomposer) to whatever `relevance_score` came back,
    including on the `rerank_degraded` path where the reranker never
    overwrote it.

Both dropped everything, and `reranker is None` needs nothing more exotic
than an unset AZURE_FOUNDRY_API_KEY. The symptom was the agent reporting
"insufficient information" about a corpus that plainly contained the
answer — indistinguishable, from the outside, from working correctly.

The threshold sweep could not have caught it: scripts/sweep_retrieval_
threshold.py requires a reranker to be loaded, and the reranker branch
returns before reaching the gate. It measured the knob as "effectively
inert between 0.25 and 0.60", and config.py raised it 0.30 -> 0.50 on the
strength of that.

The first call site is gone: `search_documents` no longer gates the
degraded path at all (see the comment in app/agent/tools.py where the
`filter_by_quality` call used to be). The tests below pin the second — the
path the decomposer actually drives, which is still live code.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestPlanExecutorSkipsTheGateWhenDegraded:
    """The path the decomposer actually drives."""

    @staticmethod
    def _sub_query(min_relevance: float):
        from app.models.decomposition import (
            DocumentPassageSearchInput,
            SubQueryDocumentPassageSearch,
        )

        return SubQueryDocumentPassageSearch(
            id="sq-1",
            sub_query_class="document_passage_search",
            input=DocumentPassageSearchInput(
                query_text="uranium grades at Triple R",
                top_k=5,
                min_relevance=min_relevance,
            ),
            latency_budget_s=10.0,
        )

    @staticmethod
    def _ctx():
        from app.agent.deps import AgentDeps, ToolContext

        return ToolContext(
            AgentDeps(
                pg_pool=MagicMock(),
                qdrant_client=None,
                neo4j_driver=None,
                project_id="00000000-0000-0000-0000-0000000000aa",
                embedding_model=None,
                reranker=None,
            )
        )

    @staticmethod
    def _chunk(chunk_id: str, score: float):
        from app.agent.tools import DocumentChunk

        return DocumentChunk(
            chunk_id=chunk_id,
            text="Drill hole RL-22-014 returned 2.1% U3O8 over 4.5 m.",
            source_document_id="rep-001",
            document_title="NI 43-101 Technical Report",
            section_number="14.1",
            section_title="Mineral Resource Estimate",
            section="Mineral Resource Estimate",
            page=142,
            document_type="NI43",
            report_id="rep-001",
            relevance_score=score,
        )

    @pytest.mark.asyncio
    async def test_degraded_rrf_scores_are_not_gated_away(self, monkeypatch) -> None:
        """The whole point: a down reranker must not read as an empty corpus."""
        from app.agent import plan_executor
        from app.agent.tools import DocumentSearchResult

        result = DocumentSearchResult(
            chunks=[
                self._chunk("11111111-1111-4111-8111-111111111111", 0.0328),
                self._chunk("22222222-2222-4222-8222-222222222222", 0.0161),
            ],
            count=2,
            rerank_degraded=True,
            data_source="qdrant:georag_chunks (rerank unavailable)",
        )
        monkeypatch.setattr(
            plan_executor, "search_documents", AsyncMock(return_value=result)
        )

        out = await plan_executor._dispatch_document_passage_search(
            self._sub_query(min_relevance=0.6), self._ctx()
        )

        assert len(out["passages"]) == 2

    @pytest.mark.asyncio
    async def test_a_healthy_rerank_is_still_gated(self, monkeypatch) -> None:
        """Layer 1 has to keep working where the scores mean something."""
        from app.agent import plan_executor
        from app.agent.tools import DocumentSearchResult

        result = DocumentSearchResult(
            chunks=[
                self._chunk("11111111-1111-4111-8111-111111111111", 0.91),
                self._chunk("22222222-2222-4222-8222-222222222222", 0.12),
            ],
            count=2,
            rerank_degraded=False,
            data_source="qdrant:georag_chunks (reranked)",
        )
        monkeypatch.setattr(
            plan_executor, "search_documents", AsyncMock(return_value=result)
        )

        out = await plan_executor._dispatch_document_passage_search(
            self._sub_query(min_relevance=0.6), self._ctx()
        )

        assert len(out["passages"]) == 1
        # model_dump(mode="json") — the dispatcher returns plain dicts.
        assert out["passages"][0]["relevance"] == pytest.approx(0.91)
