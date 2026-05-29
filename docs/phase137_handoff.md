## Doc-phase 137 handoff — §7-A v1 first 4 report_builder graph nodes

**Status:** Live + 11/11 pytest cases. **80/80 substrate verifier**.

## What landed

Graduated the planning half of the §15.1 Report Builder graph (the
first 4 of 12 nodes — no LLM required). The 8 remaining nodes (LLM
draft, claim validators, citations, maps/charts, appendix, compliance,
sign-off, export, delivery) stay skeleton until their dependencies
ship.

### Graduated nodes — `app/services/report_builder/nodes.py`

| # | Node | Behavior |
|---|---|---|
| 1 | `select_report_type` | Validates risk_tier matches the registry; pins `started_at` |
| 2 | `plan_sections` | Seeds `state.sections_plan` from the per-report-type template (`get_template()`) |
| 3 | `gather_evidence` | Synthetic-stub Evidence Curator. Creates one `Claim` per `required_evidence_kind` per section, each with one `EvidenceItem`. Marks payload with `synthetic_stub doc-phase 137` |
| 4 | `verify_evidence_budget` | Gate node. Per-section evidence count check (default `min=1`). Hard-fails with `failure_reason` if any section is under-evidenced (real graph re-plan path lands later) |

All four nodes are idempotent (re-running with already-populated state
returns the state unchanged). Failures populate `state.failure_reason`
rather than raising — keeps the LangGraph reducer pattern intact for
the future wiring tick.

### Still-skeleton nodes (8 of 12)

| # | Node | Awaits |
|---|---|---|
| 5 | `generate_section_drafts` | LLM integration (vLLM endpoint + §7.3 drafter prompt) |
| 6 | `validate_claims` | §04i 6-layer validators |
| 7 | `attach_citations` | claim_ledger join |
| 8 | `generate_maps_charts` | SeaweedFS upload + MapLibre tile renderer |
| 9 | `build_appendix` | `audit.build_hash_chain_proof` wiring (live since doc-phase 117) |
| 10 | `compliance_check` | §29.2 10-item checklist |
| 11 | `geologist_approval` | Hatchet workflow pause/resume + sign-off UI |
| 12 | `export_package` | §7.9 renderer functions (pyproject has deps; renderers pending) |
| 13 | `activepieces_delivery` | Activepieces install |

Each skeleton's NotImplementedError message points at the dependency
that gates it — clear path forward.

## Tests — `src/fastapi/tests/test_report_builder_planning_nodes.py`

**11 pytest cases, all green:**

| Test | Verifies |
|---|---|
| `test_select_report_type_happy_path` | Tier match → no failure_reason, started_at set |
| `test_select_report_type_rejects_mismatched_tier` | R5 on weekly_project_digest → failure_reason |
| `test_select_report_type_preserves_started_at_if_set` | Pre-set started_at not overwritten |
| `test_plan_sections_seeds_from_template` | weekly_project_digest gets 4 sections including 'summary' + 'recent_findings' |
| `test_plan_sections_is_idempotent` | Re-running doesn't double-seed |
| `test_gather_evidence_creates_drafts_for_each_section` | One draft per planned section, each with claims+evidence, synthetic_stub tag in body |
| `test_gather_evidence_is_idempotent` | Re-running keeps the count |
| `test_verify_evidence_budget_passes_well_seeded` | Happy path through the chain |
| `test_verify_evidence_budget_fails_empty_drafts` | section_drafts=[] → "empty" failure_reason |
| `test_verify_evidence_budget_fails_under_evidenced_section` | A section with 0 evidence → "under-evidenced" failure |
| `test_full_planning_pipeline_runs_clean` | Chain runs cleanly for **all 11 §15.2 report types** |

The pipeline test is the strongest assertion — it exercises every
report-type template (weekly digest, NI 43-101 pack, CSA 11-348
disclosure pack, etc.) end-to-end through the 4 planning nodes,
confirming every template lands valid section + evidence shape.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_report_builder_planning_nodes.py -v
# → 11 passed in 0.11s

bash scripts/autonomous_run_substrate_verify.sh
# → 80/80 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 137
- **§15.1 nodes graduated:** **4 of 12** (33%)
- **§15.2 report types template-validated:** **11 of 11** (100% via integration test)
- **Hatchet workflow skeletons graduated:** 1 of 11
- **Reasoning-agent skeletons graduated:** 1 (hypothesis_generator)
- **§25.4 support agents graduated:** 1 of 5 (ticket_triage)
- **Live pytest cases:** 102 (91 + 11)
- **Substrate verifier:** **80/80 PASS**

## What's next

- **Doc-phase 138** — §8 score_targets graph nodes + §8.7 formula
  (same shape: graduate the deterministic planning/scoring nodes;
  leave LLM nodes skeleton)
- **Doc-phase 139** — §25.4 second support agent
  (root_cause_investigation)
- **Doc-phase 140** — Wire `evaluate_workspace` to actually call the
  graduated §7-A v1 nodes for the `report_section` golden questions
  (cross-tick payoff)

## Carry-overs

- The §15.1 LangGraph wiring (the actual graph object with reducers
  + conditional edges) is a separate tick — these 4 nodes work
  standalone today but aren't wired together into a Pregel pipeline.
  When the wiring lands, the existing nodes drop in without changes.
- The `gather_evidence` stub creates one Claim per
  `required_evidence_kind`. Real evidence curator can replace this
  function — the surrounding pipeline doesn't need changes.
- `verify_evidence_budget` hard-fails on under-evidenced sections
  today. Per §15.1 the graph should route to a clarifying re-plan;
  that re-plan lands when the Planner LLM agent ships.
- The graduated nodes have `synthetic_stub doc-phase 137` markers
  in `claim.text` and `draft.body_markdown` so any caller can
  detect they're not real LLM output.
