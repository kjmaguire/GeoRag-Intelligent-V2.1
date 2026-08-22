# Schema `eval` — Data Dictionary (skeleton)

Created by [2026_05_13_140000](../../../database/migrations/2026_05_13_140000_create_eval_schema.php).

## Tables

| Table | Purpose | Status |
|---|---|---|
| `eval.golden_questions` | SME-curated golden query set (per workspace template); awaits [golden_question_seed_loader_design.md](../golden_question_seed_loader_design.md) loader | Live (table) / Planned (loader) |
| `eval.eval_runs` | Per-nightly-run rollup | Live |
| `eval.eval_metrics_per_query` | Per-query metric breakdown (recall, citation count, etc.) | Live |
| `eval.reranker_training_pairs` | Synthetic (query, passage, label) triples — written by Dagster `reranker_labels` asset for LoRA training | Live |

## Writers

- ~~Hatchet `eval_real_rag_nightly` workflow → `eval_runs` +
  `eval_metrics_per_query`.~~ **Removed 09d1d35 (2026-07-27). These two
  tables have had NO writer since; they are empty and stay empty.**
  `scripts/run_golden_benchmark.py` writes JSON to `bench_results/`,
  not to these tables.
- Dagster `reranker_labels` + `reranker_labels_helpers` → `eval.reranker_training_pairs`.

## Reader

- Grafana panels for nightly RAG quality.
- LoRA training script: `src/fastapi/scripts/train_reranker_lora.py`.
- See [Appendix G §12](../appendix/G-rag-retrieval-contract.md#12-golden-query-eval-suite).
