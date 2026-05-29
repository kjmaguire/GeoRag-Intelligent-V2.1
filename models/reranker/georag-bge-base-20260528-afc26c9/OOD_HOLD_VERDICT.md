# 2026-05-28 LoRA candidate — HOLD verdict (post-OOD bench)

## TL;DR

The §5e LoRA candidate **failed the out-of-distribution promotion gate**
on the 119-question live `golden_queries` bench. Verdict: **HOLD** (no
production flip). Restored stock `BAAI/bge-reranker-base` as the
running reranker.

## Numbers

| Metric | Baseline (stock) | Candidate (LoRA) | Delta |
|---|---|---|---|
| pass_rate | 16/119 = 13.45% | 10/119 = 8.40% | **−5.05pp** |
| avg_latency_ms | 8,886 | 34,153 | **+25,267ms (4×)** |
| p95_latency_ms | 11,600 | 90,086 | +78,486 (cap hit) |
| 6_refusal | 79 | 66 | −13 |
| 5_chunk_provenance | 24 | 8 | −16 |
| evaluator_not_ready | 0 | 35 | **+35 (timeouts)** |

## Per-slice breakdown

| question_set | baseline | candidate | delta |
|---|---|---|---|
| **core_chat** | **6/35 = 17.1%** | **0/35 = 0%** | **−17.1pp** ⚠ |
| refusal_correctness | 10/10 = 100% | 10/10 = 100% | 0 ✓ |
| numeric_grounding | 0/30 | 0/30 | 0 |
| ocr_triage | 0/10 | 0/10 | 0 |
| public_private_boundary | 0/1 | 0/1 | 0 |
| report_section | 0/15 | 0/15 | 0 |
| schema_mapping | 0/18 | 0/18 | 0 |

The entire regression concentrates in `core_chat` (the conversational
slice). Six questions the stock model could answer with valid citations,
the LoRA model cannot.

## Contrast with the synthetic-test win

The 160-row synthetic test split (drawn from the same critique-filtered
generator that produced training) showed the LoRA candidate winning by
**+0.088 NDCG@10 / +0.114 MRR / +0.138 Recall@1**. That win did NOT
generalize.

## Root cause hypothesis

1. **Distribution mismatch**: the synthetic reranker_label_dataset
   generator produces stylized single-fact queries ("What is the
   numerical value of X?"). The `core_chat` slice is conversational —
   multi-clause, paraphrased, geological discourse markers. The LoRA
   re-weighting of attention favors the synthetic pattern at the cost
   of natural-language robustness.

2. **Latency regression**: same parameter count after `merge_and_unload`
   but ~4× slower on CPU inference. Pair-latency on 10 candidates went
   from ~3-4s (stock) to ~3-8s with frequent 8s timeouts. Possible
   causes: fp32 vs fp16 dtype after merge, lost weight-sharing
   optimization, classifier-head init difference. 35/119 questions hit
   the 90s per-question total timeout because of this.

3. **`evaluator_not_ready` bucket**: 35 questions where the per-query
   wall ran past 90s and the evaluator gave up. These would fail even
   if the candidate produced perfect rankings, because they never get
   evaluated. Some of those 35 might pass at 120s timeout but the bench
   still proves the latency regression is unacceptable for production.

## Recovery plan (already scaffolded for next cycle)

Per ADR-0011 (docs/adr/0011-reranker-domain-adaptation.md):

1. Ingest the Earle textbook (Phase 0) — diversifies the chunk pool.
2. Mine domain vocabulary INCLUDING textbook content (Phase 1).
3. MLM continued pretraining on the full corpus (Phase 2) — adapts the
   whole backbone, not just attention.
4. Full reranker fine-tune (Phase 3) — much higher capacity than
   0.42%-trainable LoRA.

Additional changes the bench result motivates:

* **Augment the synthetic generator** with natural-language paraphrases
  + multi-clause queries so the training distribution matches
  `core_chat`-style queries.
* **Wire golden_queries as a held-out OOD eval STEP** in the
  `reranker_label_dataset` Dagster asset itself — if NDCG on real
  queries doesn't move alongside the synthetic-test NDCG, abort the
  training run before consuming GPU time.
* **Investigate the latency regression** — likely a dtype / save-path
  detail. Run a side-by-side timing test with `merged.half()` before
  `save_pretrained()` and re-bench.

## Files in this directory

* `training_manifest.json` — hyperparams + dataset shape + git_sha for
  the 2026-05-28 LoRA run.
* `eval_results.json` — synthetic 160-row test split metrics.
* `bench_baseline_stock_full119q.json` — morning's stock-reranker
  candidate corpus bench (the reranker-A/B baseline).
* `bench_lora_candidate_full119q.json` — afternoon's LoRA-tuned
  reranker bench. The regressing measurement.
* `OOD_HOLD_VERDICT.md` — this file.
* `adapter/`, `best/`, `checkpoints/` (gitignored) — 1.1GB merged
  safetensors + per-epoch HF Trainer state. Kept on host disk for
  forensics; not in git.
