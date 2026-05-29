"""Plan §4b Stages 3 + 4 — strategy appliers.

Pure functions that translate a :class:`RepairStrategy` into the
concrete state mutations / system_prompt suffixes the orchestrator
applies before re-issuing the retrieval pipeline.

These are the leaf functions; the orchestrator loop that calls them
in sequence (per ``repair_loop_spec.md`` §4) is wire work that lands
once Stage 2 telemetry shows what strategies fire most often.

The split:

  - :func:`apply_llm_only_strategy` — Stage 3, loop-friendly,
    LLM-only re-issues. Returns a ``system_prompt`` SUFFIX the
    orchestrator appends to the existing prompt + re-runs
    ``_call_llm``. No retrieval re-issue.

  - :func:`apply_retrieval_strategy` — Stage 4, loop-friendly,
    retrieval-side. Returns a ``state-mutation`` dict the
    orchestrator merges into the state before re-running
    ``execute_node``.

Both are pure — they don't touch I/O. Caller threads the returned
data into the actual state / prompt.
"""

from __future__ import annotations

from typing import Any

from app.agent.repair_strategy import RepairStrategy


__all__ = [
    "LLM_STRATEGY_SUFFIXES",
    "apply_llm_only_strategy",
    "apply_retrieval_strategy",
]


# ---------------------------------------------------------------------------
# Stage 3 — LLM-only suffixes
# ---------------------------------------------------------------------------


LLM_STRATEGY_SUFFIXES: dict[str, str] = {
    RepairStrategy.REPHRASE_NUMERIC_CLAIM.value: (
        "\n\n[REPAIR INSTRUCTION] A prior attempt at this answer included "
        "numeric values that could not be verified against the retrieved "
        "evidence. ON THIS ATTEMPT: any numeric claim you cannot anchor "
        "to a specific citation MUST be marked as ESTIMATED in the text "
        "(e.g. \"approximately 250 m (ESTIMATED)\") or omitted. Do not "
        "fabricate precise figures."
    ),
    RepairStrategy.REQUEST_CITATION_RETRY.value: (
        "\n\n[REPAIR INSTRUCTION] A prior attempt at this answer had "
        "incomplete citation coverage. ON THIS ATTEMPT: every factual "
        "claim, every numeric value, and every entity reference MUST be "
        "followed by an inline citation marker (e.g. [DATA:1]). If you "
        "cannot cite a claim, omit it from the answer."
    ),
}


def apply_llm_only_strategy(strategy: RepairStrategy) -> str | None:
    """Return the system_prompt suffix for a Stage 3 strategy, or None
    when the strategy isn't Stage-3-eligible.

    The caller appends this suffix to its system prompt and re-runs the
    LLM call. No retrieval state changes.
    """
    if not isinstance(strategy, RepairStrategy):
        return None
    return LLM_STRATEGY_SUFFIXES.get(strategy.value)


# ---------------------------------------------------------------------------
# Stage 4 — Retrieval-side state mutations
# ---------------------------------------------------------------------------


def apply_retrieval_strategy(
    strategy: RepairStrategy,
    state_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Return the state-mutation dict for a Stage 4 strategy.

    Caller merges the result into the AgenticRetrievalState before
    re-running execute_node. Returns an empty dict when the strategy
    isn't Stage-4-eligible.

    The ``state_snapshot`` is a read-only view of the current state's
    relevant fields:
      {
        "retrieval_profile": {<RetrievalProfile field dump>},
        "retrieval_filters": {<RetrievalFilters field dump>},
        "context_envelope":  {<ContextEnvelope field dump or None>},
      }

    None / missing fields are treated as defaults; the function never
    raises.
    """
    if not isinstance(strategy, RepairStrategy):
        return {}

    profile = state_snapshot.get("retrieval_profile") or {}
    filters = state_snapshot.get("retrieval_filters") or {}

    if strategy == RepairStrategy.LOOSEN_FILTERS:
        # Drop the most restrictive filter — date constraints first
        # (they correlate most with empty results), then data-source
        # restrictions, then mode-specific limits.
        new_filters = dict(filters)
        for field in ("from_year", "to_year", "year_range_strict"):
            if field in new_filters:
                new_filters[field] = None
        if "allowed_data_sources" in new_filters and new_filters["allowed_data_sources"]:
            new_filters["allowed_data_sources"] = []
        return {"retrieval_filters": new_filters, "_loosen_applied": True}

    if strategy == RepairStrategy.BROADEN_KNN:
        new_profile = dict(profile)
        current = int(new_profile.get("candidate_count_pre_rerank", 40) or 40)
        # Double it; cap at 200 (the QDRANT_DENSE_TOP_K_MAX setting).
        new_profile["candidate_count_pre_rerank"] = min(200, current * 2)
        return {"retrieval_profile": new_profile, "_broaden_applied": True}

    if strategy == RepairStrategy.ENABLE_FUZZY_ENTITY:
        new_filters = dict(filters)
        new_filters["fuzzy_entity_matching"] = True
        return {"retrieval_filters": new_filters, "_fuzzy_applied": True}

    if strategy == RepairStrategy.ADD_SPATIAL_BUFFER:
        new_filters = dict(filters)
        current_buf = float(new_filters.get("spatial_buffer_m", 0) or 0)
        # Default ladder: 0 → 500 → 1000 → 2000 → 5000 m.
        if current_buf == 0:
            new_filters["spatial_buffer_m"] = 500.0
        elif current_buf <= 500:
            new_filters["spatial_buffer_m"] = 1000.0
        elif current_buf <= 1000:
            new_filters["spatial_buffer_m"] = 2000.0
        else:
            new_filters["spatial_buffer_m"] = min(5000.0, current_buf * 2)
        return {"retrieval_filters": new_filters, "_buffer_applied": True}

    if strategy == RepairStrategy.TRANSFORM_CRS:
        # Crs coercion is a pyproj operation; here we only set the
        # FLAG so the spatial executor knows to coerce on the next
        # attempt. The actual transformation happens at query-build
        # time (geospatial_planner).
        new_filters = dict(filters)
        new_filters["coerce_input_crs_to_target"] = True
        return {"retrieval_filters": new_filters, "_crs_applied": True}

    if strategy == RepairStrategy.INCREASE_GRAPH_DEPTH:
        new_profile = dict(profile)
        current_depth = int(new_profile.get("graph_max_hops", 2) or 2)
        new_profile["graph_max_hops"] = min(5, current_depth + 1)
        return {"retrieval_profile": new_profile, "_graph_depth_applied": True}

    return {}
