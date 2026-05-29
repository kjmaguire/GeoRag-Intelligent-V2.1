"""ReportBuilderState — Pydantic state model for the §15.1 graph.

Threaded through every node. Each node mutates a copy and returns
the next state. LangGraph reduces these into a single channel-mapped
state machine when the langgraph wiring lands in a later tick.

Doc-phase 80 — interface contract; field set may evolve. Bump
`schema_version` on any field add/rename/remove and update the
graph's reducer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


# Eleven report types per §15.2.
ReportType = Literal[
    "weekly_project_digest",
    "ingestion_quality",
    "technical_due_diligence",
    "executive_project_intelligence",
    "gis_arcgis_sync",
    "target_recommendation",
    "public_geo_overlay",
    "data_room_package",
    "what_changed",
    "ni43101_section_pack",
    "csa11348_disclosure_pack",
]


# Risk tier per report type. Drives sign-off requirements (R3 = none;
# R4 = geologist sign-off; R5 = QP credential verification mandatory).
ReportRiskTier = Literal["R3", "R4", "R5"]


class SectionPlan(BaseModel):
    """One section as planned by the Report Planner Agent."""

    section_id: str
    title: str
    template_slug: str           # which template controls this section
    required_evidence_kinds: list[str] = Field(default_factory=list)
    map_kinds: list[str] = Field(default_factory=list)
    chart_kinds: list[str] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    """One supporting chunk / row that backs a claim."""

    source_chunk_id: str
    data_visibility: Literal["public", "workspace"]
    license_note: str | None = None
    is_stale: bool = False
    freshness_iso: datetime | None = None


class Claim(BaseModel):
    """A claim drafted into a section, with its evidence ledger."""

    claim_id: str
    section_id: str
    text: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    validated: bool = False
    validation_notes: str | None = None


class SectionDraft(BaseModel):
    """A drafted section ready for claim validation + map/chart wiring."""

    section_id: str
    body_markdown: str
    claims: list[Claim] = Field(default_factory=list)
    pending_map_kinds: list[str] = Field(default_factory=list)
    pending_chart_kinds: list[str] = Field(default_factory=list)


class SignOffRecord(BaseModel):
    """A required sign-off (geologist + QP for R4/R5 reports)."""

    role: Literal["geologist", "qp"]
    user_id: int | None = None
    qp_credential_id: str | None = None
    signed_at: datetime | None = None
    audit_ledger_id: UUID | None = None


class ReportBuilderState(BaseModel):
    """Graph state threaded through all twelve §15.1 nodes."""

    schema_version: int = 1

    # Identity
    report_id: UUID
    workspace_id: UUID
    project_id: UUID
    report_type: ReportType
    risk_tier: ReportRiskTier
    requested_by_user_id: int

    # Plan
    sections_plan: list[SectionPlan] = Field(default_factory=list)

    # Evidence + claims
    section_drafts: list[SectionDraft] = Field(default_factory=list)
    citation_payload: dict[str, Any] = Field(default_factory=dict)
    conflicts_disclosed: list[dict[str, Any]] = Field(default_factory=list)

    # Compliance
    compliance_checks: list[dict[str, Any]] = Field(default_factory=list)
    compliance_passed: bool = False

    # Sign-off
    sign_offs: list[SignOffRecord] = Field(default_factory=list)
    sign_off_complete: bool = False

    # Bundle artifacts (S3-style URIs to SeaweedFS)
    pdf_uri: str | None = None
    docx_uri: str | None = None
    xlsx_uri: str | None = None
    citation_manifest_uri: str | None = None
    source_manifest_uri: str | None = None
    evidence_json_uri: str | None = None
    map_uris: list[str] = Field(default_factory=list)
    chart_uris: list[str] = Field(default_factory=list)

    # Proof
    hash_chain_proof: dict[str, Any] | None = None

    # Delivery
    delivery_dispatched: bool = False
    delivery_targets: list[str] = Field(default_factory=list)

    # Reporting window — required for what_changed reports (doc-phase 156).
    # Threaded in from GenerateReportInput.report_window_{start,end}_iso.
    report_window_start: datetime | None = None
    report_window_end: datetime | None = None

    # Telemetry
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
