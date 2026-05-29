## Doc-phase 164 handoff — `evaluator_kind` surfaced on Eval Dashboard

**Status:** Live + 40/40 regression + 105/105 substrate verifier.

## What landed

Quality-of-life tick that makes the Eval Dashboard tell the
multi-evaluator story at a glance. After doc-phase 162 + 163 we have
3 evaluator kinds wireable (synthetic_stub / real_llm_v1 / real_rag_v1),
but the dashboard didn't distinguish which evaluator produced which
run — every row looked the same in the Recent Runs table. This tick
fixes that.

### Three changes

**1. Persistence — `app/services/eval/workspace_evaluator.py`**

Adds `evaluator_kind` to `trigger_payload` jsonb before the
`eval.run_summaries` INSERT. Defaults to the caller-supplied value
so the field survives even when `trigger_payload` is explicit.

```python
trigger_payload = dict(trigger_payload or {})
trigger_payload.setdefault("evaluator_kind", evaluator_kind)
```

**2. Read — `EvalDashboardController::recentRuns`**

Extracts `trigger_payload->>'evaluator_kind'` with a COALESCE
fallback to `'synthetic_stub'` for older runs that predate the
field. Adds the column to the returned array.

**3. Render — `resources/js/Pages/Admin/EvalDashboard.tsx`**

Adds a new "Evaluator" column to the Recent Runs table. Color-coded
badges:
- `real_rag_v1` → **emerald** (full pipeline)
- `real_llm_v1` → **sky** (LLM-only)
- `synthetic_stub` (or other) → stone (default)

Bumped table colspan for the empty-state from 8 to 9.

## Live verification

Probed `recentRuns()` via reflection:

```text
Recent runs: 20
  5a7cdb67  evaluator=real_rag_v1       set=refusal_correctness    8/8
  f1746943  evaluator=real_rag_v1       set=core_chat              0/0
  72448a15  evaluator=synthetic_stub    set=core_chat              0/0
  ...
```

The COALESCE fallback works — historical runs that predate the field
still render (tagged as `synthetic_stub`), and new runs persist the
real evaluator name.

Verified the DB persistence directly:

```text
   evaluator    | runs
----------------+------
 synthetic_stub |    1
 real_rag_v1    |    2
```

(The remaining ~15 historical runs have no `evaluator_kind` key in
`trigger_payload`, so they show via the COALESCE fallback.)

## Smoke verification

```bash
# Regression
docker exec georag-fastapi python -m pytest \
    tests/test_eval_validators.py \
    tests/test_workspace_evaluator.py \
    tests/test_real_llm_evaluator.py \
    tests/test_real_rag_evaluator.py
# → 40 passed in 5.70s

# Pint
vendor/bin/pint --dirty --format agent
# → {"tool":"pint","result":"passed"}

# Vite build (new column renders)
npm run build
# → EvalDashboard-Dqd_H83q.js bundled

# Verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 105/105 checks passed
```

## Cumulative session state — 33 ticks closed

- **Doc-phase ticks this run:** **33** (132 → 164)
- **Substrate verifier:** **105/105 PASS**
- **Live pytest cases:** 255 (no change — UI surface tick)
- **Sections closed:** §25.4 + §6
- **§04i validators graduated:** 2 of 6
- **Evaluator kinds visible on dashboard:** **3** (badges color-coded)
- **§21.3 types covered:** 8 of 8
- **PublicGeo features on map:** 95

## What's next

Polish / observability options:
- Add a per-evaluator-kind filter on the Recent Runs table
- Surface `validators_applied` from actual_payload on a per-row drill-in
- Add a sparkline showing pass_count over the last 30 days per evaluator

Validator graduations:
- Layer 4 entity-resolution (needs SME questions with real entity expectations)
- Layer 5 chunk-provenance (Qdrant lookup)
- Layer 1 retrieval-quality (relevance score gates)

Real wiring:
- Embedding model into AgentDeps (today None → keyword-only retrieval)
- Reranker into AgentDeps (today None → no reranking)

## Carry-overs

- The COALESCE-to-'synthetic_stub' fallback for older runs is a
  one-way story. To repopulate `evaluator_kind` on those historical
  rows, you'd need a backfill migration. Low priority — historical
  runs were all synthetic_stub.
- The badge color palette uses the existing dashboard stone/emerald/
  sky scheme. When `real_rag_v2` (or other future evaluator) lands,
  the React file picks up the new value via the existing string union;
  the fallback styling kicks in for unknown kinds.
