"""ADR-0008 Option D — bi-encoder eval for the domain fine-tuned bge-small.

Bi-encoder analogue of scripts/_eval_reranker_full.py. Loads the candidate
SentenceTransformer model produced by scripts/_train_bge_small_finetune.py
and the stock BAAI/bge-small-en-v1.5 baseline, encodes (query, positive,
hard_negatives[]) groups via a normalized dot-product (== cosine since we
normalize) bi-encoder score, then reports NDCG@10 / MRR / Recall@{1,5,10}
with the same JSON shape as the reranker bench.

This script implements the same evaluation contract as
scripts/_eval_reranker_full.py:

  * Reads JSONL rows shaped {query, positive_chunk_text, hard_negative_chunk_texts[]}
  * For each row: ranks the (1 positive + N hard negatives) candidate list
  * Aggregates NDCG@10, MRR, Recall@{1,5,10}
  * Writes JSON to --output identical in shape to the reranker bench
  * Computes candidate - baseline delta and prints it at the end

WHY A NEW SCRIPT (vs reusing _eval_reranker_full.py)
====================================================
_eval_reranker_full.py drives a cross-encoder
(AutoModelForSequenceClassification with num_labels=1 that takes
(query, passage) pairs and emits one logit). bge-small is a bi-encoder:
the model emits one vector per text; query-document score is the
dot-product of normalized embeddings. Different forward pass, different
tokenizer call shape, different scoring contract.

Same eval HARNESS contract though — metrics formula, JSON output shape,
delta printout — so reranker and embedding cycles share dashboard glue
downstream.

USAGE (when Kyle greenlights actual eval — DO NOT RUN now)
==========================================================

    docker exec georag-fastapi python /app/scripts/_eval_bge_small.py \\
        --candidate /tmp/bge-small-domain-ft \\
        --test /tmp/reranker-train-combined/test.jsonl \\
        --output /tmp/bge-small-bench.json

    # baseline only (sanity check the harness):
    docker exec georag-fastapi python /app/scripts/_eval_bge_small.py \\
        --candidate BAAI/bge-small-en-v1.5 \\
        --test /tmp/reranker-train-combined/test.jsonl

LIKELY HOLD CONTEXT
===================
The test split at /tmp/reranker-train-combined/test.jsonl is the same
5,143-row OOD bench that hosted §38 + §39's HOLD verdicts. Promote
the candidate only if it beats stock on NDCG@10 AND MRR AND
recall_at_10 (consistent with reranker promotion threshold). A
narrow win on a small in-distribution subset is the §39 failure mode
and should NOT be treated as a green light.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from statistics import mean
from typing import Any


def load_test(path: str) -> list[dict[str, Any]]:
    return [json.loads(l) for l in open(path) if l.strip()]


def metrics_for_rank(rank_of_positive: int, k_list=(1, 5, 10)) -> dict[str, float]:
    """Identical to _eval_reranker_full.py — shared metric definition."""
    out: dict[str, float] = {}
    out["ndcg_at_10"] = (
        1.0 / math.log2(rank_of_positive + 1) if rank_of_positive <= 10 else 0.0
    )
    out["mrr"] = 1.0 / rank_of_positive
    for k in k_list:
        out[f"recall_at_{k}"] = 1.0 if rank_of_positive <= k else 0.0
    return out


def _score_row(model, query: str, candidates: list[str], device: str,
               batch_size: int = 64, max_length: int = 512) -> list[float]:
    """Bi-encoder score: dot-product of normalized embeddings.

    Since both query and candidate vectors are L2-normalized (stock
    bge-small + our Stage B Normalize module), dot-product equals cosine
    similarity. Higher == more relevant.
    """
    # SentenceTransformer.encode handles batching, padding, truncation,
    # device placement, and normalization (we pass normalize_embeddings=True
    # explicitly to be safe for the stock-model branch — domain-FT model
    # already has a Normalize module in its pipeline but the kwarg is a
    # no-op there).
    q_vec = model.encode(
        [query], batch_size=1, convert_to_tensor=True, device=device,
        normalize_embeddings=True, show_progress_bar=False,
    )
    c_vec = model.encode(
        candidates, batch_size=batch_size, convert_to_tensor=True,
        device=device, normalize_embeddings=True, show_progress_bar=False,
    )
    # cosine == dot for normalized vectors
    import torch  # noqa: PLC0415
    with torch.no_grad():
        sims = torch.matmul(q_vec, c_vec.T).squeeze(0)
        scores = sims.float().cpu().tolist()
    if isinstance(scores, float):
        return [scores]
    return list(scores)


def evaluate(model, rows: list[dict[str, Any]], device: str,
             batch_size: int = 64, max_length: int = 512) -> dict[str, float]:
    per_row: list[dict[str, float]] = []
    skipped = 0
    for r in rows:
        q = r.get("query")
        pos = r.get("positive_chunk_text")
        negs = r.get("hard_negative_chunk_texts") or []
        if not q or not pos or not negs:
            skipped += 1
            continue
        candidates = [pos] + [n for n in negs if isinstance(n, str) and n]
        if len(candidates) < 2:
            skipped += 1
            continue
        scores = _score_row(model, q, candidates, device,
                            batch_size=batch_size, max_length=max_length)
        ranked = sorted(range(len(scores)), key=lambda i: -float(scores[i]))
        rank_of_positive = ranked.index(0) + 1
        per_row.append(metrics_for_rank(rank_of_positive))
    if not per_row:
        return {"n_queries": 0, "n_skipped": skipped}
    keys = list(per_row[0].keys())
    agg: dict[str, float] = {k: mean(r[k] for r in per_row) for k in keys}
    agg["n_queries"] = len(per_row)
    agg["n_skipped"] = skipped
    return agg


def _load_model(path_or_id: str, device: str, max_seq_length: int):
    """Load a SentenceTransformer model from either an HF id or a local dir."""
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    model = SentenceTransformer(path_or_id, device=device)
    # Force the same max seq length on both candidate + baseline so the
    # truncation contract matches across the bench.
    try:
        model.max_seq_length = max_seq_length
    except Exception:  # noqa: BLE001
        # Older sentence-transformers versions expose this via the inner
        # transformer module only — set it there as a fallback.
        try:
            model[0].max_seq_length = max_seq_length
        except Exception:  # noqa: BLE001
            pass
    return model


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", default="BAAI/bge-small-en-v1.5",
                   help="Stock bge-small reference (HF id or local dir).")
    p.add_argument("--candidate", required=True,
                   help="Path to a SentenceTransformer model dir produced by "
                        "scripts/_train_bge_small_finetune.py — or another "
                        "HF id for sanity checks.")
    p.add_argument("--test", default="/tmp/reranker-train-combined/test.jsonl",
                   help="JSONL test split (default: same 5,143-row OOD bench "
                        "as the reranker HOLD verdicts in §38 / §39).")
    p.add_argument("--output", default="/tmp/bge-small-bench.json")
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--max-seq-length", type=int, default=512)
    args = p.parse_args()

    os.environ.setdefault("LOG_LEVEL", "INFO")
    import torch  # noqa: PLC0415

    rows = load_test(args.test)
    print(f"loaded {len(rows)} test rows from {args.test}")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device: {device}")

    # --- Baseline -----------------------------------------------------------
    print(f"\n[1/2] baseline: {args.baseline}")
    baseline_model = _load_model(args.baseline, device, args.max_seq_length)
    baseline_metrics = evaluate(
        baseline_model, rows, device,
        batch_size=args.batch_size, max_length=args.max_seq_length,
    )
    print(f"  {baseline_metrics}")
    del baseline_model
    if device == "cuda":
        torch.cuda.empty_cache()

    # --- Candidate ----------------------------------------------------------
    print(f"\n[2/2] candidate: {args.candidate}")
    candidate_model = _load_model(args.candidate, device, args.max_seq_length)
    candidate_metrics = evaluate(
        candidate_model, rows, device,
        batch_size=args.batch_size, max_length=args.max_seq_length,
    )
    print(f"  {candidate_metrics}")

    delta = {
        k: candidate_metrics[k] - baseline_metrics[k]
        for k in baseline_metrics
        if k not in ("n_queries", "n_skipped")
        and isinstance(baseline_metrics.get(k), (int, float))
    }
    result = {
        "adr":                  "ADR-0008 Option D",
        "baseline_model":       args.baseline,
        "candidate_model_path": args.candidate,
        "test_split":           args.test,
        "n_queries":            baseline_metrics.get("n_queries"),
        "baseline":             baseline_metrics,
        "candidate":            candidate_metrics,
        "delta":                delta,
    }
    Path(args.output).write_text(json.dumps(result, indent=2))

    print("\n=== DELTA (candidate - baseline) ===")
    for k, v in delta.items():
        sign = "+" if v >= 0 else ""
        print(f"  {k}: {sign}{v:.4f}")
    print(f"\nwrote: {args.output}")

    # Promotion-threshold reminder: ADR-0008 §Trigger conditions imply a
    # ≥5pp recall lift on the golden set; this proxy bench requires at
    # minimum non-negative deltas on NDCG@10 AND MRR AND recall_at_10
    # before any further investment. Print a one-line verdict for the log.
    verdict_ok = (
        delta.get("ndcg_at_10", -1) >= 0
        and delta.get("mrr", -1) >= 0
        and delta.get("recall_at_10", -1) >= 0
    )
    if verdict_ok:
        print("verdict: candidate non-regressing on all 3 headline metrics — "
              "promote-candidate path is OPEN (still subject to golden-set bench).")
    else:
        print("verdict: candidate REGRESSES on at least one headline metric — "
              "HOLD recommended; do NOT flip EMBEDDING_MODEL_PATH.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
