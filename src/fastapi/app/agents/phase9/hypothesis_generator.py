"""Hypothesis Generator Agent (§9.5 / §20.3).

Doc-phase 91 skeleton → doc-phase 134 graduation.

Generates competing hypotheses for a parent_question. The live
orchestration is in
`app.services.geological_reasoning.hypothesis_generator`; this agent
wrapper exposes it through the §35.1 agent operational contract
(timeouts, idempotency, audit, etc.).

Today's content (synthetic stub):
- 3 hypotheses per call (labels A, B, C)
- A = primary working hypothesis (confidence 0.55)
- B = competing alternative (confidence 0.30)
- C = null hypothesis (confidence 0.15)
- Evidence chunks distributed across roles
  (supporting/contradicting/missing/recommended_test)
- `description` carries `[synthetic_stub doc-phase 134]` so the
  Hypothesis Workspace surface can badge synthetic rows

Real LLM-driven reasoning replaces `_synthetic_hypothesis_set` in
the service module without touching this wrapper or the schema.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent
from app.services.geological_reasoning import generate_hypotheses_for_question


@georag_agent(
    name="Hypothesis Generator Agent",
    risk_tier="R2",  # Writes hypothesis + evidence_link rows
    version="0.2.0",
)
async def hypothesis_generator(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    parent_question: str,
    candidate_evidence_chunk_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Generate competing hypotheses for the parent_question.

    Args:
        workspace_id: RLS scope; hypotheses are workspace-private.
        parent_question: the user's question or retrieval target.
        candidate_evidence_chunk_ids: chunks the retrieval layer
            already surfaced; the synthetic stub distributes these
            across the hypotheses with role tags. Real LLM evaluator
            will re-rank them per hypothesis.

    Returns:
        A summary dict with the new hypothesis_ids + labels +
        evidence link total.
    """
    result = await generate_hypotheses_for_question(
        workspace_id=workspace_id,
        parent_question=parent_question,
        candidate_evidence_chunk_ids=candidate_evidence_chunk_ids,
    )
    return {
        "workspace_id": result.workspace_id,
        "parent_question": result.parent_question,
        "hypothesis_ids": [str(h) for h in result.hypothesis_ids],
        "labels": list(result.labels),
        "evidence_link_count": result.evidence_link_count,
    }
