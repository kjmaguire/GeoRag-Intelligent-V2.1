"""Decision Intelligence Layer (§9.10 / §21) — doc-phase 92.

Single facade module that records R3+ decisions into
`silver.decision_records` + related tables. Eight decision types
per §21.3 funnel through here; the caller stays simple while the
schema + audit emission stay consistent.

Live behavior lands when the eight capture hooks wire up (§9.10's
hook-into-existing-flows pass). Skeleton-stage: API contract +
single-function entry point locked.

Typical call (when behavior lands):

    from app.services.decision_intelligence import record_decision

    await record_decision(
        conn,
        workspace_id=ws,
        decision_type="target_recommendation",
        recommendation="Rank zone Z-42 first",
        human_decision="accepted",
        reason="Anomaly cluster + alteration overlap matches model",
        decided_by_user_id=user_id,
        evidence_chunk_ids=["chunk_a", "chunk_b"],
        options_considered=[
            {"label": "Z-17", "description": "..."},
            {"label": "Z-42", "description": "..."},  # chosen
        ],
        uncertainty=0.25,
    )
"""
from app.services.decision_intelligence.recorder import (
    DecisionType,
    record_decision,
)
from app.services.decision_intelligence.summary import (
    ALL_DECISION_TYPES,
    DecisionTypeCounts,
    WorkspaceDecisionSummary,
    get_workspace_decision_summary,
)

__all__ = [
    "ALL_DECISION_TYPES",
    "DecisionType",
    "DecisionTypeCounts",
    "WorkspaceDecisionSummary",
    "get_workspace_decision_summary",
    "record_decision",
]
