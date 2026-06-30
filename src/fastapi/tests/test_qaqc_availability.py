"""Tests for the Phase 4 QA/QC availability detector + anomaly prompt hint."""

from __future__ import annotations

from app.agent.agentic_retrieval import (
    detect_qaqc_availability,
)

# ---------------------------------------------------------------------------
# Fixtures: tool-result shapes
# ---------------------------------------------------------------------------


def _rich_row(**overrides):
    """Row carrying ALL the Phase-4 QA/QC fields."""
    return {
        "sample_id": "S-001",
        "value": 1.23,
        # New Phase-4 columns
        "blank_result": 0.001,
        "blank_threshold": 0.01,
        "blank_pass": True,
        "crm_id": "OREAS-200",
        "crm_expected": 1.50,
        "crm_result": 1.48,
        "crm_pass": True,
        "duplicate_pair_id": "DUP-001",
        "duplicate_rpd": 4.2,
        "duplicate_pass": True,
        "half_dl_substituted": False,
        "batch_id": "B-2026-04",
        "digestion_code": "4A",
        # Pre-existing columns
        "detection_limit": 0.005,
        "under_detection": False,
        "lab_name": "ALS-Vancouver",
        "analysis_method": "ICP-AES",
        "qaqc_flag": "PASS",
        **overrides,
    }


def _legacy_row(**overrides):
    """Pre-Phase-4 row — only the legacy qaqc_flag column."""
    return {
        "sample_id": "S-002",
        "value": 2.34,
        # All new fields are None
        "blank_result": None,
        "blank_pass": None,
        "crm_id": None,
        "crm_pass": None,
        "duplicate_pair_id": None,
        "duplicate_pass": None,
        "batch_id": None,
        # Pre-existing legacy
        "detection_limit": 0.005,
        "under_detection": False,
        "qaqc_flag": "PASS",
        **overrides,
    }


def _bare_row(**overrides):
    """No QA/QC at all — geological signal only."""
    return {
        "sample_id": "S-003",
        "value": 5.67,
        **overrides,
    }


# ---------------------------------------------------------------------------
# Status calculation
# ---------------------------------------------------------------------------


def test_rich_rows_report_present_for_new_groups() -> None:
    rows = [_rich_row(sample_id=f"S-{i}") for i in range(3)]
    tool_results = [("query_assay_data", {"samples": rows})]
    avail = detect_qaqc_availability(tool_results)
    assert avail.inspected_rows == 3
    assert avail.has_any_new_qaqc is True
    assert avail.groups["blanks"].status == "present"
    assert avail.groups["crms"].status == "present"
    assert avail.groups["duplicates"].status == "present"


def test_legacy_rows_have_only_legacy_flag() -> None:
    rows = [_legacy_row(sample_id=f"S-{i}") for i in range(3)]
    tool_results = [("query_assay_data", {"samples": rows})]
    avail = detect_qaqc_availability(tool_results)
    assert avail.has_any_new_qaqc is False
    assert avail.has_legacy_qaqc_flag is True
    assert avail.groups["blanks"].status == "absent"
    assert avail.groups["legacy_flag"].status == "present"


def test_bare_rows_have_nothing() -> None:
    rows = [_bare_row(sample_id=f"S-{i}") for i in range(3)]
    tool_results = [("query_assay_data", {"samples": rows})]
    avail = detect_qaqc_availability(tool_results)
    assert avail.has_any_new_qaqc is False
    assert avail.has_legacy_qaqc_flag is False
    assert avail.groups["blanks"].status == "absent"
    assert avail.groups["legacy_flag"].status == "absent"


def test_mixed_rows_yield_partial_status() -> None:
    rows = [_rich_row(), _legacy_row(), _bare_row()]
    tool_results = [("query_assay_data", {"samples": rows})]
    avail = detect_qaqc_availability(tool_results)
    assert avail.groups["blanks"].status == "partial"
    assert 0 < avail.groups["blanks"].rows_with_any_field < 3


def test_zero_rows_report_no_assay_data() -> None:
    tool_results = [("query_assay_data", {"samples": []})]
    avail = detect_qaqc_availability(tool_results)
    assert avail.inspected_rows == 0
    assert avail.has_any_new_qaqc is False
    assert avail.has_legacy_qaqc_flag is False


