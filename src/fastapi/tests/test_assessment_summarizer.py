"""CC-01 Item 5 — Unit tests for the assessment summariser pure helpers.

Covers the deterministic logic that doesn't require a DB or VL backend:
    - Heading-pattern regex matching for each canonical section_id
    - Completeness-checklist build from a mixed found/missing section list

End-to-end test against the full service path (with mocked PdfVlService
and a real asyncpg pool) lives in
``test_assessment_summarizer_integration.py`` (not yet written — flagged
for a follow-up after the migration is applied to the test DB).
"""

from __future__ import annotations

import uuid

import pytest

from app.models.assessment_summary import (
    CANONICAL_SECTIONS,
    AssessmentReportSummary,
    CompletenessChecklist,
    CompletenessItem,
    SectionId,
    SummaryClaim,
    SummarySection,
)
from app.services.assessment_summarizer import (
    _SECTION_PATTERNS,
    _build_completeness_checklist,
)


# ---------------------------------------------------------------------------
# Heading patterns — every canonical section has at least one pattern
# ---------------------------------------------------------------------------


def test_every_canonical_section_has_patterns() -> None:
    for sid in CANONICAL_SECTIONS:
        assert sid in _SECTION_PATTERNS, f"{sid} missing from _SECTION_PATTERNS"
        assert len(_SECTION_PATTERNS[sid]) > 0, f"{sid} has no patterns"


@pytest.mark.parametrize(
    ("section_id", "headings_that_should_match"),
    [
        (
            "property_project",
            [
                "Property Description and Location",
                "4. PROPERTY DESCRIPTION",
                "Section 4 — Property Location and Tenure",
                "Project Description",
            ],
        ),
        (
            "location",
            [
                "Accessibility, Climate, Local Resources and Infrastructure",
                "Project Location",
            ],
        ),
        (
            "commodities",
            [
                "14. MINERAL RESOURCE ESTIMATES",
                "Mineralization",
                "Deposit Type",
            ],
        ),
        (
            "operator",
            [
                "Authors of the Technical Report",
                "Issuer",
                "Operator",
            ],
        ),
        (
            "year",
            ["Effective Date", "Date of Report"],
        ),
        (
            "work_performed",
            [
                "Exploration by the Issuer",
                "Historical Exploration",
                "Drilling",
                "Work Program",
            ],
        ),
        (
            "qa_qc",
            [
                "QA/QC",
                "Quality Assurance and Quality Control",
                "11. SAMPLE PREPARATION, ANALYSES AND SECURITY",
                "Data Verification",
            ],
        ),
        (
            "recommendations",
            [
                # NI 43-101 §26 is "Interpretations and Conclusions",
                # §27 is "Recommendations" — they're separate, so we only
                # match a heading that actually contains "recommendation"
                # (possibly fused with "conclusions" as some reports do).
                "27. RECOMMENDATIONS",
                "Conclusions and Recommendations",
                "Recommendations",
            ],
        ),
    ],
)
def test_canonical_headings_match_at_least_one_pattern(
    section_id: SectionId, headings_that_should_match: list[str]
) -> None:
    patterns = _SECTION_PATTERNS[section_id]
    for heading in headings_that_should_match:
        assert any(p.search(heading) for p in patterns), (
            f"{section_id!s}: heading {heading!r} matched none of "
            f"{[p.pattern for p in patterns]}"
        )


def test_unrelated_text_does_not_match_recommendations() -> None:
    """Negative case — 'recommendation' is broad; we accept inline matches
    but want to make sure paragraphs of unrelated text don't false-match
    the more specific patterns."""
    text = "Drillhole PLS-22-08 returned 12.4 m at 5.6 g/t Au from 145.2 m."
    for sid in ("property_project", "qa_qc", "recommendations"):
        patterns = _SECTION_PATTERNS[sid]
        assert not any(p.search(text) for p in patterns), (
            f"{sid} false-matched assay narrative {text!r}"
        )


# ---------------------------------------------------------------------------
# Completeness checklist
# ---------------------------------------------------------------------------


