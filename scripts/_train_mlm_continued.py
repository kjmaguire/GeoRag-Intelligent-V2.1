#!/usr/bin/env python
"""ADR-0011 Phase 2 — continued masked-language-modeling on the GeoRAG corpus.

Adapts the full backbone (XLMRobertaForMaskedLM head replacing the
SequenceClassification head from bge-reranker-base) to the GeoRAG
distribution. Runs after Phase 1's tokenizer extension lands and uses
that expanded tokenizer + model as the input backbone.

Why MLM continued pretraining
=============================

The ADR-0010 §5e LoRA approach moved 0.42% of the parameters. The other
~99.58% (token embeddings, frozen attention K + output, feed-forward,
layer norms, classifier head) still encode whatever bge-reranker-base
learned on multilingual web data — not geology. MLM continued
pretraining touches the WHOLE backbone:

  * Token embeddings (including the new Phase-1 tokens) shift toward
    geological co-occurrence patterns.
  * All 12 encoder layers re-weight slightly to match the domain
    distribution.
  * The model learns "what context word X tends to appear in"
    for geological X.

The output backbone is then the input to Phase 3 (full reranker
fine-tune), where the reranker classification head learns relevance
on top of a backbone that already knows geology.

Locked hyperparameters per ADR-0011
===================================

  * Epochs:           2
  * Batch size:       16 per device
  * Grad accum:       4   (effective batch = 64)
  * Learning rate:    5e-5  (10× the reranker LR; embeddings move)
  * Warmup ratio:     0.06
  * Max seq length:   512  (matches reranker training)
  * MLM masking:      15%  (HF default; matches RoBERTa recipe)

Usage
-----

    docker stop georag-vllm georag-hatchet-worker-ai  # free the GPU
    docker exec georag-fastapi bash -c \\
        "LOG_LEVEL=INFO python /app/scripts/_train_mlm_continued.py \\
            --backbone /tmp/hf_cache/_bge_extended/v1-2026-05-28 \\
            --output models/backbones/bge-reranker-base-georag-mlm-{date}-{sha}"

Wall time on the A4500: ~4-8 hours over the current corpus (~12k
chunks at ~1.4k tokens each, batch 16, 2 epochs).
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

logger = logging.getLogger("train_mlm_continued")


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
    return Path("models/backbones") / f"bge-reranker-base-georag-mlm-{today}-{_git_sha()}"


async def _stream_corpus_text(conn, batch_size: int = 2000):
    """Async generator yielding chunk text from silver.document_passages.

    Column name is `text` not `chunk_text` (matches the §1b chunker
    spec — see scripts/_extract_domain_vocab.py for the same fix).
    Streams in ID-sorted batches so dataset construction is
    deterministic for a given corpus state.
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
    """Materialize the corpus into memory (it's ~50-200MB of text — fits)."""
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
        if args.max_chunks and args.max_chunks > 0:
            texts = texts[: args.max_chunks]
            logger.warning("--max-chunks truncated to %d", len(texts))
        return texts
    finally:
        await conn.close()


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--backbone", required=True,
                   help="Path to the Phase-1 expanded tokenizer + model dir")
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--epochs", type=int, default=2)
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--grad-accum", type=int, default=4)
    p.add_argument("--learning-rate", type=float, default=5e-5)
    p.add_argument("--warmup-ratio", type=float, default=0.06)
    p.add_argument("--max-seq-length", type=int, default=512)
    p.add_argument("--mlm-probability", type=float, default=0.15)
    p.add_argument("--max-chunks", type=int, default=0,
                   help="Smoke-test knob: cap corpus to N chunks (0 = no cap)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    out_dir = args.output or _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("output: %s", out_dir)

    import torch  # noqa: PLC0415
    from datasets import Dataset  # noqa: PLC0415
    from transformers import (  # noqa: PLC0415
        AutoModelForMaskedLM,
        AutoTokenizer,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
    )

    logger.info("loading expanded tokenizer + backbone from %s", args.backbone)
    tokenizer = AutoTokenizer.from_pretrained(args.backbone)
    # bge-reranker-base is XLMRoberta with a SequenceClassification head;
    # for MLM training we swap to the MLM head. The backbone weights
    # carry over; the new MLM head is randomly initialized and gets
    # trained from scratch. That's fine — we discard it after Phase 2
    # because Phase 3 re-attaches a SequenceClassification head.
    model = AutoModelForMaskedLM.from_pretrained(args.backbone)
    logger.info("model: %s, vocab=%d, params=%s",
                type(model).__name__,
                len(tokenizer),
                f"{sum(p.numel() for p in model.parameters()):,}")

    # Load corpus.
    texts = asyncio.run(_load_corpus(args))
    if not texts:
        logger.error("empty corpus — aborting")
        return 64

    # Tokenize + group into 512-token chunks. Use a streaming map for
    # memory safety on large corpora.
    def _tokenize(batch):
        return tokenizer(
            batch["text"],
            truncation=True,
            padding=False,
            max_length=args.max_seq_length,
            return_special_tokens_mask=True,
        )

    raw_ds = Dataset.from_dict({"text": texts})
    tok_ds = raw_ds.map(
        _tokenize, batched=True, remove_columns=["text"], desc="tokenize-mlm",
    )
    logger.info("tokenized dataset: %d examples", len(tok_ds))

    if args.dry_run:
        logger.info("--dry-run set; skipping Trainer.train()")
        return 0

    # Hold out 1% for eval.
    split_ds = tok_ds.train_test_split(test_size=0.01, seed=42)
    train_ds = split_ds["train"]
    eval_ds = split_ds["test"]
    logger.info("train=%d eval=%d", len(train_ds), len(eval_ds))

    collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer, mlm=True, mlm_probability=args.mlm_probability,
    )

    steps_per_epoch = max(
        len(train_ds) // (args.batch_size * args.grad_accum), 1,
    )
    total_steps = steps_per_epoch * args.epochs
    warmup_steps = int(total_steps * args.warmup_ratio)

    training_args = TrainingArguments(
        output_dir=str(out_dir / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.learning_rate,
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
        "MLM training: epochs=%d batch=%d grad_accum=%d effective_batch=%d "
        "steps_per_epoch=%d total_steps=%d warmup=%d lr=%g",
        args.epochs, args.batch_size, args.grad_accum,
        args.batch_size * args.grad_accum,
        steps_per_epoch, total_steps, warmup_steps, args.learning_rate,
    )
    logger.info("starting transformers.Trainer.train() (MLM) ...")
    trainer.train()
    logger.info("MLM training complete.")

    # Save the adapted backbone + tokenizer.
    model.save_pretrained(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))

    manifest = {
        "adr":             "ADR-0011 Phase 2",
        "base_backbone":   args.backbone,
        "epochs":          args.epochs,
        "batch_size":      args.batch_size,
        "grad_accum":      args.grad_accum,
        "learning_rate":   args.learning_rate,
        "warmup_ratio":    args.warmup_ratio,
        "max_seq_length":  args.max_seq_length,
        "mlm_probability": args.mlm_probability,
        "train_examples":  len(train_ds),
        "eval_examples":   len(eval_ds),
        "git_sha":         _git_sha(),
        "trained_at_utc":  datetime.now(timezone.utc).isoformat(),
    }
    with open(out_dir / "mlm_training_manifest.json", "w") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("wrote manifest: %s", out_dir / "mlm_training_manifest.json")

    logger.info(
        "Phase 2 complete. Next: docker exec georag-fastapi python "
        "/app/scripts/_train_reranker_full.py --backbone %s ...",
        out_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
