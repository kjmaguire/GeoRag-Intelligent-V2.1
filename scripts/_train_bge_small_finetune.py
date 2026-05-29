#!/usr/bin/env python
"""ADR-0008 Option D — domain fine-tune bge-small-en-v1.5 in place (384-dim).

Two-stage adaptation of BAAI/bge-small-en-v1.5 to the GeoRAG geological
domain. Mirrors the ADR-0011 reranker recipe (Phase 2 MLM → Phase 3
contrastive) but with a contrastive objective instead of a pointwise
relevance head, since bge-small is a bi-encoder, not a cross-encoder.

PIPELINE
========

Stage A — Continued MLM on silver.document_passages
---------------------------------------------------
Same 158k-row corpus the reranker MLM consumed (project_adr_0010_*).
2 epochs, mlm_probability=0.15, max_seq_length=512. Touches the WHOLE
backbone (token embeddings + 12 encoder layers + layer norms) so the
encoder's distribution shifts from "generic web English" toward
"NI 43-101 + assay tables + lithology logs".

Output → /tmp/bge-small-domain-ft/stage_a_mlm/

Stage B — Contrastive triplet training (MultipleNegativesRankingLoss)
---------------------------------------------------------------------
Loads the Stage A backbone as a sentence-transformers SentenceTransformer
(takes the [CLS] / mean-pooling head bge-small uses natively). Reads the
TIER 0a historical dataset at /tmp/reranker-train-historical/train.jsonl
(13,391 deduped pairs recovered from s3://reranker-labels/v1/ — see
scripts/_recover_historical_reranker_datasets.py). Each row's
(query, positive_chunk_text, hard_negative_chunk_texts[]) flattens into
triplets feeding MultipleNegativesRankingLoss (in-batch negatives +
each row's explicit hard negative).

Output → /tmp/bge-small-domain-ft/  (canonical final dir — also a
sentence-transformers / HF model directory that
src/fastapi/app/services/ingest/passage_embedder.py can swap in via
EMBEDDING_MODEL_PATH).

LIKELY HOLD WARNING — DATA QUALITY CONSTRAINT
=============================================

Per OVERNIGHT_LOG §38 + §39, the ADR-0011 reranker fine-tune was held
TWICE today on the same data-quality root cause: silver.answer_runs
holds only 27 distinct real queries. Both candidates lost to stock:

  * §38 full FT on 13,391 synthetic pairs: -0.05 NDCG / -0.07 MRR
  * §39 LoRA on 19 real queries:           -0.35 NDCG / -0.63 R@1 OOD

This script trains on the SAME TIER 0a 13,391-pair distribution.
The contrastive objective is more forgiving than the reranker's
pointwise BCE (in-batch negatives give the model more signal per
step), but the underlying distribution is still 99.96% Qwen3-
synthesized queries. Kyle is explicitly overriding the HOLD to
attempt bge-small anyway. **Expect another HOLD verdict** unless the
embedding objective happens to be less sensitive to distribution
mismatch than the cross-encoder objective.

The deciding evidence is the candidate-vs-stock NDCG@10 / MRR /
Recall@k delta from scripts/_eval_bge_small.py. Promote only if
candidate beats stock on at least NDCG@10 AND MRR (ADR-0008 §Trigger
conditions implicitly require a ≥5pp recall@20 lift on the golden
question set; this script's bench is a proxy until that set is real).

LOCKED HYPERPARAMETERS
======================

Stage A (MLM):
  * Epochs:             2
  * Batch size:         32 per device (bge-small is 23M params — fits)
  * Grad accum:         2  (effective batch = 64)
  * Learning rate:      5e-5   (matches reranker MLM)
  * Warmup ratio:       0.06
  * Max seq length:     512
  * MLM probability:    0.15

Stage B (contrastive):
  * Epochs:             3
  * Batch size:         64 per device (smaller model + smaller seq)
  * Learning rate:      2e-5
  * Warmup ratio:       0.10
  * Max seq length:     384 (queries are short; passages truncate)
  * Loss:               MultipleNegativesRankingLoss (scale=20.0)
  * Normalize embeddings: True (matches passage_embedder.py contract)

USAGE (when Kyle greenlights actual training — DO NOT RUN now)
==============================================================

    docker stop georag-vllm georag-hatchet-worker-ai   # free the GPU
    docker exec -e LOG_LEVEL=INFO georag-fastapi bash -c \\
        "python /app/scripts/_train_bge_small_finetune.py \\
            --train-pairs /tmp/reranker-train-historical/train.jsonl \\
            --output /tmp/bge-small-domain-ft"

    # smoke run (no GPU time burned):
    docker exec georag-fastapi python /app/scripts/_train_bge_small_finetune.py \\
        --train-pairs /tmp/reranker-train-historical/train.jsonl \\
        --output /tmp/bge-small-domain-ft \\
        --max-train-samples 64 --dry-run

    # skip Stage A and reuse a pre-computed MLM backbone (e.g. share the
    # reranker's /tmp/reranker-mlm — DIFFERENT TOKENIZER, will NOT work
    # for bge-small; this knob exists only for bge-small-specific MLM reruns):
    docker exec georag-fastapi python /app/scripts/_train_bge_small_finetune.py \\
        --skip-stage-a --stage-a-output /tmp/bge-small-domain-ft/stage_a_mlm \\
        --train-pairs /tmp/reranker-train-historical/train.jsonl \\
        --output /tmp/bge-small-domain-ft

Expected wall time on the A4500:
  * Stage A MLM:    ~2-4 h  (158k chunks × 2 epochs, bs=32)
  * Stage B triplet: ~1-2 h  (13,391 rows × ~4 pairs avg × 3 epochs, bs=64)
  * Total end-to-end: ~3-6 h
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("train_bge_small_finetune")


# ---------------------------------------------------------------------------
# Provenance helpers (match _train_mlm_continued.py / _train_reranker_full.py)
# ---------------------------------------------------------------------------

def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _default_output_dir() -> Path:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Path("/tmp") / f"bge-small-domain-ft-{today}-{_git_sha()}"


# ---------------------------------------------------------------------------
# Stage A — corpus loading (mirrors _train_mlm_continued.py)
# ---------------------------------------------------------------------------

async def _stream_corpus_text(conn, batch_size: int = 2000):
    """Async generator yielding chunk text from silver.document_passages.

    Same query + column name (`text`) as scripts/_train_mlm_continued.py.
    Deterministic ID-sorted batches.
    """
    offset = 0
    while True:
        rows = await conn.fetch(
            """
            SELECT text FROM silver.document_passages
            WHERE text IS NOT NULL AND length(text) >= 50
            ORDER BY passage_id
            LIMIT $1 OFFSET $2
            """,
            batch_size, offset,
        )
        if not rows:
            break
        for r in rows:
            yield r["text"]
        offset += batch_size


async def _load_corpus(args) -> list[str]:
    """Materialize the corpus into memory (~50-200 MB of text — fits)."""
    import asyncpg  # noqa: PLC0415

    dsn = os.environ.get("POSTGRES_DSN") or (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'georag')}:"
        f"{os.environ['POSTGRES_PASSWORD']}@"
        f"{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:"
        f"{os.environ.get('POSTGRES_DIRECT_PORT', 5432)}/"
        f"{os.environ.get('POSTGRES_DB', 'georag')}"
    )
    conn = await asyncpg.connect(dsn)
    try:
        texts: list[str] = []
        async for chunk in _stream_corpus_text(conn):
            texts.append(chunk)
        logger.info("loaded %d chunks from silver.document_passages", len(texts))
        if args.max_corpus_chunks and args.max_corpus_chunks > 0:
            texts = texts[: args.max_corpus_chunks]
            logger.warning("--max-corpus-chunks truncated to %d", len(texts))
        return texts
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Stage A — MLM training
# ---------------------------------------------------------------------------

def _train_stage_a_mlm(args, out_dir: Path) -> Path:
    """Continued MLM on the GeoRAG corpus. Returns the saved-backbone path."""
    import torch  # noqa: PLC0415
    from datasets import Dataset  # noqa: PLC0415
    from transformers import (  # noqa: PLC0415
        AutoModelForMaskedLM,
        AutoTokenizer,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
    )

    stage_a_out = out_dir / "stage_a_mlm"
    stage_a_out.mkdir(parents=True, exist_ok=True)

    logger.info("[Stage A] loading backbone: %s", args.backbone)
    tokenizer = AutoTokenizer.from_pretrained(args.backbone)
    model = AutoModelForMaskedLM.from_pretrained(args.backbone)
    logger.info(
        "[Stage A] model=%s vocab=%d params=%s",
        type(model).__name__, len(tokenizer),
        f"{sum(p.numel() for p in model.parameters()):,}",
    )

    texts = asyncio.run(_load_corpus(args))
    if not texts:
        raise RuntimeError("empty corpus — aborting Stage A MLM")

    def _tokenize(batch):
        return tokenizer(
            batch["text"],
            truncation=True,
            padding=False,
            max_length=args.stage_a_max_seq_length,
            return_special_tokens_mask=True,
        )

    raw_ds = Dataset.from_dict({"text": texts})
    tok_ds = raw_ds.map(
        _tokenize, batched=True, remove_columns=["text"],
        desc="stage-a-tokenize",
    )
    logger.info("[Stage A] tokenized dataset: %d examples", len(tok_ds))

    if args.dry_run:
        logger.info("[Stage A] --dry-run set; skipping Trainer.train()")
        # Persist backbone unchanged so Stage B can chain off it.
        model.save_pretrained(str(stage_a_out))
        tokenizer.save_pretrained(str(stage_a_out))
        return stage_a_out

    split_ds = tok_ds.train_test_split(test_size=0.01, seed=42)
    train_ds = split_ds["train"]
    eval_ds = split_ds["test"]
    logger.info("[Stage A] train=%d eval=%d", len(train_ds), len(eval_ds))

    collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer, mlm=True,
        mlm_probability=args.stage_a_mlm_probability,
    )

    steps_per_epoch = max(
        len(train_ds) // (args.stage_a_batch_size * args.stage_a_grad_accum), 1,
    )
    total_steps = steps_per_epoch * args.stage_a_epochs
    warmup_steps = int(total_steps * args.stage_a_warmup_ratio)

    training_args = TrainingArguments(
        output_dir=str(stage_a_out / "checkpoints"),
        num_train_epochs=args.stage_a_epochs,
        per_device_train_batch_size=args.stage_a_batch_size,
        per_device_eval_batch_size=args.stage_a_batch_size,
        gradient_accumulation_steps=args.stage_a_grad_accum,
        learning_rate=args.stage_a_learning_rate,
        warmup_steps=warmup_steps,
        logging_steps=max(steps_per_epoch // 20, 1),
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        fp16=torch.cuda.is_available(),
        report_to=[],
        remove_unused_columns=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        tokenizer=tokenizer,
        data_collator=collator,
    )

    logger.info(
        "[Stage A] training: epochs=%d batch=%d grad_accum=%d eff_batch=%d "
        "steps_per_epoch=%d total_steps=%d warmup=%d lr=%g",
        args.stage_a_epochs, args.stage_a_batch_size, args.stage_a_grad_accum,
        args.stage_a_batch_size * args.stage_a_grad_accum,
        steps_per_epoch, total_steps, warmup_steps, args.stage_a_learning_rate,
    )
    trainer.train()
    logger.info("[Stage A] MLM training complete.")

    model.save_pretrained(str(stage_a_out))
    tokenizer.save_pretrained(str(stage_a_out))

    manifest = {
        "adr":             "ADR-0008 Option D — Stage A",
        "base_backbone":   args.backbone,
        "epochs":          args.stage_a_epochs,
        "batch_size":      args.stage_a_batch_size,
        "grad_accum":      args.stage_a_grad_accum,
        "learning_rate":   args.stage_a_learning_rate,
        "warmup_ratio":    args.stage_a_warmup_ratio,
        "max_seq_length":  args.stage_a_max_seq_length,
        "mlm_probability": args.stage_a_mlm_probability,
        "train_examples":  len(train_ds),
        "eval_examples":   len(eval_ds),
        "git_sha":         _git_sha(),
        "trained_at_utc":  datetime.now(timezone.utc).isoformat(),
    }
    with open(stage_a_out / "stage_a_manifest.json", "w") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("[Stage A] wrote manifest: %s", stage_a_out / "stage_a_manifest.json")
    return stage_a_out


# ---------------------------------------------------------------------------
# Stage B — contrastive triplet training via sentence-transformers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning("bad json in %s: %s", path.name, exc)
    return rows


def _rows_to_triplets(rows: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Flatten (query, positive, [hard_negs]) into one triplet per hard neg.

    sentence-transformers' MultipleNegativesRankingLoss accepts
    (anchor, positive, negative) triplets and additionally treats every
    other row's positive in the same batch as an in-batch negative.

    Rows missing positive or with no hard negatives are dropped (the
    rerank path's val/test split sometimes ships positives-only rows).
    """
    triplets: list[tuple[str, str, str]] = []
    skipped_no_pos = 0
    skipped_no_neg = 0
    for r in rows:
        q = r.get("query")
        pos = r.get("positive_chunk_text")
        negs = r.get("hard_negative_chunk_texts") or []
        if not q or not pos:
            skipped_no_pos += 1
            continue
        if not negs:
            skipped_no_neg += 1
            continue
        for neg in negs:
            if neg and isinstance(neg, str):
                triplets.append((str(q), str(pos), str(neg)))
    logger.info(
        "[Stage B] flattened triplets: %d (skipped no-positive=%d, no-negative=%d)",
        len(triplets), skipped_no_pos, skipped_no_neg,
    )
    return triplets


