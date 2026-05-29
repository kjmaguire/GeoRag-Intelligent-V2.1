# Repair Loop Specification (Plan §4b + §4c)

**Status:** Foundation modules shipped; orchestrator wire pending.

This document describes how the four pure-function building blocks
shipped under plan §4 compose into the agentic-retrieval **repair
loop** — the controlled retry mechanism that fires when post-assembly
guards (§4a hallucination layers + §4d catalog) detect quality
problems with a generated answer.

The four building blocks (all in `src/fastapi/app/agent/`):

| Module | Module-level callable |
|---|---|
| `guards.py` | `classify_guards(...)` — warning strings → `GuardErrorCode[]` |
| `guards.py` | `RepairAttempt` dataclass — one iteration's record |
| `guards.py` | `detect_death_loop(repair_attempts) → bool` (plan §4c) |
| `repair_strategy.py` | `plan_repair(codes, *, max_attempts, prior_strategies) → RepairPlan` |

This spec ties them together. The orchestrator wiring is downstream —
this doc is the contract the wire must implement.

---

## 1. Loop shape

```
┌──────────────────────────────────────────────────────────────┐
│  classify  →  route  →  execute  →  assemble  →  validate    │
│                                                              │
│                                          ┌── guards fire ──┐ │
│                                          ▼                  │ │
│  ┌────────────────────────────────────────────────────┐    │ │
│  │ REPAIR LOOP                                        │    │ │
│  │                                                    │    │ │
│  │ 1. classify_guards(state) → codes                  │    │ │
│  │ 2. plan_repair(codes, prior_strategies) → plan     │    │ │
│  │ 3. if plan.terminal: jump to surface-terminal step │    │ │
│  │ 4. else: apply plan.first_strategy() to state      │    │ │
│  │ 5. RepairAttempt appended to state.repair_attempts │    │ │
│  │ 6. detect_death_loop(state.repair_attempts) → end? │    │ │
│  │ 7. re-issue execute → assemble → validate         │◀─┐ │ │
│  │                                                    │  │ │ │
│  └──────────────────────┬─────────────────────────────┘  │ │ │
│                         │ (validate passes OR plan       │ │ │
│                         │  terminal OR death loop)       │ │ │
│                         ▼                                │ │ │
│                       persist  ◀───── back to caller ───┘ │ │
│                                                          ▲  │ │
│                                                          │  │ │
│                                                          │  │ │
│                                          loop iteration ┘  │ │
└────────────────────────────────────────────────────────────┘
```

The loop never directly calls retrievers; it modifies state and
re-issues the same `execute → assemble → validate` triplet that the
LangGraph already wires. From LangGraph's perspective the repair pass
is a conditional edge from `validate_node` back to `route_node` (or
`execute_node`, depending on the strategy).

---

## 2. Strategy → orchestrator action mapping

Each `RepairStrategy` value maps to a concrete state-mutation the
orchestrator applies before re-issuing the graph. The mapping:

### Loop-friendly strategies

| Strategy | What the orchestrator does |
|---|---|
| `LOOSEN_FILTERS` | Drop the strictest 1–2 entries from `state.retrieval_filters.allowed_data_sources` / drop date or status filters. Persists the original filter set on `state.repair_attempts[-1].filters` so the next iteration knows what was tried. |
| `BROADEN_KNN` | Multiply `state.retrieval_profile.candidate_count_pre_rerank` by 2 (capped at `settings.QDRANT_DENSE_TOP_K_MAX`). Re-runs `execute_node` only — no need to re-classify. |
| `ENABLE_FUZZY_ENTITY` | Sets `state.retrieval_filters.fuzzy_entity_matching = True`. Triggers entity-resolver re-pass with edit-distance ≤ 2 matches accepted. |
| `ADD_SPATIAL_BUFFER` | Multiplies `state.retrieval_filters.spatial_buffer_m` by 2 (default 0 → 500m → 1000m → 2000m). Capped at the project's bounding-box diagonal. |
| `TRANSFORM_CRS` | Look up the target CRS from `context_envelope.crs_epsg`; coerce all query coordinates via `pyproj.Transformer`. The original CRS lands on `RepairAttempt.filters["source_crs"]` for the trace. |
| `INCREASE_GRAPH_DEPTH` | `state.retrieval_profile.graph_max_hops += 1` (default 2 → 3 → 4). Cap at 5. |
| `REPHRASE_NUMERIC_CLAIM` | Modifies `system_prompt` with an "explicitly mark un-grounded numeric claims as ESTIMATED" suffix; re-runs `assemble_node` only. |
| `REQUEST_CITATION_RETRY` | Modifies `system_prompt` with a stricter "every claim must end with `[DATA:n]`" suffix; re-runs `assemble_node` only. |

