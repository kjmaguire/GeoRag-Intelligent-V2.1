"""§04p quality graph behaviour tests (master-plan §3 Step 6, doc-phase 54).

Asserts routing logic for each profile / confidence combination.
Operates on synthetic parse_result dicts — no PDF I/O, no model
inference. Fast test suite (sub-second).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.ocr.quality_graph import (
    ACCEPT_OCR_CONFIDENCE,
    MAX_OCR_RETRIES,
    REVIEW_OCR_CONFIDENCE,
    route_page,
    summarize_document,
)


def _scanned_result(
    page: int,
    ocr_confidence: float,
    text_line_count: int,
) -> dict[str, Any]:
    """Build a minimal parse_scanned-shaped result for testing."""
    return {
        "parser_used": "scanned_paddleocr",
        "per_page_ocr_confidence": [0.0] * (page + 1),
        "per_page_text_line_counts": [0] * (page + 1),
        "per_page_retry_counts": [0] * (page + 1),
    } | {
        "per_page_ocr_confidence": [
            ocr_confidence if i == page else 0.0
            for i in range(page + 1)
        ],
        "per_page_text_line_counts": [
            text_line_count if i == page else 0
            for i in range(page + 1)
        ],
    }


def _native_result(page: int, passage_count: int) -> dict[str, Any]:
    return {
        "parser_used": "native",
        "per_page_passage_counts": [
            passage_count if i == page else 0
            for i in range(page + 1)
        ],
        "per_page_table_counts": [0] * (page + 1),
    }


def _table_heavy_result(
    page: int,
    table_count: int,
    structure_confidence: float = 0.95,
) -> dict[str, Any]:
    return {
        "parser_used": "table_heavy",
        "per_page_passage_counts": [
            1 if i == page else 0
            for i in range(page + 1)
        ],
        "per_page_table_counts": [
            table_count if i == page else 0
            for i in range(page + 1)
        ],
        "tables": [
            {
                "page": page,
                "table_id": i,
                "structure_confidence": structure_confidence,
                "cell_confidence": 1.0,
            }
            for i in range(table_count)
        ],
    }


# ----- profile-based shortcuts -----

def test_map_heavy_always_routes_to_review() -> None:
    result = asyncio.run(route_page({}, 0, "map_heavy"))
    assert result["route"] == "silver_review"
    assert result["reason"] == "map_heavy_v1_deferral"


def test_preflight_invalid_routes_to_reject() -> None:
    preflight = {"valid": False, "error": "encrypted_no_password"}
    result = asyncio.run(route_page({}, 0, "scanned", preflight=preflight))
    assert result["route"] == "reject"
    assert result["reason"] == "encrypted_section"


def test_preflight_corrupted_routes_to_reject_with_page_blank_reason() -> None:
    preflight = {"valid": False, "error": "pdf_error: bad header"}
    result = asyncio.run(route_page({}, 0, "scanned", preflight=preflight))
    assert result["route"] == "reject"
    assert result["reason"] == "page_blank_or_corrupted"


# ----- native + table_heavy routes -----

def test_native_with_passages_accepts() -> None:
    pr = _native_result(0, passage_count=12)
    result = asyncio.run(route_page(pr, 0, "native"))
    assert result["route"] == "accept"
    assert result["reason"] is None


def test_native_with_zero_passages_routes_to_review() -> None:
    pr = _native_result(0, passage_count=0)
    result = asyncio.run(route_page(pr, 0, "native"))
    assert result["route"] == "silver_review"
    assert result["reason"] == "page_blank_or_corrupted"


def test_table_heavy_high_confidence_accepts() -> None:
    pr = _table_heavy_result(0, table_count=3, structure_confidence=0.95)
    result = asyncio.run(route_page(pr, 0, "table_heavy"))
    assert result["route"] == "accept"


def test_table_heavy_low_structure_confidence_routes_to_review() -> None:
    pr = _table_heavy_result(0, table_count=2, structure_confidence=0.55)
    result = asyncio.run(route_page(pr, 0, "table_heavy"))
    assert result["route"] == "silver_review"
    assert result["reason"] == "table_confidence_below_threshold"


# ----- scanned/mixed routes -----

def test_scanned_high_confidence_accepts() -> None:
    pr = _scanned_result(0, ocr_confidence=0.95, text_line_count=20)
    result = asyncio.run(route_page(pr, 0, "scanned"))
    assert result["route"] == "accept"


def test_scanned_zero_text_lines_routes_to_review() -> None:
    pr = _scanned_result(0, ocr_confidence=0.0, text_line_count=0)
    result = asyncio.run(route_page(pr, 0, "scanned"))
    assert result["route"] == "silver_review"
    assert result["reason"] == "page_blank_or_corrupted"


def test_scanned_very_low_confidence_routes_directly_to_review() -> None:
    # Below REVIEW_OCR_CONFIDENCE → straight to review, no retry attempted
    pr = _scanned_result(0, ocr_confidence=0.30, text_line_count=10)
    result = asyncio.run(route_page(pr, 0, "scanned"))
    assert result["route"] == "silver_review"
    assert result["reason"] == "ocr_confidence_below_threshold"


def test_scanned_marginal_confidence_routes_to_re_ocr_with_retry_settings() -> None:
    # Between REVIEW_OCR_CONFIDENCE and ACCEPT_OCR_CONFIDENCE → re_ocr
    pr = _scanned_result(0, ocr_confidence=0.70, text_line_count=15)
    result = asyncio.run(route_page(pr, 0, "scanned", retry_count=0))

    assert result["route"] == "re_ocr"
    assert result["retry_settings"] is not None
    assert "render_scale" in result["retry_settings"]
    assert result["retry_count"] == 1  # incremented


def test_scanned_marginal_after_max_retries_routes_to_review() -> None:
    pr = _scanned_result(0, ocr_confidence=0.70, text_line_count=15)
    result = asyncio.run(route_page(pr, 0, "scanned", retry_count=MAX_OCR_RETRIES))

    assert result["route"] == "silver_review"
    assert result["reason"] == "retry_max_exceeded"


def test_scanned_retry_settings_escalate_across_attempts() -> None:
    pr = _scanned_result(0, ocr_confidence=0.70, text_line_count=15)

    r1 = asyncio.run(route_page(pr, 0, "scanned", retry_count=0))
    r2 = asyncio.run(route_page(pr, 0, "scanned", retry_count=1))

    assert r1["retry_settings"]["render_scale"] != r2["retry_settings"]["render_scale"]


# ----- threshold override -----

def test_thresholds_override_changes_routing() -> None:
    pr = _scanned_result(0, ocr_confidence=0.70, text_line_count=15)

    # Default: accept_ocr=0.85 → 0.70 falls into the retry band
    default_result = asyncio.run(route_page(pr, 0, "scanned"))
    assert default_result["route"] == "re_ocr"

    # Lower accept threshold → 0.70 should now accept
    custom = asyncio.run(route_page(
        pr, 0, "scanned",
        thresholds={"accept_ocr": 0.65},
    ))
    assert custom["route"] == "accept"


# ----- summarize_document -----

def test_summarize_document_all_accept() -> None:
    decisions = [
        {"route": "accept", "reason": None},
        {"route": "accept", "reason": None},
        {"route": "accept", "reason": None},
    ]
    summary = summarize_document(decisions)
    assert summary["recommended_action"] == "accept"
    assert summary["accept_count"] == 3
    assert summary["review_count"] == 0


def test_summarize_document_partial_review() -> None:
    decisions = [
        {"route": "accept", "reason": None},
        {"route": "silver_review", "reason": "ocr_confidence_below_threshold"},
        {"route": "accept", "reason": None},
    ]
    summary = summarize_document(decisions)
    assert summary["recommended_action"] == "accept_with_review"
    assert summary["review_count"] == 1
    assert summary["review_reasons"] == {"ocr_confidence_below_threshold": 1}


def test_summarize_document_all_review() -> None:
    decisions = [
        {"route": "silver_review", "reason": "map_heavy_v1_deferral"},
        {"route": "silver_review", "reason": "map_heavy_v1_deferral"},
    ]
    summary = summarize_document(decisions)
    assert summary["recommended_action"] == "review_all_pages"


def test_summarize_document_any_reject_wins() -> None:
    decisions = [
        {"route": "accept", "reason": None},
        {"route": "reject", "reason": "encrypted_section"},
        {"route": "accept", "reason": None},
    ]
    summary = summarize_document(decisions)
    assert summary["recommended_action"] == "reject"
    assert summary["reject_count"] == 1


def test_summarize_document_empty_input_accepts() -> None:
    # Pathological case — should not crash.
    summary = summarize_document([])
    assert summary["recommended_action"] == "accept"
    assert summary["total_pages"] == 0
