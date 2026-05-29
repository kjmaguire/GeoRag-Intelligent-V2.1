"""agent/pipeline/ — Wave 2.C orchestrator decomposition subpackage.

This subpackage holds the residual phase helpers extracted from
``app.agent.orchestrator`` during the Wave 2.C code-quality pass.
The orchestrator's public entry point (``run_deterministic_rag``) stays in
``app.agent.orchestrator``; these modules own the phase-level helpers it
delegates to.

Module layout
-------------
decomposition
    Phase 1+2 surface: workspace feature-flag check, agentic decomposition
    hook, evidence-block formatting, anaphora resolution, conversation-state
    read/write.

branching
    Phase 3 surface: D4 conditional-branching evaluators (post_decomposition,
    post_retrieval, post_binding) and the SSE-emit helper.

verification
    Phase 4+5 surface: D5 MAX_REVISE_COUNT constant, claim-verification
    builder, sub-query revise loop, continued-empty escalation, spatial and
    temporal claim verification.

Backward compatibility
----------------------
Every public name defined here is also re-exported from
``app.agent.orchestrator`` so existing import paths keep working.  See the
re-export block at the bottom of orchestrator.py.

Future work
-----------
Wave 3 may extract the synthesis context-builder and classifier-fallback
merge into further sub-modules.  The ``run_deterministic_rag`` function is
intentionally kept in orchestrator.py as the single request-flow coordinator.
"""

from app.agent.pipeline.branching import (
    _decide_post_binding,
    _decide_post_decomposition,
    _decide_post_retrieval,
    _emit_agentic_decision,
    _record_decision,
)
from app.agent.pipeline.decomposition import (
    _format_agentic_evidence_block,
    _is_agentic_retrieval_enabled,
    _maybe_read_and_resolve_anaphora,
    _maybe_run_agentic_decomposition,
    _maybe_write_conversation_state,
)
from app.agent.pipeline.verification import (
    MAX_REVISE_COUNT,
    _build_claim_verifications,
    _escalate_continued_empty,
    _revise_failing_subqueries,
    _run_spatial_temporal_verification,
)

__all__ = [
    # decomposition
    "_is_agentic_retrieval_enabled",
    "_format_agentic_evidence_block",
    "_maybe_run_agentic_decomposition",
    "_maybe_read_and_resolve_anaphora",
    "_maybe_write_conversation_state",
    # branching
    "_record_decision",
    "_decide_post_decomposition",
    "_decide_post_retrieval",
    "_decide_post_binding",
    "_emit_agentic_decision",
    # verification
    "MAX_REVISE_COUNT",
    "_build_claim_verifications",
    "_revise_failing_subqueries",
    "_escalate_continued_empty",
    "_run_spatial_temporal_verification",
]
