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
from datetime import datetime, timezone
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


def _ndcg(relevances: list[float], k: int) -> float:
    actual = _dcg(relevances, k)
    ideal = _dcg(sorted(relevances, reverse=True), k)
    return actual / ideal if ideal > 0 else 0.0


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
    from qdrant_client.models import Filter, FieldCondition, MatchValue, NamedVector

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
        score = _ndcg(relevances, args.top_k)

        latency_ms = int((time.time() - q_start) * 1000)
        per_query.append({
            "id": case["id"],
            "query": case["query"],
            "ndcg_at_10": round(score, 4),
            "hits_returned": len(hits),
            "hits_with_any_match": sum(1 for r in relevances if r > 0),
            "latency_ms": latency_ms,
        })
        log.info("  NDCG@10=%.4f  hits=%d  matched=%d  latency=%dms",
                 score, len(hits), per_query[-1]["hits_with_any_match"], latency_ms)

    elapsed = time.time() - t0
    mean_ndcg = sum(q["ndcg_at_10"] for q in per_query) / max(len(per_query), 1)

    # ── Build report ─────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
            "ndcg_at_10_mean": round(mean_ndcg, 4),
            "queries_with_any_hit": sum(1 for q in per_query if q["hits_with_any_match"] > 0),
            "queries_zero_hit": sum(1 for q in per_query if q["hits_with_any_match"] == 0),
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
    print(f"  NDCG@10 mean: {mean_ndcg:.4f}  ({len(per_query)} queries)")
    print(f"  Label:        {args.label}")
    print(f"  Collection:   {total_points:,} points")
    if "comparison" in report:
        c = report["comparison"]
        print(f"  vs baseline:  {c['baseline_ndcg']:.4f} → {c['current_ndcg']:.4f}"
              f"  ({c['delta_pct']:+.1f}%)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
