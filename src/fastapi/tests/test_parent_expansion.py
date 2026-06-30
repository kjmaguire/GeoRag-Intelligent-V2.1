"""Unit tests for plan §3d parent expansion."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agent.evidence import (
    AssayEvidence,
    DocumentEvidence,
    EvidencePacket,
)
from app.agent.parent_expansion import (
    expand_parents,
    expand_parents_sync,
    fetch_parent_chunks,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _child(
    *,
    chunk_id: str,
    parent_id: str | None,
    title: str = "T",
    text: str = "child text",
    authority_rank: int = 1,
) -> DocumentEvidence:
    return DocumentEvidence(
        document_id="d-1",
        document_title=title,
        document_type="NI 43-101",
        authority_rank=authority_rank,
        is_current=True,
        confidence=1.0,
        page=1,
        chunk_id=chunk_id,
        parent_chunk_id=parent_id,
        text=text,
        char_start=0,
        char_end=len(text),
    )


def _packet(evidence) -> EvidencePacket:
    return EvidencePacket(
        query_id="q-1",
        query_text="x",
        evidence=evidence,
        total_tokens=10,
        system_prompt_tokens=0,
        remaining_budget=5000,
    )


# ---------------------------------------------------------------------------
# Sync merge — happy path
# ---------------------------------------------------------------------------


def test_appends_parent_as_sibling_evidence():
    child = _child(chunk_id="c1", parent_id="p1")
    parents = {
        "p1": {
            "chunk_id": "p1",
            "document_id": "d-1",
            "text": "wider parent section context",
            "ordinal": 0,
            "page_first": 1,
            "page_last": 2,
            "chunk_kind": "section",
        },
    }
    packet = _packet([child])
    result = expand_parents_sync(packet, parents_by_id=parents)

    assert result.parents_added == 1
    assert result.parents_skipped == 0
    assert result.parents_failed == 0
    # Now 2 evidence: child + parent.
    assert len(result.packet.evidence) == 2
    parent = result.packet.evidence[1]
    assert isinstance(parent, DocumentEvidence)
    assert parent.chunk_id == "p1"
    assert parent.text == "wider parent section context"


def test_parent_inherits_document_metadata_from_child():
    child = _child(
        chunk_id="c1", parent_id="p1",
        title="NI 43-101 Crackingstone", authority_rank=1,
    )
    parents = {"p1": {"text": "parent text", "page_first": 5}}
    result = expand_parents_sync(_packet([child]), parents_by_id=parents)

    parent = result.packet.evidence[1]
    assert parent.document_title == "NI 43-101 Crackingstone"
    assert parent.authority_rank == 1


def test_parent_confidence_is_slightly_lower_than_child():
    """Parents are wider context — useful but slightly less precise.
    Multiply child confidence by 0.9 as a conservative penalty."""
    child = _child(chunk_id="c1", parent_id="p1")
    parents = {"p1": {"text": "p"}}
    result = expand_parents_sync(_packet([child]), parents_by_id=parents)
    parent = result.packet.evidence[1]
    # 1.0 * 0.9 = 0.9
    assert abs(parent.confidence - 0.9) < 1e-9


def test_parent_chunk_id_is_null_on_expanded_parent_to_prevent_recursion():
    """The expanded parent must not point back at another parent — if
    the expander runs again on the same packet, it shouldn't loop."""
    child = _child(chunk_id="c1", parent_id="p1")
    parents = {"p1": {"text": "p"}}
    result = expand_parents_sync(_packet([child]), parents_by_id=parents)
    parent = result.packet.evidence[1]
    assert parent.parent_chunk_id is None


# ---------------------------------------------------------------------------
# Skip + fail paths
# ---------------------------------------------------------------------------


def test_skips_when_no_parent_chunk_id():
    """A child with parent_chunk_id=None contributes 0 to expansion."""
    flat = _child(chunk_id="c1", parent_id=None)
    result = expand_parents_sync(_packet([flat]), parents_by_id={})
    assert result.parents_added == 0
    assert result.parents_skipped == 0
    assert result.parents_failed == 0
    assert len(result.packet.evidence) == 1  # unchanged


def test_skips_when_parent_already_in_packet():
    """If the parent itself was retrieved + is already in the packet,
    don't double-add."""
    parent_in_packet = _child(chunk_id="p1", parent_id=None, text="parent already here")
    child = _child(chunk_id="c1", parent_id="p1")
    parents = {"p1": {"text": "parent already here"}}
    result = expand_parents_sync(
        _packet([parent_in_packet, child]),
        parents_by_id=parents,
    )
    assert result.parents_added == 0
    assert result.parents_skipped == 1


def test_skips_duplicate_parents_across_siblings():
    """When two children share the same parent, only one parent is added."""
    c1 = _child(chunk_id="c1", parent_id="p1")
    c2 = _child(chunk_id="c2", parent_id="p1")
    parents = {"p1": {"text": "shared parent"}}
    result = expand_parents_sync(_packet([c1, c2]), parents_by_id=parents)
    assert result.parents_added == 1
    assert result.parents_skipped == 1


def test_parent_lookup_miss_counts_as_failed():
    """Child has parent_chunk_id but the parent row wasn't fetched.
    Counts as failed, NOT skipped."""
    child = _child(chunk_id="c1", parent_id="p1")
    result = expand_parents_sync(_packet([child]), parents_by_id={})
    assert result.parents_added == 0
    assert result.parents_failed == 1


