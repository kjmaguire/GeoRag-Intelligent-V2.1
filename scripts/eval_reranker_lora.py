"""Reranker NDCG eval v4 — strip-then-prepend `base_model.model.` key prefix.

HF Trainer's save_model on the PEFT-wrapped backbone stored the state_dict
without the outer `base_model.model.` prefix. Rewrite the keys before
loading into a freshly-wrapped PeftModel, then merge_and_unload + score.
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


def score_pairs(model, tokenizer, pairs, device, batch_size=16):
    import torch
    scores = []
    model.eval()
    with torch.no_grad():
        for i in range(0, len(pairs), batch_size):
            chunk = pairs[i:i+batch_size]
            enc = tokenizer(
                [p[0] for p in chunk], [p[1] for p in chunk],
                padding=True, truncation=True, max_length=512, return_tensors="pt",
            ).to(device)
            out = model(**enc)
            logits = out.logits.squeeze(-1).float().cpu().tolist()
            scores.extend(logits if isinstance(logits, list) else [logits])
    return scores


def evaluate(model, tokenizer, rows, device):
    per_row = []
    for r in rows:
        q = r["query"]
        pos = r["positive_chunk_text"]
        negs = r.get("hard_negative_chunk_texts") or []
        candidates = [pos] + negs
        scores = score_pairs(model, tokenizer, [(q, c) for c in candidates], device)
        ranked = sorted(range(len(scores)), key=lambda i: -float(scores[i]))
        rank_of_positive = ranked.index(0) + 1
        per_row.append(metrics_for_rank(rank_of_positive))
    keys = list(per_row[0].keys())
    agg = {k: mean(r[k] for r in per_row) for k in keys}
    agg["n_queries"] = len(per_row)
    return agg


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", default="BAAI/bge-reranker-base")
    p.add_argument("--candidate-checkpoint", required=True)
    p.add_argument("--test", required=True)
    p.add_argument("--output", default="/tmp/reranker-train/eval_results.json")
    args = p.parse_args()

    os.environ.setdefault("LOG_LEVEL", "INFO")
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    from peft import LoraConfig, TaskType, get_peft_model
    from safetensors.torch import load_file

    rows = load_test(args.test)
    print(f"loaded {len(rows)} test rows")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(args.baseline)

    print(f"\n[1/2] baseline: {args.baseline}")
    baseline_model = AutoModelForSequenceClassification.from_pretrained(
        args.baseline, num_labels=1,
    ).to(device).eval()
    baseline_metrics = evaluate(baseline_model, tokenizer, rows, device)
    print(f"  {baseline_metrics}")
    del baseline_model
    torch.cuda.empty_cache()

    print(f"\n[2/2] candidate (key-prefix fix + merge): {args.candidate_checkpoint}")
    base = AutoModelForSequenceClassification.from_pretrained(args.baseline, num_labels=1)
    lora_config = LoraConfig(
        task_type=TaskType.SEQ_CLS, r=16, lora_alpha=32, lora_dropout=0.05,
        target_modules=["query", "value"], bias="none",
    )
    candidate_model = get_peft_model(base, lora_config)

    # Saved state_dict keys lack the `base_model.model.` outer prefix that
    # PeftModel uses internally. Prepend it. The PEFT-format inner names
    # (base_layer, lora_A.default, lora_B.default, modules_to_save) already
    # match — only the outer wrapper prefix is missing.
    raw_state = load_file(args.candidate_checkpoint, device=device)
    fixed_state = {f"base_model.model.{k}": v for k, v in raw_state.items()}
    missing, unexpected = candidate_model.load_state_dict(fixed_state, strict=False)
    print(f"  load_state_dict missing={len(missing)} unexpected={len(unexpected)}")
    if missing[:3]:
        print(f"  (first missing) {missing[:3]}")
    if unexpected[:3]:
        print(f"  (first unexpected) {unexpected[:3]}")

    candidate_model = candidate_model.merge_and_unload()
    candidate_model.to(device).eval()
    candidate_metrics = evaluate(candidate_model, tokenizer, rows, device)
    print(f"  {candidate_metrics}")

    delta = {k: candidate_metrics[k] - baseline_metrics[k]
             for k in baseline_metrics if k != "n_queries"}
    result = {
        "baseline_model": args.baseline,
        "candidate_checkpoint": args.candidate_checkpoint,
        "n_queries": len(rows),
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
