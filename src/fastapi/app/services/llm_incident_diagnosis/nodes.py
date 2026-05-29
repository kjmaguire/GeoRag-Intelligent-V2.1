"""LLM Incident Diagnosis Graph nodes (§12.9).

5 async nodes. Each takes IncidentDiagnosisState and returns
(possibly mutated) state. LangGraph wiring lives in the orchestrator.

Phase H4 graduation — deterministic rule-table classifier + heuristic
trace-gather + template-remediation. The §25.4 LLM-driven nodes plug
in when prompt-lock + langgraph wiring ships; the state contract is
preserved.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.services.llm_incident_diagnosis.state import (
    IncidentDiagnosisState,
    IncidentKind,
)


log = logging.getLogger(__name__)


# Keyword → classified kind. Order matters (first match wins).
_CLASSIFICATION_KEYWORDS: list[tuple[str, IncidentKind]] = [
    ("hallucinat",          "hallucination"),
    ("fabricat",            "hallucination"),
    ("made up",             "hallucination"),
    ("missing citation",    "citation_drift"),
    ("no source",           "citation_drift"),
    ("wrong number",        "numeric_grounding_failure"),
    ("off by",              "numeric_grounding_failure"),
    ("refuse",              "refusal_failure"),
    ("didn't answer",       "refusal_failure"),
    ("tone",                "tone_template_violation"),
    ("violated",            "tone_template_violation"),
    ("timeout",             "latency_spike"),
    ("slow",                "latency_spike"),
    ("expensive",           "cost_spike"),
    ("cost",                "cost_spike"),
]


_REMEDIATION_BY_KIND: dict[str, tuple[str, str]] = {
    "hallucination": (
        "rerun_with_layer3_strict",
        "Re-run with §04i Layer 3 numerical-claim verification "
        "explicitly enabled. Escalate to Conflict Resolver Agent "
        "for the affected claim ledger if the failure repeats.",
    ),
    "refusal_failure": (
        "lower_retrieval_floor",
        "Inspect §04i Layer 1 retrieval quality gate. Likely no "
        "retrieved chunks crossed the threshold; lower the floor "
        "temporarily or escalate to add evidence to the workspace.",
    ),
    "citation_drift": (
        "rerun_attach_citations",
        "Re-emit through §7.6 attach_citations pipeline. Audit the "
        "§04i Layer 5 chunk-provenance gate if drift persists.",
    ),
    "numeric_grounding_failure": (
        "claim_validator_numeric_recheck",
        "Re-validate via Claim Validator Agent (§7.4) numerical "
        "layer. Compare to cited evidence; if values diverge, the "
        "LLM hallucinated and the answer should be re-emitted with "
        "the corrected value.",
    ),
    "tone_template_violation": (
        "rerun_presentation_coach",
        "Re-run through Presentation Coach (§7.6) with the same "
        "tone setting. Workspace tone-config drift is common.",
    ),
    "cost_spike": (
        "audit_token_usage_dashboard",
        "Compare prompt + response token counts against the §16.3 "
        "cost-burn dashboard. Long context windows or retried tool "
        "calls are the usual cause.",
    ),
    "latency_spike": (
        "check_vllm_qdrant_queues",
        "Check vLLM queue depth + Qdrant retrieval latency in §16.3 "
        "services dashboard. Cold model after worker restart is "
        "common.",
    ),
    "other": (
        "manual_operator_triage",
        "Manual operator triage required — no auto-remediation "
        "pattern matched the classified_kind.",
    ),
}


async def classify_incident(
    state: IncidentDiagnosisState,
) -> IncidentDiagnosisState:
    """Refine triage_kind into classified_kind via keyword classifier."""
    haystack = " ".join(
        str(v) for v in (state.initial_payload or {}).values()
    ).lower()
    classified: IncidentKind = state.triage_kind
    confidence = 0.5
    for keyword, kind in _CLASSIFICATION_KEYWORDS:
        if keyword in haystack:
            classified = kind
            confidence = 0.85
            break

    log.info(
        "classify_incident incident_id=%s triage=%s classified=%s confidence=%.2f",
        state.incident_id, state.triage_kind, classified, confidence,
    )
    return state.model_copy(update={
        "classified_kind":           classified,
        "classification_confidence": confidence,
    })


async def gather_traces(
    state: IncidentDiagnosisState,
) -> IncidentDiagnosisState:
    """Pull correlated trace excerpts.

    Phase H4 — deterministic stub. Records the intent (trace sources
    + time window) in `trace_excerpts`. Real LangFuse + audit_ledger
    + workflow_runs fetch lands when the orchestrator wires a DB
    connection through.
    """
    excerpts = list(state.trace_excerpts or [])
    if not excerpts:
        excerpts.append({
            "source":      "audit.audit_ledger",
            "summary":     (
                "Trace fetch pending — operator runs §25 trace helper "
                "with incident time window."
            ),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        })
    log.info(
        "gather_traces incident_id=%s excerpts=%d",
        state.incident_id, len(excerpts),
    )
    return state.model_copy(update={"trace_excerpts": excerpts})


async def identify_root_cause(
    state: IncidentDiagnosisState,
) -> IncidentDiagnosisState:
    """Draft a root-cause hypothesis from classified_kind + traces."""
    kind = state.classified_kind or state.triage_kind
    hypothesis = (
        f"Likely root-cause cluster: **{kind}**. "
        f"Refer to the §10.12 incident-runbook section for {kind} "
        f"for the prescribed triage steps."
    )
    log.info(
        "identify_root_cause incident_id=%s root_cause=%s",
        state.incident_id, kind,
    )
    return state.model_copy(update={
        "root_cause_hypothesis": hypothesis,
        "root_cause_evidence":   [{
            "classifier_confidence": state.classification_confidence or 0.5,
            "trace_excerpt_count":   len(state.trace_excerpts or []),
        }],
    })


async def propose_remediation(
    state: IncidentDiagnosisState,
) -> IncidentDiagnosisState:
    """Suggest a remediation action keyed on classified_kind."""
    kind = state.classified_kind or state.triage_kind
    remediation_kind, remediation_text = _REMEDIATION_BY_KIND.get(
        kind, _REMEDIATION_BY_KIND["other"],
    )
    log.info(
        "propose_remediation incident_id=%s kind=%s remediation_kind=%s",
        state.incident_id, kind, remediation_kind,
    )
    return state.model_copy(update={
        "proposed_remediation_kind":    remediation_kind,
        "proposed_remediation_payload": {
            "remediation_text": remediation_text,
            "applies_to_kind":  kind,
        },
    })


async def record_diagnosis(
    state: IncidentDiagnosisState,
) -> IncidentDiagnosisState:
    """Persist the diagnosis manifest in state.

    Phase H4 — marks the diagnosis as recorded + stamps completed_at.
    The orchestrator handles the audit_ledger emit + optional
    decision_records row via its DB connection; state is the
    in-memory record of what the graph decided.
    """
    summary = (
        f"incident_id={state.incident_id} "
        f"classified_kind={state.classified_kind or state.triage_kind} "
        f"remediation={state.proposed_remediation_kind or 'unknown'}"
    )
    log.info("record_diagnosis: %s", summary)
    return state.model_copy(update={
        "diagnosis_recorded": True,
        "completed_at":       datetime.now(timezone.utc),
    })


__all__ = [
    "classify_incident",
    "gather_traces",
    "identify_root_cause",
    "propose_remediation",
    "record_diagnosis",
]