### Terminal strategies

| Strategy | What the orchestrator does |
|---|---|
| `ASK_FOR_DISAMBIGUATION` | Stamps a structured payload on `response.refusal_payload` listing the ambiguous candidates (`{hole_id: [...], formation: [...], property: [...]}`). The React `AmbiguityPicker` renders chips for the user to pick. |
| `SURFACE_CONFLICT` | Stamps `response.conflicting_evidence` with a `{entity_key, property, values, evidence_ids}` record per conflict. The React `ConflictSideBySide` renders the comparison. Global Invariant 7: never silently pick a winner. |
| `REQUEST_UNIT_CLARIFICATION` | Stamps `response.refusal_payload.reason_code = "MISSING_ASSAY_UNITS"` and lists candidate unit families (`g/t`, `ppm`, `wt%`). React renders a unit-picker chip strip. |
| `REQUEST_DEPTH_CLARIFICATION` | Same shape as unit clarification; lists candidate depth-units (`m`, `ft`). |
| `REFUSE_OUT_OF_SCOPE` | Stamps `response.refusal_payload.reason_code` matching the originating guard code (`SOURCE_SCOPE_VIOLATION` / `UNSUPPORTED_QUERY_TYPE` / `max_attempts_exceeded`). React renders `RefusalBanner`. |

### Non-mappings (deliberate)

The orchestrator MUST NOT:

- Silently widen a query past the active workspace (Global Invariant: workspace_id is sacred).
- Substitute a different LLM model mid-loop (changes the fingerprint).
- Drop citations to fit the budget (Global Invariant 7).
- Continue after a `terminal=True` plan unless the next step is `persist_node`.

---

## 3. State extensions required

The wire needs three new fields on `AgenticRetrievalState`:

```python
class AgenticRetrievalState(BaseModel):
    ...
    # ── Plan §4b/§4c repair loop ─────────────────────────────────────
    repair_attempts: list[RepairAttempt] = Field(default_factory=list)
    repair_terminal_reason: str | None = Field(default=None)
    repair_strategy_history: list[str] = Field(default_factory=list)
```

- `repair_attempts` — chronological list. `detect_death_loop` reads the
  last two entries.
- `repair_terminal_reason` — when the loop ends terminally, this holds
  the `RepairPlan.reason` for the trace.
- `repair_strategy_history` — `RepairStrategy.value` strings, in
  execution order. Persisted to `silver.query_traces.repair_strategies_used`
  for cross-query analysis.

---

## 4. Loop algorithm (pseudocode)

