## Doc-phase 161 handoff — `evaluator_kind` threaded through Hatchet workflow

**Status:** Live + 3/3 pytest cases + 20/20 regression tests + 103/103 substrate verifier.

## What landed

Wired the doc-phase 159 `evaluator_kind` selector all the way through
the Hatchet workflow layer. Callers (cron, Activepieces, ad-hoc admin
triggers) can now choose `synthetic_stub` vs `real_llm_v1` per-run.

### `EvaluateWorkspaceInput` — added field

```python
evaluator_kind: EvaluatorKind = Field(
    default="synthetic_stub",
    description="'synthetic_stub' (always-pass) or 'real_llm_v1' "
                "(vLLM + §04i refusal-correctness validator).",
)
```

`EvaluatorKind = Literal["synthetic_stub", "real_llm_v1"]` so Pydantic
rejects invalid values at validator time (caught before the workflow
even starts).

Backward-compatible: default is `synthetic_stub` so doc-phase 132
callers continue working unchanged.

### `EvaluateWorkspaceOutput` — echoes evaluator_kind

The output now carries `evaluator_kind` so downstream observability
(audit anchor consumers, Eval Dashboard rollups) can attribute the
run to its evaluator without a second DB lookup.

### Task body wires it through

`execute()` passes `input.evaluator_kind` to `run_workspace_evaluation`
and echoes it back in the output. Log line updated to include
`evaluator_kind=...` for observability.

## Tests — 3/3 pytest cases green

| Test | Verifies |
|---|---|
| `test_evaluate_workspace_default_evaluator_is_synthetic_stub` | Backward-compat default still `synthetic_stub`; output echoes it |
| `test_evaluate_workspace_real_llm_v1_threads_through` | `evaluator_kind='real_llm_v1'` is accepted + echoed |
| `test_evaluate_workspace_rejects_invalid_evaluator_kind` | Pydantic rejects unknown values (Literal validation) |

20/20 regression tests pass across test_workspace_evaluator.py +
test_real_llm_evaluator.py + test_evaluate_workspace_workflow.py.

## Now wireable from cron / Activepieces

A cron / Activepieces flow can fire the workflow with either evaluator:

```python
# Synthetic stub (fast smoke check — always green)
{
    "triggered_by": "cron",
    "eval_request_id": "<uuid>",
    "evaluator_kind": "synthetic_stub",
}

# Real LLM (refusal-correctness validator — runs vLLM per question)
{
    "triggered_by": "cron",
    "eval_request_id": "<uuid>",
    "question_set_filter": "refusal_correctness",
    "evaluator_kind": "real_llm_v1",
}
```

The §10.6 promotion gate (live since doc-phase 132) reads the
resulting `eval.run_summaries` row regardless of evaluator —
threshold logic doesn't change with the evaluator switch.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_evaluate_workspace_workflow.py -v
# → 3 passed in 1.98s

# Regression across all eval-related tests
docker exec georag-fastapi python -m pytest \
    tests/test_workspace_evaluator.py \
    tests/test_real_llm_evaluator.py \
    tests/test_evaluate_workspace_workflow.py
# → 20 passed in 5.49s

bash scripts/autonomous_run_substrate_verify.sh
# → 103/103 checks passed
```

## Cumulative session state — 30 ticks closed

- **Doc-phase ticks this run:** **30** (132 → 161)
- **Substrate verifier:** **103/103 PASS**
- **Live pytest cases:** 231 (228 + 3)
- **Sections closed:** §25.4 + §6
- **§04i validators graduated:** 1 of 6
- **Golden questions in DB:** 53 across 5 sets
- **Evaluator kinds wireable end-to-end:** 2 (synthetic_stub + real_llm_v1)
- **§21.3 types covered:** 8 of 8
- **PublicGeo features on map:** 95

## What's next

The Hatchet workflow now supports evaluator selection end-to-end.
A natural follow-on is to **schedule a cron** that fires the workflow
with `evaluator_kind='real_llm_v1'` on the refusal_correctness set
nightly — that becomes a real regression-detection alarm for refusal
behavior drift.

Productive next ticks:
- **Doc-phase 162** — graduate the full RAG-backed evaluator
  (real_rag_v1) wiring AgentDeps + run_deterministic_rag
- **Doc-phase 163** — add a §10.6 promotion gate threshold for
  `evaluator_kind='real_llm_v1'` (zero tolerance on refusal
  regressions per §2.9)
- **Doc-phase 164** — author SME-led question sets (core_chat,
  public_private_boundary, target_recommendation)

## Carry-overs

- `EvaluatorKind = Literal[...]` keeps additions strict — when
  `real_rag_v1` lands it needs to extend this union, which surfaces
  in the IDE + Pydantic both.
- The Eval Dashboard rendering doesn't surface `evaluator_kind` yet —
  future tick adds a column to the Recent Runs table so reviewers
  can tell which runs were synthetic vs real-LLM at a glance.
