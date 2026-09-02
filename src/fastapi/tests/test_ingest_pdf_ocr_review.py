"""Tests for OCR quality assessment → silver.review_queue translation."""

from __future__ import annotations

from app.hatchet_workflows.ingest_pdf import (
    _build_ocr_review_rows,
    _ocr_review_pages,
    _ocr_status_for_section,
    _stable_report_id,
)


def test_builds_review_row_from_required_assessment() -> None:
    parsed = {
        "warnings": [
            {
                "code": "ocr_quality_assessment",
                "page": 7,
                "parser_version": "2.0.0",
                "ocr_method": "document_intelligence",
                "extracted_text": "Recovered geological text",
                "tier": "mandatory_review",
                "routing_decision": "review_required",
                "reasons": ["median_confidence", "output_coverage_ratio"],
                "signals": {
                    "mean_confidence": 0.62,
                    "median_confidence": 0.58,
                    "output_coverage_ratio": 0.44,
                },
            }
        ]
    }

    rows = _build_ocr_review_rows(
        parsed,
        report_id="5c896a88-5fd8-48e3-a93f-3805490b39c5",
        workspace_id="29188735-7bb5-4262-8b3c-6a236ed90bf0",
        project_id="f96f7413-4f1e-4f84-8baf-c3bd3ea027ee",
        bronze_uri="s3://bronze/reports/project/scan.pdf",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["payload"]["page_number"] == 7
    assert row["queue_id"] == ("983be7e4-aab7-51a9-a788-57275407f6c9")
    assert row["payload"]["ocr_quality_tier"] == "mandatory_review"
    assert row["confidence_record"] == 0.62
    assert row["parser_version"] == ("pdf_report:2.0.0:document_intelligence:ocr-quality-v1")
    assert row["routing_reason"] == ("median_confidence, output_coverage_ratio")
    assert row["outlier_flags"] == [
        {"field": "ocr_quality", "reason": "median_confidence"},
        {"field": "ocr_quality", "reason": "output_coverage_ratio"},
    ]


def test_skips_auto_pass_and_unrelated_warnings() -> None:
    parsed = {
        "warnings": [
            {
                "code": "ocr_quality_assessment",
                "page": 1,
                "routing_decision": "auto_pass",
            },
            {"code": "pdf_extraction_partial", "page": 2},
        ]
    }

    rows = _build_ocr_review_rows(
        parsed,
        report_id="5c896a88-5fd8-48e3-a93f-3805490b39c5",
        workspace_id="29188735-7bb5-4262-8b3c-6a236ed90bf0",
        project_id="f96f7413-4f1e-4f84-8baf-c3bd3ea027ee",
        bronze_uri="s3://bronze/reports/project/scan.pdf",
    )

    assert rows == []


def test_review_queue_id_is_stable_across_retries() -> None:
    parsed = {
        "warnings": [
            {
                "code": "ocr_quality_assessment",
                "page": 7,
                "routing_decision": "review_required",
            }
        ]
    }
    arguments = {
        "report_id": "5c896a88-5fd8-48e3-a93f-3805490b39c5",
        "workspace_id": "29188735-7bb5-4262-8b3c-6a236ed90bf0",
        "project_id": "f96f7413-4f1e-4f84-8baf-c3bd3ea027ee",
        "bronze_uri": "s3://bronze/reports/project/scan.pdf",
    }

    first = _build_ocr_review_rows(parsed, **arguments)
    second = _build_ocr_review_rows(parsed, **arguments)

    assert first[0]["queue_id"] == second[0]["queue_id"]


def test_report_id_is_stable_across_persist_retries() -> None:
    arguments = {
        "workspace_id": "29188735-7bb5-4262-8b3c-6a236ed90bf0",
        "project_id": "f96f7413-4f1e-4f84-8baf-c3bd3ea027ee",
        "source_identity": "abc123",
    }

    assert _stable_report_id(**arguments) == _stable_report_id(**arguments)
    assert _stable_report_id(**arguments) != _stable_report_id(
        **{**arguments, "source_identity": "different"},
    )


def test_review_pages_mark_overlapping_passages_low_confidence() -> None:
    parsed = {
        "warnings": [
            {
                "code": "ocr_quality_assessment",
                "page": 7,
                "routing_decision": "review_required",
            },
            {
                "code": "ocr_quality_assessment",
                "page": 8,
                "routing_decision": "auto_pass",
            },
        ]
    }

    review_pages = _ocr_review_pages(parsed)

    assert review_pages == {7}
    assert (
        _ocr_status_for_section(
            {"page_first": 6, "page_last": 7},
            review_pages,
        )
        == "low_confidence"
    )
    assert (
        _ocr_status_for_section(
            {"page_first": 8, "page_last": 9},
            review_pages,
        )
        == "accepted"
    )


def test_invalid_or_missing_page_is_not_enqueued() -> None:
    parsed = {
        "warnings": [
            {
                "code": "ocr_quality_assessment",
                "page": 0,
                "routing_decision": "review_required",
            }
        ]
    }

    rows = _build_ocr_review_rows(
        parsed,
        report_id="5c896a88-5fd8-48e3-a93f-3805490b39c5",
        workspace_id="29188735-7bb5-4262-8b3c-6a236ed90bf0",
        project_id="f96f7413-4f1e-4f84-8baf-c3bd3ea027ee",
        bronze_uri="s3://bronze/reports/project/scan.pdf",
    )

    assert rows == []


def test_non_numeric_page_is_not_allowed_to_break_persistence() -> None:
    parsed = {
        "warnings": [
            {
                "code": "ocr_quality_assessment",
                "page": "not-a-page",
                "routing_decision": "review_required",
            }
        ]
    }

    rows = _build_ocr_review_rows(
        parsed,
        report_id="5c896a88-5fd8-48e3-a93f-3805490b39c5",
        workspace_id="29188735-7bb5-4262-8b3c-6a236ed90bf0",
        project_id="f96f7413-4f1e-4f84-8baf-c3bd3ea027ee",
        bronze_uri="s3://bronze/reports/project/scan.pdf",
    )

    assert rows == []


def test_a_page_with_no_engine_confidence_keeps_a_numeric_record_and_says_so() -> None:
    """silver.review_queue.confidence_record is NOT NULL; the signals dict carries the truth."""
    parsed = {
        "warnings": [
            {
                "code": "ocr_quality_assessment",
                "page": 3,
                "parser_version": "2.1.0",
                "ocr_method": "cohere_parse",
                "extracted_text": "qzxv wkjq pfzt",
                "tier": "mandatory_review",
                "routing_decision": "review_required",
                "reasons": ["gibberish_word_ratio"],
                "signals": {
                    "mean_confidence": 0.0,
                    "median_confidence": 0.0,
                    "gibberish_word_ratio": 1.0,
                    "confidence_reported": False,
                },
            }
        ]
    }

    rows = _build_ocr_review_rows(
        parsed,
        report_id="5c896a88-5fd8-48e3-a93f-3805490b39c5",
        workspace_id="29188735-7bb5-4262-8b3c-6a236ed90bf0",
        project_id="f96f7413-4f1e-4f84-8baf-c3bd3ea027ee",
        bronze_uri="s3://bronze/reports/project/scan.pdf",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["confidence_record"] == 0.0
    assert row["confidence_per_field"]["confidence_reported"] is False
    assert row["parser_version"] == "pdf_report:2.1.0:cohere_parse:ocr-quality-v1"
    assert row["payload"]["ocr_method"] == "cohere_parse"


def test_a_spot_check_page_is_queued_but_its_passages_are_not_demoted() -> None:
    """The floor_tier posture: every Parse page gets a review row, none is low_confidence."""
    parsed = {
        "warnings": [
            {
                "code": "ocr_quality_assessment",
                "page": 4,
                "parser_version": "2.1.0",
                "ocr_method": "cohere_parse",
                "extracted_text": "Measured 1.2 Mt at 2.4 g/t Au",
                "tier": "spot_check",
                "routing_decision": "spot_check",
                "reasons": ["floor_tier"],
                "signals": {"mean_confidence": 0.0, "confidence_reported": False},
            },
            {
                "code": "ocr_quality_assessment",
                "page": 5,
                "routing_decision": "review_required",
                "tier": "mandatory_review",
                "reasons": ["gibberish_word_ratio"],
                "signals": {},
            },
        ]
    }
    arguments = {
        "report_id": "5c896a88-5fd8-48e3-a93f-3805490b39c5",
        "workspace_id": "29188735-7bb5-4262-8b3c-6a236ed90bf0",
        "project_id": "f96f7413-4f1e-4f84-8baf-c3bd3ea027ee",
        "bronze_uri": "s3://bronze/reports/project/scan.pdf",
    }

    rows = _build_ocr_review_rows(parsed, **arguments)

    assert [row["payload"]["page_number"] for row in rows] == [4, 5]
    assert rows[0]["payload"]["ocr_quality_tier"] == "spot_check"
    assert _ocr_review_pages(parsed) == {5}
    assert _ocr_status_for_section({"page_first": 4, "page_last": 4}, _ocr_review_pages(parsed)) == "accepted"
    assert _ocr_status_for_section({"page_first": 5, "page_last": 5}, _ocr_review_pages(parsed)) == "low_confidence"
