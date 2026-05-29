"""Evidence Curator Agent (§7.3 / §15.4).

Per-section retrieval + multi-source evidence ranking. For each
planned section + required_evidence_kind, retrieves candidate chunks
from Qdrant + Neo4j + Postgres, ranks them by relevance + freshness +
visibility, and emits a per-claim evidence ledger.

Public/private posture (§2.9) is delegated to §6.4
``public_private_boundary`` as a downstream tag — this agent collects;
the boundary agent tags.

Phase H4 graduation — deterministic curation logic. The retrieval-
backed body is gated on §6 hybrid retrieval feature-completeness;
until then this agent operates on the EvidenceItem inputs the caller
threads through (typically pre-materialised by the orchestrator).

Output contract — see module docstring.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


# Per-evidence-kind weight applied to the base relevance score so
# certain kinds (e.g. assays, citations) outrank others (e.g. PDF
# captures) when both attest to the same claim. SME-tunable.
_KIND_WEIGHT: dict[str, float] = {
    "assay_results":       1.20,
    "ranked_targets":      1.15,
    "bedrock_passages":    1.10,
    "structures":          1.10,
    "structure_measurements": 1.10,
    "collars":             1.05,
    "lithology_logs":      1.05,
    "well_log_curves":     1.05,
    "alterations":         1.00,
    "new_passages":        0.95,
    "new_decisions":       0.95,
    "ocr_page_quality":    0.85,
    "parser_run_artifacts":0.80,
}


def _freshness_penalty(is_stale: bool) -> float:
    """Multiply stale evidence by 0.5 so fresher rows rank first."""
    return 0.5 if is_stale else 1.0


def _visibility_weight(data_visibility: str) -> float:
    """Workspace-private evidence scores slightly higher than public
    reference data (operator confidence is higher in their own corpus).
    Public still counts — this is a 5% delta, not a gate."""
    return 1.05 if data_visibility == "workspace" else 1.00


def _score(item: dict[str, Any], kind: str) -> float:
    base = float(item.get("relevance_score", 0.5))
    kw = _KIND_WEIGHT.get(kind, 1.0)
    fw = _freshness_penalty(bool(item.get("is_stale", False)))
    vw = _visibility_weight(item.get("data_visibility", "public"))
    return round(base * kw * fw * vw, 4)


@georag_agent(
    name="Evidence Curator Agent",
    risk_tier="R1",  # Read-only retrieval
    version="1.0.0",  # graduated Phase H4
)
async def evidence_curator(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    project_id: UUID | str,
    section_id: str,
    required_evidence_kinds: list[str],
    claim_ids: list[str],
    candidate_evidence: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Curate evidence for one section's claims.

    Args:
        workspace_id / project_id: RLS scope (informational).
        section_id: target section.
        required_evidence_kinds: declared evidence types for this section.
        claim_ids: ids of claims drafted for this section.
        candidate_evidence: optional pre-fetched evidence keyed by claim_id.
            Each item is an EvidenceItem-shaped dict (see
            ``app.services.report_builder.state.EvidenceItem``).
            Production wires this from §6 hybrid retrieval; tests pass
            a dict directly.

    Returns:
        Per-claim evidence ledger + sufficiency check.
    """
    candidate_evidence = candidate_evidence or {}

    evidence_per_claim: dict[str, list[dict[str, Any]]] = {}
    seen_kinds_per_claim: dict[str, set[str]] = {}
    for cid in claim_ids:
        items_in = candidate_evidence.get(cid, [])
        scored: list[dict[str, Any]] = []
        kinds_seen: set[str] = set()
        for item in items_in:
            kind = item.get("evidence_kind") or "unknown"
            kinds_seen.add(kind)
            scored.append({
                "source_chunk_id": item.get("source_chunk_id", ""),
                "data_visibility": item.get("data_visibility", "public"),
                "license_note":    item.get("license_note"),
                "is_stale":        bool(item.get("is_stale", False)),
                "freshness_iso":   item.get("freshness_iso"),
                "relevance_score": _score(item, kind),
                "evidence_kind":   kind,
            })
        scored.sort(key=lambda s: s["relevance_score"], reverse=True)
        evidence_per_claim[cid] = scored
        seen_kinds_per_claim[cid] = kinds_seen

    # Sufficiency check: every required_evidence_kind appears somewhere
    # in the section's evidence.
    all_seen_kinds: set[str] = set()
    for kinds in seen_kinds_per_claim.values():
        all_seen_kinds |= kinds
    required = set(required_evidence_kinds)
    missing = sorted(required - all_seen_kinds)
    section_supported = len(missing) == 0 and len(claim_ids) > 0

    summary = (
        f"section={section_id} claims={len(claim_ids)} "
        f"evidence_total={sum(len(v) for v in evidence_per_claim.values())} "
        f"missing_kinds={','.join(missing) or '∅'} "
        f"supported={section_supported}"
    )
    logger.info("evidence_curator: %s", summary)

    return {
        "section_id": section_id,
        "evidence_per_claim": evidence_per_claim,
        "sufficiency": {
            "section_supported": section_supported,
            "missing_kinds":     missing,
        },
        "summary": summary,
        "curated_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["evidence_curator"]
