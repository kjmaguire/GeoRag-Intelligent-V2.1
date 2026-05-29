#!/usr/bin/env python
"""ADR-0011 Phase 3 — full reranker fine-tune on the MLM-adapted backbone.

Variant of ``scripts/train_reranker_lora.py`` that:

  1. Takes ``--backbone`` pointing at the Phase-2 MLM-adapted dir
     (NOT a HuggingFace model identifier).
  2. Skips the LoRA wrapping — all 279M parameters are trainable.
  3. Adjusts hyperparameters for full fine-tune: lower LR, smaller
     per-device batch (full backbone gradients use more memory).

The training loop, dataset format, loss (BCE-with-logits on a
num_labels=1 head), data collator, and checkpoint format are
identical to the LoRA script. Same JSONL train/val/test contract,
same `reranker_label_dataset` upstream asset.

Locked hyperparameters per ADR-0011
===================================

  * Epochs:           3   (same as LoRA cycle)
  * Batch size:       8   (LoRA was 16 — full backbone gradients are
                            ~10× bigger because no LoRA r=16 rank
                            constraint)
  * Learning rate:    1e-5  (half LoRA's 2e-5 — full FT is more
                              sensitive to over-shoot)
  * Warmup ratio:     0.1
  * Max seq length:   512

Usage
-----

    docker stop georag-vllm georag-hatchet-worker-ai  # free GPU
    docker exec -e LOG_LEVEL=INFO georag-fastapi bash -c \\
        "python /app/scripts/_train_reranker_full.py \\
            --backbone /models/backbones/bge-reranker-base-georag-mlm-... \\
            --dataset-prefix /tmp/reranker-train \\
            --output /models/reranker/v2-domain-adapted-{date}-{sha}"

Wall time on the A4500: ~3-5 minutes (3 epochs × 882 steps × ~0.2s/step
at full-param backprop on the candidate test dataset). The big cost is
checkpoint I/O — 4.2 GB per epoch state.

Eval afterwards
---------------

The output dir is directly loadable via ``CrossEncoder(<path>)`` — no
PEFT rehydration is needed. Run the existing eval harness against it:

    docker exec georag-fastapi python /app/scripts/eval_reranker_lora.py \\
        --candidate-checkpoint <output>/model.safetensors \\
        --test //tmp/reranker-train/test.jsonl \\
        --output <output>/eval_results.json

And the 119-q live bench (after setting RERANKER_MODEL_PATH):

    docker exec -e RERANKER_MODEL_PATH=<output> georag-fastapi bash -c \\
        "python /app/scripts/run_golden_benchmark.py --max-questions 119 \\
            --label adr0011-full-finetune-candidate"
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("train_reranker_full")


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
    return Path("models/reranker") / f"v2-domain-adapted-{today}-{_git_sha()}"


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _rows_to_pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten one row into (1 positive + N negatives) pointwise pairs.
    Identical to train_reranker_lora.py's _rows_to_pairs."""
    pairs: list[dict[str, Any]] = []
    for r in rows:
        q = r["query"]
        pairs.append({"query": q, "doc": r["positive_chunk_text"], "label": 1.0})
        for neg in (r.get("hard_negative_chunk_texts") or []):
            pairs.append({"query": q, "doc": neg, "label": 0.0})
    return pairs


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--backbone", required=True,
                   help="Path to the Phase-2 MLM-adapted backbone dir.")
    p.add_argument("--dataset-prefix", required=True,
                   help="Local dir containing train/val/test.jsonl.")
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--learning-rate", type=float, default=1e-5)
    p.add_argument("--warmup-ratio", type=float, default=0.1)
    p.add_argument("--max-seq-length", type=int, default=512)
    p.add_argument("--max-train-samples", type=int, default=0,
                   help="Smoke knob: 0 = no cap")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    out_dir = args.output or _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("output: %s", out_dir)

    # --- Load splits -----------------------------------------------------
    dataset_dir = Path(args.dataset_prefix)
    train_path = dataset_dir / "train.jsonl"
    val_path = dataset_dir / "val.jsonl"
    test_path = dataset_dir / "test.jsonl"
    for p_ in (train_path, val_path, test_path):
        if not p_.is_file():
            logger.error("missing split: %s", p_)
            return 64
    train_rows = _load_jsonl(train_path)
    val_rows = _load_jsonl(val_path)
    test_rows = _load_jsonl(test_path)
    logger.info("loaded train=%d val=%d test=%d",
                len(train_rows), len(val_rows), len(test_rows))

    # --- Imports for training -------------------------------------------
    import torch  # noqa: PLC0415
    from datasets import Dataset  # noqa: PLC0415
    from transformers import (  # noqa: PLC0415
        AutoModelForSequenceClassification, AutoTokenizer,
        DataCollatorWithPadding, Trainer, TrainingArguments,
    )
    import torch.nn.functional as F  # noqa: PLC0415, N812

    logger.info("loading MLM-adapted backbone from %s", args.backbone)
    tokenizer = AutoTokenizer.from_pretrained(args.backbone)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.backbone,
        num_labels=1,
        ignore_mismatched_sizes=True,  # MLM head won't match — re-init the
                                        # classification head.
    )
    n_params = sum(p.numel() for p in model.parameters())
    logger.info("backbone: %s, vocab=%d, params=%s",
                type(model).__name__, len(tokenizer), f"{n_params:,}")

    if torch.cuda.is_available():
        device = torch.device("cuda")
        logger.info("device: cuda — %s", torch.cuda.get_device_name(0))
    else:
        device = torch.device("cpu")
        logger.warning("device: cpu — full FT on CPU will be VERY slow")
    model.to(device)

    # --- Build dataset --------------------------------------------------
    train_pairs = _rows_to_pairs(train_rows)
    val_pairs = _rows_to_pairs(val_rows)
    if args.max_train_samples and args.max_train_samples > 0:
        train_pairs = train_pairs[: args.max_train_samples]
        val_pairs = val_pairs[: max(args.max_train_samples // 10, 4)]
        logger.warning("--max-train-samples truncated: train=%d val=%d",
                       len(train_pairs), len(val_pairs))
    logger.info("pairs: train=%d val=%d", len(train_pairs), len(val_pairs))

    def _tokenize(batch):
        enc = tokenizer(
            batch["query"], batch["doc"],
            truncation=True, max_length=args.max_seq_length, padding=False,
        )
        enc["labels"] = [float(x) for x in batch["label"]]
        return enc

    train_ds = Dataset.from_list(train_pairs).map(
        _tokenize, batched=True, remove_columns=["query", "doc", "label"],
        desc="tokenize-train",
    )
    val_ds = Dataset.from_list(val_pairs).map(
        _tokenize, batched=True, remove_columns=["query", "doc", "label"],
        desc="tokenize-val",
    )
    logger.info("tokenized: train=%d val=%d", len(train_ds), len(val_ds))

    if args.dry_run:
        logger.info("--dry-run set; exiting before Trainer.train()")
        return 0

    # --- Trainer --------------------------------------------------------
    class BceLossTrainer(Trainer):
        def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
            labels = inputs.pop("labels").float()
            outputs = model(**inputs)
            logits = outputs.logits.squeeze(-1).float()
            loss = F.binary_cross_entropy_with_logits(logits, labels)
            return (loss, outputs) if return_outputs else loss

    steps_per_epoch = max(
        (len(train_ds) + args.batch_size - 1) // args.batch_size, 1,
    )
    total_steps = steps_per_epoch * args.epochs
    warmup_steps = int(total_steps * args.warmup_ratio)

    training_args = TrainingArguments(
        output_dir=str(out_dir / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        warmup_steps=warmup_steps,
        logging_steps=max(steps_per_epoch // 10, 1),
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        fp16=torch.cuda.is_available(),
        report_to=[],
        remove_unused_columns=False,
        label_names=["labels"],
    )

    trainer = BceLossTrainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        tokenizer=tokenizer,
        data_collator=DataCollatorWithPadding(tokenizer),
    )

    logger.info(
        "training: epochs=%d batch=%d steps_per_epoch=%d total=%d warmup=%d lr=%g",
        args.epochs, args.batch_size, steps_per_epoch, total_steps,
        warmup_steps, args.learning_rate,
    )
    logger.info("starting Trainer.train() (full FT, no LoRA) ...")
    trainer.train()
    logger.info("Trainer.train() complete.")

    # --- Save model + tokenizer (no PEFT — directly CrossEncoder-loadable) -
    model.save_pretrained(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))

    manifest = {
        "adr":             "ADR-0011 Phase 3",
        "backbone":        args.backbone,
        "dataset_prefix":  str(dataset_dir),
        "epochs":          args.epochs,
        "batch_size":      args.batch_size,
        "learning_rate":   args.learning_rate,
        "warmup_ratio":    args.warmup_ratio,
        "max_seq_length":  args.max_seq_length,
        "all_params_trainable": True,
        "total_params":    n_params,
        "train_rows":      len(train_rows),
        "val_rows":        len(val_rows),
        "test_rows":       len(test_rows),
        "git_sha":         _git_sha(),
        "trained_at_utc":  datetime.now(timezone.utc).isoformat(),
    }
    with open(out_dir / "training_manifest.json", "w") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("wrote manifest: %s", out_dir / "training_manifest.json")

    logger.info(
        "Phase 3 complete. Next: eval via scripts/eval_reranker_lora.py "
        "then 119-q bench with RERANKER_MODEL_PATH=%s",
        out_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
