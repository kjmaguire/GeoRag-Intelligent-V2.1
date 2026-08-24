"""Retrieval-only NDCG@10 benchmark — measure contextual-retrieval improvement.

Bypass the LLM entirely: embed each golden query with bge-small, search
Qdrant georag_chunks, score the returned passages against known-relevant
substrings, report NDCG@10.

Run this BEFORE re-embedding (plain-text vectors) and AFTER (enriched
vectors) to quantify the contextual-retrieval lift.

Usage (inside georag-fastapi or georag-hatchet-worker-ai container):
    python3 /app/scripts/bench_retrieval_ndcg.py
    python3 /app/scripts/bench_retrieval_ndcg.py --label post-contextual-retrieval
    python3 /app/scripts/bench_retrieval_ndcg.py --baseline bench_results/pre-*.json

Options via CLI:
    --label TEXT       Human label for this run (default: pre-contextual-retrieval)
    --top-k INT        Number of Qdrant results to score (default: 10)
    --workspace-id     Workspace UUID to filter Qdrant results (default: from env)
    --baseline PATH    Prior run JSON to diff against
    --output-dir PATH  Where to write bench_results/ (default: /app/bench_results)
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("georag.bench_ndcg")

QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_COLLECTION = "georag_chunks"

# ---------------------------------------------------------------------------
# Graded golden queries (id, query, expected substrings at least one of which
# a relevant retrieved passage should contain).  Drawn from test_golden_queries.py.
# ---------------------------------------------------------------------------

GOLDEN_BENCH_QUERIES: list[dict] = [
    {
        "id": "gq-001-count-holes",
        "query": "How many drill holes are in this project?",
        "expected_substrings": ["20", "twenty"],
    },
    {
        "id": "gq-002-deepest-hole",
        "query": "What is the deepest drillhole in the project?",
        "expected_substrings": ["PLS-22-08", "510"],
    },
    {
        "id": "gq-003-shallowest-hole",
        "query": "What is the shallowest drillhole in the project?",
        "expected_substrings": ["PLS-21-06", "265"],
    },
    {
        "id": "gq-004-diamond-drill",
        "query": "What drill type was used for the holes?",
        "expected_substrings": ["diamond", "Diamond", "DDH"],
    },
    {
        "id": "gq-005-hole-status",
        "query": "Which drillholes are completed and which are in progress?",
        "expected_substrings": ["PLS-22-10", "progress", "completed"],
    },
    {
        "id": "gq-006-drill-years",
        "query": "What years were the drillholes drilled?",
        "expected_substrings": ["2020", "2021", "2022"],
    },
    {
        "id": "gq-007-assay-grade",
        "query": "What was the top assay grade in PLS-22-08?",
        "expected_substrings": ["PLS-22-08"],
    },
    {
        "id": "gq-008-uranium-grade",
        "query": "What are the highest uranium grades in the project?",
        "expected_substrings": ["U3O8", "uranium", "grade"],
    },
    {
        "id": "gq-009-geology",
        "query": "Describe the geological setting of this uranium property",
        "expected_substrings": ["Athabasca", "uranium", "unconformity", "basement"],
    },
    {
        "id": "gq-010-mineralisation",
        "query": "What is the mineralisation style at this project?",
        "expected_substrings": ["uranium", "mineralisation", "mineralization"],
    },
    {
        "id": "gq-011-easting",
        "query": "What is the easternmost drillhole location?",
        "expected_substrings": ["PLS-22-10", "498256", "easting"],
    },
    {
        "id": "gq-012-lithology",
        "query": "What rock types were logged in the drillholes?",
        "expected_substrings": ["sandstone", "basement", "lithology", "granite"],
    },
    {
        "id": "gq-013-alteration",
        "query": "What alteration types are present near mineralisation?",
        "expected_substrings": ["alteration", "clay", "illite", "chlorite"],
    },
    {
        "id": "gq-014-report-sections",
        "query": "What does the NI 43-101 technical report cover?",
        "expected_substrings": ["43-101", "technical", "mineral"],
    },
    {
        "id": "gq-015-qualified-person",
        "query": "Who is the qualified person for the NI 43-101 report?",
        "expected_substrings": ["qualified person", "P.Geo", "QP"],
    },
]


# ---------------------------------------------------------------------------
# NDCG math
# ---------------------------------------------------------------------------

def _dcg(relevances: list[float], k: int) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(relevances[:k]))


def _self_normalised_dcg(relevances: list[float], k: int) -> float:
    """DCG of the retrieved list over the DCG of its own best ordering.

    THIS IS NOT nDCG, and the distinction is the whole finding. Textbook
    nDCG divides by the ideal DCG over the full JUDGED POOL — every passage
    a human or a strong model marked relevant for this query. This divides
    by the ideal ordering of the ten passages that were actually returned,
    so it can only ever say "were the returned results in a good order",
    never "were the right results returned".

    The failure mode is not hypothetical, and it is wider than "ties score
    1.0". Because the denominator is the sorted copy of the same vector,
    ANY non-increasing grade vector is already its own ideal ordering and
    scores exactly 1.0. Replayed against the shipped function:

        [3,3,3,3,3,3,3,3,3,3]  -> 1.0   ten uniform keyword matches
        [3,3,3,2,2,0,0,0,0,0]  -> 1.0   five matched, well ordered
        [3,0,0,0,0,0,0,0,0,0]  -> 1.0   ONE matched, nine empty

    A result with one hit out of ten is indistinguishable from a result with
    ten. That is why `_precision_at_k` is reported alongside: it separates
    those same three at 1.0 / 0.5 / 0.1.

    gq-008-uranium-grade is the concrete case for the tie form: it expects
    ['U3O8', 'uranium', 'grade'], and in a uranium-project corpus nearly
    every passage contains the word "uranium", so `_grade` returns 3.0 for
    essentially any result and the query scores a perfect 1.0 for ten
    irrelevant passages. Conversely a total miss grades all-zero, ideal is
    0, and this returns 0.0. So the committed 0.601 headline mostly reflects
    how often a keyword happens to appear, not whether retrieval found the
    right passage.

    Kept, renamed, and reported alongside `_mrr` and `_precision_at_k`,
    which do move when retrieval degrades. Restoring real nDCG needs a
    judged pool that does not exist yet; until it does, the honest move is
    to stop calling this nDCG in anything a reader will quote.
    """
    actual = _dcg(relevances, k)
    ideal = _dcg(sorted(relevances, reverse=True), k)
    return actual / ideal if ideal > 0 else 0.0


def _mrr(relevances: list[float]) -> float:
    """Reciprocal rank of the first matching passage; 0.0 if none matched.

    Unlike the self-normalised DCG above, this is sensitive to a miss: a
    query whose first hit is at rank 5 scores 0.2, and one with no hit at
    all scores 0.0. It needs no judged pool because it asks about position,
    not completeness.
    """
    for i, r in enumerate(relevances):
        if r > 0:
            return 1.0 / (i + 1)
    return 0.0


def _precision_at_k(relevances: list[float], k: int) -> float:
    """Fraction of the returned top-k that matched at all.

    Recall@k is deliberately absent: it needs the size of the full relevant
    set, which is exactly the judged pool this bench does not have. Claiming
    a recall number off a substring proxy would repeat the error this fix
    exists to correct.
    """
    window = relevances[:k]
    if not window:
        return 0.0
    return sum(1 for r in window if r > 0) / len(window)


def _is_degenerate(relevances: list[float]) -> bool:
    """True when every returned passage graded identically and non-zero.

    That is the case where the self-normalised DCG is forced to 1.0
    regardless of whether anything relevant was found.
    """
    return bool(relevances) and len(set(relevances)) == 1 and relevances[0] > 0


def _grade(text: str, substrings: list[str]) -> float:
    """Score a passage against expected substrings.

    3 — exact case-sensitive match
    2 — case-insensitive match
    0 — no match
    """
    if not substrings:
        return 0.0
    for s in substrings:
        if s in text:
            return 3.0
    tlow = text.lower()
    for s in substrings:
        if s.lower() in tlow:
            return 2.0
    return 0.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Retrieval NDCG@10 benchmark")
    parser.add_argument("--label", default="pre-contextual-retrieval")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--workspace-id", default=os.environ.get("DEFAULT_WORKSPACE_ID", ""))
    parser.add_argument("--baseline", default="")
    parser.add_argument("--output-dir", default="/app/bench_results")
    # CI eval gate (audit 2026-06-29): exit non-zero when NDCG@10 regresses
    # below the --baseline by more than this absolute delta. Lets a CI job run
    #   bench_retrieval_ndcg.py --baseline bench_results/retrieval_ndcg_baseline.json \
    #     --gate-regression 0.03
    # and FAIL a retrieval-touching PR that drops quality. No-op without --baseline.
    parser.add_argument("--gate-regression", type=float, default=None)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Load embedding model ─────────────────────────────────────────────
    log.info("Loading bge-small embedding model...")
    import torch
    from sentence_transformers import SentenceTransformer

    from app.config import settings

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("Embedding device: %s", device)
    model = SentenceTransformer(settings.EMBEDDING_MODEL_NAME, device=device)

    # ── Connect to Qdrant ────────────────────────────────────────────────
    from qdrant_client import QdrantClient
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    qc = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

    # Sanity-check collection exists
    collections = [c.name for c in qc.get_collections().collections]
    if QDRANT_COLLECTION not in collections:
        log.error("Collection %s not found in Qdrant. Available: %s",
                  QDRANT_COLLECTION, collections)
        sys.exit(1)

    collection_info = qc.get_collection(QDRANT_COLLECTION)
    total_points = collection_info.points_count
    log.info("Qdrant collection %s: %d points", QDRANT_COLLECTION, total_points)

    # Optional workspace filter
    qdrant_filter = None
    if args.workspace_id:
        qdrant_filter = Filter(
            must=[FieldCondition(
                key="workspace_id",
                match=MatchValue(value=args.workspace_id),
            )]
        )
        log.info("Applying workspace_id filter: %s", args.workspace_id)

    # ── Run bench ────────────────────────────────────────────────────────
    per_query = []
    t0 = time.time()

    for case in GOLDEN_BENCH_QUERIES:
        q_start = time.time()
        log.info("Querying: %s", case["id"])

        # Embed query
        dense = model.encode(
            [case["query"]], normalize_embeddings=True, show_progress_bar=False,
        ).tolist()[0]

        # Search Qdrant — qdrant-client ≥1.10 uses query_points instead of search.
        # The dense vector is stored under the "" (empty string) named vector key.
        result = qc.query_points(
            collection_name=QDRANT_COLLECTION,
            query=dense,
            using="",       # "" is the dense vector name in georag_chunks
            limit=args.top_k,
            query_filter=qdrant_filter,
            with_payload=True,
        )
        hits = result.points

        # Score
        passage_texts = [
            str(h.payload.get("text") or h.payload.get("passage_text") or "")
            for h in hits
        ]
        relevances = [_grade(t, case["expected_substrings"]) for t in passage_texts]
        score = _self_normalised_dcg(relevances, args.top_k)

        latency_ms = int((time.time() - q_start) * 1000)
        per_query.append({
            "id": case["id"],
            "query": case["query"],
            "ndcg_at_10": round(score, 4),
            "self_normalised_dcg": round(score, 4),
            "mrr": round(_mrr(relevances), 4),
            "precision_at_10": round(_precision_at_k(relevances, args.top_k), 4),
            "degenerate_uniform_grades": _is_degenerate(relevances),
            "hits_returned": len(hits),
            "hits_with_any_match": sum(1 for r in relevances if r > 0),
            "latency_ms": latency_ms,
        })
        log.info("  selfDCG=%.4f  MRR=%.4f  P@10=%.4f  hits=%d  matched=%d  latency=%dms",
                 score, per_query[-1]["mrr"], per_query[-1]["precision_at_10"],
                 len(hits), per_query[-1]["hits_with_any_match"], latency_ms)

    elapsed = time.time() - t0
    n_q = max(len(per_query), 1)
    mean_ndcg = sum(q["ndcg_at_10"] for q in per_query) / n_q
    mean_mrr = sum(q["mrr"] for q in per_query) / n_q
    mean_p_at_k = sum(q["precision_at_10"] for q in per_query) / n_q
    degenerate = sum(1 for q in per_query if q["degenerate_uniform_grades"])

    # ── Build report ─────────────────────────────────────────────────────
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "meta": {
            "label": args.label,
            "timestamp": ts,
            "total_points_in_collection": total_points,
            "queries_run": len(per_query),
            "top_k": args.top_k,
            "elapsed_s": round(elapsed, 1),
        },
        "summary": {
            # Key retained for backward compatibility with committed
            # baselines. It is NOT nDCG — see _self_normalised_dcg.
            "ndcg_at_10_mean": round(mean_ndcg, 4),
            "self_normalised_dcg_mean": round(mean_ndcg, 4),
            "mrr_mean": round(mean_mrr, 4),
            "precision_at_10_mean": round(mean_p_at_k, 4),
            "degenerate_query_count": degenerate,
            "queries_with_any_hit": sum(1 for q in per_query if q["hits_with_any_match"] > 0),
            "queries_zero_hit": sum(1 for q in per_query if q["hits_with_any_match"] == 0),
            "metric_caveats": [
                "ndcg_at_10_mean is a SELF-NORMALISED DCG: the ideal ranking "
                "is taken from the retrieved slice, not from a judged pool, "
                "so it scores ordering within the results and cannot detect "
                "a retrieval miss. Do not quote it as retrieval quality.",
                "Relevance is a keyword-substring proxy (3.0 case-sensitive, "
                "2.0 case-insensitive, else 0.0), not a human judgement.",
                f"{degenerate} of {len(per_query)} queries returned uniform "
                "non-zero grades, which forces the self-normalised DCG to "
                "exactly 1.0 for those queries whatever was retrieved.",
                "recall@k is deliberately not reported: it requires the size "
                "of the full relevant set, which needs a judged pool this "
                "bench does not have. mrr_mean and precision_at_10_mean are "
                "the metrics here that move when retrieval degrades.",
            ],
        },
        "per_query": per_query,
    }

    # Baseline comparison
    if args.baseline:
        baseline_path = Path(args.baseline)
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text())
            baseline_mean = baseline.get("summary", {}).get("ndcg_at_10_mean", 0.0)
            delta = mean_ndcg - baseline_mean
            report["comparison"] = {
                "baseline_label": baseline.get("meta", {}).get("label", "unknown"),
                "baseline_ndcg": baseline_mean,
                "current_ndcg": round(mean_ndcg, 4),
                "delta": round(delta, 4),
                "delta_pct": round(100 * delta / max(baseline_mean, 0.001), 1),
            }
            log.info("vs baseline '%s': %.4f → %.4f  (%+.1f%%)",
                     report["comparison"]["baseline_label"],
                     baseline_mean, mean_ndcg, report["comparison"]["delta_pct"])
        else:
            log.warning("Baseline file not found: %s", baseline_path)

    # Save
    out_file = output_dir / f"{ts}_{args.label}.json"
    out_file.write_text(json.dumps(report, indent=2))
    log.info("Report saved: %s", out_file)

    # Print summary
    print(f"\n{'='*60}")
    print(f"  self-norm DCG: {mean_ndcg:.4f}  ({len(per_query)} queries)")
    print("    ^ ordering within the retrieved list — NOT nDCG, cannot see a miss")
    print(f"  MRR:           {mean_mrr:.4f}")
    print(f"  P@{args.top_k}:          {mean_p_at_k:.4f}")
    print(f"  zero-hit:      {report['summary']['queries_zero_hit']}/{len(per_query)} queries")
    if degenerate:
        print(f"  DEGENERATE:    {degenerate} query(s) scored a forced 1.0 "
              f"(all grades equal)")
    print(f"  Label:        {args.label}")
    print(f"  Collection:   {total_points:,} points")
    if "comparison" in report:
        c = report["comparison"]
        print(f"  vs baseline:  {c['baseline_ndcg']:.4f} → {c['current_ndcg']:.4f}"
              f"  ({c['delta_pct']:+.1f}%)")
    print(f"{'='*60}\n")

    # CI eval gate — fail the job on a real NDCG regression vs baseline.
    if args.gate_regression is not None:
        if "comparison" not in report:
            log.error("--gate-regression requires a valid --baseline file; none loaded.")
            sys.exit(3)
        # Gate on MRR as well as the self-normalised DCG. Gating on the
        # latter alone is close to unfalsifiable: a retriever that returns
        # ten uniformly keyword-matching but irrelevant passages scores a
        # forced 1.0, so quality can collapse without the delta moving. MRR
        # drops the moment the first real hit slides down the list, and goes
        # to 0.0 when there is no hit at all.
        delta = report["comparison"]["delta"]
        baseline_mrr = baseline.get("summary", {}).get("mrr_mean")
        mrr_delta = (
            mean_mrr - baseline_mrr if baseline_mrr is not None else None
        )
        report["comparison"]["baseline_mrr"] = baseline_mrr
        report["comparison"]["mrr_delta"] = (
            round(mrr_delta, 4) if mrr_delta is not None else None
        )

        failures = []
        if delta < -abs(args.gate_regression):
            failures.append(
                f"self-normalised DCG regressed {delta:.4f} "
                f"(baseline {baseline_mean:.4f} -> {mean_ndcg:.4f})",
            )
        if mrr_delta is not None and mrr_delta < -abs(args.gate_regression):
            failures.append(
                f"MRR regressed {mrr_delta:.4f} "
                f"(baseline {baseline_mrr:.4f} -> {mean_mrr:.4f})",
            )
        if baseline_mrr is None:
            log.warning(
                "Baseline has no mrr_mean (written before 2026-08-21), so "
                "only the self-normalised DCG is gated — which a uniformly "
                "keyword-matching result can hold at 1.0. Regenerate the "
                "baseline to gate on MRR.",
            )

        if failures:
            log.error(
                "EVAL GATE FAILED (tolerance -%.4f): %s",
                abs(args.gate_regression), "; ".join(failures),
            )
            sys.exit(2)
        log.info(
            "EVAL GATE PASSED: DCG delta %.4f, MRR delta %s, within -%.4f.",
            delta,
            f"{mrr_delta:.4f}" if mrr_delta is not None else "n/a",
            abs(args.gate_regression),
        )


if __name__ == "__main__":
    main()
