"""LLM Incident Diagnosis state (§12.9) — doc-phase 102 skeleton.

Pydantic state threaded through the diagnosis graph nodes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

IncidentKind = Literal[
    "hallucination",
    "refusal_failure",
    "citation_drift",
    "numeric_grounding_failure",
    "tone_template_violation",
    "cost_spike",
    "latency_spike",
    "other",
]


class IncidentDiagnosisState(BaseModel):
    schema_version: int = 1

    # Identity
    incident_id: UUID
    workspace_id: UUID | None = None
    triage_kind: IncidentKind
    reported_at: datetime
    initial_payload: dict[str, Any] = Field(default_factory=dict)

    # Classification
    classified_kind: IncidentKind | None = None
    classification_confidence: float | None = None

    # Traces
    correlated_trace_ids: list[str] = Field(default_factory=list)
    trace_excerpts: list[dict[str, Any]] = Field(default_factory=list)

    # Root cause
    root_cause_hypothesis: str | None = None
    root_cause_evidence: list[dict[str, Any]] = Field(default_factory=list)

    # Remediation
    proposed_remediation_kind: str | None = None
    proposed_remediation_payload: dict[str, Any] = Field(default_factory=dict)

    # Recording
    audit_ledger_entry_id: UUID | None = None
    diagnosis_recorded: bool = False

    # Telemetry
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
