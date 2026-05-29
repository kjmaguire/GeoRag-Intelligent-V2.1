"""Pydantic models for CC-01 Item 5 — Assessment Report Structured Summary.

The geologist-facing surface that takes an ingested assessment report
(NI 43-101, JORC, internal report) and produces a structured summary
with source-cited claims and a completeness checklist.

The summary composes the existing §04p ``/pdf/summarize_section``
service — every claim carries the same (page, bbox) provenance shape
as :class:`app.services.pdf_vl.VlClaim`, so §04i Citation completeness
is satisfied end-to-end.

Section taxonomy
----------------
Nine canonical sections, per the CC-01 spec:

    property_project, location, commodities, operator, year,
    work_performed, qa_qc, recommendations, completeness_checklist

(``completeness_checklist`` is a sibling field on the envelope, not a
section. See :class:`CompletenessChecklist`.)

Persistence
-----------
Records land in ``silver.assessment_report_summaries`` (migration
``2026_05_23_010000_create_silver_assessment_report_summaries.php``).
The cache is keyed on ``(workspace_id, pdf_id, model_id)`` — different
VL model versions cache independently.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Section vocabulary
# ---------------------------------------------------------------------------

#: The nine canonical sections per the CC-01 spec. ``other`` is the
#: bucket for facts the LLM surfaces that don't fit a canonical section.
SectionId = Literal[
    "property_project",
    "location",
    "commodities",
    "operator",
    "year",
    "work_performed",
    "qa_qc",
    "recommendations",
    "other",
]

CANONICAL_SECTIONS: tuple[SectionId, ...] = (
    "property_project",
    "location",
    "commodities",
    "operator",
    "year",
    "work_performed",
    "qa_qc",
    "recommendations",
)

#: Backend strings allowed by ``silver.assessment_report_summaries.model_backend``
#: CHECK constraint. Must stay in sync with the migration.
ModelBackend = Literal["vllm", "anthropic", "ollama"]


# ---------------------------------------------------------------------------
# Claim — re-shaped to keep this module decoupled from app.services.pdf_vl
# ---------------------------------------------------------------------------


class SummaryClaim(BaseModel):
    """A single factual claim grounded to a (page, bbox) in the source PDF.

    Wire-compatible with :class:`app.services.pdf_vl.VlClaim` — the
    summariser copies the VL claims through verbatim. Kept as a separate
    type so the assessment-summary API doesn't import pdf_vl directly.
    """

    claim_text: str = Field(..., min_length=1, description="Verbatim factual claim from the section")
    page: int = Field(..., ge=1, description="1-indexed page number where the claim is visible")
    bbox: tuple[float, float, float, float] = Field(
        ...,
        description="[x0, y0, x1, y1] in PDF user-space points (bottom-left origin, y-up)",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="VL model self-reported confidence that the claim is accurately grounded",
    )


# ---------------------------------------------------------------------------
# Section payload
# ---------------------------------------------------------------------------


class SummarySection(BaseModel):
    """One section of the assessment summary.

    ``summary_text`` is the natural-language narrative for the section;
    every factual statement inside it must appear in ``claims`` with a
    (page, bbox) grounding — enforced by the §04i Citation completeness
    guard at the upstream VL layer.
    """

    section_id: SectionId = Field(..., description="Canonical section identifier")
    title: str = Field(..., min_length=1, description="Human-readable section title")
    summary_text: str = Field(
        ...,
        description="Narrative summary of the section. May be empty when the section was not found in the source.",
    )
    claims: list[SummaryClaim] = Field(
        default_factory=list,
        description="Per-claim provenance — every factual statement in summary_text grounded to (page, bbox).",
    )
    page_range: tuple[int, int] | None = Field(
        None,
        description="(page_start, page_end) inclusive — the source pages the VL model summarised for this section. None when the section was not located.",
    )

    @field_validator("page_range")
    @classmethod
    def _page_range_ordered(
        cls, v: tuple[int, int] | None
    ) -> tuple[int, int] | None:
        if v is None:
            return None
        start, end = v
        if start < 1 or end < start:
            raise ValueError(f"page_range must be (start>=1, end>=start); got {v!r}")
        return v


# ---------------------------------------------------------------------------
# Completeness checklist
# ---------------------------------------------------------------------------


class CompletenessItem(BaseModel):
    """One expected-section coverage entry in the completeness checklist."""

    section_id: SectionId = Field(..., description="The canonical section this entry covers")
    expected: bool = Field(
        ...,
        description="Whether this section is expected for the document's report type (NI 43-101 always expects all 8 canonical).",
    )
    found: bool = Field(..., description="Whether the summariser located source pages for this section")
    notes: str | None = Field(
        None,
        description="Optional explanation for missing sections — e.g. 'no heading match in pdf_layout_regions'.",
    )


class CompletenessChecklist(BaseModel):
    """Rule-based 'things that may have been missed' checklist.

    v1: pure heading-presence check against ``silver.pdf_layout_regions``
    and the section_id → heading pattern map in
    :mod:`app.services.assessment_summarizer.section_resolver`.
    v2 (deferred): LLM-assisted gap detection comparing summary content to
    NI 43-101 §1–§27 requirements.
    """

    expected_sections: list[SectionId] = Field(
        ..., description="Sections expected by the report-type template."
    )
    found_sections: list[SectionId] = Field(
        default_factory=list,
        description="Sections the summariser successfully located + summarised.",
    )
    missing_sections: list[SectionId] = Field(
        default_factory=list,
        description="Expected sections with no source pages found.",
    )
    items: list[CompletenessItem] = Field(
        default_factory=list,
        description="Per-section detail for UI rendering.",
    )


# ---------------------------------------------------------------------------
# Envelope — request + response shapes
# ---------------------------------------------------------------------------


class AssessmentSummaryGenerateRequest(BaseModel):
    """POST /assessment_summary/{pdf_id} body.

    All fields optional — the service defaults to summarising every
    canonical section against the configured VL model.
    """

    sections: list[SectionId] | None = Field(
        None,
        description="Subset of sections to (re)generate. None = all canonical sections.",
    )
    force_regenerate: bool = Field(
        False,
        description="When True, bypass the silver cache and regenerate every requested section.",
    )


class AssessmentReportSummary(BaseModel):
    """Full response envelope returned from the summariser.

    Mirrors the columns in ``silver.assessment_report_summaries`` 1-to-1
    so the service can ``model_dump()`` and INSERT in a single hop.
    """

    summary_id: uuid.UUID = Field(..., description="silver.assessment_report_summaries.summary_id")
    workspace_id: uuid.UUID = Field(..., description="silver.workspaces.workspace_id (tenancy)")
    pdf_id: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 hex of the normalised PDF bytes (§04p Bronze key)",
    )
    report_id: uuid.UUID | None = Field(
        None,
        description="silver.reports.report_id when the PDF has been promoted to a report record.",
    )
    sections: list[SummarySection] = Field(..., description="The structured section payload.")
    completeness_checklist: CompletenessChecklist
    mean_claim_confidence: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Mean of per-claim confidence across all sections. None when zero claims.",
    )
    model_id: str = Field(..., description="VL model identifier (e.g. 'Qwen/Qwen2.5-VL-7B-Instruct')")
    model_backend: ModelBackend = Field(..., description="vllm | anthropic | ollama")
    generated_at: datetime = Field(..., description="Timestamp the summary completed")
    cache_hit: bool = Field(
        False,
        description="True when the response was served from silver cache without re-running the VL model.",
    )


__all__ = [
    "CANONICAL_SECTIONS",
    "AssessmentReportSummary",
    "AssessmentSummaryGenerateRequest",
    "CompletenessChecklist",
    "CompletenessItem",
    "ModelBackend",
    "SectionId",
    "SummaryClaim",
    "SummarySection",
]
