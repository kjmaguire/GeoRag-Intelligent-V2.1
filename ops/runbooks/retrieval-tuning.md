# Retrieval Tuning Runbook

**v1.5-17 — Module 4 Phase D.** Operator-facing decision tree for the
retrieval pipeline knobs. Use this when answer quality regresses, when a
new corpus shifts the relevance distribution, or when adding a new query
class.

For pipeline mechanics (how RRF + reranker work), see
[`retrieval-pipeline.md`](retrieval-pipeline.md). For cache invalidation,
see [`retrieval-cache.md`](retrieval-cache.md). This runbook is about
**which knob to turn when**.

---

## When to use this runbook

You are here because one of these happened:

- A user reported a wrong / missing answer and you confirmed retrieval
  is the problem (the LLM didn't get the right chunks).
- The nightly perf-baseline workflow (`perf-baseline.yml`) flagged a
  >20 % p95 regression.
- The integration suite `test_reranker_lift_averaged` failed (reranker
  regressed MRR by >5 %).
- A new corpus was ingested and recall@5 dropped against the golden set.

Before turning knobs, **always** run the diagnostic queries in
[`retrieval-pipeline.md` §Debugging a poor retrieval](retrieval-pipeline.md)
to localise the problem to a stage. The cheapest fix is the one closest
to the symptom — don't retrain SPLADE if the issue is the reranker
top-k.

---

## Knob inventory

| # | Knob | File | Default | Effect | Cost to change |
|---|------|------|---------|--------|----------------|
| 1 | `RETRIEVAL_STRATEGY_VERSION` | `app/services/query_classifier.py` | `v3.1-think-off-2026-04-21` | Cache key component; bump invalidates all cached answers | Free (intentional cache reset) |
| 2 | RRF `k` | `app/agent/fusion.py` | 60 | Smooths score blending across stores; lower → top of one store dominates | Free; bumps `RETRIEVAL_STRATEGY_VERSION` |
| 3 | Sparse prefetch limit | `app/services/sparse_encoder.py` | 100 (200 with identifier boost) | How many SPLADE candidates feed RRF | Free |
| 4 | Dense prefetch limit | `app/agent/orchestrator.py` Qdrant call | 100 | How many BGE-embedding candidates feed RRF | Free |
| 5 | Reranker top-k per class | `app/services/query_classifier.py` `RERANKER_TOP_K_BY_CLASS` | 20 (factual/unknown), 12 (spatial/document/computation/viz) | Survivors after BGE rerank | Free |
| 6 | Reranker timeout | `app/services/reranker.py` `RERANKER_TIMEOUT_S` | 2.0 s | Hard ceiling; on hit, RRF order is used | Free |
| 7 | Per-store timeouts | `app/agent/orchestrator.py` `TIMEOUT_*_S` | 5 (PG) / 2 (QDR/N4J) | Each timed-out store contributes 0 candidates | Free |
| 8 | Identifier-boost regex | `app/services/identifier_detector.py` | drillhole/section/CRS patterns | Doubles sparse prefetch when matched | Free; bumps `RETRIEVAL_STRATEGY_VERSION` |
| 9 | Relevance gate | `app/services/context_builder.py` `MIN_RERANKER_SCORE` | 0.05 | Below-gate chunks excluded from context | Free |
| 10 | SPLADE model revision | `app/services/sparse_encoder.py` `SPARSE_MODEL_REVISION` | `49cf4c7b` | Sparse retriever quality | Re-encode entire corpus |
| 11 | BGE reranker model | `app/services/reranker.py` `BGE_RERANKER_MODEL_REVISION` | `5ccf1b81` | Reranker quality | Pull model + re-cache |
| 12 | Dense embedding model | `app/services/embeddings.py` | `bge-small-en-v1.5` (384-dim) | Dense retriever quality | Re-embed entire corpus + Qdrant collection rebuild |

Knobs 1–9 are **runtime config**, free to change. Knobs 10–12 are
**model swaps** that cost a re-ingestion or re-embedding pass. Always
exhaust 1–9 before reaching for 10–12.

---

## Decision tree

```
Symptom
│
├─ Recall@5 dropped
│   ├─ Specific identifier (hole_id, section number) missing in citations
│   │   → Knob 8: extend identifier-boost regex; bump RSV
│   │
│   ├─ Right document, wrong section
│   │   → Knob 5: raise reranker top-k for that class (e.g. 20→30)
│   │
│   ├─ Document not in top 100 candidates
│   │   → Knob 3 or 4: raise prefetch limit; or Knob 10: re-embed/SPLADE
│   │
│   └─ Brand-new corpus, broad miss
│       → Re-ingestion problem, not retrieval. See ingestion-pipeline.md.
│
├─ Reranker MRR delta < 0 (test_reranker_lift_averaged red)
│   ├─ Cross-store fusion broke
│   │   → Knob 2: RRF k; lower (e.g. 60→30) makes per-store rank dominate
│   │
│   ├─ Reranker timing out frequently
│   │   → Check answer_runs.partial_failure_details for "reranker"
│   │   → Knob 6: raise timeout (2.0s→3.0s) IF p95 budget allows
│   │
│   └─ Reranker model drift / wrong revision
│       → Knob 11: pin/swap BGE model; re-cache
│
├─ Latency p95 over budget
│   ├─ One store dominating tail
│   │   → silver.answer_runs.partial_failure_details points to it
│   │   → Knob 7: tighten that store's timeout
│   │
│   ├─ Reranker is the tail
│   │   → Knob 5: lower top-k per class (20→12)
│   │   → Knob 6: lower timeout — reranker drops out cleanly under timeout
│   │
│   └─ LLM synthesis (not retrieval)
│       → Out of scope; see llm-model-swap.md
│
├─ Refusal rate spike
│   → See refusal-rate-spike.md (this runbook only covers retrieval-side
│     causes; refusal triage is its own decision tree)
│
└─ Stale answer served
    → See retrieval-cache.md
```

---

## Turning a knob — the standard procedure

1. **Open a tuning issue.** Title `tuning: <knob> <old>→<new>`. Body: the
   golden / retrieval case that motivated the change, the stage where
   you localised the regression, the diagnostic SQL output, and the
   hypothesis.

2. **Make the change in code.** Single-knob edits only — never tune two
   knobs in the same PR. You won't be able to attribute the effect.

3. **Bump `RETRIEVAL_STRATEGY_VERSION` if knob ∈ {1, 2, 8, 10, 11, 12}.**
   Knobs 3–7 and 9 don't require a bump (they don't change cached
   answer correctness).

