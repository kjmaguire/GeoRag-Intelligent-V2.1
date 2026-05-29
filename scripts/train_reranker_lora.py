#!/usr/bin/env python
"""Reranker v1 LoRA fine-tune script — domain-fine-tune bge-reranker-base in place.

Reads the JSONL splits the `reranker_label_dataset` Dagster asset writes
to ``s3://reranker-labels/v1/run_id=<id>/{train,val,test}.jsonl`` (one
row per (chunk_id, variant, query, hard_negative_chunk_ids, ...) — see
`assets/reranker_labels.py` for the exact schema), fine-tunes
``BAAI/bge-reranker-base`` with LoRA r=16 listwise InfoNCE, and writes
the adapter + training_manifest.json under
``models/reranker/georag-bge-base-{date}-{git_sha}/``.

Runs OFFLINE — this script is not invoked from the FastAPI app or from
Hatchet workflows. It expects:

  * A separate GPU pool (NOT the vLLM host — needs ~6 GiB VRAM)
  * Network access to HuggingFace for the base model checkpoint
  * Read access to the s3://reranker-labels bucket

Promotion path: after training, the eval harness
(``services/eval/ndcg_harness.py``) computes MRR@10 / NDCG@10 /
Recall@{1,5,10} / citation-precision on the test split for BOTH the
fine-tuned adapter and stock bge-reranker-base. The promotion gate in
``services/eval/promotion_gate.py`` enforces ≥+5pp NDCG@10 with no
per-slice regression >2pp before flipping the production reranker to
the new adapter.

Usage:
  python scripts/train_reranker_lora.py \\
      --dataset-prefix s3://reranker-labels/v1/run_id=<dagster_run_id> \\
      --epochs 3 \\
      --batch-size 16 \\
      --output models/reranker/georag-bge-base-$(date +%Y%m%d)-$(git rev-parse --short HEAD)
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

logger = logging.getLogger("train_reranker_lora")


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out or "nogit"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "nogit"


def _default_output_dir() -> Path:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Path("models/reranker") / f"georag-bge-base-{today}-{_git_sha_short()}"


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _build_input_examples(rows: list[dict[str, Any]]):
    """Convert reranker_label_dataset rows into sentence-transformers
    InputExamples for the cross-encoder training loop.

    Each row in the dataset has:
      query: str
      positive_chunk_text: str
      hard_negative_chunk_texts: list[str]   # 6 negatives per row
      variant: 'literal' | 'paraphrase' | 'multi_hop'
      query_group_id: str

    We emit one (query, positive, label=1.0) and 6 (query, negative,
    label=0.0) examples per row. Listwise InfoNCE is achieved by the
    CrossEncoderTrainer's contrastive loss; the per-example labels are
    just the supervision signal.
    """
    from sentence_transformers import InputExample  # deferred import

    examples: list = []
    for r in rows:
        query = r["query"]
        positive = r["positive_chunk_text"]
        negatives = r.get("hard_negative_chunk_texts", []) or []
        examples.append(InputExample(texts=[query, positive], label=1.0))
        for neg in negatives:
            examples.append(InputExample(texts=[query, neg], label=0.0))
    return examples


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dataset-prefix",
        required=True,
        help=(
            "Either a local directory containing train.jsonl / val.jsonl / "
            "test.jsonl OR an s3://reranker-labels/v1/run_id=<id> prefix. "
            "S3 prefixes are streamed via boto3."
        ),
    )
    parser.add_argument("--base-model", default="BAAI/bge-reranker-base")
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--warmup-ratio", type=float, default=0.1)
    parser.add_argument("--max-seq-length", type=int, default=512)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output dir (defaults to models/reranker/georag-bge-base-{date}-{sha})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve dataset + build examples + log the plan, but don't train.",
    )
    parser.add_argument(
        "--max-train-samples",
        type=int,
        default=0,
        help=(
            "If >0, truncate the (positive+negative) training pair list to this "
            "many examples. Smoke-test knob: use 200 to confirm the training "
            "step actually progresses before committing to a multi-hour run."
        ),
    )
    args = parser.parse_args()

    out_dir: Path = args.output or _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("output dir: %s", out_dir)

    # --- Stage 1: load dataset --------------------------------------------
    prefix = args.dataset_prefix
    if prefix.startswith("s3://"):
        logger.error(
            "S3 streaming is not wired in this scaffold yet — "
            "download the splits locally first: "
            "aws s3 cp --recursive %s ./dataset/  then re-run with "
            "--dataset-prefix ./dataset",
            prefix,
        )
        return 64

    dataset_dir = Path(prefix)
    train_path = dataset_dir / "train.jsonl"
    val_path = dataset_dir / "val.jsonl"
    test_path = dataset_dir / "test.jsonl"
    for p in (train_path, val_path, test_path):
        if not p.is_file():
            logger.error("missing required split: %s", p)
            return 64

    logger.info("loading train / val / test ...")
    train_rows = _load_jsonl(train_path)
    val_rows = _load_jsonl(val_path)
    test_rows = _load_jsonl(test_path)
    logger.info(
        "loaded train=%d val=%d test=%d", len(train_rows), len(val_rows), len(test_rows),
    )

    # Multi-hop accounting (rows with variant='multi_hop' belong to a
    # query_group_id shared across 2 chunks — the trainer treats both
    # as positives for the same query implicitly via duplicate query
    # InputExamples).
    multi_hop_groups = {r["query_group_id"] for r in train_rows if r.get("variant") == "multi_hop"}
    logger.info("multi-hop training query-groups: %d", len(multi_hop_groups))

    # --- Stage 2: write training manifest ---------------------------------
    manifest = {
        "base_model": args.base_model,
        "lora": {
            "r": args.lora_r,
            "alpha": args.lora_alpha,
            "dropout": args.lora_dropout,
        },
        "training": {
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "learning_rate": args.learning_rate,
            "warmup_ratio": args.warmup_ratio,
            "max_seq_length": args.max_seq_length,
        },
        "dataset": {
            "prefix": str(prefix),
            "train_rows": len(train_rows),
            "val_rows": len(val_rows),
            "test_rows": len(test_rows),
            "multi_hop_groups": len(multi_hop_groups),
        },
        "git_sha": _git_sha_short(),
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
    }
    manifest_path = out_dir / "training_manifest.json"
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("wrote manifest: %s", manifest_path)

    if args.dry_run:
        logger.info("--dry-run set; skipping CrossEncoderTrainer invocation")
        return 0

    # --- Stage 3: training ------------------------------------------------
    # Imports are deferred so --dry-run works without the training deps
    # installed (sentence-transformers + peft + transformers + accelerate).
    try:
        import torch
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        logger.error(
            "Training dependencies not installed (%s). "
            "Install them first: pip install sentence-transformers peft "
            "transformers accelerate torch. "
            "See the reranker-v1 ADR for the recommended pinned versions.",
            exc,
        )
        return 65

    try:
        from peft import LoraConfig, TaskType, get_peft_model
    except ImportError as exc:
        logger.error(
            "PEFT not installed (%s). Install with: pip install peft. "
            "Required for LoRA r=%d fine-tune.",
            exc,
            args.lora_r,
        )
        return 65

    # --- Stage 3a: build the CrossEncoder + wrap the backbone in LoRA ----
    #
    # The bge-reranker-base architecture is BertForSequenceClassification.
    # PEFT's LoraConfig wraps the query/value attention projections per
    # the standard BERT LoRA recipe — same target_modules the reranker
    # LoRA literature settled on.
    logger.info("loading base model: %s", args.base_model)
    cross_encoder = CrossEncoder(
        args.base_model,
        num_labels=1,
        max_length=args.max_seq_length,
    )

    lora_config = LoraConfig(
        task_type=TaskType.SEQ_CLS,
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        target_modules=["query", "value"],
        bias="none",
    )
    cross_encoder.model = get_peft_model(cross_encoder.model, lora_config)
    trainable = sum(
        p.numel() for p in cross_encoder.model.parameters() if p.requires_grad
    )
    total = sum(p.numel() for p in cross_encoder.model.parameters())
    logger.info(
        "LoRA wrapped: trainable=%s / total=%s (%.2f%%)",
        f"{trainable:,}", f"{total:,}", 100.0 * trainable / max(total, 1),
    )
    if torch.cuda.is_available():
        device = torch.device("cuda")
        logger.info("device: cuda — %s", torch.cuda.get_device_name(0))
    else:
        device = torch.device("cpu")
        logger.warning(
            "device: cpu — CUDA not available; training will be VERY slow. "
            "Verify nvidia-smi works inside this container before relying "
            "on a multi-hour run."
        )
    cross_encoder.model.to(device)

    # --- Stage 3b: build a tokenized HF Dataset for transformers.Trainer --
    #
    # 2026-05-28 ADR-0010 training-stack workaround:
    # sentence_transformers v5.5 + peft 0.19 + transformers 4.57 combine
    # to break cross_encoder.fit() — the v5 fit_mixin path passes a
    # BatchEncoding object as positional `input_ids` to the PEFT-wrapped
    # XLMRoberta, then warn_if_padding_and_no_attention_mask crashes on
    # `input_ids[:, [-1, 0]]` with "list indices must be integers or
    # slices, not tuple".
    #
    # Option (b) per Kyle's 2026-05-28 sign-off: drive transformers.Trainer
    # directly against cross_encoder.model (which IS the PEFT-wrapped
    # XLMRoberta). We pre-tokenize into an HF Dataset, attach a standard
    # DataCollatorWithPadding, and override compute_loss to BCE because
    # the model is num_labels=1 (regression-style head) but our labels
    # are {0.0, 1.0} pointwise targets — the default Trainer MSE loss
    # would underfit relative to BCE.
    #
    # Touches ZERO production dependency versions — the live reranker /
    # embedder paths in fastapi continue using sentence_transformers v5
    # the same way. Only the offline training script changes shape.
    from datasets import Dataset
    from transformers import (
        DataCollatorWithPadding,
        Trainer,
        TrainingArguments,
    )
    import torch.nn.functional as F  # noqa: N812 — torch convention

    def _rows_to_pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Flatten one row into (1 positive + N negatives) pointwise pairs.

        Matches the previous _build_input_examples semantics — same
        supervision signal (label=1.0 for positives, 0.0 for negatives),
        same listwise-via-pointwise approximation under BCE loss.
        """
        pairs: list[dict[str, Any]] = []
        for r in rows:
            q = r["query"]
            pairs.append({"query": q, "doc": r["positive_chunk_text"], "label": 1.0})
            for neg in (r.get("hard_negative_chunk_texts") or []):
                pairs.append({"query": q, "doc": neg, "label": 0.0})
        return pairs

    train_pairs = _rows_to_pairs(train_rows)
    val_pairs = _rows_to_pairs(val_rows)
    if args.max_train_samples and args.max_train_samples > 0:
        train_pairs = train_pairs[: args.max_train_samples]
        val_pairs = val_pairs[: max(args.max_train_samples // 10, 4)]
        logger.warning(
            "--max-train-samples=%d → truncated train_pairs=%d val_pairs=%d "
            "(smoke-test mode — DO NOT promote artifacts from this run)",
            args.max_train_samples, len(train_pairs), len(val_pairs),
        )
    logger.info(
        "pairs: train=%d val=%d (1 positive + N negatives per row)",
        len(train_pairs), len(val_pairs),
    )

    tokenizer = cross_encoder.tokenizer

    def _tokenize(batch: dict[str, list]) -> dict[str, list]:
        enc = tokenizer(
            batch["query"],
            batch["doc"],
            truncation=True,
            max_length=args.max_seq_length,
            padding=False,
        )
        enc["labels"] = [float(x) for x in batch["label"]]
        return enc

    train_ds = Dataset.from_list(train_pairs).map(
        _tokenize,
        batched=True,
        remove_columns=["query", "doc", "label"],
        desc="tokenize-train",
    )
    val_ds = Dataset.from_list(val_pairs).map(
        _tokenize,
        batched=True,
        remove_columns=["query", "doc", "label"],
        desc="tokenize-val",
    )
    logger.info("tokenized: train=%d val=%d", len(train_ds), len(val_ds))

    steps_per_epoch = max(
        (len(train_ds) + args.batch_size - 1) // args.batch_size, 1,
    )
    total_steps = steps_per_epoch * args.epochs
    warmup_steps = int(total_steps * args.warmup_ratio)

    # --- Stage 3c: BCE Trainer against the PEFT-wrapped backbone ---------
    class BceLossTrainer(Trainer):
        """num_labels=1 head + {0.0, 1.0} pointwise targets → BCE loss."""

        def compute_loss(
            self,
            model,
            inputs,
            return_outputs: bool = False,
            **kwargs,
        ):
            labels = inputs.pop("labels").float()
            outputs = model(**inputs)
            logits = outputs.logits.squeeze(-1).float()
            loss = F.binary_cross_entropy_with_logits(logits, labels)
            return (loss, outputs) if return_outputs else loss

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
        disable_tqdm=False,
    )

    trainer = BceLossTrainer(
        model=cross_encoder.model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        tokenizer=tokenizer,
        data_collator=DataCollatorWithPadding(tokenizer),
    )

    logger.info(
        "training: epochs=%d batch_size=%d steps_per_epoch=%d "
        "total_steps=%d warmup_steps=%d lr=%g",
        args.epochs, args.batch_size, steps_per_epoch, total_steps,
        warmup_steps, args.learning_rate,
    )
    logger.info("starting transformers.Trainer.train() (BCE-on-PEFT) ...")
    trainer.train()
    logger.info("transformers.Trainer.train() complete.")

    # --- Stage 3d: save LoRA adapter + final checkpoint -------------------
    adapter_dir = out_dir / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    cross_encoder.model.save_pretrained(str(adapter_dir))
    logger.info("LoRA adapter saved: %s", adapter_dir)

    # Save the tokenizer alongside the adapter so the eval harness can
    # reload the model in one call without re-fetching the base tokenizer.
    cross_encoder.tokenizer.save_pretrained(str(adapter_dir))
    # 2026-05-28 ADR-0010 workaround: `cross_encoder.save(final_dir)` is
    # broken in sentence_transformers v5.5 when the underlying module is
    # PEFT-wrapped — it calls `.save()` on the wrapper, which only exposes
    # `.save_pretrained()`. The adapter is already serialised on the line
    # above; the eval harness loads the base model via `CrossEncoder(base)`
    # then attaches the adapter via `PeftModel.from_pretrained()`, so the
    # `final/` snapshot was never on the load path. Skip it.

    # Update the manifest with training-completion metadata.
    manifest["training"]["trainable_params"] = trainable
    manifest["training"]["total_params"] = total
    manifest["training"]["warmup_steps"] = warmup_steps
    manifest["training"]["steps_per_epoch"] = steps_per_epoch
    manifest["training"]["device"] = str(device)
    manifest["training"]["completed_at_utc"] = (
        datetime.now(timezone.utc).isoformat()
    )
    manifest["artifacts"] = {
        "adapter": str(adapter_dir),
        "checkpoints": str(out_dir / "checkpoints"),
    }
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("manifest updated with training completion: %s", manifest_path)

    # --- Stage 3e: hand off to promotion gate -----------------------------
    logger.info(
        "training complete. Next step: run the eval harness against the "
        "test split + adapter dir, then promotion_gate. Example:\n"
        "  python -m georag_fastapi.services.eval.ndcg_harness "
        "--checkpoint %s --test-split %s\n"
        "  python -m georag_fastapi.services.eval.promotion_gate "
        "--candidate %s --baseline BAAI/bge-reranker-base",
        adapter_dir, test_path, adapter_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
