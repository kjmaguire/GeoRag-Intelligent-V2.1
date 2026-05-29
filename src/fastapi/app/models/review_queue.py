"""Review queue Pydantic models for Track A.1 Phase 1.A.

Mirrors the schema introduced by:

    database/migrations/2026_04_29_120000_create_silver_review_queue.php
    database/migrations/2026_04_29_120100_create_silver_review_audit_log.php

These models are used by:
  - The parser library when emitting queue rows (Phase 1.B — pending)
  - The Phase 3 /review API endpoints (pending)
  - The Phase 5 commit job (pending)
  - The Phase 6 bulk-ops endpoints (pending)

Phase 1.A scope is models + DB schema only; no endpoints read or write
these models yet. They are stable contracts the downstream phases pin to.

Enum literals match the Postgres ENUM types exactly. Adding a value
requires a coordinated migration that ALTERs the enum + Pydantic Literal
+ any consumer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enum literals — exact-match with Postgres ENUM types
# ---------------------------------------------------------------------------

ReviewRoutingLiteral = Literal["auto_pass", "review_required", "auto_reject"]
"""Routing decision set at parse time. See D3 in the plan for thresholds."""

ReviewLifecycleLiteral = Literal[
    "pending",
    "in_review",
    "decided",
    "committed",
    "archived",
]
"""State machine for queue rows. Transitions:

    pending ─→ in_review ─→ decided ─→ committed
    pending ─→ committed (auto_pass / auto_reject fast path)
    decided ─→ archived  (operator hard-archive; rare)
"""

ReviewDecisionLiteral = Literal[
    "approve_as_parsed",
    "approve_with_corrections",
    "reject",
    "defer",
]
"""Decision kind set at decide time.

  approve_as_parsed         — payload is correct as-parsed; commit unchanged
  approve_with_corrections  — payload has reviewer overrides in decision_payload
  reject                    — drop the record; do not commit to silver.*
  defer                     — needs more info; lifecycle returns to pending
"""

# ---------------------------------------------------------------------------
# silver.review_queue
# ---------------------------------------------------------------------------


class ReviewQueueCreate(BaseModel):
    """Parser-emitted queue row at parse time. Lifecycle starts as 'pending'."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    project_id: UUID
    target_table: str = Field(
        ...,
        description="Fully-qualified Silver table name, e.g. 'silver.collars'",
        max_length=128,
    )
    target_record_kind: str = Field(
        ...,
        description="Semantic record name for UI display, e.g. 'collar', 'sample'",
        max_length=64,
    )
    bronze_uri: str = Field(
        ...,
        description="SeaweedFS URI of the source Bronze object (§02 + ADR-0001)",
        max_length=1024,
    )
    bronze_row_offset: int | None = Field(
        default=None,
        description="Row-in-file offset for tabular sources (CSV/XLSX). NULL for PDFs.",
        ge=0,
    )

    payload: dict[str, Any] = Field(
        ...,
        description="Parsed-but-not-yet-merged record. Keys match target_table columns.",
    )
    confidence_per_field: dict[str, float] = Field(
        default_factory=dict,
        description="Per-column confidence in [0, 1]. Empty dict allowed for parsers without per-field signals (legacy).",
    )
    confidence_record: float = Field(
        ...,
        description="Aggregate confidence in [0, 1]. Default rule: min(confidence_per_field). Workspace-configurable to mean/weighted/p10 in a later phase.",
        ge=0.0,
        le=1.0,
    )
    parser_version: str = Field(
        ...,
        description="Parser+vendor_profile version stamp for traceability.",
        max_length=128,
    )

    routing_decision: ReviewRoutingLiteral
    routing_reason: str | None = Field(
        default=None,
        description="Free-text — why this routing decision was applied.",
        max_length=512,
    )


class ReviewQueueRead(ReviewQueueCreate):
    """Persisted queue row including server-managed fields."""

    queue_id: UUID

    outlier_flags: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Phase 4 LLM-assist anomaly hits, e.g. [{field, reason}].",
    )

    lifecycle: ReviewLifecycleLiteral
    # users.id is BIGINT (Laravel default) — see silver.review_queue FK to
    # users(id). Reconciled 2026-05-24 after CC-01 Item 1 SRQ rollout
    # surfaced the type drift between this model and the live schema.
    assigned_to_user_id: int | None = None

    decided_by_user_id: int | None = None
    decision_kind: ReviewDecisionLiteral | None = None
    decision_payload: dict[str, Any] | None = None
    decision_rationale: str | None = None
    decided_at: datetime | None = None

    committed_silver_pk: UUID | None = None

    created_at: datetime
    updated_at: datetime


class ReviewQueueDecision(BaseModel):
    """Reviewer decision payload posted from the /review UI (Phase 3)."""

    model_config = ConfigDict(extra="forbid")

    decision_kind: ReviewDecisionLiteral
    decision_payload: dict[str, Any] | None = Field(
        default=None,
        description="Required for approve_with_corrections; subset of original payload with overrides. Must be NULL for approve_as_parsed / reject / defer.",
    )
    decision_rationale: str | None = Field(
        default=None,
        description="Optional reviewer free-text note.",
        max_length=2048,
    )


# ---------------------------------------------------------------------------
# silver.review_audit_log
# ---------------------------------------------------------------------------


class ReviewAuditLogEntry(BaseModel):
    """Append-only audit row for queue state transitions."""

    model_config = ConfigDict(extra="forbid")

    audit_id: UUID
    queue_id: UUID
    # users.id BIGINT — see decided_by_user_id reconciliation above.
    actor_user_id: int | None = Field(
        default=None,
        description="NULL for system actions (auto_pass, auto_reject, scheduled archival).",
    )

    from_lifecycle: ReviewLifecycleLiteral | None = None
    to_lifecycle: ReviewLifecycleLiteral
    decision_kind: ReviewDecisionLiteral | None = None
    decision_payload_diff: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description="Diff-shaped: {field_name: {original: ..., corrected: ...}}. Compact representation; original payload is in review_queue.payload.",
    )

    created_at: datetime


# ---------------------------------------------------------------------------
# answer_citation_items.review_lineage (D6 integration)
# ---------------------------------------------------------------------------


class ReviewLineageOverride(BaseModel):
    """Per-field correction in the review_lineage payload."""

    model_config = ConfigDict(extra="forbid")

    original: Any
    corrected: Any


class ReviewLineage(BaseModel):
    """Reviewer-correction lineage embedded in answer_citation_items.review_lineage.

    Set at Phase 5 commit time when decision_kind = 'approve_with_corrections'.
    Read by §10s EvidenceInspector to render reviewer-authorship per §10w P1.
    """

    model_config = ConfigDict(extra="forbid")

    queue_id: UUID
    # users.id BIGINT — see decided_by_user_id reconciliation above.
    decided_by_user_id: int
    decided_at: datetime
    decision_kind: ReviewDecisionLiteral
    field_overrides: dict[str, ReviewLineageOverride] = Field(
        default_factory=dict,
        description="Per-field {original, corrected} pairs; empty when decision_kind != approve_with_corrections.",
    )
