"""LLM Incident Diagnosis Graph (§12.9) — doc-phase 102 skeleton.

LangGraph that classifies + diagnoses LLM-related incidents:
- Hallucination reports (claim cited evidence that doesn't support it)
- Refusal failures (refused when it shouldn't have, or vice versa)
- Citation drift (citations don't resolve to chunks)
- Numeric grounding failures (claimed values don't match silver.*)
- Tone / language template violations (§2.9 / §18.1)
- Cost / latency spikes

Each detected incident type routes to a tailored remediation
suggestion (re-rank prompt, tighten retrieval gate, adjust
classifier threshold, etc.).

Already-stubbed phase0 LLM Incident Diagnosis agent (in
phase0_agents) graduates here when langgraph wiring lands.
"""
from app.services.llm_incident_diagnosis.state import (
    IncidentDiagnosisState,
    IncidentKind,
)
from app.services.llm_incident_diagnosis.nodes import (
    classify_incident,
    gather_traces,
    identify_root_cause,
    propose_remediation,
    record_diagnosis,
)

__all__ = [
    "IncidentDiagnosisState",
    "IncidentKind",
    "classify_incident",
    "gather_traces",
    "identify_root_cause",
    "propose_remediation",
    "record_diagnosis",
]