4. **Run the integration retrieval suite locally** against the dev stack:

   ```bash
   docker compose up -d fastapi postgresql pgbouncer ollama qdrant
   cd src/fastapi
   python -m pytest tests/test_retrieval_quality.py -v -m integration
   ```

   `test_reranker_lift_averaged` and `test_per_class_mrr_visibility` print
   per-case rank tables to stdout — capture them for the issue.

5. **Run the golden suite.** A retrieval tweak should not regress
   `test_golden_queries.py`:

   ```bash
   python -m pytest tests/test_golden_queries.py -v
   ```

6. **Wait for the next perf-baseline run** (`perf-baseline.yml`, nightly
   02:00 UTC). If p95 regressed >20 % the workflow fails and pings.

7. **Roll back via Git revert if the suite or the baseline goes red.**
   Don't try to fix-forward a tuning regression — undo and re-think.

---

## Reading the measurement output

`test_reranker_lift_averaged` prints per-case rows like:

```
[reranker-lift] retrieved MRR=0.420  reranked MRR=0.667  delta=+58.8%  n=8
    [ret-001] retr=0.500 rerk=1.000
    [ret-002] retr=0.333 rerk=0.500
    ...
```

**How to read it:**

- `retrieved MRR` — RRF-only ranking, before reranker. Floor.
- `reranked MRR` — after BGE reranker. Should be ≥ floor.
- `delta` — relative lift. Healthy values are +20 % to +80 %. Negative
  delta is a regression and the test fails.
- Per-case rows surface which queries hurt the average. A single
  outlier dragging MRR down usually points at a SPLADE term mismatch
  for that query's identifier — fix at Knob 8.

`test_per_class_mrr_visibility` emits:

```
[per-class MRR — reranked stage]
    ret: MRR=0.667  n=8
```

Future enhancement: when more case prefixes are added (e.g. `spatial-*`,
`computation-*`), this naturally buckets by class. Operators should
watch for one class collapsing while others stay healthy — that's
usually a query-class-specific top-k problem (Knob 5).

---

## Anti-patterns

- **Don't tune to one query.** Single-case tuning overfits. Always
  validate against the full golden + retrieval suite before merging.
- **Don't lower `MIN_RERANKER_SCORE` (Knob 9) to make a refusal go
  away.** That's how you get hallucination — the gate is there to keep
  weak chunks out of the LLM context. If the right chunk is below the
  gate, the right chunk is wrong; tune retrieval (Knobs 3-5, 8) or
  ingestion instead.
- **Don't bump prefetch limits (Knob 3, 4) "for safety".** Each
  candidate costs reranker time. Bigger prefetch + same top-k =
  no recall lift, just latency.
- **Don't swap models (Knobs 10-12) without a corpus re-ingest plan.**
  Embedding-model swaps invalidate every Qdrant point ID.

---

## Cross-references

- [`retrieval-pipeline.md`](retrieval-pipeline.md) — flow + per-stage
  diagnostic SQL.
- [`retrieval-cache.md`](retrieval-cache.md) — invalidation when a
  knob bumps `RETRIEVAL_STRATEGY_VERSION`.
- [`refusal-rate-spike.md`](refusal-rate-spike.md) — when "fix retrieval"
  is actually "fix refusal logic".
- [`hybrid-retrieval.md`](hybrid-retrieval.md) — SPLADE + dense + RRF
  cross-store fusion mechanics.
- [`../baselines/2026-04-22-api-latency.md`](../baselines/2026-04-22-api-latency.md) — current p50/p95/p99 floors.

---

*Written 2026-04-26 during V1.5 Sprint 4 (v1.5-17 close-out). Update
whenever a knob changes or a new query class is added.*
