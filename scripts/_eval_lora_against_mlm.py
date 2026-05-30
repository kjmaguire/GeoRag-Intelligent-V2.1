"""PEFT-aware NDCG eval for the Plan B v2 LoRA cycle.

scripts/eval_reranker_lora.py was hardcoded for:
  - baseline = stock BAAI/bge-reranker-base
  - candidate base = stock BAAI/bge-reranker-base (re-wrapped)

But our Plan B v2 LoRA was trained on top of the MLM-adapted backbone
(/tmp/reranker-mlm, vocab=250242), not stock — so we must wrap THAT as
the base before loading the LoRA state dict. Otherwise the keys mismatch
and the candidate effectively scores as random-init.

Both baseline (stock) and candidate (mlm-base + LoRA merged) are
evaluated on the same test split. Output JSON shape matches
_eval_reranker_full.py.
"""
from __future__ import annotations
import argparse, json, math
from pathlib import Path
from statistics import mean


def load_test(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def metrics_for_rank(rank_of_positive, k_list=(1, 5, 10)):
    out = {}
    out["ndcg_at_10"] = 1.0 / math.log2(rank_of_positive + 1) if rank_of_positive <= 10 else 0.0
    out["mrr"] = 1.0 / rank_of_positive
    for k in k_list:
        out[f"recall_at_{k}"] = 1.0 if rank_of_positive <= k else 0.0
    return out


def score_pairs(model, tokenizer, pairs, device, batch_size=32, max_length=512):
    import torch
    scores = []
    model.eval()
    with torch.no_grad():
        for i in range(0, len(pairs), batch_size):
            chunk = pairs[i:i + batch_size]
            enc = tokenizer(
                [p[0] for p in chunk], [p[1] for p in chunk],
                padding=True, truncation=True, max_length=max_length, return_tensors="pt",
            ).to(device)
            out = model(**enc)
            logits = out.logits.squeeze(-1).float().cpu().tolist()
            scores.extend(logits if isinstance(logits, list) else [logits])
    return scores


def evaluate(model, tokenizer, rows, device):
    per_row = []
    skipped = 0
    for r in rows:
        q = r.get("query")
        pos = r.get("positive_chunk_text")
        negs = r.get("hard_negative_chunk_texts") or []
        if not q or not pos or not negs:
            skipped += 1
            continue
        candidates = [pos] + negs
        scores = score_pairs(model, tokenizer, [(q, c) for c in candidates], device)
        ranked = sorted(range(len(scores)), key=lambda i: -float(scores[i]))
        rank_of_positive = ranked.index(0) + 1
        per_row.append(metrics_for_rank(rank_of_positive))
    keys = list(per_row[0].keys())
    agg = {k: mean(r[k] for r in per_row) for k in keys}
    agg["n_queries"] = len(per_row)
    agg["n_skipped"] = skipped
    return agg


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", default="BAAI/bge-reranker-base")
    p.add_argument("--candidate-base", required=True,
                   help="The MLM-adapted backbone the LoRA was trained on top of.")
    p.add_argument("--candidate-adapter", required=True,
                   help="LoRA adapter directory (contains model.safetensors with PEFT keys).")
    p.add_argument("--lora-r", type=int, default=8)
    p.add_argument("--lora-alpha", type=int, default=16)
    p.add_argument("--lora-dropout", type=float, default=0.05)
    p.add_argument("--test", required=True)
    p.add_argument("--output", default="/tmp/reranker-lora-bench.json")
    args = p.parse_args()

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    rows = load_test(args.test)
    print(f"loaded {len(rows)} test rows")
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # ── Baseline (stock) ────────────────────────────────────────────
    print(f"\n[1/2] baseline: {args.baseline}")
    baseline_tok = AutoTokenizer.from_pretrained(args.baseline)
    baseline_model = AutoModelForSequenceClassification.from_pretrained(
        args.baseline, num_labels=1,
    ).to(device).eval()
    baseline_metrics = evaluate(baseline_model, baseline_tok, rows, device)
    print(f"  {baseline_metrics}")
    del baseline_model
    if device == "cuda":
        torch.cuda.empty_cache()

    # ── Candidate (mlm-base + LoRA merged) ──────────────────────────
    print(f"\n[2/2] candidate: base={args.candidate_base} + adapter={args.candidate_adapter}")
    candidate_tok = AutoTokenizer.from_pretrained(args.candidate_base)
    base = AutoModelForSequenceClassification.from_pretrained(
        args.candidate_base, num_labels=1, ignore_mismatched_sizes=True,
    )
    # Use PeftModel.from_pretrained — handles adapter_model.safetensors and
    # all key-prefix wrangling automatically regardless of PEFT version.
    from peft import PeftModel
    candidate_model = PeftModel.from_pretrained(base, args.candidate_adapter)
    candidate_model = candidate_model.merge_and_unload()
    candidate_model.to(device).eval()
    candidate_metrics = evaluate(candidate_model, candidate_tok, rows, device)
    print(f"  {candidate_metrics}")

    delta = {k: candidate_metrics[k] - baseline_metrics[k]
             for k in baseline_metrics
             if k not in ("n_queries", "n_skipped")}
    result = {
        "baseline_model": args.baseline,
        "candidate_base": args.candidate_base,
        "candidate_adapter": args.candidate_adapter,
        "n_queries": baseline_metrics.get("n_queries"),
        "baseline": baseline_metrics,
        "candidate": candidate_metrics,
        "delta": delta,
    }
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(f"\n=== DELTA (candidate - baseline) ===")
    for k, v in delta.items():
        sign = "+" if v >= 0 else ""
        print(f"  {k}: {sign}{v:.4f}")
    print(f"\nwrote: {args.output}")


if __name__ == "__main__":
    main()
