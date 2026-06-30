"""Structured answer contracts for the GeoRAG synthesis layer.

Phase 1 / Step 1.2 — OIUR (Observations → Interpretations → Uncertainty →
Recommended actions) answer schema. Wraps the existing ``Citation`` provenance
contract from ``app.models.rag`` — does not replace it.
"""

from __future__ import annotations

from app.agent.schemas.geo_answer import (
    GEO_ANSWER_SCHEMA_VERSION,
    ConfidenceBlock,
    ConfidenceLevel,
    DecisionSupport,
    GeoAnswer,
    Interpretation,
    Observation,
    RecommendedAction,
    SectionEmpty,
    UncertaintyBlock,
)

__all__ = [
    "ConfidenceBlock",
    "ConfidenceLevel",
    "DecisionSupport",
    "GEO_ANSWER_SCHEMA_VERSION",
    "GeoAnswer",
    "Interpretation",
    "Observation",
    "RecommendedAction",
    "SectionEmpty",
    "UncertaintyBlock",
]
