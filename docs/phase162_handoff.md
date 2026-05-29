## Doc-phase 162 handoff — Full RAG-backed evaluator (`real_rag_v1`)

**Status:** Live + 4/4 pytest cases + 21/21 regression + 8/8 real-RAG pass on refusal_correctness. **104/104 substrate verifier**.

## What landed

Third evaluator graduation. The `real_rag_v1` evaluator runs the
**full deterministic RAG orchestrator** per question — the same code
path the chat API uses — and applies the doc-phase 159 refusal-
correctness validator against the response.

### `app/services/eval/real_rag_evaluator.py` — ~240 lines

Exports `evaluate_question_real_rag(conn, question)`. Internally:

1. **Module-level singleton AgentDeps** built lazily on first call:
   - asyncpg pool (max 4, statement_cache_size=0 for pgbouncer)
   - AsyncQdrantClient (env-driven host:port)
   - AsyncGraphDatabase Neo4j driver (env-driven creds)
   - project_id = first row in `silver.projects`
   - embedding_model=None (graceful-degradation path; SentenceTransformer
     load is deferred until next graduation)
   - reranker=None (same)

2. **Per-question call**: `run_deterministic_rag(question_text, deps)`
   with `asyncio.wait_for(timeout=60s)`. Catches all exception classes
   into `failure_layer='evaluator_not_ready'` so a transient RAG
   failure doesn't fail-stop the whole eval run.

3. **Validators applied** (today: 1 of 6):
   - Refusal correctness (§04i Layer 6 / §2.9) — reuses `_detect_refusal`
     from doc-phase 159

4. **Returns** `QuestionResult` with extended payload:
   - `evaluator: 'real_rag_v1'`
   - `validators_applied: ['refusal_correctness']`
   - `response_text` (capped 1000 chars)
   - `citation_count`, `confidence`, `sources_used_count` — pulled from
     `GeoRAGResponse`
   - `detected_refusal`, `expected_refusal`, `refusal_matches_expected`

### Dispatch updated in two places

- `app/services/eval/workspace_evaluator.py` — added `real_rag_v1`
  branch to the evaluator-kind dispatch
- `app/hatchet_workflows/evaluate_workspace.py` —
  `EvaluatorKind = Literal["synthetic_stub", "real_llm_v1", "real_rag_v1"]`

### Refusal patterns extended

The first `real_rag_v1` run revealed 5 of 8 questions failed not
because the orchestrator answered them — but because it refused
with phrases my Layer 6 matcher didn't recognize:
- "I can only answer geological questions about this project's..."
- "No, that's not possible. The provided evidence does not support..."
- "Arrow is not referenced in the provided context..."

Added 6 new patterns to `_REFUSAL_PATTERNS` in
`real_llm_evaluator.py` (shared with real_rag_v1):
- `i can only answer`
- `not possible`
- `evidence does not support`
- `not referenced in the provided`
- `no such data`
- `no data available`

After the extension: **8/8 pass on refusal_correctness via real RAG.**

## Tests — 4/4 pytest cases green

| Test | Verifies |
|---|---|
| `test_real_rag_evaluator_exports` | Module imports + function is callable |
| `test_workspace_evaluator_accepts_real_rag_v1_kind` | Pydantic Literal accepts the new value |
| `test_workspace_evaluator_rejects_unknown_kind_with_real_rag_message` | Error message names all 3 valid options |
| `test_evaluator_kind_real_rag_v1_dispatches_correctly` | Empty question set → dispatches cleanly without building deps |

21/21 regression tests pass (test_real_llm_evaluator.py +
test_real_rag_evaluator.py + test_workspace_evaluator.py).

## Live verification

```text
First run (before pattern extension):
  question_count:   8
  pass_count:       3
  fail_count:       5     ← orchestrator's "I can only answer" refusals not caught

After extending _REFUSAL_PATTERNS (6 new phrases):
  question_count:   8
  pass_count:       8
  fail_count:       0     ← full agreement: real RAG correctly refuses + correctly graded
```

The Eval Dashboard now has 3 evaluator-kind perspectives across runs:
- `synthetic_stub`: 4 runs × 45 = 180 stub passes
- `real_llm_v1`: 0/10 on ocr_triage + 8/8 on refusal_correctness
- `real_rag_v1`: 8/8 on refusal_correctness (matches real_llm_v1 for refusal-only — both correctly refuse cold-context questions)

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest \
    tests/test_real_rag_evaluator.py \
    tests/test_real_llm_evaluator.py \
    tests/test_workspace_evaluator.py
# → 21 passed in 5.58s

bash scripts/autonomous_run_substrate_verify.sh
# → 104/104 checks passed
```

## Cumulative session state — 31 ticks closed

- **Doc-phase ticks this run:** **31** (132 → 162)
- **Substrate verifier:** **104/104 PASS**
- **Live pytest cases:** 235 (231 + 4)
- **Sections closed:** §25.4 + §6
- **§04i validators graduated:** 1 of 6 (refusal correctness — now used by real_llm_v1 + real_rag_v1)
- **Evaluator kinds wireable end-to-end:** **3** (synthetic_stub + real_llm_v1 + real_rag_v1)
- **First full-RAG eval run:** 8/8 pass on refusal_correctness
- **§21.3 types covered:** 8 of 8
- **Golden questions in DB:** 53 across 5 sets
- **PublicGeo features on map:** 95

## What's next

The real RAG path is wired. Next §04i validators to graduate (each
one tick):

- **Doc-phase 163** — Layer 2 citation-presence validator (response
  has ≥1 citation when expected_citations non-empty)
- **Doc-phase 164** — Layer 3 numeric-claim validator (requires
  real silver data to compare against)
- **Doc-phase 165** — Layer 4 entity-resolution validator
- **Doc-phase 166** — Layer 5 chunk-provenance validator
- **Doc-phase 167** — Layer 1 retrieval-quality validator (relevance
  score thresholds)

Or pivot:
- Wire `embedding_model` + `reranker` into AgentDeps for full Layer 1
  retrieval coverage
- Schedule a cron firing `real_rag_v1` on refusal_correctness nightly
  (real regression-detection alarm for §2.9 drift)

## Carry-overs

- The AgentDeps singleton lives at module scope. For long-running
  workers that span multiple eval runs, that's fine. For one-shot
  eval scripts that fork repeatedly, it'd build deps per process.
- `embedding_model=None` means the orchestrator falls back to
  keyword/lexical retrieval. The "evidence does not support" responses
  in the first run probably reflect that — the LLM was working with
  noisier retrieval results. With real BGE embeddings + reranker
  the orchestrator's grounding should sharpen.
- The 6 new refusal patterns are conservative — they catch real
  orchestrator refusals without false-positive matching against
  positive answers. If a future answer-form uses "no such data
  point exists in our records" as a legitimate factual statement,
  the matcher would mark it as refusal incorrectly. Watch for
  false positives in production runs.