```python
MAX_ATTEMPTS = settings.REPAIR_LOOP_MAX_ATTEMPTS  # default 2

async def repair_loop_node(state):
    # 1. Classify what fired.
    codes = classify_guards(
        validation_warnings=state.validation_warnings,
        demotion_reasons=state.demotion_reasons,
        tool_results=state.tool_results,
        response_citations=list(state.response.citations) if state.response else [],
        citation_lifecycle_state=state.response.citation_lifecycle_state if state.response else None,
        conflicting_evidence_present=bool(state.response and state.response.conflicting_evidence),
    )

    # 2. Plan the next attempt.
    plan = plan_repair(
        codes,
        max_attempts=MAX_ATTEMPTS,
        prior_strategies=state.repair_strategy_history,
    )

    # 3. No work → return the current answer.
    if plan.is_empty() and not plan.terminal:
        return {"repair_terminal_reason": None}

    # 4. Terminal plan → stamp the response with the right surface,
    # don't re-issue the graph.
    if plan.terminal:
        terminal_strategy = plan.first_strategy()
        if terminal_strategy is not None:
            response = _apply_terminal_strategy(state.response, terminal_strategy, codes)
            return {
                "response": response,
                "repair_terminal_reason": plan.reason,
                "repair_strategy_history": [
                    *state.repair_strategy_history,
                    terminal_strategy.value,
                ],
            }
        return {"repair_terminal_reason": plan.reason}

    # 5. Apply the first loop-friendly strategy.
    strategy = plan.first_strategy()
    new_state_updates = _apply_strategy(state, strategy)

    # 6. Record the attempt.
    attempt = RepairAttempt(
        tool_name=new_state_updates.get("retrieval_tool_name", "unknown"),
        filters=new_state_updates.get("retrieval_filters_snapshot", {}),
        result_count=len(state.tool_results),
        attempted_at_monotonic=time.monotonic(),
    )
    history = [*state.repair_attempts, attempt]
    strat_history = [*state.repair_strategy_history, strategy.value]

    # 7. Death-loop check.
    if detect_death_loop(history):
        return {
            "response": _stamp_refuse_with_death_loop(state.response),
            "repair_attempts": history,
            "repair_strategy_history": strat_history,
            "repair_terminal_reason": "death loop detected",
        }

    # 8. Loop continues — return state updates that re-route to execute_node.
    return {
        **new_state_updates,
        "repair_attempts": history,
        "repair_strategy_history": strat_history,
    }
```

The LangGraph wiring uses a conditional edge from `validate_node`:

```python
def _should_repair(state) -> str:
    codes = classify_guards(...)  # cheap; we run it twice for the edge decision
    if codes and len(state.repair_strategy_history) < MAX_ATTEMPTS:
        return "repair_loop_node"
    return "demote_node"

graph.add_conditional_edges(
    "validate_node",
    _should_repair,
    {"repair_loop_node": "repair_loop_node", "demote_node": "demote_node"},
)
graph.add_edge("repair_loop_node", "execute_node")  # re-run
```

---

## 5. Cost + safety guards

| Risk | Mitigation |
|---|---|
| LLM cost amplification (each repair issues another `_call_llm`) | `MAX_ATTEMPTS=2` default; `cost_burn_watcher` Hatchet workflow already alerts on per-workspace token burn. |
| Long latency | Each iteration adds ~2–4s. Total tail with 2 repairs ≈ 14s. Frontend SSE `status` frames surface "Refining answer (attempt 2/3)" so the user sees progress. |
| Stuck on the same tool + filter | `detect_death_loop` short-circuits after 2 identical empty/low-result attempts. |
| Infinite loop via state corruption | LangGraph's recursion limit (default 25) catches the worst case; the `MAX_ATTEMPTS` strategy check catches the normal case. |
| Repair leaks workspace boundary | `_apply_strategy` MUST NOT mutate `workspace_id`, `tenant_id`, or `state.deps.pg_pool` configuration. Asserted in `test_repair_strategy_workspace_invariant` (to be added with the wire). |
| Repair widens query past consent | `protected_kinds_override` from §3a context_prep is honored by `_apply_strategy("LOOSEN_FILTERS")` — never relax filters past what `effective_intent` permits. |

---

## 6. Observability

Every repair attempt writes a row in `silver.query_traces.repair_attempts`
(`int`) + `silver.query_traces.repair_strategies_used` (`list[str]`).
The trace inspector renders the chain:

```
attempt 1: LOOSEN_FILTERS    → 3 results
attempt 2: BROADEN_KNN       → 12 results
attempt 3: ASK_FOR_DISAMBIGUATION (terminal)
```

`death_loop_triggered` is already on `RetrievalTrace` (boolean). When
true, the trace UI shows a red badge and the retrieval-inspector page
surfaces the death-loop reason banner.

Sentry tagging (deferred to wire):

```python
sentry_sdk.set_tag("repair.terminal_reason", plan.reason or "")
sentry_sdk.set_tag("repair.attempts", len(state.repair_attempts))
sentry_sdk.set_tag("repair.death_loop", detect_death_loop(state.repair_attempts))
```

---

## 7. Per-strategy implementation status

