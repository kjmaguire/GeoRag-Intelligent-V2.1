"""Unit tests for Module 6 Phase B Chunk 2+3 — Stage 2: span resolver.

Chunk 3 changes:
  - resolve_spans is now async (pg_pool parameter for passage_id lookup).
  - All resolve_spans test functions converted to async / pytest.mark.asyncio.
  - Telemetry keys tool_slot_passage_resolved + tool_slot_unresolvable added.
  - passage_id lookup via pg_pool tested (mock pool returns a passage UUID).

Tests cover:
  - _normalize_markers: dash-form input → colon-form output, count correct
  - _normalize_markers: mixed dash + colon — only dashes rewritten
  - _normalize_markers: no markers → no rewrites, count=0
  - _normalize_markers: [ev:*] markers never rewritten (no dash form exists)
  - resolve_spans: 3 markers all resolved → 3 items + correct spans, fully_resolved=True
  - resolve_spans: 2 of 3 markers resolved → 2 items, fully_resolved=False,
                   partial_resolution_rate ≈ 0.67
  - resolve_spans: marker not in bound_set → treated as unresolved
  - resolve_spans: same marker twice in text → 1 citation_item, 2 citation_spans
  - resolve_spans: no markers → empty items + spans, telemetry populated correctly
  - resolve_spans: binding in set but no FK target → unresolved (has_target guard)
  - resolve_spans: legacy dash markers in answer → normalized then resolved
  - resolve_spans: mixed legacy dash + colon → only dash rewritten; both resolved
  - resolve_spans: telemetry keys all present (including Chunk 3 additions)
  - resolve_spans: span offsets are correct (match marker position in text)
  - resolve_spans: [ev:*] marker resolved via evidence_items binding
  - resolve_spans: invalid span (span_end <= span_start) skipped gracefully
  - resolve_spans: marker with invalid source_store → still inserted (store=None)
  - resolve_spans: NI43 binding + pg_pool → passage_id resolved via embedding_id lookup
  - resolve_spans: DATA binding → not attempted (no chunk_id, tool_slot_unresolvable++)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.agent.citation_binding import BoundEvidence, BoundEvidenceSet
from app.models.answer_run import AnswerCitationSpanCreate
from app.services.span_resolver import _normalize_markers, resolve_spans

# ---------------------------------------------------------------------------
# Helpers to build BoundEvidenceSets for tests
# ---------------------------------------------------------------------------

_WS_ID = UUID("a0000000-0000-0000-0000-000000000001")
_RUN_ID = UUID("b1111111-1111-1111-1111-111111111111")


def _make_binding(
    marker_text: str,
    kind: str = "DATA",
    store: str = "qdrant",
    evidence_id: UUID | None = None,
    passage_id: UUID | None = None,
    chunk_id: str | None = None,
) -> BoundEvidence:
    """Create a BoundEvidence with at least one FK target (for insertable items)."""
    if evidence_id is None and passage_id is None:
        passage_id = uuid4()  # default: give it a passage_id so has_target passes
    return BoundEvidence(
        marker_text=marker_text,
        kind=kind,  # type: ignore[arg-type]
        index_or_id=marker_text.split(":")[1].rstrip("]"),
        source_store=store,
        evidence_id=evidence_id,
        passage_id=passage_id,
        display_ref={"chunk_id": chunk_id} if chunk_id is not None else None,
    )


def _make_set(*markers: str, **kwargs) -> BoundEvidenceSet:
    """Build a BoundEvidenceSet from marker strings."""
    bound = BoundEvidenceSet()
    for m in markers:
        kind = m.lstrip("[").split(":")[0]
        bound.add(_make_binding(m, kind=kind))
    return bound


def _empty_binding(
    marker_text: str,
    kind: str = "DATA",
    chunk_id: str | None = None,
) -> BoundEvidence:
    """BoundEvidence with no FK targets (simulates tool-slot with no passage_id)."""
    return BoundEvidence(
        marker_text=marker_text,
        kind=kind,  # type: ignore[arg-type]
        index_or_id="1",
        source_store="postgis",
        evidence_id=None,
        passage_id=None,
        display_ref={"chunk_id": chunk_id} if chunk_id is not None else None,
    )


# ---------------------------------------------------------------------------
# _normalize_markers
# ---------------------------------------------------------------------------

def test_normalize_markers_dash_to_colon():
    """[DATA-N] → [DATA:N], count=1."""
    text = "There are 20 holes [DATA-1]."
    result, count = _normalize_markers(text)
    assert count == 1
    assert result == "There are 20 holes [DATA:1]."


def test_normalize_markers_multiple_dash():
    """Multiple dash-form markers all converted."""
    text = "Grade [DATA-1], depth [NI43-2], formation [PUB-3]."
    result, count = _normalize_markers(text)
    assert count == 3
    assert "[DATA:1]" in result
    assert "[NI43:2]" in result
    assert "[PUB:3]" in result
    assert "-" not in result.replace(" ", "")  # no dashes remain in markers


def test_normalize_markers_mixed_dash_and_colon():
    """Mixed input: only dash-form rewritten; colon-form untouched."""
    text = "First [DATA-1] second [NI43:2]."
    result, count = _normalize_markers(text)
    assert count == 1  # only [DATA-1] was dash-form
    assert "[DATA:1]" in result
    assert "[NI43:2]" in result


def test_normalize_markers_no_markers():
    """No markers → count=0, text unchanged."""
    text = "There are no citation markers here."
    result, count = _normalize_markers(text)
    assert count == 0
    assert result == text


def test_normalize_markers_pgeo_dash():
    """[PGEO-N] is also normalized."""
    text = "See [PGEO-4]."
    result, count = _normalize_markers(text)
    assert count == 1
    assert result == "See [PGEO:4]."


def test_normalize_markers_ev_marker_unchanged():
    """[ev:*] has no dash form — _normalize_markers does not touch it."""
    text = "Evidence [ev:019d74a7]."
    result, count = _normalize_markers(text)
    assert count == 0
    assert result == text


# ---------------------------------------------------------------------------
# resolve_spans — basic cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spans_three_markers_all_resolved():
    """3 unique markers all in bound_set → 3 items, fully_resolved=True."""
    bound = _make_set("[DATA:1]", "[NI43:2]", "[PGEO:3]")
    answer = "Holes count is 20 [DATA:1]. Grade is 1.2% [NI43:2]. Regional geology [PGEO:3]."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert len(items) == 3
    assert len(spans_per_item) == 3
    assert tel["markers_resolved"] == 3
    assert tel["markers_unresolved"] == 0
    assert tel["unique_markers"] == 3
    assert tel["total_markers_found"] == 3
    assert tel["fully_resolved"] is True
    assert tel["partial_resolution_rate"] == 1.0


@pytest.mark.asyncio
async def test_resolve_spans_partial_resolution():
    """2 of 3 markers resolved → partially resolved, rate ≈ 0.67."""
    # [DATA:1] and [NI43:2] in bound_set; [PGEO:3] is not
    bound = _make_set("[DATA:1]", "[NI43:2]")
    answer = "Holes [DATA:1]. Report [NI43:2]. Regional [PGEO:3]."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert len(items) == 2
    assert tel["markers_resolved"] == 2
    assert tel["markers_unresolved"] == 1
    assert tel["fully_resolved"] is False
    assert abs(tel["partial_resolution_rate"] - 2 / 3) < 0.01


@pytest.mark.asyncio
async def test_resolve_spans_marker_not_in_bound_set():
    """Marker not in bound_set → unresolved, not inserted."""
    bound = _make_set("[DATA:1]")
    answer = "Holes [DATA:1]. Unknown [DATA:99]."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    # Only [DATA:1] is resolved
    assert len(items) == 1
    assert tel["markers_unresolved"] == 1
    assert items[0].marker_text == "[DATA:1]"


@pytest.mark.asyncio
async def test_resolve_spans_same_marker_twice():
    """Same marker appearing twice → 1 item, 2 spans."""
    bound = _make_set("[DATA:1]")
    answer = "Count is 20 [DATA:1]. Also 20 [DATA:1]."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert len(items) == 1
    assert len(spans_per_item) == 1
    assert len(spans_per_item[0]) == 2  # two occurrences
    assert tel["total_markers_found"] == 2
    assert tel["unique_markers"] == 1
    assert tel["markers_resolved"] == 1


@pytest.mark.asyncio
async def test_resolve_spans_no_markers():
    """No markers in text → empty items + spans, telemetry zeroed."""
    bound = _make_set("[DATA:1]")
    answer = "The project has no annotated evidence in this text."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert items == []
    assert spans_per_item == []
    assert tel["total_markers_found"] == 0
    assert tel["unique_markers"] == 0
    assert tel["markers_resolved"] == 0
    assert tel["markers_unresolved"] == 0
    assert tel["fully_resolved"] is False  # no markers found at all
    assert tel["partial_resolution_rate"] == 0.0


@pytest.mark.asyncio
async def test_resolve_spans_binding_no_fk_target_is_unresolved():
    """Binding exists in bound_set but has no evidence_id or passage_id → unresolved.

    DATA kind without chunk_id — no passage_id lookup attempted.
    """
    bound = BoundEvidenceSet()
    bound.add(_empty_binding("[DATA:1]", kind="DATA"))  # DATA, no chunk_id
    answer = "Holes [DATA:1]."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert len(items) == 0
    assert tel["markers_unresolved"] == 1
    assert tel["fully_resolved"] is False
    assert tel["tool_slot_unresolvable"] >= 1


@pytest.mark.asyncio
async def test_resolve_spans_legacy_dash_normalized_and_resolved():
    """Legacy [DATA-N] in answer text is normalized then resolved."""
    bound = _make_set("[DATA:1]")
    answer = "Holes count is 20 [DATA-1]."  # dash-form from old model

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert len(items) == 1
    assert tel["legacy_dash_rewrites"] == 1
    assert items[0].marker_text == "[DATA:1]"


@pytest.mark.asyncio
async def test_resolve_spans_mixed_legacy_and_colon():
    """Mixed dash + colon in one answer — both resolve, dash count correct."""
    bound = _make_set("[DATA:1]", "[NI43:2]")
    answer = "Holes [DATA-1]. Report [NI43:2]."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert len(items) == 2
    assert tel["legacy_dash_rewrites"] == 1
    assert tel["markers_resolved"] == 2


# ---------------------------------------------------------------------------
# resolve_spans — span offset correctness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spans_span_offsets_correct():
    """Span offsets match the actual marker position in normalized text."""
    bound = _make_set("[DATA:1]")
    prefix = "The count is 20 "
    marker = "[DATA:1]"
    answer = prefix + marker + " more text."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert len(items) == 1
    assert len(spans_per_item[0]) == 1
    span = spans_per_item[0][0]
    normalized = tel["normalized_text"]
    assert span.span_start == normalized.index(marker)
    assert span.span_end == span.span_start + len(marker)
    assert normalized[span.span_start:span.span_end] == marker


# ---------------------------------------------------------------------------
# resolve_spans — [ev:*] markers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spans_ev_marker_resolved():
    """[ev:<id>] binding with evidence_id → resolved item with evidence_id set."""
    ev_id = uuid4()
    short = ev_id.hex[:8]
    marker_text = f"[ev:{short}]"

    bound = BoundEvidenceSet()
    bound.add(BoundEvidence(
        marker_text=marker_text,
        kind="ev",
        index_or_id=short,
        source_store="qdrant",
        evidence_id=ev_id,
        passage_id=None,
    ))

    answer = f"This finding is backed by evidence {marker_text}."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert len(items) == 1
    assert items[0].evidence_id == ev_id
    assert items[0].marker_text == marker_text
    assert items[0].passage_id is None


# ---------------------------------------------------------------------------
# resolve_spans — telemetry completeness (Chunk 3 additions included)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spans_telemetry_keys_all_present():
    """All expected telemetry keys are present in the returned dict."""
    bound = _make_set("[DATA:1]")
    answer = "Holes [DATA:1]."

    _, _, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    expected_keys = {
        "total_markers_found",
        "unique_markers",
        "markers_resolved",
        "markers_unresolved",
        "legacy_dash_rewrites",
        "fully_resolved",
        "partial_resolution_rate",
        "normalized_text",
        # Chunk 3 additions:
        "tool_slot_passage_resolved",
        "tool_slot_unresolvable",
    }
    assert expected_keys <= set(tel.keys())


@pytest.mark.asyncio
async def test_resolve_spans_telemetry_normalized_text_is_string():
    """normalized_text in telemetry is a string (safe to pass to LLM)."""
    bound = _make_set("[DATA:1]")
    answer = "Holes [DATA-1]."  # dash-form

    _, _, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert isinstance(tel["normalized_text"], str)
    assert "[DATA:1]" in tel["normalized_text"]


# ---------------------------------------------------------------------------
# resolve_spans — source_store handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spans_unknown_source_store_becomes_none():
    """Binding with invalid source_store → item inserted with source_store=None."""
    ev_id = uuid4()
    marker_text = "[DATA:1]"
    bound = BoundEvidenceSet()
    bound.add(BoundEvidence(
        marker_text=marker_text,
        kind="DATA",
        index_or_id="1",
        source_store="unknown_store",  # not in valid_stores
        evidence_id=ev_id,
        passage_id=None,
    ))
    answer = "Result [DATA:1]."

    items, _, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    # Item is created (evidence_id set), but source_store is None
    assert len(items) == 1
    assert items[0].source_store is None


# ---------------------------------------------------------------------------
# resolve_spans — spans_per_item structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spans_spans_per_item_parallel_to_items():
    """spans_per_item is parallel to items (len match)."""
    bound = _make_set("[DATA:1]", "[NI43:2]", "[PGEO:3]")
    answer = "Holes [DATA:1]. Report [NI43:2]. Regional [PGEO:3]."

    items, spans_per_item, _ = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    assert len(items) == len(spans_per_item)
    for span_list in spans_per_item:
        assert isinstance(span_list, list)
        assert all(isinstance(s, AnswerCitationSpanCreate) for s in span_list)


@pytest.mark.asyncio
async def test_resolve_spans_nil_uuid_in_spans():
    """Spans returned by resolve_spans have nil UUID as answer_citation_item_id."""
    NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")
    bound = _make_set("[DATA:1]")
    answer = "Holes [DATA:1]."

    items, spans_per_item, _ = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    for span_list in spans_per_item:
        for span in span_list:
            assert span.answer_citation_item_id == NIL_UUID


# ---------------------------------------------------------------------------
# resolve_spans — Chunk 3: passage_id lookup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spans_ni43_slot_with_chunk_id_resolves_passage():
    """NI43 tool-slot binding with chunk_id → passage_id looked up via pg_pool."""
    chunk_uuid = uuid4()
    passage_uuid = uuid4()
    marker_text = "[NI43:1]"

    # Build a binding with no FK targets but a chunk_id in display_ref.
    bound = BoundEvidenceSet()
    bound.add(BoundEvidence(
        marker_text=marker_text,
        kind="NI43",
        index_or_id="1",
        source_store="qdrant",
        evidence_id=None,
        passage_id=None,
        display_ref={"chunk_id": str(chunk_uuid)},
    ))

    # Mock a pg_pool that returns the passage_id for this chunk.
    mock_row = {"passage_id": passage_uuid}
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=mock_row)
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    answer = f"The NI 43-101 report states 500 Mlb U3O8 {marker_text}."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        pg_pool=mock_pool,
    )

    assert len(items) == 1
    assert items[0].passage_id == passage_uuid
    assert items[0].marker_text == marker_text
    assert tel["tool_slot_passage_resolved"] == 1
    assert tel["tool_slot_unresolvable"] == 0


@pytest.mark.asyncio
async def test_resolve_spans_data_slot_no_chunk_id_unresolvable():
    """DATA tool-slot binding has no chunk_id → unresolvable (expected behaviour)."""
    marker_text = "[DATA:1]"
    bound = BoundEvidenceSet()
    bound.add(_empty_binding(marker_text, kind="DATA", chunk_id=None))
    answer = f"There are 10 drill holes {marker_text}."

    items, _, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        pg_pool=None,  # pool irrelevant; DATA has no chunk_id
    )

    assert len(items) == 0  # DATA markers cannot land without passage_id
    assert tel["tool_slot_unresolvable"] >= 1
    assert tel["tool_slot_passage_resolved"] == 0


@pytest.mark.asyncio
async def test_resolve_spans_ni43_chunk_id_not_in_passages():
    """NI43 binding with chunk_id that has no matching document_passages row → unresolvable."""
    marker_text = "[NI43:1]"
    bound = BoundEvidenceSet()
    bound.add(BoundEvidence(
        marker_text=marker_text,
        kind="NI43",
        index_or_id="1",
        source_store="qdrant",
        evidence_id=None,
        passage_id=None,
        display_ref={"chunk_id": str(uuid4())},
    ))

    # pg_pool returns None (no row found).
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)  # no matching passage
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    answer = f"Report states mineral reserve {marker_text}."

    items, _, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        pg_pool=mock_pool,
    )

    assert len(items) == 0
    assert tel["tool_slot_unresolvable"] == 1
    assert tel["tool_slot_passage_resolved"] == 0
    assert tel["markers_unresolved"] == 1
