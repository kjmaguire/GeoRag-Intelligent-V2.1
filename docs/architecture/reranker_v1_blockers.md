# Reranker v1 — Phase 3 (§5e) Blockers and Status

**Status:** Asset surgery landed 2026-05-29 (this session). Remaining work is a focused-session task — see "What's left" below.

## Background

Phase 3 of the Phase-1→6 dependency chain is **§5e reranker LoRA fine-tune** — fine-tune `BAAI/bge-reranker-base` on the GeoRAG corpus to improve retrieval ranking. Per MEMORY *project_reranker_v1_2026-05-19*, the synthetic-label asset graph + the NDCG eval harness + the promotion gate were already scaffolded; the training script was authored with an aspirational dataset schema.

When this session attempted to run training on the existing 2026-05-19 dataset (905 rows at `s3://reranker-labels/v1/run_id=314b2f19-0a61-4bdd-a785-2826fdeb5d1c/`), three independent blockers surfaced:

## The three original blockers

### Blocker 1 — Schema mismatch (FIXED 2026-05-29)

The `reranker_label_dataset` asset wrote records with `chunk_id` + `hardneg_ids` (UUIDs only). The training script (`scripts/train_reranker_lora.py:_build_input_examples`) expects `positive_chunk_text` + `hard_negative_chunk_texts` (denormalised text). Loading the existing JSONL would crash at `row["positive_chunk_text"]`.

