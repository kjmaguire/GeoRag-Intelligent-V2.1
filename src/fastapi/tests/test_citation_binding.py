"""Unit tests for Module 6 Phase B Chunk 2 — Stage 1: evidence binding.

Tests cover:
  - bind_evidence: tool_results → BoundEvidenceSet with correct marker shapes
  - bind_evidence: DATA markers for spatial/graph/assay tools
  - bind_evidence: NI43/PUB markers for document search results
  - bind_evidence: PGEO markers, one per record
  - bind_evidence: evidence_items present → [ev:<id>] markers
  - bind_evidence: empty tool_results → empty set
  - bind_evidence: collision handling in [ev:<id>] short UUIDs
  - BoundEvidenceSet.get: hit + miss
  - render_evidence_block: produces non-empty string when bindings exist
  - render_evidence_block: returns empty string when no bindings
  - _short_ev_id: produces 8-char hex without hyphens
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.agent.citation_binding import (
    BoundEvidence,
    BoundEvidenceSet,
    bind_evidence,
    render_evidence_block,
    _short_ev_id,
)


# ---------------------------------------------------------------------------
# Minimal stub result objects
# ---------------------------------------------------------------------------

@dataclass
class _Chunk:
    text: str
    document_type: str = "NI43"
    chunk_id: str = ""


@dataclass
class _DocResult:
    chunks: list[_Chunk]
    data_source: str = "qdrant"


@dataclass
class _SpatialResult:
    collars: list[str]
    data_source: str = "postgis"


@dataclass
class _GraphResult:
    entities: list[str]
    data_source: str = "neo4j"


@dataclass
class _PGEORecord:
    text: str
    abstract: str = ""


@dataclass
class _PGEOResult:
    records: list[_PGEORecord]
    data_source: str = "hybrid"


@dataclass
class _EvidenceItem:
    evidence_id: UUID
    evidence_type: str = "passage"
    passage_id: UUID | None = None
    preview_text: str = ""


# ---------------------------------------------------------------------------
# _short_ev_id
# ---------------------------------------------------------------------------

def test_short_ev_id_length():
    """_short_ev_id returns exactly 8 lowercase hex chars."""
    uid = UUID("019d74a7-0000-0000-0000-000000000001")
    result = _short_ev_id(uid)
    assert len(result) == 8
    assert result == "019d74a7"


def test_short_ev_id_no_hyphens():
    """_short_ev_id strips hyphens from UUID hex."""
    uid = UUID("abcdef01-0000-0000-0000-000000000000")
    result = _short_ev_id(uid)
    assert "-" not in result
    assert result == "abcdef01"


# ---------------------------------------------------------------------------
# bind_evidence — basic tool-slot markers
# ---------------------------------------------------------------------------

def test_bind_evidence_spatial_produces_data_marker():
    """Spatial tool results get [DATA:N] markers."""
    tool_results = [
        ("query_spatial_collars", _SpatialResult(collars=["COL-001", "COL-002"])),
    ]
    ws_id = UUID("a0000000-0000-0000-0000-000000000001")
    bound = bind_evidence(workspace_id=ws_id, tool_results=tool_results)

    assert len(bound.bindings) == 1
    b = bound.bindings[0]
    assert b.marker_text == "[DATA:1]"
    assert b.kind == "DATA"
    assert b.index_or_id == "1"
    assert b.source_store == "postgis"


def test_bind_evidence_graph_produces_data_marker():
    """Graph traversal tool results get [DATA:N] markers."""
    tool_results = [
        ("traverse_knowledge_graph", _GraphResult(entities=["Triple R deposit"])),
    ]
    ws_id = UUID("a0000000-0000-0000-0000-000000000001")
    bound = bind_evidence(workspace_id=ws_id, tool_results=tool_results)

    assert len(bound.bindings) == 1
    assert bound.bindings[0].marker_text == "[DATA:1]"
    assert bound.bindings[0].source_store == "neo4j"


def test_bind_evidence_document_ni43_produces_ni43_marker():
    """NI 43-101 document chunks get [NI43:N] markers."""
    tool_results = [
        ("search_documents", _DocResult(chunks=[_Chunk("Some NI43 text", "NI43")])),
    ]
    ws_id = UUID("a0000000-0000-0000-0000-000000000001")
    bound = bind_evidence(workspace_id=ws_id, tool_results=tool_results)

    assert len(bound.bindings) == 1
    b = bound.bindings[0]
    assert b.marker_text == "[NI43:1]"
    assert b.kind == "NI43"
    assert b.source_store == "qdrant"


def test_bind_evidence_document_pub_produces_pub_marker():
    """Publication chunks get [PUB:N] markers."""
    tool_results = [
        ("search_documents", _DocResult(chunks=[_Chunk("Published paper text", "PUB")])),
    ]
    ws_id = UUID("a0000000-0000-0000-0000-000000000001")
    bound = bind_evidence(workspace_id=ws_id, tool_results=tool_results)

    assert bound.bindings[0].marker_text == "[PUB:1]"
    assert bound.bindings[0].kind == "PUB"


def test_bind_evidence_pgeo_one_per_record():
    """PublicGeoscience results get one [PGEO:N] binding per record."""
    records = [_PGEORecord("Record A"), _PGEORecord("Record B"), _PGEORecord("Record C")]
    tool_results = [
        ("search_public_geoscience", _PGEOResult(records=records)),
    ]
    ws_id = UUID("a0000000-0000-0000-0000-000000000001")
    bound = bind_evidence(workspace_id=ws_id, tool_results=tool_results)

    assert len(bound.bindings) == 3
    assert bound.bindings[0].marker_text == "[PGEO:1]"
    assert bound.bindings[1].marker_text == "[PGEO:2]"
    assert bound.bindings[2].marker_text == "[PGEO:3]"
    for b in bound.bindings:
        assert b.kind == "PGEO"
        assert b.source_store == "hybrid"


def test_bind_evidence_mixed_tool_results_counter_shared():
    """Counter increments across all tool types in order."""
    tool_results = [
        ("query_spatial_collars", _SpatialResult(collars=["COL-001"])),
        ("search_documents", _DocResult(chunks=[_Chunk("NI43 text", "NI43")])),
        ("search_public_geoscience", _PGEOResult(records=[_PGEORecord("R1"), _PGEORecord("R2")])),
        ("traverse_knowledge_graph", _GraphResult(entities=["Entity1"])),
    ]
    ws_id = UUID("a0000000-0000-0000-0000-000000000001")
    bound = bind_evidence(workspace_id=ws_id, tool_results=tool_results)

    # Counter: DATA:1, NI43:2, PGEO:3, PGEO:4, DATA:5
    markers = [b.marker_text for b in bound.bindings]
    assert markers == ["[DATA:1]", "[NI43:2]", "[PGEO:3]", "[PGEO:4]", "[DATA:5]"]


def test_bind_evidence_empty_tool_results():
    """Empty tool_results → empty BoundEvidenceSet."""
    ws_id = UUID("a0000000-0000-0000-0000-000000000001")
    bound = bind_evidence(workspace_id=ws_id, tool_results=[])

    assert len(bound.bindings) == 0
    assert len(bound.by_marker) == 0


# ---------------------------------------------------------------------------
# bind_evidence — [ev:*] markers from evidence_items
# ---------------------------------------------------------------------------

def test_bind_evidence_items_produce_ev_markers():
    """evidence_items present → [ev:<short>] bindings with evidence_id populated."""
    ev_id = UUID("019d74a7-1234-0000-0000-000000000001")
    passage_id = uuid4()
    ev = _EvidenceItem(
        evidence_id=ev_id,
        evidence_type="passage",
        passage_id=passage_id,
        preview_text="Passage preview",
    )
    ws_id = UUID("a0000000-0000-0000-0000-000000000001")
    bound = bind_evidence(workspace_id=ws_id, tool_results=[], evidence_items=[ev])

    assert len(bound.bindings) == 1
    b = bound.bindings[0]
    assert b.kind == "ev"
    assert b.evidence_id == ev_id
    assert b.passage_id == passage_id
    assert b.marker_text == f"[ev:{ev_id.hex[:8]}]"
    assert b.source_store == "qdrant"


def test_bind_evidence_items_and_tools_combined():
    """Tool-slot bindings + evidence_item bindings coexist correctly."""
    tool_results = [
        ("query_spatial_collars", _SpatialResult(collars=["COL-001"])),
    ]
    ev_id = uuid4()
    ev = _EvidenceItem(evidence_id=ev_id, passage_id=uuid4())
    ws_id = UUID("a0000000-0000-0000-0000-000000000001")
    bound = bind_evidence(workspace_id=ws_id, tool_results=tool_results, evidence_items=[ev])

    assert len(bound.bindings) == 2
    assert bound.bindings[0].kind == "DATA"
    assert bound.bindings[1].kind == "ev"


# ---------------------------------------------------------------------------
# BoundEvidenceSet.get
# ---------------------------------------------------------------------------

def test_bound_evidence_set_get_hit():
    """get() returns the correct binding for an existing marker."""
    bound = BoundEvidenceSet()
    b = BoundEvidence(
        marker_text="[DATA:1]",
        kind="DATA",
        index_or_id="1",
        source_store="postgis",
    )
    bound.add(b)
    result = bound.get("[DATA:1]")
    assert result is b


def test_bound_evidence_set_get_miss():
    """get() returns None for an absent marker."""
    bound = BoundEvidenceSet()
    assert bound.get("[DATA:99]") is None


# ---------------------------------------------------------------------------
# render_evidence_block
# ---------------------------------------------------------------------------

def test_render_evidence_block_non_empty_when_bindings_exist():
    """render_evidence_block returns a non-empty string when bindings are present."""
    tool_results = [
        ("query_spatial_collars", _SpatialResult(collars=["COL-001"])),
        ("search_documents", _DocResult(chunks=[_Chunk("Report text", "NI43")])),
    ]
    ws_id = UUID("a0000000-0000-0000-0000-000000000001")
    bound = bind_evidence(workspace_id=ws_id, tool_results=tool_results)
    block = render_evidence_block(bound)

    assert block != ""
    assert "[DATA:1]" in block
    assert "[NI43:2]" in block
    assert "Evidence Set" in block


def test_render_evidence_block_empty_when_no_bindings():
    """render_evidence_block returns empty string when no bindings."""
    bound = BoundEvidenceSet()
    assert render_evidence_block(bound) == ""