| Strategy | Implementation site | Status |
|---|---|---|
| `LOOSEN_FILTERS` | `_apply_strategy` (wire) | ⚠️ pending |
| `BROADEN_KNN` | `_apply_strategy` (wire) | ⚠️ pending |
| `ENABLE_FUZZY_ENTITY` | `_apply_strategy` + entity_resolver (§2c) | ⚠️ pending — depends on §2c node |
| `ADD_SPATIAL_BUFFER` | `_apply_strategy` + §2g geospatial node | ⚠️ pending — depends on §2g node |
| `TRANSFORM_CRS` | `_apply_strategy` (pyproj already vendored) | ⚠️ pending |
| `INCREASE_GRAPH_DEPTH` | `_apply_strategy` + Neo4j cypher rewrite | ⚠️ pending |
| `REPHRASE_NUMERIC_CLAIM` | `_apply_strategy` system_prompt suffix | ⚠️ pending |
| `REQUEST_CITATION_RETRY` | `_apply_strategy` system_prompt suffix | ⚠️ pending |
| `ASK_FOR_DISAMBIGUATION` | `_apply_terminal_strategy` + React `AmbiguityPicker` | ⚠️ Picker exists; wire pending |
| `SURFACE_CONFLICT` | `_apply_terminal_strategy` + React `ConflictSideBySide` | ⚠️ Component exists; wire pending |
| `REQUEST_UNIT_CLARIFICATION` | `_apply_terminal_strategy` + new React picker | ❌ No component yet |
| `REQUEST_DEPTH_CLARIFICATION` | `_apply_terminal_strategy` + new React picker | ❌ No component yet |
| `REFUSE_OUT_OF_SCOPE` | `_apply_terminal_strategy` + React `RefusalBanner` | ⚠️ Banner exists; wire pending |

---

## 8. Rollout plan

1. **Stage 1 — feature-flagged shadow mode.**
   Wire the loop behind `REPAIR_LOOP_ENABLED=False`. The loop runs in
   shadow mode: it builds the plan and writes the strategies it WOULD
   have applied to the trace, but doesn't modify state. This gives us
   real-corpus telemetry on which codes fire and which strategies the
   dispatcher chooses, without changing user-visible answers.

2. **Stage 2 — terminal-only.**
   Enable `_apply_terminal_strategy` only. Loop-friendly strategies
   still shadow-mode. This ships the user-facing surfaces (Refusal,
   Ambiguity, Conflict) without amplifying LLM cost.

3. **Stage 3 — loop-friendly, low-cost first.**
   Enable `REPHRASE_NUMERIC_CLAIM` + `REQUEST_CITATION_RETRY` (both
   are LLM-only re-issues, no extra retrieval). Validate against the
   golden-query harness before promoting further.

4. **Stage 4 — full retrieval-side strategies.**
   Enable `LOOSEN_FILTERS`, `BROADEN_KNN`, `TRANSFORM_CRS`, etc. By
   this point we have real telemetry on which codes the loop catches
   most often and can tune the strategy ordering in
   `STRATEGY_FOR_CODE`.

Each stage is gated by:
- Golden-query regression (no answer-quality drop)
- Cost burn under +20% of pre-loop baseline
- p95 latency under +6s of pre-loop baseline

---

## 9. Open questions

- **Should `REPAIR_LOOP_MAX_ATTEMPTS` be per-intent?** factual_lookup
  benefits less from repair than synthesis. Defer until Stage 3
  telemetry lands.
- **What's the right `protected_kinds` interaction with `LOOSEN_FILTERS`?**
  Loosening filters can re-introduce a kind the §3c diversity pass
  protected. Stage 4 decision.
- **Should `SURFACE_CONFLICT` ALWAYS run when `conflicting_evidence`
  is non-empty, even if other strategies could fire?** Plan §4b says
  yes (Global Invariant 7); the dispatcher implements that via
  truncate-at-terminal. Confirmed.

---

## References

- `src/fastapi/app/agent/guards.py` — code classifier + death-loop detector
- `src/fastapi/app/agent/repair_strategy.py` — dispatcher
- `docs/architecture/user_facing_error_catalog.md` — per-code user message
- `docs/architecture/structured_answer_format_spec.md` — plan §4a output shape
- `docs/architecture/six_subgraphs_spec.md` — per-intent retrieval profile
- `OVERNIGHT_LOG.md` §25 — repair dispatcher foundation
- `OVERNIGHT_LOG.md` §26 — composition pipeline (`prepare_evidence_for_intent`)
