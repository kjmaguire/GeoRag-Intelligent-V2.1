# ADR-0011: Reranker domain adaptation — vocabulary, MLM, full fine-tune

| Status     | Proposed                                              |
|------------|-------------------------------------------------------|
| Date       | 2026-05-28                                            |
| Deciders   | Kyle (SME)                                            |
| Supersedes | —                                                     |
| Related    | ADR-0010 (silver.document_passages canonical corpus)  |

## Context

ADR-0010 §5e delivered a LoRA r=16 fine-tune of `BAAI/bge-reranker-base`
that re-weights `query` + `value` attention projections only — 1.18M
trainable parameters out of 279M (0.42%). It does **not** touch the
tokenizer, token embeddings, or the ~99.58% of frozen weights.

For geological-domain reranking, three escalating axes can move beyond
the LoRA ceiling:

1. **Vocabulary** — XLM-RoBERTa's SentencePiece tokenizer has 250k whole
   words but most domain terms (sphalerite, chalcopyrite, feldspathic,
   pyroxene, supergene, hypogene, andesitic, rhyolitic, anorthositic,
   …) are subword sequences. Subword tokenization works, but every
   domain term burns 2-5 token slots vs 1 for whole-word tokens, and
   the embeddings for those subwords were trained on multilingual web
   data, not geology.

2. **Continued pretraining (MLM)** — the embedding matrix and all
   frozen layers stay at their stock-bge values. Even with LoRA on
   attention, the model never updates its sense of "what context word
   X tends to appear in" for geological X. Continued masked-language-
   modeling on the GeoRAG corpus updates the whole stack to align with
   our distribution.

3. **Full fine-tune** — the 0.42%-trainable LoRA approach has a
   ceiling. For the production reranker, training all 279M params on
   the reranker objective (after MLM continued pretraining lands a
   better backbone) is the highest-ceiling option, with the trade-off
   of much larger checkpoints and longer training time.

These layer cleanly on top of each other:

    stock bge-reranker-base
        + extended tokenizer  (1)
        + MLM continued pretraining on GeoRAG corpus  (2)
        + full reranker fine-tune  (3)
        = production reranker v2

## Decision

Build all three out as runnable scripts. Execute in order on a
multi-cycle GPU window once the Earle Physical Geology textbook is
ingested (see Plan §6c). Stage all artifacts in
`models/reranker/v2-domain-adapted/` and gate promotion on the same
≥+5pp NDCG@10 + no per-slice regression criterion ADR-0010 §5e
established.

### Locked decisions per phase

#### Phase 1 — Vocabulary extension

* **Source corpus**: `silver.document_passages` (chunked-content
  canonical from ADR-0010) + textbook chapters once landed.
* **Frequency threshold**: term must appear in ≥ 100 chunks AND be
  tokenized as ≥ 3 subwords by the stock tokenizer.
* **Whitelist filter**: term must be a noun or noun-phrase (CGI vocab
  + `silver.entity_aliases` + `silver.terms` reference lists).
  Reject anything that's a number-bearing token, a units string, or a
  whole-word that's already in the tokenizer.
* **Cap**: maximum 5,000 new tokens. Above that we hit the
  position-embedding capacity ceiling and have to widen the model.
  Below that there's plenty of headroom in the stock 250k vocab.
* **Initialization**: new token embeddings = mean of the subword token
  embeddings the term used to be tokenized to. Standard recipe;
  preserves "the model already kinda knew this word from its parts."

#### Phase 2 — Continued MLM pretraining

* **Backbone**: `BAAI/bge-reranker-base` with the Phase-1 expanded
  tokenizer. NOT the LoRA-tuned candidate — we adapt the backbone
  then re-train reranker on the adapted backbone, not adapt-on-top-of
  an existing fine-tune.
* **Corpus**: same as Phase 1 — `silver.document_passages` chunks +
  textbook chapters. Plain text only (we discard chunk_kind, page,
  bbox during MLM — only the text is supervised).
* **MLM masking**: 15% (HuggingFace default; matches RoBERTa recipe).
* **Epochs**: 2. Domain-adaptation MLM is usually 1-3 epochs;
  2 is the safe centre.
* **Effective batch size**: 64. (16 per-device × 4 grad accumulation
  on the A4500.)
* **Learning rate**: 5e-5 (10× the reranker LR — embedding matrix is
  the big mover in this phase).
* **Warmup ratio**: 0.06.
* **Max seq length**: 512 (matches reranker training).
* **Output**: full backbone saved to
  `models/backbones/bge-reranker-base-georag-mlm-{date}-{sha}/` —
  becomes the input to Phase 3.

#### Phase 3 — Full reranker fine-tune

* **Backbone**: Phase-2 MLM-adapted checkpoint.
* **Training data**: same `reranker_label_dataset` JSONL the LoRA
  cycle uses.