def test_non_assay_tool_results_are_ignored() -> None:
    tool_results = [
        ("search_documents", {"chunks": [{"text": "irrelevant"}]}),
        ("query_spatial_collars", {"collars": []}),
    ]
    avail = detect_qaqc_availability(tool_results)
    assert avail.inspected_rows == 0


# ---------------------------------------------------------------------------
# Prompt-hint rendering
# ---------------------------------------------------------------------------


def test_hint_empty_when_no_assay_rows() -> None:
    avail = detect_qaqc_availability([("search_documents", {})])
    assert avail.to_prompt_hint() == ""


def test_hint_includes_rich_qaqc_guidance() -> None:
    rows = [_rich_row()]
    avail = detect_qaqc_availability([("query_assay_data", {"samples": rows})])
    hint = avail.to_prompt_hint()
    assert "QA/QC FIELD AVAILABILITY" in hint
    assert "blanks=present" in hint
    assert "crms=present" in hint
    assert "Use the rich QA/QC fields" in hint
    # Anomaly classification language carries through.
    assert "geological signal" in hint
    assert "QA/QC artifact" in hint


def test_hint_emits_graceful_degrade_for_legacy_only() -> None:
    rows = [_legacy_row()]
    avail = detect_qaqc_availability([("query_assay_data", {"samples": rows})])
    hint = avail.to_prompt_hint()
    assert "GRACEFUL DEGRADE" in hint
    assert "qaqc_flag" in hint
    assert "Silver Review" in hint


def test_hint_emits_strong_degrade_when_no_qaqc_at_all() -> None:
    rows = [_bare_row()]
    avail = detect_qaqc_availability([("query_assay_data", {"samples": rows})])
    hint = avail.to_prompt_hint()
    assert "GRACEFUL DEGRADE" in hint
    assert "geological signal alone" in hint
    assert "key uncertainty driver" in hint


# ---------------------------------------------------------------------------
# Source extraction robustness
# ---------------------------------------------------------------------------


def test_pydantic_model_rows_are_unpacked() -> None:
    """Per-row results may arrive as Pydantic models (model_dump-able)."""
    from pydantic import BaseModel

    class FakeRow(BaseModel):
        sample_id: str
        blank_pass: bool | None = True
        blank_result: float | None = 0.01

    rows = [FakeRow(sample_id="S-1"), FakeRow(sample_id="S-2")]
    tool_results = [("query_assay_data", {"samples": rows})]
    avail = detect_qaqc_availability(tool_results)
    assert avail.inspected_rows == 2
    assert avail.groups["blanks"].rows_with_any_field == 2


def test_dataclass_rows_are_unpacked() -> None:
    """Per-row results may arrive as dataclasses (vars()-able)."""
    from dataclasses import dataclass

    @dataclass
    class FakeRow:
        sample_id: str
        blank_pass: bool | None = True
        blank_result: float | None = 0.01

    rows = [FakeRow(sample_id="S-1")]
    tool_results = [("query_assay_data", {"samples": rows})]
    avail = detect_qaqc_availability(tool_results)
    assert avail.inspected_rows == 1
    assert avail.groups["blanks"].rows_with_any_field == 1


def test_result_object_with_samples_attribute() -> None:
    """Real orchestrator tool results expose ``samples`` as an attribute,
    not as a dict key — exercise that path too."""

    class FakeAssayDataResult:
        def __init__(self, samples):
            self.samples = samples

    rows = [_rich_row()]
    result = FakeAssayDataResult(samples=rows)
    tool_results = [("query_assay_data", result)]
    avail = detect_qaqc_availability(tool_results)
    assert avail.inspected_rows == 1
    assert avail.has_any_new_qaqc is True


# ---------------------------------------------------------------------------
# Group describe()
# ---------------------------------------------------------------------------


def test_group_describe_formats_predictably() -> None:
    rows = [_rich_row(), _legacy_row()]
    avail = detect_qaqc_availability([("query_assay_data", {"samples": rows})])
    desc = avail.groups["blanks"].describe()
    # "blanks=partial(1/2 rows)" or similar — exact string for the prompt hint.
    assert desc.startswith("blanks=")
    assert "1/2 rows" in desc
