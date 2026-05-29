"""Geologist Sign-Off Agent (§8.5 / §18.4).

Manages the R5 approval flow with QP credential verification per
§19.6.

Phase H4 graduation — the agent now records sign-off decisions in
the expected envelope shape (target_recommendations_hash +
claim_ledger_hash + audit-ready payload). The real DB write to
`targeting.target_review_decisions` happens when the orchestrator
threads this through with a live asyncpg connection; the agent
itself is pure-function over its inputs + emits the structured
decision row.

R5 = highest risk tier. Per §29.6.1, QP credential verification is
staffed-ops work — the agent gates on caller-provided
`credential_verified` boolean; the agent will not auto-verify.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


Decision = Literal["accepted", "modified", "rejected", "signed_off"]


def _hash_payload(payload: Any) -> str:
    """Stable SHA-256 hex over a canonical JSON serialisation."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@georag_agent(
    name="Geologist Sign-Off Agent",
    risk_tier="R5",
    version="1.0.0",  # graduated Phase H4
)
async def geologist_signoff(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    target_id: UUID | str,
    qp_user_id: int,
    qp_credential_id: str,
    decision: Decision,
    rationale: str,
    qp_signature_method: str,
    credential_verified: bool = False,
    target_recommendations_payload: dict[str, Any] | None = None,
    claim_ledger_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record an R5 sign-off decision.

    Args:
        workspace_id: RLS scope (informational; caller writes the
            row through their own conn).
        target_id: recommendation under review.
        qp_user_id / qp_credential_id: QP identity.
        decision: accepted | modified | rejected | signed_off.
        rationale: free-text justification.
        qp_signature_method: "wet_signature" | "digital_token" | "manual".
        credential_verified: caller-driven gate. The agent REFUSES to
            return a `signed_off` decision unless this is True.
        target_recommendations_payload / claim_ledger_payload: optional
            proof anchors; the agent hashes them into the envelope.

    Returns:
        Sign-off envelope ready to insert into
        `targeting.target_review_decisions`.

    Raises:
        ValueError if decision='signed_off' and credential_verified=False.
    """
    if decision == "signed_off" and not credential_verified:
        raise ValueError(
            "R5 invariant: signed_off requires credential_verified=True. "
            "QP credential verification is staffed-ops work — the agent "
            "will not auto-verify."
        )

    review_id = uuid4()
    target_hash = _hash_payload(target_recommendations_payload or {"target_id": str(target_id)})
    ledger_hash = _hash_payload(claim_ledger_payload or {"target_id": str(target_id)})

    envelope = {
        "review_id":                     str(review_id),
        "workspace_id":                  str(workspace_id),
        "target_id":                     str(target_id),
        "qp_user_id":                    qp_user_id,
        "qp_credential_id":              qp_credential_id,
        "credential_verified_at":        (
            datetime.now(timezone.utc).isoformat() if credential_verified else None
        ),
        "decision":                      decision,
        "rationale":                     rationale,
        "qp_signature_method":           qp_signature_method,
        "target_recommendations_hash":   target_hash,
        "claim_ledger_hash":             ledger_hash,
        "signed_at":                     datetime.now(timezone.utc).isoformat(),
        "audit_action_type":             f"target.r5_signoff.{decision}",
    }
    logger.info(
        "geologist_signoff: target=%s decision=%s verified=%s qp=%s",
        target_id, decision, credential_verified, qp_user_id,
    )
    return envelope


__all__ = ["geologist_signoff", "Decision"]
