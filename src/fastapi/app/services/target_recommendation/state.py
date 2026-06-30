"""TargetRecommendationState — Pydantic state for the §18.2 graph.

Threaded through every node. Each node mutates a copy and returns
the next state. LangGraph reduces these into a channel-mapped state
machine when the langgraph wiring lands.

Doc-phase 86. Same skeleton-first pattern as §7.1
`ReportBuilderState`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

ScoringKind = Literal["weighted", "xgboost", "ensemble"]


class CandidateZone(BaseModel):
    """One generated polygon under consideration."""

    zone_id: UUID
    geom_wkt: str                # PostGIS WKT for the polygon (EPSG:4326)
    evidence_payload: dict[str, Any] = Field(default_factory=dict)


class ScoreFactor(BaseModel):
    """One factor's contribution to a zone's aggregate score."""

    factor_name: str
    factor_value: float          # raw factor measurement (0..1 typical)
    factor_weight: float         # weight from target_model_version
    contribution: float          # value * weight (signed)
    evidence_chunk_ids: list[str] = Field(default_factory=list)


class ZoneScore(BaseModel):
    """One zone's aggregate score + per-factor breakdown."""

    zone_id: UUID
    aggregate_score: float
    aggregate_uncertainty: float | None = None
    factors: list[ScoreFactor] = Field(default_factory=list)


class UncertaintyEntry(BaseModel):
    """One uncertainty record — per-factor or aggregate."""

    factor_name: str | None      # None = aggregate uncertainty
    uncertainty_kind: str        # e.g. "data_sparsity", "model_drift"
    uncertainty_value: float
    method: Literal["bayesian", "bootstrap", "analytical", "heuristic"]
    payload: dict[str, Any] = Field(default_factory=dict)


class RankedTarget(BaseModel):
    """One zone + rank + per-target rationale."""

    zone_id: UUID
    rank: int                    # 1-based; lower = higher priority
    aggregate_score: float
    explanation_markdown: str
    factors: list[ScoreFactor] = Field(default_factory=list)


class TargetRecommendationState(BaseModel):
    """Graph state threaded through all 12 §18.2 nodes."""

    schema_version: int = 1

    # Identity
    run_id: UUID
    workspace_id: UUID
    project_id: UUID
    requested_by_user_id: int
    aoi_geom_wkt: str            # project AOI polygon (EPSG:4326)

    # Deposit model + scoring
    target_model_id: UUID | None = None
    target_model_version_id: UUID | None = None
    scoring_kind: ScoringKind = "weighted"
    workspace_playbook: dict[str, Any] = Field(default_factory=dict)

    # Evidence
    private_evidence: dict[str, Any] = Field(default_factory=dict)
    public_evidence: dict[str, Any] = Field(default_factory=dict)

    # Zones
    candidate_zones: list[CandidateZone] = Field(default_factory=list)
    excluded_zone_ids: list[UUID] = Field(default_factory=list)

    # Scoring
    scores: list[ZoneScore] = Field(default_factory=list)
    uncertainties: list[UncertaintyEntry] = Field(default_factory=list)

    # Ranked output
    ranked_targets: list[RankedTarget] = Field(default_factory=list)

    # Map outputs (URIs on SeaweedFS or Martin layer references)
    map_layer_uris: dict[str, str] = Field(default_factory=dict)

    # Routing
    sent_to_review_cockpit: bool = False
    review_cockpit_url: str | None = None

    # Telemetry
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
