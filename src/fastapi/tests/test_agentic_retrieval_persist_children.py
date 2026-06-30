"""RetrievalInspector follow-up — child-table writes from persist_node.

The agentic-retrieval persist_node previously wrote only the parent
silver.answer_runs row. The inspector's Retrieval / Context panels
read silver.answer_retrieval_items and silver.answer_citation_items
respectively, so they always rendered empty for agentic runs.

These tests pin the contract for the helpers introduced in this commit:

  * _maybe_uuid coerces a chunk_id to its UUID-string form when valid
  * _normalise_marker rewrites legacy `[DATA-1]` to canonical `[DATA:1]`
    so the answer_citation_items_marker_shape CHECK accepts it
  * _citation_source_store maps citation_type onto the source_store enum
  * _extract_retrieval_rows flattens DocumentSearchResult + CollarDetails
  * _extract_citation_rows drops citations that can't satisfy the
    evidence_id-or-passage_id CHECK constraint
  * _persist_retrieval_and_citation_items writes inspectable rows
    end-to-end against the live PG schema
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from app.agent.agentic_retrieval.nodes import (
    _citation_source_store,
    _extract_citation_rows,
    _extract_retrieval_rows,
    _maybe_uuid,
    _normalise_marker,
    _persist_retrieval_and_citation_items,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.models.rag import Citation, GeoRAGResponse

PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)
TEST_WORKSPACE_ID = UUID("a0000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------


class TestMaybeUuid:
    def test_returns_str_for_valid_uuid(self) -> None:
        u = uuid.uuid4()
        assert _maybe_uuid(str(u)) == str(u)

    def test_accepts_uuid_object(self) -> None:
        u = uuid.uuid4()
        assert _maybe_uuid(u) == str(u)

    def test_rejects_non_uuid_string(self) -> None:
        assert _maybe_uuid("postgis:collars:3774-36") is None

    def test_returns_none_for_none(self) -> None:
        assert _maybe_uuid(None) is None


class TestNormaliseMarker:
    def test_passes_canonical_colon_form(self) -> None:
        assert _normalise_marker("[DATA:1]") == "[DATA:1]"
        assert _normalise_marker("[NI43:42]") == "[NI43:42]"
        assert _normalise_marker("[ev:abc-123]") == "[ev:abc-123]"

    def test_rewrites_legacy_hyphen_form(self) -> None:
        assert _normalise_marker("[DATA-1]") == "[DATA:1]"
        assert _normalise_marker("[NI43-7]") == "[NI43:7]"
        assert _normalise_marker("[PUB-12]") == "[PUB:12]"
        assert _normalise_marker("[PGEO-3]") == "[PGEO:3]"

    def test_rejects_garbage(self) -> None:
        assert _normalise_marker("DATA-1") is None         # no brackets
        assert _normalise_marker("[BAD:1]") is None        # bad prefix
        assert _normalise_marker("") is None
        assert _normalise_marker(None) is None


class TestCitationSourceStore:
    def test_maps_document_types_to_qdrant(self) -> None:
        for t in ("DATA", "NI43", "PUB", "PGEO", "data", "ni43"):
            assert _citation_source_store(t) == "qdrant"

    def test_unknown_returns_none(self) -> None:
        assert _citation_source_store(None) is None
        assert _citation_source_store("") is None
        assert _citation_source_store("XYZ") is None


# ---------------------------------------------------------------------------
# Unit tests — extractors
# ---------------------------------------------------------------------------


@dataclass
class _FakeChunk:
    """Minimal stand-in for app.agent.tools.DocumentChunk."""
    chunk_id: str
    text: str
    document_title: str
    section: str | None = None
    section_number: str | None = None
    page: int | None = None
    document_type: str | None = None
    relevance_score: float = 0.5


@dataclass
class _FakeDocumentSearchResult:
    chunks: list[_FakeChunk] = field(default_factory=list)


@dataclass
class _FakeCollarResult:
    """Stand-in for app.agent.tools.CollarDetailsResult."""
    collar_id: str
    hole_id: str
    drill_type: str | None = None
    total_depth: float | None = None
    drill_date: str | None = None


class TestExtractRetrievalRows:
    def test_document_chunks_become_qdrant_rows(self) -> None:
        chunk_id = str(uuid.uuid4())
        chunk = _FakeChunk(
            chunk_id=chunk_id,
            text="The Roll-Front sandstone dips north-east at 12°...",
            document_title="NI 43-101 Shirley Basin",
            section="Mineralization",
            page=12,
            document_type="NI43",
            relevance_score=0.87,
        )
        rows = _extract_retrieval_rows([
            ("search_documents", _FakeDocumentSearchResult(chunks=[chunk])),
        ])
        assert len(rows) == 1
        r = rows[0]
        # search_documents applies the cross-encoder reranker in-place, so
        # chunks landing here are post-rerank. The Inspector's Rerank panel
        # filters on stage='reranked' — pin the contract.
        assert r["stage"] == "reranked"
        assert r["source_store"] == "qdrant"
        assert r["passage_id"] == chunk_id        # UUID was parseable
        assert r["reranker_score"] == 0.87
        assert r["retriever_score"] is None
        assert r["candidate_ref"]["document_title"] == "NI 43-101 Shirley Basin"
        assert "Roll-Front" in r["candidate_ref"]["snippet"]
        assert r["candidate_ref"]["section"] == "Mineralization"
        assert r["candidate_ref"]["page"] == 12

    def test_collar_results_stay_in_retrieved_stage(self) -> None:
        # Direct PK lookups bypass the reranker, so they must NOT be
        # labelled 'reranked' — otherwise they'd skew the Rerank panel
        # with a synthetic 1.0 score that never came from the cross-encoder.
        result = _FakeCollarResult(collar_id="3774-36-1458", hole_id="36-1085")
        rows = _extract_retrieval_rows([("query_collar_details", result)])
        assert rows[0]["stage"] == "retrieved"
        assert rows[0]["reranker_score"] is None
        assert rows[0]["retriever_score"] == 1.0

    def test_collar_results_become_postgis_rows(self) -> None:
        result = _FakeCollarResult(
            collar_id="3774-36-1458",
            hole_id="36-1085",
            drill_type="rotary",
            total_depth=152.4,
            drill_date="1985-05-22",
        )
        rows = _extract_retrieval_rows([("query_collar_details", result)])
        assert len(rows) == 1
        r = rows[0]
        assert r["source_store"] == "postgis"
        assert r["passage_id"] is None
        assert r["candidate_ref"]["pk"] == {"collar_id": "3774-36-1458"}
        assert "36-1085" in r["candidate_ref"]["snippet"]
        assert "152.4" in r["candidate_ref"]["snippet"]

    def test_unknown_shape_is_ignored(self) -> None:
        rows = _extract_retrieval_rows([("unknown_tool", object())])
        assert rows == []

    def test_non_uuid_chunk_id_carries_via_candidate_ref(self) -> None:
        chunk = _FakeChunk(
            chunk_id="qdrant:pub:1234",
            text="snippet",
            document_title="Public-geoscience layer",
            relevance_score=0.5,
        )
        rows = _extract_retrieval_rows([
            ("search_documents", _FakeDocumentSearchResult(chunks=[chunk])),
        ])
        assert rows[0]["passage_id"] is None
        assert rows[0]["candidate_ref"]["chunk_id"] == "qdrant:pub:1234"


class TestExtractCitationRows:
    def test_only_passage_backed_citations_survive(self) -> None:
        chunk_id = str(uuid.uuid4())
        retrieval = [{
            "source_store": "qdrant",
            "passage_id": chunk_id,
            "candidate_ref": {"chunk_id": chunk_id, "document_title": "x"},
            "retriever_score": 0.6,
        }]
        citations = [
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id=chunk_id,
                document_title="x",
                relevance_score=0.8,
            ),
            # Tool-result citation with no UUID chunk — must be dropped.
            Citation(
                citation_id="[DATA-2]",
                citation_type="DATA",
                source_chunk_id="postgis:collars:3774",
                document_title="hole",
                relevance_score=0.5,
            ),
        ]
        rows = _extract_citation_rows(citations, retrieval)
        assert len(rows) == 1
        assert rows[0]["marker_text"] == "[DATA:1]"  # normalised
        assert rows[0]["passage_id"] == chunk_id
        assert rows[0]["source_store"] == "qdrant"
        assert rows[0]["confidence"] == 0.8

    def test_dedupes_repeated_markers(self) -> None:
        chunk_id = str(uuid.uuid4())
        retrieval = [{
            "passage_id": chunk_id,
            "candidate_ref": {"chunk_id": chunk_id},
        }]
        citations = [
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id=chunk_id,
                document_title="x",
                relevance_score=0.9,
            ),
            Citation(
                citation_id="[DATA-1]",  # same marker twice in answer text
                citation_type="DATA",
                source_chunk_id=chunk_id,
                document_title="x",
                relevance_score=0.9,
            ),
        ]
        assert len(_extract_citation_rows(citations, retrieval)) == 1


# ---------------------------------------------------------------------------
# Integration — write actual rows against live PG
# ---------------------------------------------------------------------------


pytestmark_integration = pytest.mark.integration


@pytest.fixture
async def pg_pool():
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@dataclass
class _DepsStub:
    pg_pool: Any
    workspace_id: str | None = None
    project_id: str | None = None


async def _seed_parent_run(pool) -> UUID:
    """Insert a minimal answer_runs row so FK from child tables resolves."""
    async with pool.acquire() as conn:
        rec = await conn.fetchrow(
            "INSERT INTO silver.answer_runs ("
            "  workspace_id, query_text, query_class, "
            "  workspace_data_version_at_query"
            ") VALUES ($1::uuid, $2, 'factual', 1) "
            "RETURNING answer_run_id",
            str(TEST_WORKSPACE_ID),
            "child-write integration test",
        )
    return UUID(str(rec["answer_run_id"]))


async def _seed_passage(pool) -> UUID:
    """Seed a silver.document_passages row so passage_id FK INSERTs succeed."""
    pid = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO silver.document_passages ("
            "  passage_id, workspace_id, revision_number, text, "
            "  text_hash, ordinal"
            ") VALUES ($1::uuid, $2::uuid, 1, $3, $4, 1)",
            str(pid),
            str(TEST_WORKSPACE_ID),
            "Roll-Front sandstone hosts the deposit.",
            "a" * 64,  # SHA-256 hex; column is CHAR(64) NOT NULL
        )
    return pid


async def _cleanup(pool, run_id: UUID, passage_id: UUID | None = None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM silver.answer_runs WHERE answer_run_id = $1::uuid",
            str(run_id),
        )
        if passage_id is not None:
            await conn.execute(
                "DELETE FROM silver.document_passages "
                " WHERE passage_id = $1::uuid",
                str(passage_id),
            )


@pytestmark_integration
@pytest.mark.asyncio
async def test_persist_writes_qdrant_retrieval_and_citation_rows(pg_pool):
    """End-to-end: chunk → retrieval row + matching citation row."""
    run_id = await _seed_parent_run(pg_pool)
    passage_id = await _seed_passage(pg_pool)
    try:
        chunk_id = str(passage_id)
        chunk = _FakeChunk(
            chunk_id=chunk_id,
            text="Roll-Front sandstone hosts the deposit.",
            document_title="NI 43-101 Shirley Basin",
            section="Geology",
            page=24,
            document_type="NI43",
            relevance_score=0.78,
        )
        response = GeoRAGResponse(
            text="The deposit is hosted in roll-front sandstone.",
            citations=[
                Citation(
                    citation_id="[NI43-1]",  # legacy hyphen form
                    citation_type="NI43",
                    source_chunk_id=chunk_id,
                    document_title="NI 43-101 Shirley Basin",
                    relevance_score=0.78,
                )
            ],
            confidence=0.78,
            sources_used=[chunk_id],
        )
        state = AgenticRetrievalState(
            query="what hosts the deposit",
            deps=_DepsStub(
                pg_pool=pg_pool, workspace_id=str(TEST_WORKSPACE_ID)
            ),
            response=response,
            tool_results=[(
                "search_documents",
                _FakeDocumentSearchResult(chunks=[chunk]),
            )],
        )

        retr, cite = await _persist_retrieval_and_citation_items(
            pg_pool=pg_pool,
            answer_run_id=str(run_id),
            workspace_id=str(TEST_WORKSPACE_ID),
            state=state,
        )
        assert retr == 1
        assert cite == 1

        async with pg_pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT stage, source_store, passage_id::text AS passage_id, "
                "       candidate_ref, retriever_score::float8 AS rs, "
                "       reranker_score::float8 AS rrk, "
                "       used_in_citation, included_in_context "
                "  FROM silver.answer_retrieval_items "
                " WHERE answer_run_id = $1::uuid",
                str(run_id),
            )
            # Chunks come out of search_documents post-rerank; the Rerank
            # panel reads on stage='reranked'.
            assert r["stage"] == "reranked"
            assert r["source_store"] == "qdrant"
            assert r["passage_id"] == chunk_id
            assert r["used_in_citation"] is True
            assert r["included_in_context"] is True
            assert r["rrk"] == pytest.approx(0.78, abs=1e-4)
            assert r["rs"] is None  # retriever score is N/A post-rerank

            c = await conn.fetchrow(
                "SELECT marker_text, source_store, "
                "       passage_id::text AS passage_id, "
                "       confidence::float8 AS conf "
                "  FROM silver.answer_citation_items "
                " WHERE answer_run_id = $1::uuid",
                str(run_id),
            )
            assert c["marker_text"] == "[NI43:1]"  # canonical form
            assert c["source_store"] == "qdrant"
            assert c["passage_id"] == chunk_id
            assert c["conf"] == pytest.approx(0.78, abs=1e-4)
    finally:
        await _cleanup(pg_pool, run_id, passage_id)


@pytestmark_integration
@pytest.mark.asyncio
async def test_persist_writes_postgis_collar_row(pg_pool):
    """Collar lookups land as postgis candidate_ref rows (no passage_id)."""
    run_id = await _seed_parent_run(pg_pool)
    try:
        result = _FakeCollarResult(
            collar_id="3774-36-1458",
            hole_id="36-1085",
            drill_type="rotary",
            total_depth=152.4,
            drill_date="1985-05-22",
        )
        response = GeoRAGResponse(
            text="Hole 36-1085 was rotary-drilled to 152.4 m.",
            citations=[
                Citation(
                    citation_id="[DATA-1]",
                    citation_type="DATA",
                    source_chunk_id="postgis:collars:3774-36-1458",
                    document_title="Hole 36-1085",
                    relevance_score=1.0,
                )
            ],
            confidence=0.85,
            sources_used=["postgis:collars:3774-36-1458"],
        )
        state = AgenticRetrievalState(
            query="tell me about hole 36-1085",
            deps=_DepsStub(
                pg_pool=pg_pool, workspace_id=str(TEST_WORKSPACE_ID)
            ),
            response=response,
            tool_results=[("query_collar_details", result)],
        )

        retr, cite = await _persist_retrieval_and_citation_items(
            pg_pool=pg_pool,
            answer_run_id=str(run_id),
            workspace_id=str(TEST_WORKSPACE_ID),
            state=state,
        )
        assert retr == 1
        # Citation backed by non-passage tool result → CHECK constraint
        # would reject it, so the extractor drops it. We accept 0 here
        # rather than fabricate evidence_id rows.
        assert cite == 0

        async with pg_pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT source_store, passage_id, candidate_ref "
                "  FROM silver.answer_retrieval_items "
                " WHERE answer_run_id = $1::uuid",
                str(run_id),
            )
            assert r["source_store"] == "postgis"
            assert r["passage_id"] is None
            import json
            ref = json.loads(r["candidate_ref"])
            assert ref["pk"]["collar_id"] == "3774-36-1458"
    finally:
        await _cleanup(pg_pool, run_id)


@pytestmark_integration
@pytest.mark.asyncio
async def test_persist_falls_back_to_null_passage_on_missing_fk(pg_pool):
    """Chunk pointing at a nonexistent passage → row still lands, passage_id NULL."""
    run_id = await _seed_parent_run(pg_pool)
    try:
        ghost_chunk_id = str(uuid.uuid4())  # NOT seeded in document_passages
        chunk = _FakeChunk(
            chunk_id=ghost_chunk_id,
            text="orphaned chunk",
            document_title="Unknown document",
            relevance_score=0.42,
        )
        response = GeoRAGResponse(
            text="answer",
            citations=[
                Citation(
                    citation_id="[DATA-1]",
                    citation_type="DATA",
                    source_chunk_id=ghost_chunk_id,
                    document_title="Unknown document",
                    relevance_score=0.42,
                )
            ],
            confidence=0.42,
            sources_used=[ghost_chunk_id],
        )
        state = AgenticRetrievalState(
            query="orphan",
            deps=_DepsStub(
                pg_pool=pg_pool, workspace_id=str(TEST_WORKSPACE_ID)
            ),
            response=response,
            tool_results=[(
                "search_documents",
                _FakeDocumentSearchResult(chunks=[chunk]),
            )],
        )
        retr, cite = await _persist_retrieval_and_citation_items(
            pg_pool=pg_pool,
            answer_run_id=str(run_id),
            workspace_id=str(TEST_WORKSPACE_ID),
            state=state,
        )
        # Retrieval row lands with NULL passage_id (graceful degradation).
        assert retr == 1
        # Citation dropped — the CHECK constraint forbids
        # evidence_id=NULL + passage_id=NULL, so the missing FK leaves
        # nothing valid to insert.
        assert cite == 0

        async with pg_pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT passage_id, candidate_ref "
                "  FROM silver.answer_retrieval_items "
                " WHERE answer_run_id = $1::uuid",
                str(run_id),
            )
            assert r["passage_id"] is None
            import json
            ref = json.loads(r["candidate_ref"])
            assert ref["chunk_id"] == ghost_chunk_id
    finally:
        await _cleanup(pg_pool, run_id)


@pytestmark_integration
@pytest.mark.asyncio
async def test_persist_skips_empty_inputs(pg_pool):
    """Nothing to write → returns (0, 0) cleanly without errors."""
    run_id = await _seed_parent_run(pg_pool)
    try:
        state = AgenticRetrievalState(
            query="empty",
            deps=_DepsStub(
                pg_pool=pg_pool, workspace_id=str(TEST_WORKSPACE_ID)
            ),
            response=None,
            tool_results=[],
        )
        retr, cite = await _persist_retrieval_and_citation_items(
            pg_pool=pg_pool,
            answer_run_id=str(run_id),
            workspace_id=str(TEST_WORKSPACE_ID),
            state=state,
        )
        assert (retr, cite) == (0, 0)
    finally:
        await _cleanup(pg_pool, run_id)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
