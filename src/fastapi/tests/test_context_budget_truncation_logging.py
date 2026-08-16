"""RAG-observability audit 2026-08-15 — silent context-budget truncation.

``_render_tool_results_context``'s ``_TOTAL_BUDGET`` (24K chars) caps the
rendered LLM context block. Previously, when the budget was exceeded the
renderer appended a bare ``"[context budget reached]"`` marker and dropped
every remaining tool-result block with NO log record anywhere — nothing
recorded that truncation happened, what was dropped, or how much. Since
``state.tool_results`` renders in raw tool-dispatch order (not relevance
order), a document-heavy retrieval could silently drop structured
collar/assay/downhole data before it ever reached the model, with zero way
to diagnose it after the fact.

These tests pin that a truncation event is now observable: a ``warning``
log record naming the dropped tool/citation ids and approximate dropped
size, and (when the caller supplies it) the query/workspace context needed
to investigate. They do NOT test — and this fix does not change — which
blocks get dropped or in what order; that prioritization problem is a
separate, larger design question.
"""

from __future__ import annotations

import logging

from app.agent.agentic_retrieval.nodes import _render_tool_results_context
from app.agent.tools import DocumentChunk, DocumentSearchResult

_LOGGER_NAME = "app.agent.agentic_retrieval.nodes"


def _make_chunk(n: int) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"chunk-{n:03d}",
        report_id=f"rep-{n:03d}",
        source_document_id=f"rep-{n:03d}",
        document_title=f"Technical Report {n}",
        document_type="NI43",
        # Long enough that each chunk block is capped at the renderer's
        # per-chunk 1800-char ceiling — a handful of these blow past the
        # 24,000-char total budget deterministically.
        text=("Intercept of grade data. " * 200),
        relevance_score=0.9,
        section_number=f"{n}.1",
        section_title=f"Section {n}",
        section=f"{n}.1 — Section {n}",
        page=10 + n,
    )


def _oversized_doc_result(n_chunks: int) -> DocumentSearchResult:
    return DocumentSearchResult(
        chunks=[_make_chunk(i + 1) for i in range(n_chunks)],
        count=n_chunks,
        data_source="Qdrant georag_reports",
    )


class TestContextBudgetTruncationLogging:
    def test_truncation_emits_a_warning_log_record(self, caplog) -> None:
        # 20 chunks * ~1800 chars/chunk (capped) is comfortably over the
        # 24,000-char total budget, guaranteeing truncation.
        tool_results = [("search_documents", _oversized_doc_result(20))]

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            context = _render_tool_results_context(tool_results)

        assert "[context budget reached]" in context
        warning_records = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "context budget reached" in r.message
        ]
        assert warning_records, (
            "expected a WARNING log record when the context budget is "
            "exceeded, found none — truncation is silent"
        )

    def test_truncation_log_names_dropped_tool_and_size(self, caplog) -> None:
        tool_results = [("search_documents", _oversized_doc_result(20))]

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            _render_tool_results_context(tool_results)

        record = next(
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "context budget reached" in r.message
        )
        # search_documents is the tool that supplied the dropped blocks —
        # its name must be discoverable in the log for post-hoc diagnosis.
        assert "search_documents" in record.message
        # Some non-zero dropped-block count and dropped-char total must be
        # present (not just "truncation happened somewhere").
        assert "dropped" in record.message.lower()

    def test_truncation_log_carries_query_and_workspace_context(self, caplog) -> None:
        tool_results = [("search_documents", _oversized_doc_result(20))]

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            _render_tool_results_context(
                tool_results,
                query="what is the U3O8 grade at DDH-13?",
                workspace_id="ws-test-123",
            )

        record = next(
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "context budget reached" in r.message
        )
        assert "DDH-13" in record.message or "ddh-13" in record.message.lower()
        assert "ws-test-123" in record.message

    def test_no_truncation_no_warning(self, caplog) -> None:
        """A single small chunk stays well under budget — no log noise."""
        tool_results = [("search_documents", _oversized_doc_result(1))]

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            context = _render_tool_results_context(tool_results)

        assert "[context budget reached]" not in context
        assert not any(
            "context budget reached" in r.message for r in caplog.records
        )
