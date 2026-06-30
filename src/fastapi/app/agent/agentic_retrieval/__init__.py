"""§04j Agentic Retrieval — Track A.2 (Phase 2 of the geologist-question plan).

Routes each query to one of six intent-specific subgraphs, each with its own
retrieval strategy and OIUR answer-template variant. Built on top of
LangGraph; gated by ``settings.AGENTIC_RETRIEVAL_V2_ENABLED`` (separate flag
from Phase 1's ``GEO_ANSWER_OIUR_ENABLED``).

Phase 2 sub-step landings:
  - 2.2 → :mod:`app.agent.agentic_retrieval.intent_classifier`
  - 2.3 → ``state.py``, ``graph.py``, ``nodes.py``, ``subgraphs/*``
  - 2.4 → ``context_envelope.py``

Until the LangGraph skeleton lands in 2.3, this module exports only the
intent classifier. The orchestrator does not call into here yet.
"""

from __future__ import annotations

from app.agent.agentic_retrieval.context_envelope import (
    DEFAULT_QUERY_MODE,
    DEFAULT_REPORTING_CODE,
    EMPTY_ENVELOPE,
    ContextEnvelope,
    EnvelopeRoutingDecision,
    QueryMode,
    apply_envelope_overrides,
    unspecified_field_descriptions,
)
from app.agent.agentic_retrieval.graph import (
    get_compiled_graph,
    run_agentic_retrieval,
)
from app.agent.agentic_retrieval.intent_classifier import (
    INTENT_LABELS,
    Intent,
    IntentResult,
    classify_intent,
    classify_intent_sync,
)
from app.agent.agentic_retrieval.preprocessor import (
    FIELD_MODE_MAX_CHUNKS,
    TOOL_DATA_SOURCE_MAP,
    RetrievalFilters,
    preprocess_envelope,
)
from app.agent.agentic_retrieval.qaqc_availability import (
    QaqcAvailability,
    QaqcGroupAvailability,
    detect_qaqc_availability,
)
from app.agent.agentic_retrieval.retrieval_profile import (
    RetrievalProfile,
    profile_for_intent,
)
from app.agent.agentic_retrieval.state import AgenticRetrievalState

__all__ = [
    "AgenticRetrievalState",
    "ContextEnvelope",
    "DEFAULT_QUERY_MODE",
    "DEFAULT_REPORTING_CODE",
    "EMPTY_ENVELOPE",
    "EnvelopeRoutingDecision",
    "FIELD_MODE_MAX_CHUNKS",
    "INTENT_LABELS",
    "Intent",
    "IntentResult",
    "QaqcAvailability",
    "QaqcGroupAvailability",
    "QueryMode",
    "RetrievalFilters",
    "RetrievalProfile",
    "TOOL_DATA_SOURCE_MAP",
    "apply_envelope_overrides",
    "classify_intent",
    "classify_intent_sync",
    "detect_qaqc_availability",
    "get_compiled_graph",
    "preprocess_envelope",
    "profile_for_intent",
    "run_agentic_retrieval",
    "unspecified_field_descriptions",
]