def test_parent_with_empty_text_counts_as_failed():
    """A parent row with empty text isn't useful to merge."""
    child = _child(chunk_id="c1", parent_id="p1")
    parents = {"p1": {"text": ""}}
    result = expand_parents_sync(_packet([child]), parents_by_id=parents)
    assert result.parents_failed == 1
    assert result.parents_added == 0


# ---------------------------------------------------------------------------
# Cap
# ---------------------------------------------------------------------------


def test_max_parents_per_packet_caps_additions():
    """Stop after max_parents_per_packet even with more children eligible."""
    children = [
        _child(chunk_id=f"c{i}", parent_id=f"p{i}")
        for i in range(10)
    ]
    parents = {f"p{i}": {"text": f"parent {i}"} for i in range(10)}
    result = expand_parents_sync(
        _packet(children),
        parents_by_id=parents,
        max_parents_per_packet=3,
    )
    assert result.parents_added == 3
    assert len(result.packet.evidence) == 13  # 10 children + 3 parents


# ---------------------------------------------------------------------------
# Empty packet + non-document evidence
# ---------------------------------------------------------------------------


def test_empty_packet_returns_unchanged():
    packet = _packet([])
    result = expand_parents_sync(packet, parents_by_id={})
    assert result.parents_added == 0
    assert result.packet is packet


def test_assay_evidence_alone_is_skipped():
    """Only DocumentEvidence has parent_chunk_id."""
    assay = AssayEvidence(
        project_id="p", hole_id="X",
        depth_from_m=0.0, depth_to_m=10.0, interval_length_m=10.0,
        commodity="Au", value=1.0, unit="g/t",
    )
    result = expand_parents_sync(_packet([assay]), parents_by_id={})
    assert result.parents_added == 0
    assert len(result.packet.evidence) == 1


# ---------------------------------------------------------------------------
# Budget recompute
# ---------------------------------------------------------------------------


def test_adding_parent_grows_total_tokens_and_shrinks_remaining_budget():
    child = _child(chunk_id="c1", parent_id="p1", text="x" * 40)
    parents = {"p1": {"text": "x" * 100}}
    packet = _packet([child])
    result = expand_parents_sync(packet, parents_by_id=parents)
    assert result.packet.total_tokens > packet.total_tokens
    assert result.packet.remaining_budget < packet.remaining_budget
    # Invariant: total + remaining sums to a constant (no system_prompt change).
    assert (
        result.packet.total_tokens + result.packet.remaining_budget
        == packet.total_tokens + packet.remaining_budget
    )


# ---------------------------------------------------------------------------
# Async wrapper — mocked pool
# ---------------------------------------------------------------------------


class _MockConn:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.execute_calls: list = []
        self.fetch_calls: list = []

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return self.rows

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield None
        return _tx()


class _MockPool:
    def __init__(self, conn: _MockConn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _ctx():
            yield self.conn
        return _ctx()


@pytest.mark.asyncio
async def test_fetch_parent_chunks_dedupes_input_ids():
    conn = _MockConn(rows=[{"chunk_id": "p1", "text": "p", "page_first": 1}])
    pool = _MockPool(conn)
    # Send the same id three times.
    await fetch_parent_chunks(
        pool, workspace_id="ws-1", parent_chunk_ids=["p1", "p1", "p1"],
    )
    # The SQL fetch was called once with a single-element array.
    assert len(conn.fetch_calls) == 1
    _sql, args = conn.fetch_calls[0]
    assert len(args[0]) == 1


@pytest.mark.asyncio
async def test_fetch_parent_chunks_empty_input_short_circuits():
    pool = _MockPool(_MockConn())
    result = await fetch_parent_chunks(
        pool, workspace_id="ws-1", parent_chunk_ids=[],
    )
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_parent_chunks_requires_workspace_id():
    pool = _MockPool(_MockConn())
    with pytest.raises(ValueError, match="workspace_id is required"):
        await fetch_parent_chunks(
            pool, workspace_id="", parent_chunk_ids=["p1"],
        )


@pytest.mark.asyncio
async def test_fetch_parent_chunks_swallows_db_error():
    class _BrokenPool:
        def acquire(self):
            @asynccontextmanager
            async def _ctx():
                raise RuntimeError("pool failed")
                yield  # unreachable

            return _ctx()

    result = await fetch_parent_chunks(
        _BrokenPool(), workspace_id="ws-1", parent_chunk_ids=["p1"],
    )
    assert result == {}


@pytest.mark.asyncio
async def test_expand_parents_async_no_op_when_no_parent_ids():
    pool = _MockPool(_MockConn())
    flat_child = _child(chunk_id="c1", parent_id=None)
    result = await expand_parents(
        _packet([flat_child]),
        pool,
        workspace_id="ws-1",
    )
    assert result.parents_added == 0
    assert result.reason and "no parent_chunk_ids" in result.reason


@pytest.mark.asyncio
async def test_expand_parents_async_happy_path():
    rows = [
        {
            "chunk_id": "p1",
            "document_id": "d-1",
            "text": "parent text",
            "ordinal": 0,
            "page_first": 5,
            "page_last": 6,
            "chunk_kind": "section",
        },
    ]
    pool = _MockPool(_MockConn(rows=rows))
    child = _child(chunk_id="c1", parent_id="p1")
    result = await expand_parents(
        _packet([child]),
        pool,
        workspace_id="ws-1",
    )
    assert result.parents_added == 1
    assert len(result.packet.evidence) == 2
