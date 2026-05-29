## Doc-phase 159 handoff — Real vLLM-backed evaluator (§10.4 + §04i refusal-correctness)

**Status:** Live + 9/9 pytest cases + first real-LLM eval run lands honest signal. **102/102 substrate verifier**.

## What landed

First real graduation of `evaluate_question` away from the doc-phase
132 synthetic_stub. The new `real_llm_v1` evaluator calls vLLM
directly + applies the first §04i validator (refusal correctness /
Layer 6 / §2.9 language template).

### `app/services/eval/real_llm_evaluator.py` — ~150 lines

Exports:
- `evaluate_question_real_llm(conn, question)` — drop-in replacement
  for the synthetic stub
- `_detect_refusal(text)` — refusal-pattern matcher (case-insensitive,
  16 canonical phrases including the §2.9 "no public data within 25"
  template)
- `_call_vllm()` — OpenAI-compatible POST to
  `${VLLM_URL}/chat/completions`

Pipeline per question:
1. Build prompt: §2.9-aware system prompt + question_text as user message
2. POST to vLLM (qwen3-30B-AWQ today) with T=0, max_tokens=256
3. Capture response_text, total_tokens, latency_ms
4. Run `_detect_refusal(response_text)` → bool
5. Compare to `question.expected_refusal` → set passed flag
6. Build QuestionResult:
   - `actual_payload.evaluator='real_llm_v1'`
   - `actual_payload.response_text=<first 1000 chars>`
   - `actual_payload.detected_refusal=<bool>`
   - `actual_payload.expected_refusal=<bool>`
   - `failure_layer='refusal'` on mismatch, `'evaluator_not_ready'`
     on network/timeout, else None

### `app/services/eval/workspace_evaluator.py` — dispatch

Added `evaluator_kind` parameter to `run_workspace_evaluation`.
Default `'synthetic_stub'` preserves doc-phase 132 behavior;
`'real_llm_v1'` selects the new path. Invalid values raise.

Per-question loop now uses `per_question_evaluator(conn, q)` rather
than calling `evaluate_question` directly — single point of dispatch.

## Tests — `src/fastapi/tests/test_real_llm_evaluator.py`

**9 pytest cases, all green:**

Unit (5):
- `test_detect_refusal_canonical_phrases` — "I cannot", "I can't", "unable to" all detected
- `test_detect_refusal_29_template_phrase` — §2.9 "no public data within 25"
- `test_detect_refusal_no_match_on_normal_answer`
- `test_detect_refusal_empty_or_none`
- `test_detect_refusal_case_insensitive`

Orchestration (2):
- `test_run_workspace_evaluation_rejects_unknown_evaluator` — `evaluator_kind='nonsense'` raises ValueError
- `test_run_workspace_evaluation_synthetic_stub_default` — backward compat preserved

vLLM integration (2 — skip when vLLM unreachable):
- `test_real_llm_evaluator_returns_refusal_correctly_paired` — verify
  `actual_payload` shape + tokens + latency populated when vLLM live
- `test_real_llm_evaluator_handles_vllm_unreachable_gracefully` —
  unreachable URL → `failure_layer='evaluator_not_ready'`

## Live verification — first real eval run

Ran `real_llm_v1` against the 10 active `ocr_triage` golden questions:

```text
run_id:           9ef9b3af-4347-448d-90ea-bf747f35fcbd
question_count:   10
pass_count:       0
fail_count:       10
success:          True
```

**Every question failed — and that's honest signal.** Inspection of
the run_results shows the LLM correctly refused each OCR-triage
question because it lacks the context to answer:

> "I cannot provide a route page with layout confidence, as the
> request involves a technical issue (Docling layout failure) and
> lacks sufficient context for a meaningful response. Please clarify
> or provi..."

These questions are designed to test **RAG-with-context** behavior.
The `real_llm_v1` evaluator runs cold — no retrieval, no document
context. The LLM correctly identifies the missing context and
refuses; the evaluator correctly grades this as a mismatch against
`expected_refusal=False`. Net signal: **"the LLM-only path fails on
context-dependent questions"** — exactly what a sanity test should
say.

When the next graduation lands (RAG-backed evaluator with
`AgentDeps` + `run_deterministic_rag`), the same questions should
pass because retrieval provides the needed context.

## Eval Dashboard now shows real signal

`/admin/eval-dashboard` recent runs:
- Doc-phase 132 runs: 4 runs / 115 passed via synthetic_stub
- **Doc-phase 159 run: 1 run / 0 passed / 10 failed via real_llm_v1**

The dashboard tells a clean story: stub-mode runs report green;
real-LLM-mode reveals which questions actually need retrieval.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_real_llm_evaluator.py -v
# → 9 passed in 3.18s

# Verify backward compat — synthetic stub still works
docker exec georag-fastapi python -m pytest tests/test_workspace_evaluator.py
# → all green (no regression)

bash scripts/autonomous_run_substrate_verify.sh
# → 102/102 checks passed
```

## Cumulative session state — 28 ticks closed

- **Doc-phase ticks this run:** **28** (132 → 159)
- **Sections closed:** §25.4 + §6 (2 of 12)
- **§04i hallucination-prevention validators graduated in evaluator:** **1 of 6** (refusal correctness)
- **Real LLM integration milestones:** **1** (first vLLM call in production eval path)
- **Cross-section integrations live:** 1 (§7.2 ↔ §9.13)
- **Inertia writer surfaces:** 1 (DecisionNew)
- **§21.3 decision types with authentic captures:** 8 of 8
- **Substrate verifier:** **102/102 PASS**
- **Live pytest cases:** 228 (219 + 9)

## What's next

Real LLM evaluator's first validator (refusal correctness) is live.
Each subsequent §04i layer is a one-tick graduation that adds another
validator without changing the surrounding orchestration:

- **Doc-phase 160** — Layer 1 retrieval validator (would need Qdrant
  hooked up + AgentDeps construction — bigger scope)
- **Doc-phase 161** — Layer 2 typed-output validator (Pydantic AI
  validation against expected_citations schema)
- **Doc-phase 162** — Layer 3 numeric-claim validator (compare
  extracted numbers against expected_numeric_values)
- **Doc-phase 163** — Layer 4 entity validator (extracted entities
  against expected_entities)
- **Doc-phase 164** — Layer 5 chunk-provenance validator (citations
  resolve to real chunks)

Each adds rows to the `validators_applied` list in actual_payload.
A future evaluator_kind='real_rag_v1' would chain all 6 + use
`run_deterministic_rag` for real retrieval.

## Carry-overs

- The `evaluator_kind` parameter is set at the workspace_evaluator
  level — there's no way to mix evaluators per-question yet. If we
  want a "synthetic_stub on most + real_llm_v1 on refusal_correctness"
  pattern, we'd add per-question evaluator routing in `_load_active_questions`.
- The refusal-detection pattern list is conservative. Production
  systems should escalate to LLM-judged refusal classification (a
  second LLM call asking "did this response refuse?"). That'd be a
  separate evaluator layer.
- `actual_payload.response_text` is capped at 1000 chars. Longer
  responses get truncated for storage. Future: store full text in
  SeaweedFS + reference the URI on the row.