**Root cause:** the persist step at `reranker_labels.py:973` constructed a record dict that dropped `positive["chunk_text"]` (which IS in scope from `mined_negatives`) and never carried the hardneg chunk texts (which weren't captured by `mined_negatives` in the first place).

**Fix:** two surgical edits in `src/dagster/georag_dagster/assets/reranker_labels.py`:

1. `reranker_mined_negatives` (around line 800): added `"chunk_text": payload.get("text", "")` to the candidate dict so each hardneg carries the text from the Qdrant payload.
2. `reranker_label_dataset` (around line 973): added `positive_chunk_text`, `hard_negative_chunk_texts`, `variant`, `query_group_id` to the persisted record. `variant` defaults to `"literal"` for back-compat; `query_group_id` is `None` for non-multi-hop rows.

Locked in by `src/dagster/tests/test_reranker_labels_schema.py` — 9 tests pinning the new schema, parallel-list invariant, default-value behaviour, and the full training-script contract.

### Blocker 2 — Stale chunk references (NOT FIXABLE on the old dataset)

The 2026-05-19 dataset references chunk UUIDs that no longer exist in `silver.document_passages`. Sampled all 5 first train rows: every `chunk_id` returned NULL when joined back. Documents were re-ingested or deleted between the 2026-05-19 materialisation and 2026-05-27, invalidating the references.

**Implication:** even with Blocker 1 fixed in the asset code, the OLD dataset's chunk IDs would still be stale. Any future training **must** re-materialise the asset chain against current data.

**Mitigation:** the Blocker-1 fix puts `positive_chunk_text` + `hard_negative_chunk_texts` directly into the JSONL — so a future re-materialisation produces self-contained samples that don't need the upstream join at training time. The stale-chunk problem can't recur on the new schema.

### Blocker 3 — Placeholder query generation (NOT YET FIXED)

Every row in the 2026-05-19 dataset has the **same** query: `"What is the numerical value of the chunk?"` — a generic placeholder, not a query the LLM actually generated from the chunk content. Sampled 5 train rows: identical. This is fatal for a reranker — training on 905 copies of the same query would teach the model nothing about retrieval.

**Suspected root causes** (none verified — needs a test materialisation):

- The 2026-05-19 run used `ELVISIO/Qwen3-30B-A3B-Instruct-2507-AWQ`. The 30B model was reverted to Qwen3-14B-AWQ in early May (MEMORY: *project_phase0_decisions*) to free VRAM for hatchet-worker-ai co-tenanting. The 30B model on that final run may have been resource-starved and falling back to a JSON-decode default.
- The chunks may have been very short / table-like (sample `fact_span` values were "3" / "8" — page-number-shaped). The query prompt has no specific guidance for empty / numeric-only chunks.
- Possible vLLM timeout + silent fallback at the asset's per-chunk LLM call (lines 540-570 of `reranker_labels.py`).

The prompts themselves (lines 117-150 of `reranker_labels.py`) look reasonable — no hard-coded placeholder string.

**Resolution path:** materialise a small (~50-chunk) test batch on the current Qwen3-14B and inspect the generated queries. If they're still placeholder-y, then the prompt or chunk-filter logic needs work. If they're meaningful, proceed to full re-materialisation.

## What landed this session (2026-05-29)

| Change | Status |
|---|---|
| `reranker_mined_negatives` captures `chunk_text` from Qdrant payload | ✅ committed |
| `reranker_label_dataset` persists 4 new fields (positive text, hardneg texts, variant, group_id) | ✅ committed |
| 9 schema regression tests (`test_reranker_labels_schema.py`) | ✅ passing |
| 41/41 existing reranker tests still green | ✅ verified |

## What's left for the next focused session

1. **Test materialisation** (~30 min GPU): run `reranker_label_dataset` (full asset chain) on a 50-chunk sample population. Inspect 10 random `train.jsonl` rows + verify queries are diverse and meaningful.
2. **If queries are good**: trigger full re-materialisation on current corpus (~6-8h GPU). The new schema means the output is self-contained and won't go stale.
3. **If queries are still placeholder-y**: prompt engineering pass on `LITERAL_PROMPT_SYSTEM` / `PARAPHRASE_PROMPT_SYSTEM` (`reranker_labels.py` lines 117-134). Possible directions: per-chunk-class prompts, explicit anti-placeholder instruction, chunk pre-filter for too-short / numeric-only.
4. **Training-script wiring** (~3-4h): implement the PEFT + sentence-transformers `CrossEncoderTrainer` + LoRA adapter save logic in `scripts/train_reranker_lora.py` (currently stubbed at line 232).
5. **Training run** (~6-12h GPU): execute against the new dataset. Output to `models/reranker/georag-bge-base-<date>-<sha>/`.
6. **NDCG eval** (~1h + GPU): `app/services/eval/ndcg_harness.py` against the test split.
7. **Promotion gate**: `app/services/eval/promotion_gate.py` enforces ≥+5pp NDCG@10 with no per-slice regression >2pp.
8. **Deploy**: only if promotion gate passes.

**Realistic total**: 2-3 days wall-clock, 12-26h active work, with real risk of re-stopping at step 3 if prompt engineering uncovers more issues.

## Decision points the next session needs

- **Sample population size**: 905 was small for a real LoRA fine-tune (~1-10K typical). Worth lowering the critique-filter threshold (`CRITIQUE_MIN_SCORE` in `reranker_labels_helpers.py`) or expanding the chunk sample to grow the keep rate.
- **Pre-filter for short chunks**: the `fact_span="3"` observation suggests pages or chunks dominated by numeric content. Adding a min-text-length filter to `reranker_chunk_sample` would prevent the LLM from being asked to generate questions about meaningless content.
- **Multi-hop ratio**: the asset emits 2 single-chunk variants (literal/paraphrase) per chunk + 1 multi-hop per report. Worth confirming the 2:1 ratio matches Kyle's intent for the corpus.

## References

- `src/dagster/georag_dagster/assets/reranker_labels.py` — the asset chain
- `src/dagster/georag_dagster/assets/reranker_labels_helpers.py` — `CRITIQUE_MIN_SCORE`, `LEAKAGE_THRESHOLD`, prompt versioning
- `src/dagster/tests/test_reranker_labels_schema.py` — schema lock-in tests added this session
- `scripts/train_reranker_lora.py` — training script (stubbed at `fit()`)
- `src/fastapi/app/services/eval/ndcg_harness.py` — NDCG@10 / MRR@10 / Recall@k evaluator
- `src/fastapi/app/services/eval/promotion_gate.py` — ≥+5pp promotion enforcement
- MEMORY: `project_reranker_v1_2026-05-19` — earlier scaffold note
- MEMORY: `project_phase0_decisions` — Qwen3-30B → 14B revert
- `src/fastapi/bench_results/2026-05-27T20-49-25Z_60e5fe5_baseline-phase2-full.json` — the "before" benchmark this work eventually compares against
