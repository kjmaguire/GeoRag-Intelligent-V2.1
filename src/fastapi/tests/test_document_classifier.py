"""Unit tests for plan §1c document classification step."""

from __future__ import annotations

import pytest

from app.agent.document_classifier import (
    DOCUMENT_CLASS_PATTERNS,
    DocumentClassification,
    classify_document_type,
)

# ---------------------------------------------------------------------------
# Default + empty paths
# ---------------------------------------------------------------------------


def test_empty_inputs_return_unknown():
    result = classify_document_type()
    assert result.document_class == "Unknown"
    assert result.confidence == 0.0
    assert result.signal == "default"


def test_text_only_with_no_pattern_match_returns_unknown():
    result = classify_document_type(
        "Some generic text without any recognised boilerplate."
    )
    assert result.document_class == "Unknown"


def test_filename_only_no_match_returns_unknown():
    result = classify_document_type(filename="random_file.pdf")
    assert result.document_class == "Unknown"


# ---------------------------------------------------------------------------
# Filename signal (confidence 0.95)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename,expected_class", [
    ("NI43-101_Crackingstone_2024.pdf", "NI 43-101"),
    ("NI_43-101F1_Cameco.pdf", "NI 43-101"),
    ("Technical_Report_Q1.pdf", "Technical Report"),
    ("Feasibility_Study_2024.pdf", "Feasibility Study"),
    ("PEA_Crackingstone.pdf", "PEA"),
    ("AssessmentReport.pdf", "Assessment Report"),
    ("Annual_Report_2024.pdf", "Annual Report"),
    ("Fact-Sheet.pdf", "Fact Sheet"),
    ("Press_Release_2024-05-12.pdf", "Press Release"),
    ("Investor_Presentation_Q2.pdf", "Investor Presentation"),
    ("CorporatePresentation.pdf", "Corporate Presentation"),
    ("NewsRelease.html", "News Release"),
    ("Historical_Report_1987.pdf", "Historical Report"),
    ("Internal_Memo_2024.docx", "Internal Memo"),
    ("project_email.eml", "Email"),
    ("Field_Notes_2024-05.pdf", "Field Note"),
])
def test_filename_pattern_matches_expected_class(filename, expected_class):
    result = classify_document_type(filename=filename)
    assert result.document_class == expected_class
    assert result.signal == "filename"
    assert result.confidence == 0.95


def test_filename_wins_over_text_when_both_match():
    """When filename matches NI 43-101 but text says 'Press Release',
    filename wins (higher confidence)."""
    result = classify_document_type(
        text="Press Release: Crackingstone announces Q1 results.",
        filename="NI43-101_Crackingstone.pdf",
    )
    assert result.signal == "filename"
    assert result.document_class == "NI 43-101"


# ---------------------------------------------------------------------------
# Title signal (confidence 0.85)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title_text,expected_class", [
    ("NI 43-101 Technical Report — Crackingstone Project", "NI 43-101"),
    ("Form 43-101F1 — Mineral Resource Estimate", "NI 43-101"),
    ("Technical Report on the Crackingstone Property", "Technical Report"),
    ("Feasibility Study Summary Q1 2024", "Feasibility Study"),
    ("Preliminary Economic Assessment — Northwest Zone", "PEA"),
    ("Assessment Report 2024 — Permit BX-417", "Assessment Report"),
    ("Annual Report 2024 — Cameco Corporation", "Annual Report"),
    ("Fact Sheet — Mineralisation Overview", "Fact Sheet"),
    ("Press Release — Q1 Drill Results", "Press Release"),
    ("Investor Presentation — Q2 2024", "Investor Presentation"),
    ("Corporate Presentation — Strategic Overview", "Corporate Presentation"),
    ("News Release — June 12, 2024", "News Release"),
    ("Historical Report — 1987 Mapping Programme", "Historical Report"),
    ("Internal Memo — Compliance Notes", "Internal Memo"),
    ("Field Notes — Site Visit 2024-05-22", "Field Note"),
])
def test_title_pattern_matches_expected_class(title_text, expected_class):
    result = classify_document_type(text=title_text)
    assert result.document_class == expected_class
    assert result.signal == "title"
    assert result.confidence == 0.85


def test_title_window_is_first_200_chars_only():
    """A pattern present beyond char 200 should NOT match via the
    title signal — it falls through to the body signal."""
    # Pad 250 chars of irrelevant prefix, then NI 43-101.
    prefix = "geological observations across multiple drill campaigns. " * 5
    text = prefix + " — Technical Report — Crackingstone."
    assert len(prefix) > 200  # invariant the test relies on
    result = classify_document_type(text=text)
    # Should match via body, NOT title.
    assert result.signal in ("body", "default")
    # Confidence is not 0.85 (title tier).
    assert result.confidence != 0.85


# ---------------------------------------------------------------------------
# Body signal (confidence 0.7)
# ---------------------------------------------------------------------------


def test_body_pattern_matches_ni43_101_boilerplate():
    # Pad past 200 chars so the trigger is OUTSIDE the title window
    # and only matches via the body pattern.
    text = (
        "Geological summary follows. "
        + "x " * 200
        + "This report is compliant with NI 43-101 standards "
        + "as published by the Canadian Securities Administrators."
    )
    result = classify_document_type(text=text)
    assert result.document_class == "NI 43-101"
    assert result.signal == "body"
    assert result.confidence == 0.70


