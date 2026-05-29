## Doc-phase 132 handoff — §10.4 evaluate_workspace graduation

**Status:** Live + 8/8 pytest cases + Eval Dashboard populated with real data. **73/73 substrate verifier**.

## What landed

The `evaluate_workspace` Hatchet workflow (§10.4 / §24.3, doc-phase 98
skeleton) is now graduated. The §10.6 promotion gate
(`check_promotion_gate`, doc-phase 99 skeleton) is also live alongside.

### New live module — `app/services/eval/workspace_evaluator.py`

~370 lines. Pure async orchestration. Exports:
- `run_workspace_evaluation()` — full end-to-end orchestrator
- `evaluate_question()` — per-question evaluator (synthetic stub —
  see below)
- `QuestionRecord`, `QuestionResult`, `WorkspaceEvaluationResult`
  NamedTuples

Orchestration steps:
1. INSERT row into `eval.run_summaries` (status: in-progress)
2. Load active `eval.golden_questions` (optional question_set filter)
3. Per-question fanout (sequential v1; asyncio.gather will land
   with the real evaluator)
4. INSERT row per question into `eval.run_results`
5. Compute `regression_count` via prior-run comparison (DISTINCT ON
   per question, ordered by `started_at` DESC)
6. UPDATE run_summaries with pass/fail/regression counts +
   `completed_at`
7. Call `check_promotion_gate()` — returns `blocks_promotion` +
   `reasons`
8. Emit `eval.run.complete` audit ledger anchor
9. Return `WorkspaceEvaluationResult`

### Graduated `app/services/eval/thresholds.py::check_promotion_gate`

Three gates evaluated against `RegressionThresholds`:
- `max_absolute_fail_count` global cap
- `max_regression_count` global cap
- `per_set_max_regression` per-question-set caps

Two modes:
- `warning_only` (default) — never blocks even if gates trip;
  `reasons` populated for surfacing
- `blocking` — blocks if any gate trips

Returns `{blocks_promotion, would_block, reasons, mode}`.

### Updated `app/hatchet_workflows/evaluate_workspace.py`

Task body replaces `NotImplementedError` with a thin wrapper that
calls `run_workspace_evaluation()` and maps the result tuple to
`EvaluateWorkspaceOutput`.

### Synthetic stub evaluator — what's NOT live yet

`evaluate_question()` is currently a **deterministic synthetic stub**:
every active question "passes" with
`actual_payload = {"evaluator": "synthetic_stub", "doc_phase": 132, ...}`.

The orchestration around it is fully live: DB writes, regression
detection (against real prior runs), promotion gate, audit anchors.
When the real evaluator graduates (paired with §04i RAG/LLM
integration), `evaluate_question` is swapped without touching the
surrounding orchestration.

The synthetic tag is honest — Eval Dashboard rows show real run
counts but the `actual_payload.evaluator` field marks them as
synthetic until the real evaluator lands.

## Tests — `src/fastapi/tests/test_workspace_evaluator.py`

**8 pytest cases, all green:**

| Test | Verifies |
|---|---|
| `test_evaluate_question_synthetic_stub_passes` | Stub returns passed=True with stub tag |
| `test_check_promotion_gate_clean_run_passes` | No fails/regressions → no block |
| `test_check_promotion_gate_warning_only_does_not_block` | warning_only never sets blocks_promotion |
| `test_check_promotion_gate_blocking_mode_trips_on_regression` | Blocking mode + over-cap regression → blocks |
| `test_check_promotion_gate_per_set_regression_trips` | Per-set cap fires independently of global |
| `test_run_workspace_evaluation_minimal_end_to_end` | Single synthetic question → run_summaries + run_results + audit anchor |
| `test_run_workspace_evaluation_regression_detection` | Prior-run comparison logic works (no false positives) |
| `test_run_workspace_evaluation_no_questions_for_filter` | Filter scoping returns expected count |

Cleanup fixture explicitly deletes `eval.run_results` rows (no FK to
`run_summaries` so orphans would otherwise linger).

## Live verification on real data

Ran 4 production evals against the 45 mechanical golden questions:

```text
run 1 (manual, all):     8467985a 45/45 pass
run 2 (cron, all):       5da99bb5 45/45 pass
run 3 (gate, numeric):   6f4fc9c2 15/15 pass
run 4 (prompt, ocr):     a2e3a765 10/10 pass

Final state:
  run_summaries:  4
  run_results:   115
  audit anchors: 10 (eval.run.complete)
```

The Eval Dashboard's "Recent runs" panel
(`/admin/eval-dashboard`) now shows these 4 runs with real counts.

## Smoke verification

```bash
# All pytest cases pass
docker exec georag-fastapi python -m pytest tests/test_workspace_evaluator.py -v
# → 8 passed in 0.71s

# Substrate verifier extended with new test
bash scripts/autonomous_run_substrate_verify.sh
# → 73/73 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 132
- **Track 3 admin surfaces live:** 4 of 4 (Eval Dashboard now with
  real data)
- **Live helpers:** 9 (added: workspace_evaluator)
- **Hatchet workflow skeletons remaining:** 10 of 11
  (evaluate_workspace just graduated)
- **Live pytest cases:** 74 (66 + 8)
- **Substrate verifier:** **73/73 PASS**

## What's next in the partial-section closeout

- **Doc-phase 133** — `record_decision` capture hooks at 8 §21.3
  sites → lights up Decision History with real decisions
- **Doc-phase 134** — §9.10 ai_suggested hypothesis emitter → lights
  up Hypothesis Workspace
- **Doc-phase 135** — §6 BC/NRCan PublicGeo adapters (mirror Sask)
- **Doc-phase 136** — §10.11 5 §25.4 support agents → lights up
  Support Cockpit writer side
- **Doc-phase 137** — §7-A v1 report_builder nodes graduation
- **Doc-phase 138** — §8 score_targets graph nodes + §8.7 formula

## Carry-overs

- The real `evaluate_question` (real RAG + 6-layer §04i validators)
  is the highest-value follow-on. It's a multi-tick scope — needs
  vLLM endpoint integration, RAG pipeline call, citation/numeric/
  refusal/language validators, deterministic comparison logic.
- `actual_payload.evaluator == "synthetic_stub"` is the marker the
  Eval Dashboard can use to badge runs as "evaluator stub" until
  the real one lands.
- Threshold default is `warning_only` — flips to `blocking` per
  the §10.6 2-week soak plan from Kyle.
