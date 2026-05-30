"""ADR-0011 LoRA v2 — train bge-reranker-base LoRA on the augmented dataset.

Differences from v1 (_train_reranker_lora.py):
  - Input: /tmp/reranker-train-augmented/{train,val,test}.jsonl
    (~180-220 pairs across 8 domains and 6 phrasing styles)
  - Base model: /tmp/reranker-mlm (our Phase 2 MLM-adapted backbone)
  - LoRA: r=16, alpha=32 (up from r=8, α=16) — more capacity given larger
    and more diverse dataset
  - Epochs: 8 (up from 5) — dataset is ~10× bigger so we can afford more
  - Batch: 16 with gradient accumulation 2 → effective 32
  - LR: 8e-6 (slightly lower to prevent overfit on domain-adapted backbone)
  - Warmup: 10% of steps

Usage:
    docker exec georag-fastapi bash -c "python3 /tmp/_train_reranker_lora_v2.py"
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import torch

DATA_DIR = pathlib.Path("/tmp/reranker-train-augmented")
BASE_MODEL = "/tmp/reranker-mlm"  # Phase 2 MLM backbone
OUT_DIR = pathlib.Path("/tmp/reranker-lora-v2")
OUT_DIR.mkdir(exist_ok=True)

LORA_R = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
TARGET_MODULES = ["query", "value"]

EPOCHS = 8
BATCH_SIZE = 16
GRAD_ACCUM = 2   # effective batch = 32
LR = 8e-6
WARMUP_RATIO = 0.10
MAX_LEN = 512
WEIGHT_DECAY = 0.01


def load_jsonl(path: pathlib.Path) -> list[dict]:
    rows = []
    for line in path.read_text().strip().splitlines():
        rows.append(json.loads(line))
    return rows


def make_pairs(rows: list[dict]) -> list[tuple[str, str, int]]:
    """Return (query, passage, label) triples: 1=positive, 0=negative."""
    pairs = []
    for r in rows:
        q = r["query"]
        pairs.append((q, r["positive_chunk_text"], 1))
        for neg in r["hard_negative_chunk_texts"]:
            pairs.append((q, neg, 0))
    return pairs


def main():
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    from peft import LoraConfig, get_peft_model, TaskType
    from torch.utils.data import Dataset, DataLoader
    from torch.optim import AdamW
    from transformers import get_cosine_schedule_with_warmup

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device: {device}")

    # ── Data ──────────────────────────────────────────────────────────
    train_rows = load_jsonl(DATA_DIR / "train.jsonl")
    val_rows = load_jsonl(DATA_DIR / "val.jsonl")
    print(f"train: {len(train_rows)} records, val: {len(val_rows)} records")

    train_pairs = make_pairs(train_rows)
    val_pairs = make_pairs(val_rows)
    print(f"train pairs (pos+neg): {len(train_pairs)}, val: {len(val_pairs)}")

    # ── Model & tokenizer ─────────────────────────────────────────────
    print(f"Loading base model from {BASE_MODEL}...")
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    base = AutoModelForSequenceClassification.from_pretrained(
        BASE_MODEL, num_labels=1, ignore_mismatched_sizes=True,
    ).to(device)

    lora_cfg = LoraConfig(
        r=LORA_R,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        target_modules=TARGET_MODULES,
        task_type=TaskType.SEQ_CLS,
        bias="none",
    )
    model = get_peft_model(base, lora_cfg)
    model.print_trainable_parameters()

    # ── Dataset / DataLoader ──────────────────────────────────────────
    class PairDataset(Dataset):
        def __init__(self, pairs):
            self.pairs = pairs

        def __len__(self):
            return len(self.pairs)

        def __getitem__(self, idx):
            q, p, label = self.pairs[idx]
            enc = tokenizer(
                q, p,
                max_length=MAX_LEN,
                truncation=True,
                padding="max_length",
                return_tensors="pt",
            )
            return {k: v.squeeze(0) for k, v in enc.items()}, torch.tensor(label, dtype=torch.float)

    def collate(batch):
        enc_list, labels = zip(*batch)
        keys = enc_list[0].keys()
        batched = {k: torch.stack([e[k] for e in enc_list]) for k in keys}
        return batched, torch.stack(labels)

    train_loader = DataLoader(PairDataset(train_pairs), batch_size=BATCH_SIZE, shuffle=True,
                              collate_fn=collate, num_workers=0)
    val_loader   = DataLoader(PairDataset(val_pairs),  batch_size=BATCH_SIZE, shuffle=False,
                              collate_fn=collate, num_workers=0)

    # ── Optimizer & scheduler ─────────────────────────────────────────
    optimizer = AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    total_steps = (len(train_loader) // GRAD_ACCUM) * EPOCHS
    warmup_steps = int(total_steps * WARMUP_RATIO)
    scheduler = get_cosine_schedule_with_warmup(optimizer, warmup_steps, total_steps)
    loss_fn = torch.nn.BCEWithLogitsLoss()

    # ── Training loop ──────────────────────────────────────────────────
    print(f"\nTraining {EPOCHS} epochs, {total_steps} steps, warmup={warmup_steps}...")
    best_val_loss = float("inf")
    best_path = OUT_DIR / "best_adapter"

    for epoch in range(1, EPOCHS + 1):
        model.train()
        epoch_loss = 0.0
        optimizer.zero_grad()

        for step, (enc, labels) in enumerate(train_loader, 1):
            enc = {k: v.to(device) for k, v in enc.items()}
            labels = labels.to(device)
            logits = model(**enc).logits.squeeze(-1)
            loss = loss_fn(logits, labels) / GRAD_ACCUM
            loss.backward()
            epoch_loss += loss.item() * GRAD_ACCUM

            if step % GRAD_ACCUM == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()

        avg_train = epoch_loss / len(train_loader)

        # Validation
        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for enc, labels in val_loader:
                enc = {k: v.to(device) for k, v in enc.items()}
                labels = labels.to(device)
                logits = model(**enc).logits.squeeze(-1)
                val_loss += loss_fn(logits, labels).item()
        avg_val = val_loss / len(val_loader)

        print(f"  epoch {epoch}/{EPOCHS}  train_loss={avg_train:.4f}  val_loss={avg_val:.4f}")

        if avg_val < best_val_loss:
            best_val_loss = avg_val
            model.save_pretrained(str(best_path))
            tokenizer.save_pretrained(str(best_path))
            print(f"    ↑ best val_loss — saved to {best_path}")

    # ── Save final adapter ─────────────────────────────────────────────
    final_path = OUT_DIR / "adapter"
    model.save_pretrained(str(final_path))
    tokenizer.save_pretrained(str(final_path))
    print(f"\nFinal adapter saved to {final_path}")
    print(f"Best val_loss: {best_val_loss:.4f}")
    print(f"\nADR-0011 LoRA v2 training complete.")


if __name__ == "__main__":
    main()