def test_body_pattern_matches_press_release_forward_looking():
    text = (
        "Q1 results summary follows. "
        + "x " * 80
        + "Forward-looking statements: this release contains "
        + "forward-looking statements regarding future securities "
        + "performance, mineral resources, and exploration outcomes."
    )
    result = classify_document_type(text=text)
    assert result.document_class == "Press Release"
    assert result.signal == "body"


def test_body_pattern_matches_annual_report_mda():
    text = (
        "Cameco Corporation 2024 — opening pages. "
        + "x " * 100
        + "Management's Discussion and Analysis"
        + " follows below."
    )
    result = classify_document_type(text=text)
    assert result.document_class == "Annual Report"


def test_body_pattern_matches_assessment_report_phrase():
    text = (
        "Field campaign summary. "
        + "x " * 100
        + "Assessment work performed on permit BX-417 during 2024."
    )
    result = classify_document_type(text=text)
    assert result.document_class == "Assessment Report"


def test_body_pattern_matches_feasibility_study_npv():
    text = (
        "Project economics. "
        + "x " * 100
        + "Base case NPV calculated at $1.2B with IRR of 24%."
    )
    result = classify_document_type(text=text)
    assert result.document_class == "Feasibility Study"


# ---------------------------------------------------------------------------
# Body budget
# ---------------------------------------------------------------------------


def test_body_budget_truncates_text_scan():
    """A pattern beyond ``body_budget_chars`` is not matched."""
    # Place the trigger phrase at char 9000+ but budget at 1000.
    text = ("x" * 9000) + " compliant with NI 43-101 standards"
    result = classify_document_type(text=text, body_budget_chars=1000)
    assert result.document_class == "Unknown"


def test_body_budget_default_is_8000_chars():
    """The default budget catches patterns at typical document depths."""
    text = ("x" * 5000) + " compliant with NI 43-101 standards"
    result = classify_document_type(text=text)
    assert result.document_class == "NI 43-101"


# ---------------------------------------------------------------------------
# Signal precedence
# ---------------------------------------------------------------------------


def test_title_wins_over_body():
    """When title matches one class and body matches another, title
    wins (higher confidence)."""
    text = (
        "Annual Report 2024 — Cameco Corporation."  # title-signal Annual Report
        + " "
        + "x " * 200
        + " compliant with NI 43-101 standards "  # body-signal NI 43-101
    )
    result = classify_document_type(text=text)
    assert result.document_class == "Annual Report"
    assert result.signal == "title"


def test_filename_wins_over_title_and_body():
    """Filename + title + body all match different classes — filename wins."""
    result = classify_document_type(
        text=(
            "Annual Report 2024 — opening pages."  # title
            + " "
            + "x " * 200
            + " compliant with NI 43-101 standards "  # body
        ),
        filename="PressRelease_2024-05-12.pdf",  # filename
    )
    assert result.document_class == "Press Release"
    assert result.signal == "filename"


# ---------------------------------------------------------------------------
# Evidence text
# ---------------------------------------------------------------------------


def test_classification_carries_evidence_text():
    """The evidence_text field must capture the matched substring for
    SME audit."""
    result = classify_document_type(
        text="NI 43-101 Technical Report — Crackingstone Project",
    )
    assert "43-101" in result.evidence_text.lower() or "NI 43" in result.evidence_text


def test_unknown_has_empty_evidence_text():
    result = classify_document_type("totally generic")
    assert result.evidence_text == ""


# ---------------------------------------------------------------------------
# Public table integrity
# ---------------------------------------------------------------------------


def test_document_class_patterns_dict_has_three_signal_tiers():
    """Locks the public DOCUMENT_CLASS_PATTERNS shape."""
    assert set(DOCUMENT_CLASS_PATTERNS.keys()) == {"filename", "title", "body"}


def test_pattern_tables_are_non_empty():
    for signal, patterns in DOCUMENT_CLASS_PATTERNS.items():
        assert len(patterns) > 0, f"{signal} pattern table is empty"


def test_filename_patterns_cover_all_recognised_classes_except_uncited():
    """Every class except 'Uncited' (which is a synthetic catch-all
    for the converter's unknown-tool fallback) appears in the filename
    pattern table."""
    classes_in_filename = {c for _, c in DOCUMENT_CLASS_PATTERNS["filename"]}
    expected = {
        "NI 43-101", "Technical Report", "Feasibility Study", "PEA",
        "Assessment Report", "Annual Report", "Fact Sheet",
        "Press Release", "Investor Presentation", "Corporate Presentation",
        "News Release", "Historical Report", "Internal Memo",
        "Email", "Field Note",
    }
    missing = expected - classes_in_filename
    assert not missing, f"filename patterns missing classes: {missing}"


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


def test_result_is_frozen_dataclass():
    result = classify_document_type(filename="NI43-101.pdf")
    assert isinstance(result, DocumentClassification)
    with pytest.raises(Exception):
        result.document_class = "Press Release"  # type: ignore[misc]