* **All 279M params trainable** (no LoRA wrapping). Roughly 230×
  the trainable parameter count of the LoRA cycle.
* **Learning rate**: 1e-5. Half the LoRA LR — full FT is more sensitive.
* **Epochs**: 3, same as LoRA.
* **Batch size**: 8 (smaller — full backbone gradient memory is bigger).
* **Output**: full XLMRoberta + tokenizer + adapter-style metadata at
  `models/reranker/v2-domain-adapted/`. Loaded via `CrossEncoder(<path>)`
  directly without any PEFT rehydration.

### Promotion gate (same as ADR-0010 §5e)

Per-slice question_set comparator must show:

* **Synthetic test split**: ≥ +5pp NDCG@10 over stock baseline.
* **119-question live golden_queries bench**: NO per-slice pass_rate
  regression > 2pp (Kyle's locked criterion from this morning).
* **Latency budget**: reranker pair latency must stay under
  RERANKER_TIMEOUT_S budget on the production CPU. Full fine-tune
  doesn't change parameter count vs LoRA-merged, but warm-up cost
  may differ — verify before flip.

If gate passes: stage as `RERANKER_MODEL_PATH=models/reranker/v2-domain-adapted`
under weekday-dev-hours auto-flip rules from ADR-0010, hold for Kyle
prod-flip review.

If gate fails: keep stock bge as production. Hyperparameter sweep
allowed up to 3 retry attempts, per the §5e locked failure-recovery
decision.

## Consequences

### Positive

* Vocabulary extension makes ~50% of geological terms whole-word
  tokens — same compute, more signal per token.
* MLM continued pretraining adapts the WHOLE backbone (embeddings +
  encoder + classifier head) to GeoRAG distribution, not just two
  attention projections.
* Full fine-tune unlocks the remaining ceiling that 0.42%-trainable
  LoRA can't reach.

### Negative

* **Compute cost** — MLM continued pretraining is ~4-8h of A4500 time,
  full fine-tune another 30-60 min. Pause vLLM + hatchet-worker-ai
  during these windows (same as ADR-0010 §5e).
* **Checkpoint size** — Phase 2 + Phase 3 each produce ~1.1GB
  artifacts that compound. Total v2 directory ~3-4 GB. Already
  git-ignored under `models/reranker/.gitignore`.
* **Disaster-recovery footprint** — if the v2 path proves worse and we
  roll back, we need the Phase 1 expanded tokenizer file pinned in
  git (it's small — ~5 MB) so the next attempt doesn't restart from
  scratch.

### Neutral

* No production code path changes during Phase 1-3 development. The
  candidate only goes live when RERANKER_MODEL_PATH flips to
  `models/reranker/v2-domain-adapted`. ADR-0010's env-var wiring
  carries this through unchanged.

## Implementation

Scripts shipped in this commit (all under `scripts/`):

* `_extract_domain_vocab.py` — mines candidate terms from silver +
  textbook + reference vocab lists. Outputs a frequency-ranked TSV.
* `_extend_reranker_tokenizer.py` — reads the TSV, adds tokens to a
  copy of the stock tokenizer, initializes new embeddings from
  subword means, saves expanded tokenizer + initial embedding state.
* `_train_mlm_continued.py` — HF Trainer MLM run over the GeoRAG
  corpus using the expanded tokenizer. Saves a new backbone dir.
* `_train_reranker_full.py` — variant of `train_reranker_lora.py`
  that takes a `--backbone` arg and skips LoRA wrapping (all params
  trainable).

Execution order on the next training cycle:

    # Phase 0: textbook ingest must be complete first
    docker exec georag-fastapi python /app/scripts/_ingest_earle_textbook.py

    # Phase 1: vocabulary extraction + tokenizer extension
    docker exec georag-fastapi python /app/scripts/_extract_domain_vocab.py
    docker exec georag-fastapi python /app/scripts/_extend_reranker_tokenizer.py

    # Phase 2: MLM continued pretraining (pause vLLM)
    docker stop georag-vllm georag-hatchet-worker-ai
    docker exec georag-fastapi python /app/scripts/_train_mlm_continued.py \
        --epochs 2 --batch-size 16 --grad-accum 4 --lr 5e-5

    # Phase 3: full reranker fine-tune on Phase 2 backbone
    docker exec georag-fastapi python /app/scripts/_train_reranker_full.py \
        --backbone models/backbones/bge-reranker-base-georag-mlm-... \
        --epochs 3 --batch-size 8 --lr 1e-5

    # Restore + eval
    docker start georag-vllm georag-hatchet-worker-ai
    docker exec georag-fastapi python /app/scripts/eval_reranker_lora.py ...
    docker exec georag-fastapi python /app/scripts/run_golden_benchmark.py ...

Total wall time on the A4500: ~8-12 hours end-to-end (textbook ingest
+ MLM dominates). Schedule for an overnight window.
