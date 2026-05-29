## Doc-phase 138 handoff — §8.7 weighted-scoring formula + 6 of 12 §18.2 target_recommendation nodes

**Status:** Live + 17/17 pytest cases. **81/81 substrate verifier**.

## What landed

Graduated the deterministic / non-LLM half of the §18.2 Target
Recommendation graph (6 of 12 nodes) plus the §8.7 weighted-scoring
formula.

### §8.7 weighted-scoring formula — REAL math

`weighted_aggregate(factors: list[ScoreFactor]) -> float` in
`app/services/target_recommendation/nodes.py`.

```text
aggregate = sum(factor_value * factor_weight) / sum(factor_weight)
clamped to [0, 1]
```

This is **real math, not a synthetic stub** — it's the canonical
§8.7 weighted formula. What's synthetic in this graduation is the
*factor population* (where the factor_value comes from); the math
that combines factors is final.

Edge cases handled:
- Empty `factors` → 0.0
- Total weight ≤ 0 → 0.0
- Out-of-range factor_values clamped on the aggregate (defense in depth)

### Graduated nodes (6 of 12)

| # | Node | Stub vs Real |
|---|---|---|
| 1 | `select_commodity_deposit_model` | Synthetic registry lookup (uses in-process DEPOSIT_MODEL_TEMPLATES); real DB-backed lookup against `targeting.target_models` is the follow-on |
| 2 | `load_workspace_playbook` | Synthetic passthrough; real version queries `workspace_playbooks` |
| 6 | `score_candidate_zones` | **Real §8.7 math** + synthetic factor population from zone_id hash |
| 7 | `calculate_uncertainty` | Synthetic heuristic (sparsity from score); real version uses Bayesian/bootstrap |
| 8 | `apply_constraints` | Synthetic empty (no exclusions); real version PostGIS-joins to workspace excluded_areas |
| 9 | `rank_targets` | **Real sort** by aggregate_score DESC with exclusion filtering |

The graduated nodes share the synthetic-stub pattern doc-phase 132 /
134 / 136 / 137 established — what's stubbed is clearly labeled
(`evaluator: synthetic_stub`, doc-phase tag in `payload` and
`explanation_markdown`); the surrounding orchestration is fully real.

### Still skeleton (6 of 12)

| # | Node | Awaits |
|---|---|---|
| 3 | `collect_private_evidence` | Hybrid retrieval (Qdrant + PostGIS + Neo4j) integration |
| 4 | `collect_public_geoscience` | §6 adapter pipelines (BC MINFILE, NRCan, etc.) |
| 5 | `generate_candidate_zones` | PostGIS spatial generation pipeline |
| 10 | `explain_score_factors` | §8.12 Recommendation Explainer LLM agent |
| 11 | `create_map_layers` | SeaweedFS + Martin tile renderer + §6 layer packs |
| 12 | `route_to_review_cockpit` | Hatchet workflow pause/resume + sign-off UI |

## Tests — `src/fastapi/tests/test_target_recommendation_nodes.py`

**17 pytest cases, all green:**

§8.7 formula unit tests (5):
- `test_weighted_aggregate_empty_returns_zero`
- `test_weighted_aggregate_zero_weights_returns_zero`
- `test_weighted_aggregate_single_factor`
- `test_weighted_aggregate_two_factors`
- `test_weighted_aggregate_clamps_to_range`

Synthetic factor generator (2):
- `test_synthetic_factors_produce_three_factors`
- `test_synthetic_factors_deterministic_for_same_zone_id`

Per-node tests (9):
- `test_select_commodity_deposit_model_defaults_to_athabasca`
- `test_select_commodity_deposit_model_matches_commodity_hint`
- `test_load_workspace_playbook_idempotent_passthrough`
- `test_score_candidate_zones_assigns_score_per_zone`
- `test_score_candidate_zones_is_idempotent`
- `test_score_candidate_zones_no_zones_no_scores`
- `test_calculate_uncertainty_populates_per_zone`
- `test_rank_targets_orders_by_aggregate_score_desc`
- `test_rank_targets_filters_excluded_zones`

Full pipeline integration (1):
- `test_full_scoring_pipeline_chain` — chains all 6 graduated nodes
  against 5 zones, asserts ranked output is sorted DESC and carries
  doc-phase 138 tags.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_target_recommendation_nodes.py -v
# → 17 passed in 0.12s

bash scripts/autonomous_run_substrate_verify.sh
# → 81/81 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 138
- **§18.2 nodes graduated:** **6 of 12** (50%)
- **§15.1 nodes graduated (§7-A v1):** **4 of 12** (33%)
- **§25.4 support agents graduated:** 1 of 5 (ticket_triage)
- **Reasoning agent skeletons graduated:** 1 (hypothesis_generator)
- **Hatchet workflow skeletons graduated:** 1 of 11 (evaluate_workspace)
- **§21.3 capture hooks wired:** 1 of 8 (workflow_enablement)
- **Live pytest cases:** 119 (102 + 17)
- **Substrate verifier:** **81/81 PASS**

## Partial-section progress this run (132 → 138)

| Tick | Section | Graduation |
|---|---|---|
| 132 | §10 | evaluate_workspace Hatchet task body |
| 133 | §9.12 / §21.3 | Laravel RecordDecision service + workflow_enablement hook |
| 134 | §9.10 | ai_suggested hypothesis emitter |
| 135 | §6 | jurisdictions + sources foundation seed |
| 136 | §10.11 / §25.4 | ticket_triage support agent |
| 137 | §7-A v1 | first 4 of 12 report_builder graph nodes |
| 138 | §8.4 / §8.7 / §18.2 | §8.7 formula + 6 of 12 score_targets graph nodes |

7 ticks, 4 surfaces with real data, 6 skeleton graduations, 81/81
substrate verifier.

## What's next

Continuing the sequence:
- **Doc-phase 139** — §25.4 root_cause_investigation agent (second
  of 5 support agents)
- **Doc-phase 140** — §25.4 support_packet agent
- **Doc-phase 141** — Wire the §15.1 / §18.2 graphs end-to-end (the
  LangGraph reducers + conditional edges that thread the graduated
  nodes into actual Pregel pipelines)
- **Doc-phase 142+** — remaining §25.4 agents
  (customer_response_drafting, escalation_routing)

## Carry-overs

- The 6 graduated nodes work standalone but aren't wired into the
  §18.2 LangGraph yet. The graph wiring is a separate scope; once
  done, the graduated nodes drop in unchanged.
- `_synthetic_factors_for_zone` is the only synthetic-stub piece in
  the scoring path. The §8.7 weighted_aggregate formula is real.
- The synthetic factor weights (0.4 / 0.35 / 0.25 = sum 1.0) are
  placeholder; real weights come from `target_model_versions.factor_weights`
  when the DB-backed registry graduates.
