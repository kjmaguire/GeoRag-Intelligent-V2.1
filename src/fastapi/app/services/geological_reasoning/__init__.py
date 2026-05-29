"""Geological Reasoning service (§9.10) — doc-phase 134.

Live orchestration for the competing-hypotheses register. Backs the
§9.10 ai_suggested hypothesis writer side of the Hypothesis
Workspace admin surface (doc-phase 131).

The orchestration is fully live (RLS-aware DB writes, audit anchors,
idempotency keys on parent_question). The per-question hypothesis
generator (`_synthetic_hypothesis_set`) is a deterministic stub
until the real LLM reasoning agent graduates — same pattern
doc-phase 132 used for `evaluate_workspace`.
"""
from app.services.geological_reasoning.hypothesis_generator import (
    HypothesisDraft,
    EvidenceLinkDraft,
    HypothesisGenerationResult,
    generate_hypotheses_for_question,
)

__all__ = [
    "HypothesisDraft",
    "EvidenceLinkDraft",
    "HypothesisGenerationResult",
    "generate_hypotheses_for_question",
]