def _make_section(sid: SectionId, *, found: bool) -> SummarySection:
    return SummarySection(
        section_id=sid,
        title=sid.replace("_", " ").title(),
        summary_text="" if not found else "Some summary text.",
        claims=[]
        if not found
        else [
            SummaryClaim(
                claim_text="A grounded claim.",
                page=2,
                bbox=(10.0, 20.0, 100.0, 50.0),
                confidence=0.9,
            )
        ],
        page_range=None if not found else (2, 4),
    )


def test_completeness_checklist_all_found() -> None:
    sections = [_make_section(sid, found=True) for sid in CANONICAL_SECTIONS]
    checklist = _build_completeness_checklist(sections)

    assert checklist.expected_sections == list(CANONICAL_SECTIONS)
    assert checklist.found_sections == list(CANONICAL_SECTIONS)
    assert checklist.missing_sections == []
    assert all(item.found for item in checklist.items)
    assert all(item.notes is None for item in checklist.items)


def test_completeness_checklist_mixed_state() -> None:
    sections = [
        _make_section("property_project", found=True),
        _make_section("location", found=False),
        _make_section("commodities", found=True),
        _make_section("operator", found=False),
        _make_section("year", found=True),
        _make_section("work_performed", found=True),
        _make_section("qa_qc", found=False),
        _make_section("recommendations", found=True),
    ]
    checklist = _build_completeness_checklist(sections)

    assert set(checklist.found_sections) == {
        "property_project",
        "commodities",
        "year",
        "work_performed",
        "recommendations",
    }
    assert set(checklist.missing_sections) == {"location", "operator", "qa_qc"}

    by_id: dict[SectionId, CompletenessItem] = {
        i.section_id: i for i in checklist.items
    }
    assert by_id["location"].notes is not None
    assert "no heading match" in by_id["location"].notes.lower()
    assert by_id["property_project"].notes is None


def test_completeness_checklist_excludes_other_bucket() -> None:
    sections = [_make_section(sid, found=True) for sid in CANONICAL_SECTIONS] + [
        SummarySection(
            section_id="other",
            title="Other",
            summary_text="Stray facts.",
            claims=[],
            page_range=(99, 99),
        )
    ]
    checklist = _build_completeness_checklist(sections)
    # 'other' is not expected and should not appear in any bucket
    assert "other" not in checklist.expected_sections
    assert "other" not in checklist.found_sections
    assert "other" not in checklist.missing_sections
    assert all(item.section_id != "other" for item in checklist.items)


# ---------------------------------------------------------------------------
# Envelope shape — pydantic accepts a real payload round-trip
# ---------------------------------------------------------------------------


def test_assessment_report_summary_envelope_roundtrip() -> None:
    sections = [_make_section(sid, found=True) for sid in CANONICAL_SECTIONS]
    checklist = _build_completeness_checklist(sections)

    envelope = AssessmentReportSummary(
        summary_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        pdf_id="a" * 64,
        report_id=None,
        sections=sections,
        completeness_checklist=checklist,
        mean_claim_confidence=0.87,
        model_id="Qwen/Qwen2.5-VL-7B-Instruct",
        model_backend="vllm",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        cache_hit=False,
    )

    dumped = envelope.model_dump()
    rebuilt = AssessmentReportSummary.model_validate(dumped)
    assert rebuilt.pdf_id == envelope.pdf_id
    assert len(rebuilt.sections) == len(CANONICAL_SECTIONS)
    assert rebuilt.completeness_checklist.found_sections == list(CANONICAL_SECTIONS)


def test_assessment_report_summary_rejects_invalid_pdf_id() -> None:
    sections = [_make_section(sid, found=False) for sid in CANONICAL_SECTIONS]
    checklist = _build_completeness_checklist(sections)
    with pytest.raises(ValueError):
        AssessmentReportSummary(
            summary_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            pdf_id="not-a-sha-256",
            report_id=None,
            sections=sections,
            completeness_checklist=checklist,
            mean_claim_confidence=None,
            model_id="x",
            model_backend="vllm",
            generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            cache_hit=False,
        )
