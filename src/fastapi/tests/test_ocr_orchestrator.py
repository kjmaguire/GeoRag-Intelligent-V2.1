"""§04p orchestrator behaviour tests (master-plan §3 Step 7a, doc-phase 55).

Asserts the orchestrator chains preflight → profile → parse → route →
summarize correctly on the PLS-2024 native fixture.

Tests are integration-level (touch real parsers, real PDF) but do NOT
touch the database — persistence is doc-phase 56's job.

Wall-time budget: ~30-45 sec (Docling cold-load avoided for native
profile; only pdfminer + pdfplumber + quality_graph run).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ocr"
PLS_2024 = FIXTURE_DIR / "PLS-2024-Technical-Report.pdf"


@pytest.fixture(scope="module")
def native_pdf_path() -> Path:
    if not PLS_2024.exists():
        pytest.skip(f"fixture not found: {PLS_2024}")
    return PLS_2024


@pytest.fixture(scope="module")
def orchestrator_result(native_pdf_path: Path) -> dict:
    from app.ocr._orchestrator import orchestrate

    return asyncio.run(orchestrate(native_pdf_path))


# ----- preflight gating -----

def test_orchestrate_bails_on_invalid_preflight(tmp_path: Path) -> None:
    from app.ocr._orchestrator import orchestrate

    not_pdf = tmp_path / "not-a-pdf.pdf"
    not_pdf.write_bytes(b"This is plainly not a PDF.")

    result = asyncio.run(orchestrate(not_pdf))

    assert result["preflight"]["valid"] is False
    assert result["profile"] is None
    assert result["document_summary"]["recommended_action"] == "reject"
    assert all(p is None for p in result["parses"].values())


def test_orchestrate_bails_on_missing_file(tmp_path: Path) -> None:
    from app.ocr._orchestrator import orchestrate

    result = asyncio.run(orchestrate(tmp_path / "missing.pdf"))

    assert result["preflight"]["valid"] is False
    assert result["document_summary"]["recommended_action"] == "reject"


# ----- happy path on PLS-2024 -----

def test_orchestrate_runs_preflight_then_profile(orchestrator_result: dict) -> None:
    assert orchestrator_result["preflight"]["valid"] is True
    assert orchestrator_result["preflight"]["page_count"] > 0
    assert orchestrator_result["profile"] is not None
    assert orchestrator_result["profile"]["document_profile"] == "native"


def test_orchestrate_dispatches_native_parser_for_native_doc(
    orchestrator_result: dict,
) -> None:
    parses = orchestrator_result["parses"]
    assert parses["native"] is not None
    assert parses["scanned"] is None
    assert parses["mixed"] is None
    assert parses["table_heavy"] is None
    # Native parser produced passages
    assert len(parses["native"]["passages"]) > 0


def test_orchestrate_routes_every_page(orchestrator_result: dict) -> None:
    page_count = orchestrator_result["preflight"]["page_count"]
    assert len(orchestrator_result["route_decisions"]) == page_count
    # Every decision has page + route fields
    for decision in orchestrator_result["route_decisions"]:
        assert "page" in decision
        assert decision["route"] in {"accept", "re_ocr", "silver_review", "reject"}


def test_orchestrate_summary_recommends_accept_for_native_doc(
    orchestrator_result: dict,
) -> None:
    summary = orchestrator_result["document_summary"]
    # Native PDF with high-confidence pdfminer.six extraction should
    # accept cleanly. (Some pages might be blank — that's allowed by
    # accept_with_review.)
    assert summary["recommended_action"] in {"accept", "accept_with_review"}
    assert summary["reject_count"] == 0


def test_orchestrate_no_retries_for_clean_native_doc(
    orchestrator_result: dict,
) -> None:
    # PLS-2024 is a clean native PDF; no OCR retries should fire.
    assert orchestrator_result["retry_log"] == []


# ----- helpers smoke -----

def test_parse_result_for_page_picks_right_parser() -> None:
    from app.ocr._orchestrator import _parse_result_for_page

    parses = {
        "native": {"parser_used": "native", "id": 1},
        "scanned": {"parser_used": "scanned_paddleocr", "id": 2},
        "mixed": {"parser_used": "mixed_docling", "id": 3},
        "table_heavy": None,
    }

    assert _parse_result_for_page(parses, "native", 0, "native")["id"] == 1
    assert _parse_result_for_page(parses, "scanned", 0, "scanned")["id"] == 2
    assert _parse_result_for_page(parses, "mixed", 0, "mixed")["id"] == 3
    # Map-heavy returns empty (no parser dispatched)
    assert _parse_result_for_page(parses, "map_heavy", 0, "mixed") == {}
    # Table-heavy falls back to native if no table_heavy parse ran
    assert _parse_result_for_page(parses, "table_heavy", 0, "native")["id"] == 1


def test_merge_retry_replaces_page_data() -> None:
    from app.ocr._orchestrator import _merge_retry_into_parse

    base = {
        "passages": [
            {"page": 0, "region": 0, "text_content": "old0"},
            {"page": 1, "region": 0, "text_content": "page1"},
        ],
        "per_page_ocr_confidence": [0.5, 0.9, 0.9],
        "per_page_text_line_counts": [3, 8, 8],
        "per_page_retry_counts": [0, 0, 0],
        "per_page_deskew_applied": [True, True, True],
        "per_page_rotation_applied": [0.0, 0.0, 0.0],
    }
    retry = {
        "passages": [{"page": 0, "region": 0, "text_content": "new0"}],
        "per_page_ocr_confidence": [0.92],
        "per_page_text_line_counts": [5],
        "per_page_retry_counts": [0],
        "per_page_deskew_applied": [True],
        "per_page_rotation_applied": [0.0],
    }
    merged = _merge_retry_into_parse(base, retry, page_idx=0)

    # Page 0 confidence replaced
    assert merged["per_page_ocr_confidence"][0] == 0.92
    # Other pages untouched
    assert merged["per_page_ocr_confidence"][1] == 0.9
    # Retry count incremented (not replaced with retry's 0)
    assert merged["per_page_retry_counts"][0] == 1
    # Page 0 passages replaced; page 1 preserved
    page_0 = [p for p in merged["passages"] if p["page"] == 0]
    page_1 = [p for p in merged["passages"] if p["page"] == 1]
    assert len(page_0) == 1
    assert page_0[0]["text_content"] == "new0"
    assert len(page_1) == 1
    assert page_1[0]["text_content"] == "page1"