def _train_stage_b_contrastive(args, stage_a_dir: Path, out_dir: Path) -> Path:
    """Contrastive triplet training. Returns the final model directory."""
    import torch  # noqa: PLC0415
    from sentence_transformers import (  # noqa: PLC0415
        InputExample,
        SentenceTransformer,
        losses,
        models,
    )
    from torch.utils.data import DataLoader  # noqa: PLC0415

    train_pairs_path = Path(args.train_pairs)
    if not train_pairs_path.is_file():
        raise FileNotFoundError(
            f"--train-pairs not found: {train_pairs_path}. "
            "Run scripts/_recover_historical_reranker_datasets.py first."
        )
    rows = _load_jsonl(train_pairs_path)
    logger.info("[Stage B] loaded %d rows from %s", len(rows), train_pairs_path)

    triplets = _rows_to_triplets(rows)
    if args.max_train_samples and args.max_train_samples > 0:
        triplets = triplets[: args.max_train_samples]
        logger.warning(
            "[Stage B] --max-train-samples truncated to %d triplets", len(triplets),
        )
    if not triplets:
        raise RuntimeError("no valid triplets — aborting Stage B")

    # Build a SentenceTransformer model on top of the Stage A backbone.
    # bge-small uses CLS-pooling natively — match that pooling to keep the
    # at-inference embedding shape identical to the production
    # passage_embedder.py path (which calls .encode(normalize=True)).
    word_emb = models.Transformer(
        str(stage_a_dir), max_seq_length=args.stage_b_max_seq_length,
    )
    pooling = models.Pooling(
        word_emb.get_word_embedding_dimension(),
        pooling_mode_cls_token=True,
        pooling_mode_mean_tokens=False,
        pooling_mode_max_tokens=False,
    )
    # Normalize so the saved model matches what bge-small ships natively
    # and what src/fastapi/app/services/ingest/passage_embedder.py expects.
    normalize = models.Normalize()
    model = SentenceTransformer(modules=[word_emb, pooling, normalize])

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    logger.info(
        "[Stage B] model device=%s dim=%d max_seq_length=%d",
        device, model.get_sentence_embedding_dimension(),
        args.stage_b_max_seq_length,
    )

    examples = [
        InputExample(texts=[anchor, positive, negative])
        for anchor, positive, negative in triplets
    ]
    train_loader = DataLoader(
        examples,
        shuffle=True,
        batch_size=args.stage_b_batch_size,
        drop_last=True,
    )
    train_loss = losses.MultipleNegativesRankingLoss(
        model=model, scale=20.0,
    )

    steps_per_epoch = max(len(train_loader), 1)
    total_steps = steps_per_epoch * args.stage_b_epochs
    warmup_steps = int(total_steps * args.stage_b_warmup_ratio)

    if args.dry_run:
        logger.info("[Stage B] --dry-run set; saving model without .fit()")
        model.save(str(out_dir))
        return out_dir

    logger.info(
        "[Stage B] training: epochs=%d batch=%d steps_per_epoch=%d total=%d "
        "warmup=%d lr=%g",
        args.stage_b_epochs, args.stage_b_batch_size, steps_per_epoch,
        total_steps, warmup_steps, args.stage_b_learning_rate,
    )
    model.fit(
        train_objectives=[(train_loader, train_loss)],
        epochs=args.stage_b_epochs,
        warmup_steps=warmup_steps,
        optimizer_params={"lr": args.stage_b_learning_rate},
        use_amp=torch.cuda.is_available(),
        output_path=str(out_dir),
        checkpoint_path=str(out_dir / "checkpoints"),
        checkpoint_save_total_limit=2,
        show_progress_bar=True,
    )
    logger.info("[Stage B] contrastive training complete.")

    # sentence-transformers .fit() with output_path already persists the
    # SentenceTransformer model dir; re-save defensively to confirm.
    model.save(str(out_dir))

    manifest = {
        "adr":              "ADR-0008 Option D — Stage B",
        "stage_a_backbone": str(stage_a_dir),
        "train_pairs":      str(train_pairs_path),
        "loss":             "MultipleNegativesRankingLoss",
        "loss_scale":       20.0,
        "pooling":          "cls",
        "normalize":        True,
        "embedding_dim":    model.get_sentence_embedding_dimension(),
        "epochs":           args.stage_b_epochs,
        "batch_size":       args.stage_b_batch_size,
        "learning_rate":    args.stage_b_learning_rate,
        "warmup_ratio":     args.stage_b_warmup_ratio,
        "max_seq_length":   args.stage_b_max_seq_length,
        "n_input_rows":     len(rows),
        "n_triplets":       len(triplets),
        "git_sha":          _git_sha(),
        "trained_at_utc":   datetime.now(timezone.utc).isoformat(),
    }
    with open(out_dir / "stage_b_manifest.json", "w") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("[Stage B] wrote manifest: %s", out_dir / "stage_b_manifest.json")
    return out_dir


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    # Top-level
    p.add_argument("--backbone", default="BAAI/bge-small-en-v1.5",
                   help="Starting HF backbone for Stage A MLM.")
    p.add_argument("--output", type=Path, default=None,
                   help="Final SentenceTransformer model dir (also holds stage_a_mlm/).")
    p.add_argument("--train-pairs", required=True,
                   help="JSONL path with (query, positive_chunk_text, "
                        "hard_negative_chunk_texts[]) rows for Stage B "
                        "(e.g. /tmp/reranker-train-historical/train.jsonl).")
    p.add_argument("--skip-stage-a", action="store_true",
                   help="Skip Stage A MLM and reuse --stage-a-output as the backbone.")
    p.add_argument("--stage-a-output", type=Path, default=None,
                   help="If --skip-stage-a, where Stage A backbone already lives.")
    p.add_argument("--dry-run", action="store_true",
                   help="Build datasets + models but skip .train() / .fit().")

    # Stage A knobs
    p.add_argument("--stage-a-epochs", type=int, default=2)
    p.add_argument("--stage-a-batch-size", type=int, default=32)
    p.add_argument("--stage-a-grad-accum", type=int, default=2)
    p.add_argument("--stage-a-learning-rate", type=float, default=5e-5)
    p.add_argument("--stage-a-warmup-ratio", type=float, default=0.06)
    p.add_argument("--stage-a-max-seq-length", type=int, default=512)
    p.add_argument("--stage-a-mlm-probability", type=float, default=0.15)
    p.add_argument("--max-corpus-chunks", type=int, default=0,
                   help="Smoke knob for Stage A corpus (0 = no cap).")

    # Stage B knobs
    p.add_argument("--stage-b-epochs", type=int, default=3)
    p.add_argument("--stage-b-batch-size", type=int, default=64)
    p.add_argument("--stage-b-learning-rate", type=float, default=2e-5)
    p.add_argument("--stage-b-warmup-ratio", type=float, default=0.10)
    p.add_argument("--stage-b-max-seq-length", type=int, default=384)
    p.add_argument("--max-train-samples", type=int, default=0,
                   help="Smoke knob for Stage B triplets (0 = no cap).")

    args = p.parse_args()

    out_dir = args.output or _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("output: %s", out_dir)

    # Stage A
    if args.skip_stage_a:
        stage_a_dir = args.stage_a_output
        if stage_a_dir is None or not Path(stage_a_dir).is_dir():
            logger.error(
                "--skip-stage-a requires --stage-a-output to point at an existing dir",
            )
            return 64
        stage_a_dir = Path(stage_a_dir)
        logger.info("[Stage A] SKIPPED — reusing backbone at %s", stage_a_dir)
    else:
        stage_a_dir = _train_stage_a_mlm(args, out_dir)

    # Stage B
    final_dir = _train_stage_b_contrastive(args, stage_a_dir, out_dir)

    # Top-level pipeline manifest (small index of the two stage manifests)
    pipeline_manifest = {
        "adr":             "ADR-0008 Option D",
        "starting_backbone": args.backbone,
        "stage_a_dir":     str(stage_a_dir),
        "final_model_dir": str(final_dir),
        "skip_stage_a":    bool(args.skip_stage_a),
        "dry_run":         bool(args.dry_run),
        "git_sha":         _git_sha(),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    with open(out_dir / "pipeline_manifest.json", "w") as fh:
        json.dump(pipeline_manifest, fh, indent=2)
    logger.info("wrote pipeline manifest: %s", out_dir / "pipeline_manifest.json")

    logger.info(
        "ADR-0008 Option D complete. Next: docker exec georag-fastapi python "
        "/app/scripts/_eval_bge_small.py --candidate %s "
        "--test /tmp/reranker-train-combined/test.jsonl",
        final_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
