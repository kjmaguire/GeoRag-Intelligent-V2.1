## Doc-phase 170 handoff — Nightly real_rag_v1 cron — §10.6 regression alarm locked in

**Status:** Live + 107/107 substrate verifier + 56/56 eval-workflow regression + new pytest module green.

## What landed

A new Hatchet cron workflow `eval_real_rag_nightly` that fires the
full §04i 6-layer chain against the `refusal_correctness` question
set every night @ 05:15 UTC. This is the §10.6 promotion-gate
regression alarm — the cadence checkpoint that detects §2.9
hallucination drift before it reaches production traffic.

### Why a wrapper, not on_crons on `evaluate_workspace`

`evaluate_workspace` requires `eval_request_id: UUID` with no
default — incompatible with cron's empty payload. The wrapper
generates a fresh UUID per fire + bakes in the nightly flavor
(`real_rag_v1` + `refusal_correctness` + `blocks_promotion=True`)
while leaving `evaluate_workspace` callable ad-hoc with arbitrary
evaluator_kind / question_set from Activepieces, manual UI, or the
promotion-gate driver.

### Cron slot

`15 5 * * *` UTC — 15 min offset from phase0_agents' 05:00 slot so
the AI worker pool isn't all firing at the same second. Slot map:

| Time (UTC) | Workflow |
|---|---|
| 01:00 | phase0_agents |
| 02:00 | phase0_agents + audit_ledger_verify |
| 03:00 | phase0_agents + mv_refresh_silver |
| 04:00 | phase0_agents + flow_jwt_key_reaper |
| 05:00 | phase0_agents |
| **05:15** | **eval_real_rag_nightly (new)** |
| 06:00 | phase0_agents |

### Manual invocation

```python
# Re-target the cron flavor against a different question_set
eval_real_rag_nightly.run({"question_set_filter": "numeric_grounding"})

# Disable promotion-block while debugging
eval_real_rag_nightly.run({"blocks_promotion": False})
```

### Output shape

```python
class EvalRealRagNightlyOutput(BaseModel):
    run_id: UUID
    success: bool                    # False iff regression_count > 0
    question_count: int
    pass_count: int
    fail_count: int
    regression_count: int
    promotion_blocked: bool
    failure_summary: str | None
    evaluator_kind: str = "real_rag_v1"
    question_set_filter: str = "refusal_correctness"
```

The `failure_summary` field carries the human-readable rollup for
PagerDuty / Slack hand-off; `success=False` is the boolean the
promotion-gate driver short-circuits on.

## Tests — 4 new + 56/56 regression

`src/fastapi/tests/test_eval_real_rag_nightly_workflow.py`:

| Case | Verifies |
|---|---|
| `test_default_input_matches_cron_fire_path` | Empty `EvalRealRagNightlyInput()` defaults to refusal_correctness + blocks_promotion=True |
| `test_workflow_carries_correct_cron_schedule` | `15 5 * * *` is in workflow's on_crons (catches accidental slot drift) |
| `test_workflow_body_fires_real_rag_v1` | Cron-fire path runs full §04i chain against 8 refusal questions (live) |
| `test_workflow_body_accepts_manual_override` | Manual `question_set_filter='core_chat'` re-targets cron flavor (empty set, fast pass) |

`test_workflow_body_fires_real_rag_v1` is the live end-to-end
exercise — loads BGE embedding model, hits vLLM 8 times, runs all
6 §04i layers, asserts the workflow output shape is honest.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_eval_real_rag_nightly_workflow.py -v
# → 4 passed in 39.49s (BGE warm-up + 8 live RAG calls)

# Full eval-workflow regression
docker exec georag-fastapi python -m pytest \
    tests/test_evaluate_workspace_workflow.py \
    tests/test_eval_real_rag_nightly_workflow.py \
    tests/test_real_rag_evaluator.py \
    tests/test_eval_validators.py
# → 56 passed in 64.13s

# Workflow registered in AI pool
docker exec georag-fastapi python -m app.hatchet_workflows.worker --list | grep eval_real_rag_nightly
# → eval_real_rag_nightly

# Substrate verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 107/107 checks passed (was 105 — +1 wf check + 1 pytest check)
```

## Cumulative session state — 39 ticks closed

- **Doc-phase ticks this run:** **39** (132 → 170)
- **Substrate verifier:** **107/107 PASS**
- **Live pytest cases:** 284 (280 + 4)
- **Sections closed:** §25.4 + §6 + §04i validators
- **§04i validators:** 6 of 6
- **Evaluator kinds wireable:** 3
- **Real RAG eval — full chain green:** ✓
- **§10.6 nightly cron alarm:** **live**
- **§21.3 types covered:** 8 of 8
- **PublicGeo features on map:** 95
- **Hatchet workflows registered (AI pool):** 12 (was 11)

## What's next

Productive next directions:

- **Fix the bge-reranker-base ONNX config** so the cross-encoder gate
  fires across both chat + eval paths (carry-over from doc-phase 169)
- **Ingest a sample project's documents** so retrieval surfaces real
  chunks (turns the 8/8 refusal pass into "8/8 refused correctly with
  empty context" + lets non-refusal questions answer with real citations)
- **SME-author core_chat / public_private_boundary / target_recommendation**
  question sets — exercises Layers 1-5 in non-vacuous mode
- **Wire the cron's `failure_summary` into a Slack / PagerDuty notification**
  — close the loop from "alarm fired" to "operator notified"

## Carry-overs

- The cron tests use `aio_mock_run` (Hatchet's public test API for
  task bodies). When Hatchet v2 drops the `WorkflowConfig.config`
  internal access, the `test_workflow_carries_correct_cron_schedule`
  test will need to switch to a public schedule-introspection API.
  Today it's gated with `getattr` fallbacks so either Hatchet version
  works.
- `execution_timeout="30m"` triggers a Hatchet deprecation warning
  (string → timedelta in v2). Identical to the warning on every other
  workflow file in the AI pool; sweep all in one tick when Hatchet v2
  lands.
- The 8/8 refusal pass + `success=True` from the cron is meaningful
  precisely because the underlying eval is real (vLLM + Qdrant +
  Neo4j + embedding model + 6-layer §04i chain). If document ingestion
  lands without an SME-author non-refusal set, the cron stays green
  but only proves "refusals work" — it does NOT prove "non-refusal
  answers are grounded". That second proof needs the SME question
  sets noted above.
