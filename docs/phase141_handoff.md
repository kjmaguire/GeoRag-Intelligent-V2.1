## Doc-phase 141 handoff — §15.1 + §18.2 LangGraph Pregel pipelines wired

**Status:** Live + 4/4 pytest cases. **84/84 substrate verifier**.

## What landed

Wired the graduated graph nodes from doc-phase 137 (§15.1, report
builder) and doc-phase 138 (§18.2, target recommendation) into real
**LangGraph Pregel pipelines** using `StateGraph`. Both graphs compile
and run end-to-end via `.ainvoke(initial_state)`.

When the remaining skeleton nodes graduate, the wirings extend without
breaking the existing pipeline shape.

### §15.1 Report Builder graph — `app/services/report_builder/graph.py`

```text
START → select_report_type
      → plan_sections
      → gather_evidence
      → verify_evidence_budget
      → END
```

4 graduated nodes wired sequentially. When nodes 5-12 graduate
(LLM-dependent layers + WeasyPrint + SeaweedFS + sign-off UI),
they slot in between `verify_evidence_budget` and `END`.

### §18.2 Target Recommendation graph — `app/services/target_recommendation/graph.py`

```text
START → select_commodity_deposit_model
      → load_workspace_playbook
      → score_candidate_zones     (§8.7 weighted formula — real math)
      → calculate_uncertainty
      → apply_constraints
      → rank_targets              (real sort + exclusion filtering)
      → END
```

6 graduated nodes wired sequentially. The caller pre-populates
`state.candidate_zones`; when `generate_candidate_zones` graduates
(PostGIS spatial pipeline), it joins between `load_workspace_playbook`
and `score_candidate_zones`.

### Exports

- `app.services.report_builder.build_report_builder_graph() -> CompiledGraph`
- `app.services.target_recommendation.build_target_recommendation_graph() -> CompiledGraph`

Both compile once + can be reused across invocations — no per-call
StateGraph rebuild needed.

## Tests — `src/fastapi/tests/test_langgraph_wirings.py`

**4 pytest cases, all green:**

| Test | Verifies |
|---|---|
| `test_report_builder_graph_runs_end_to_end` | Pipeline runs all 4 nodes; started_at + sections_plan + section_drafts populated |
| `test_report_builder_graph_propagates_failure_reason` | Tier mismatch → failure_reason set + preserved through subsequent nodes |
| `test_target_recommendation_graph_runs_end_to_end` | Pipeline with 4 candidate zones → 4 ranked targets sorted DESC, doc-phase 138 tags visible |
| `test_target_recommendation_graph_with_no_zones_still_runs` | Empty candidate_zones → clean run to END, no failure |

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_langgraph_wirings.py -v
# → 4 passed in 0.48s

bash scripts/autonomous_run_substrate_verify.sh
# → 84/84 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 141
- **LangGraph wirings live:** **2 of 2** (report + targeting)
- **§15.1 nodes graduated + wired:** 4 of 12
- **§18.2 nodes graduated + wired:** 6 of 12
- **§25.4 support agents graduated:** 3 of 5
- **§21.3 capture hooks wired:** 1 of 8
- **Hatchet workflow skeletons graduated:** 1 of 11
- **Reasoning agent skeletons graduated:** 1
- **Live pytest cases:** 134 (130 + 4)
- **Substrate verifier:** **84/84 PASS**

## What's next

The LangGraph wirings unlock the `generate_report` + `score_targets`
Hatchet workflow body graduations — the Hatchet task can now just
build the compiled graph + ainvoke it. That's a small follow-on
graduation that bridges the workflow layer to the graph layer.

Planned sequence:
- **Doc-phase 142** — graduate `generate_report` + `score_targets`
  Hatchet task bodies (call the compiled LangGraphs)
- **Doc-phase 143** — §25.4 customer_response_drafting agent
- **Doc-phase 144** — §25.4 escalation_routing agent (closes §25.4 set)

## Carry-overs

- The LangGraph wirings use `StateGraph(ReportBuilderState)` / etc.
  with Pydantic state — LangGraph handles serialization between
  nodes automatically.
- `ainvoke()` returns a dict in LangGraph's reduction semantics; the
  caller rehydrates with `State.model_validate(result)`. The pattern
  is documented in both graph module docstrings.
- Both graphs are sequential today. Fan-out via `Send` (per-zone
  parallelism for `score_candidate_zones` / `calculate_uncertainty` /
  `explain_score_factors`) is a future optimization — the current
  nodes already handle per-zone scoring internally so the user-facing
  output is identical.
