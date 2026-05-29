"""Reranker NDCG eval for FULL fine-tunes (not LoRA).

Variant of scripts/eval_reranker_lora.py that loads the candidate as a
regular HuggingFace AutoModelForSequenceClassification directory instead
of a single-file LoRA adapter. The baseline still loads from a HF model
id (defaults to BAAI/bge-reranker-base).

Outputs the same JSON shape so the bench manifest is interchangeable.
"""
from __future__ import annotations
import argparse, json, math, os
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
    p.add_argument("--candidate-checkpoint", required=True,
                   help="Path to a HF model directory produced by full FT.")
    p.add_argument("--test", required=True)
    p.add_argument("--output", default="/tmp/reranker-bench.json")
    args = p.parse_args()

    os.environ.setdefault("LOG_LEVEL", "INFO")
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    rows = load_test(args.test)
    print(f"loaded {len(rows)} test rows")
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # Baseline — stock reranker, baseline-vocab tokenizer
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

    # Candidate — extended-vocab tokenizer travels with the FT checkpoint
    print(f"\n[2/2] candidate: {args.candidate_checkpoint}")
    candidate_tok = AutoTokenizer.from_pretrained(args.candidate_checkpoint)
    candidate_model = AutoModelForSequenceClassification.from_pretrained(
        args.candidate_checkpoint, num_labels=1,
    ).to(device).eval()
    candidate_metrics = evaluate(candidate_model, candidate_tok, rows, device)
    print(f"  {candidate_metrics}")

    delta = {k: candidate_metrics[k] - baseline_metrics[k]
             for k in baseline_metrics
             if k not in ("n_queries", "n_skipped")}
    result = {
        "baseline_model": args.baseline,
        "candidate_checkpoint": args.candidate_checkpoint,
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
