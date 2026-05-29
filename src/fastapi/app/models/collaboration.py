"""Pydantic v2 models for the §10v Team Collaboration subsystem — Phase 1.A.

Source: georag-architecture.html §10v (v1.45), and the full design at
docs/plans/track-a3-team-collaboration.md (D1–D9 design decisions).

These models cover:
  - Comment creation and read (including threading, soft-delete, edit tracking)
  - Mention read and inbox surface
  - Review request creation, read, and lifecycle transition
  - Audit log read
  - The /me inbox response envelope

Phase 1.A scope is data models only. Endpoint wiring (Laravel API + Reverb
broadcasts) is Phase 1.B.

─── Cross-field validation (CollaborationCommentCreate) ────────────────────

target_kind and target_table form 12 valid combinations per the D1 design
decision. The cross-field validator (model_validator) rejects any combination
outside this set:

    document     → silver.reports
    answer_run   → silver.answer_runs
    map_feature  → silver.collars
                 → silver.drill_traces
                 → silver.mineral_claims
                 → silver.seismic_surveys

Any other combination raises a ValueError, preventing e.g. a document comment
accidentally pointing at silver.collars.

─── User ID type note ──────────────────────────────────────────────────────

public.users.id is a BIGINT auto-increment (Laravel $table->id()). User ID
fields in these models are `int` (not UUID) to match the actual database type.
Earlier review_queue models used UUID for user IDs — that was a type error that
would fail the PostgreSQL FK constraint. This module uses int throughout.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Type aliases matching CHECK constraint vocabularies in the migrations
# ---------------------------------------------------------------------------

TargetKind = Literal["document", "answer_run", "map_feature"]

TargetTable = Literal[
    "silver.reports",
    "silver.answer_runs",
    "silver.collars",
    "silver.drill_traces",
    "silver.mineral_claims",
    "silver.seismic_surveys",
]

ReviewRequestState = Literal["requested", "in_review", "resolved", "dismissed"]

AuditEntityKind = Literal["comment", "mention", "review_request"]

# The 12 valid (target_kind, target_table) pairs per D1.
# Used by the cross-field validator on CollaborationCommentCreate and
# CollaborationReviewRequestCreate to reject incoherent anchor combinations.
_VALID_KIND_TABLE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    [
        ("document", "silver.reports"),
        ("answer_run", "silver.answer_runs"),
        ("map_feature", "silver.collars"),
        ("map_feature", "silver.drill_traces"),
        ("map_feature", "silver.mineral_claims"),
        ("map_feature", "silver.seismic_surveys"),
    ]
)


def _validate_kind_table_pair(target_kind: str, target_table: str) -> None:
    """Raise ValueError when (target_kind, target_table) is not a valid D1 anchor pair.

    Called from model_validator on request models that carry both fields.
    """
    if (target_kind, target_table) not in _VALID_KIND_TABLE_PAIRS:
        valid_tables_for_kind = sorted(
            t for (k, t) in _VALID_KIND_TABLE_PAIRS if k == target_kind
        )
        raise ValueError(
            f"target_table '{target_table}' is not valid for target_kind '{target_kind}'. "
            f"Valid tables for '{target_kind}': {valid_tables_for_kind}. "
            "See §10v D1 for the complete anchor-type table."
        )


# ---------------------------------------------------------------------------
# Comment models
# ---------------------------------------------------------------------------


class CollaborationCommentCreate(BaseModel):
    """Request body for POST /api/v1/collaboration/comments.

    §10v Team Collaboration Phase 1.B endpoint.
    Plan doc: docs/plans/track-a3-team-collaboration.md §API surface.

    The server side parses body_markdown for @username patterns and writes
    mention rows before firing the Reverb broadcast.

    parent_comment_id is required to be a top-level comment's ID (D2 hybrid
    threading). The server enforces this invariant via the PostgreSQL trigger
    silver.collaboration_no_replies_of_replies_trigger(). If the FK points at
    a reply, the INSERT will raise a RAISE EXCEPTION with detail
    'replies_of_replies_not_supported'.

    The cross-field model_validator rejects incoherent (target_kind, target_table)
    combinations (e.g. target_kind='document' + target_table='silver.collars').
    See _VALID_KIND_TABLE_PAIRS for the 12 allowed combinations.
    """

    target_kind: TargetKind = Field(
        ...,
        description="Anchor type — must be 'document', 'answer_run', or 'map_feature'",
    )
    target_table: TargetTable = Field(
        ...,
        description=(
            "Target table containing the anchored record. "
            "Must be consistent with target_kind per D1 design decision."
        ),
    )
    target_id: UUID = Field(
        ...,
        description="Primary key of the anchored record in target_table",
    )
    parent_comment_id: UUID | None = Field(
        None,
        description=(
            "When set, this comment is a reply to the identified comment. "
            "The parent must be a top-level comment (D2: no replies-of-replies). "
            "The server enforces this via a PostgreSQL trigger."
        ),
    )
    body_markdown: str = Field(
        ...,
        min_length=1,
        max_length=8192,
        description=(
            "Comment body in Markdown. @username mentions are parsed server-side "
            "and create collaboration_mentions rows. Max 8192 characters."
        ),
    )

    @model_validator(mode="after")
    def validate_kind_table_consistency(self) -> CollaborationCommentCreate:
        """Reject target_table values inconsistent with target_kind.

        Validates against the 12 valid D1 anchor pairs. Raises ValueError
        if the combination is not in _VALID_KIND_TABLE_PAIRS.
        """
        _validate_kind_table_pair(self.target_kind, self.target_table)
        return self


class CollaborationCommentRead(BaseModel):
    """Full row shape for silver.collaboration_comments.

    §10v Team Collaboration — read response for GET /api/v1/collaboration/comments.
    Plan doc: docs/plans/track-a3-team-collaboration.md §Schema surface.

    deleted_at is included so the frontend can render [deleted] placeholders
    for soft-deleted comments while preserving reply thread structure (D2).
    The body_markdown field will be set to None by the controller on soft-deleted
    rows to avoid surfacing removed content while keeping the row visible for
    thread structure.
    """

    comment_id: UUID
    workspace_id: UUID
    target_project_id: UUID
    target_kind: TargetKind
    target_table: TargetTable
    target_id: UUID
    parent_comment_id: UUID | None
    author_user_id: int = Field(..., description="BIGINT FK to public.users(id)")
    body_markdown: str | None = Field(
        ...,
        description="Comment body; None for soft-deleted rows (controller redacts before returning)",
    )
    is_edited: bool
    edited_at: datetime | None
    deleted_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Mention models
# ---------------------------------------------------------------------------


class CollaborationMentionRead(BaseModel):
    """Full row shape for silver.collaboration_mentions.

    §10v Team Collaboration — read response for mention-related endpoints.
    Plan doc: docs/plans/track-a3-team-collaboration.md §Schema surface.

    read_at is NULL until POST /api/v1/collaboration/mentions/{mentionId}/read
    is called (or the bulk /read-all endpoint). The /me inbox queries
    WHERE read_at IS NULL for the unread count badge.
    """

    mention_id: UUID
    comment_id: UUID
    mentioned_user_id: int = Field(..., description="BIGINT FK to public.users(id)")
    workspace_id: UUID
    read_at: datetime | None
    created_at: datetime


class CollaborationMentionWithComment(CollaborationMentionRead):
    """CollaborationMentionRead extended with a comment preview for the inbox surface.

    §10v Team Collaboration — used in CollaborationInboxResponse.unread_mentions.
    Plan doc: docs/plans/track-a3-team-collaboration.md §D4 notification model.

    comment_preview is the first 200 characters of the comment's body_markdown,
    allowing the inbox to show context without loading the full comment thread.
    Truncation is applied by the controller, not by this model (the model accepts
    any length for flexibility; the controller guarantees ≤200 chars).
    """

    comment_preview: str = Field(
        ...,
        description=(
            "First 200 characters of the parent comment's body_markdown. "
            "Populated by the controller from the collaboration_comments row. "
            "Empty string if the parent comment is soft-deleted."
        ),
    )


# ---------------------------------------------------------------------------
# Review request models
# ---------------------------------------------------------------------------


class CollaborationReviewRequestCreate(BaseModel):
    """Request body for POST /api/v1/collaboration/review-requests.

    §10v Team Collaboration Phase 1.B endpoint.
    Plan doc: docs/plans/track-a3-team-collaboration.md §API surface.

    The server validates that assignee_user_id belongs to the same workspace
    as the requester before INSERT (D9: reuse the existing user→projects pivot).

    Same cross-field (target_kind, target_table) validator as
    CollaborationCommentCreate — rejects incoherent anchor combinations.
    """

    target_kind: TargetKind = Field(
        ...,
        description="Anchor type — must be 'document', 'answer_run', or 'map_feature'",
    )
    target_table: TargetTable = Field(
        ...,
        description=(
            "Target table containing the anchored record. "
            "Must be consistent with target_kind per D1 design decision."
        ),
    )
    target_id: UUID = Field(
        ...,
        description="Primary key of the anchored record in target_table",
    )
    assignee_user_id: int = Field(
        ...,
        description=(
            "BIGINT user ID of the geologist assigned to this review request. "
            "Server validates workspace membership before INSERT."
        ),
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Short description of what needs review. Max 256 characters.",
    )
    body_markdown: str | None = Field(
        None,
        max_length=8192,
        description=(
            "Optional extended description in Markdown. "
            "Max 8192 characters. None = no description provided."
        ),
    )
    due_at: datetime | None = Field(
        None,
        description=(
            "Optional deadline for the review. "
            "Surfaced in the §10w P2 amber affordance on ReviewRequestCard when approaching."
        ),
    )

    @model_validator(mode="after")
    def validate_kind_table_consistency(self) -> CollaborationReviewRequestCreate:
        """Reject target_table values inconsistent with target_kind.

        Validates against the 12 valid D1 anchor pairs. Raises ValueError
        if the combination is not in _VALID_KIND_TABLE_PAIRS.
        """
        _validate_kind_table_pair(self.target_kind, self.target_table)
        return self


class CollaborationReviewRequestRead(BaseModel):
    """Full row shape for silver.collaboration_review_requests.

    §10v Team Collaboration — read response for review request endpoints.
    Plan doc: docs/plans/track-a3-team-collaboration.md §Schema surface.

    State lifecycle (D6):
        requested → in_review → resolved
                             ↘ dismissed
        in_review → requested   (re-open)

    resolution_note is populated on resolved/dismissed transitions.
    """

    request_id: UUID
    workspace_id: UUID
    target_project_id: UUID
    target_kind: TargetKind
    target_table: TargetTable
    target_id: UUID
    requester_user_id: int = Field(..., description="BIGINT FK to public.users(id)")
    assignee_user_id: int = Field(..., description="BIGINT FK to public.users(id)")
    state: ReviewRequestState
    title: str
    body_markdown: str | None
    due_at: datetime | None
    state_changed_at: datetime
    state_changed_by: int = Field(..., description="BIGINT FK to public.users(id)")
    resolution_note: str | None
    created_at: datetime


class CollaborationReviewRequestTransition(BaseModel):
    """Request body for POST /api/v1/collaboration/review-requests/{requestId}/transition.

    §10v Team Collaboration Phase 1.B endpoint.
    Plan doc: docs/plans/track-a3-team-collaboration.md §D6 review lifecycle.

    State transitions (server-side enforcement — this model carries only the
    target state and optional resolution note):
        requested  → in_review | dismissed
        in_review  → resolved  | dismissed | requested (re-open)
        resolved   → (terminal — no further transitions)
        dismissed  → (terminal — no further transitions)

    The 'requested' value in to_state supports re-open (in_review → requested).
    The server rejects invalid transitions with a 422 and a structured error
    body indicating the current state and the allowed next states.

    resolution_note is required when to_state is 'resolved' or 'dismissed'.
    This is not enforced at the Pydantic layer (the controller validates it)
    to allow the model to be used in test contexts before Phase 1.B wiring.
    """

    to_state: ReviewRequestState = Field(
        ...,
        description=(
            "Target lifecycle state. "
            "Valid transitions are enforced server-side; "
            "resolved and dismissed are terminal — no further transitions allowed."
        ),
    )
    resolution_note: str | None = Field(
        None,
        description=(
            "Optional note explaining the resolution or dismissal. "
            "Required by convention (enforced by the controller) when "
            "to_state is 'resolved' or 'dismissed'."
        ),
    )


# ---------------------------------------------------------------------------
# Audit log model
# ---------------------------------------------------------------------------


class CollaborationAuditLogRead(BaseModel):
    """Full row shape for silver.collaboration_audit_log.

    §10v Team Collaboration — read response for audit history queries.
    Plan doc: docs/plans/track-a3-team-collaboration.md §Schema surface.

    event_type examples (not exhaustive — these are freeform strings):
        comment.created, comment.edited, comment.deleted,
        mention.created, mention.read,
        review_request.created, review_request.state_changed

    payload carries event-specific data (e.g. {from_state, to_state} for
    state_changed events, or {mentioned_user_id} for mention events).
    """

    audit_id: UUID
    workspace_id: UUID
    target_project_id: UUID
    entity_kind: AuditEntityKind
    entity_id: UUID
    event_type: str = Field(..., description="Event discriminator string, e.g. 'comment.created'")
    actor_user_id: int | None = Field(
        ...,
        description=(
            "BIGINT FK to public.users(id). "
            "None for system-initiated events (Dagster, scheduled jobs)."
        ),
    )
    payload: dict = Field(
        default_factory=dict,
        description="Event-specific payload JSONB, e.g. {from_state, to_state} for state transitions",
    )
    created_at: datetime


# ---------------------------------------------------------------------------
# Inbox response envelope
# ---------------------------------------------------------------------------


class CollaborationInboxResponse(BaseModel):
    """Response envelope for GET /api/v1/collaboration/inbox.

    §10v Team Collaboration Phase 1.B endpoint.
    Plan doc: docs/plans/track-a3-team-collaboration.md §D4 notification model.

    The /me inbox surface (§10v §Frontend surface) aggregates:
      1. unread_mentions — collaboration_mentions rows WHERE read_at IS NULL
         AND mentioned_user_id = current_user, enriched with a 200-char
         comment_preview from the parent comment.
      2. assigned_review_requests — collaboration_review_requests rows WHERE
         assignee_user_id = current_user AND state IN ('requested', 'in_review').

    Both lists are workspace-scoped (D9: workspace RBAC via the user→projects pivot).
    The inbox does NOT include resolved/dismissed review requests to keep the
    surface actionable per §10w P2 ("draw attention, don't direct").
    """

    unread_mentions: list[CollaborationMentionWithComment] = Field(
        default_factory=list,
        description=(
            "Unread @-mentions of the current user within the workspace. "
            "Ordered by created_at DESC (most recent first). "
            "Each mention includes a 200-char preview of the parent comment body."
        ),
    )
    assigned_review_requests: list[CollaborationReviewRequestRead] = Field(
        default_factory=list,
        description=(
            "Open review requests assigned to the current user "
            "(state IN ('requested', 'in_review')). "
            "Ordered by due_at ASC NULLS LAST, then created_at DESC."
        ),
    )
