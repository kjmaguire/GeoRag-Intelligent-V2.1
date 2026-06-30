"""NDCG@10 harness for retrieval-quality evaluation (Eval 15 R3 follow-up).

Per the Eval 15 rubric we need a measurement that lets us tell whether
a retrieval change (chunk size, reranker, query expansion, …) actually
improved relevance, or just shuffled scores.

NDCG@10 = (DCG@10 of returned ranking) / (IDCG@10 of ideal ranking).
DCG = Σ rel_i / log2(i + 1)  where i ∈ 1..10.

This module is the offline scoring step. The golden-question set
defined in `tests/test_golden_queries.py` carries `expected_citation_*`
fields; we score the actual returned citations against those expecteds
and write a single NDCG@10 number per query plus a fleet mean.

Usage
-----
    python -m app.services.eval.ndcg_harness \
        --output /tmp/ndcg-results.json \
        --baseline /path/to/previous-ndcg-results.json

The optional `--baseline` argument computes the delta — useful for
PR-comment automation that catches retrieval regressions before merge.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class QueryRun:
    """One scored query — the inputs the scorer needs."""

    query_id: str
    expected_substrings: list[str]
    returned_citation_ids: list[str]
    returned_passage_texts: list[str]
    expected_citation_ids: list[str] = field(default_factory=list)


def dcg_at_k(relevances: list[float], k: int = 10) -> float:
    """Discounted cumulative gain over the top-k relevance scores.

    Standard log2(i+1) discount with i starting at 1.
    """
    return sum(
        rel / math.log2(i + 2)  # i=0 → log2(2)=1, i=1 → log2(3)≈1.585, …
        for i, rel in enumerate(relevances[:k])
    )


def ndcg_at_k(relevances: list[float], k: int = 10) -> float:
    """NDCG@k. relevances[i] is the graded relevance of position i."""
    actual = dcg_at_k(relevances, k)
    ideal = dcg_at_k(sorted(relevances, reverse=True), k)
    return actual / ideal if ideal > 0 else 0.0


def score_query(run: QueryRun, k: int = 10) -> float:
    """Score one query.

    Relevance grading:
      - 3 — returned citation_id is in expected_citation_ids (exact match)
      - 2 — returned passage text contains any expected_substring
      - 1 — returned passage text loosely contains any expected substring
            (substring match after lowercasing)
      - 0 — no signal

    The two-tier substring grading rewards verbatim matches over
    case-folded matches, which empirically separates well-grounded
    answers from "the model paraphrased the substring."
    """
    relevances: list[float] = []
    expected_ids = set(run.expected_citation_ids or [])
    expected_subs = [s for s in run.expected_substrings if s]
    expected_subs_lower = [s.lower() for s in expected_subs]

    for cid, ptext in zip(
        run.returned_citation_ids[:k],
        run.returned_passage_texts[:k],
        strict=False,
    ):
        if cid in expected_ids:
            relevances.append(3.0)
            continue
        if any(s in ptext for s in expected_subs):
            relevances.append(2.0)
            continue
        ptext_low = (ptext or "").lower()
        if any(s in ptext_low for s in expected_subs_lower):
            relevances.append(1.0)
            continue
        relevances.append(0.0)

    return ndcg_at_k(relevances, k)


async def run_harness(output_path: Path) -> dict[str, Any]:
    """Execute every golden query, score it, write JSON.

    Pulls the golden-query definitions from
    `tests.test_golden_queries.GOLDEN_QUERIES`. Submits each to the
    live FastAPI endpoint, parses the SSE response, and computes the
    NDCG@10 against the expected substrings.
    """
    # Lazy import so this script can be imported (and unit-tested at
    # the math layer) without bringing in pytest.
    import httpx  # noqa: PLC0415

    from tests.conftest import (  # noqa: PLC0415
        AUTH_HEADERS,
        FASTAPI_URL,
        TEST_PROJECT_ID,
        parse_sse_stream,
    )
    from tests.test_golden_queries import GOLDEN_QUERIES  # noqa: PLC0415

    per_query: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=120.0) as client:
        for case in GOLDEN_QUERIES:
            async with client.stream(
                "POST",
                f"{FASTAPI_URL}/internal/queries",
                headers=AUTH_HEADERS,
                json={
                    "query": case["query"],
                    "project_id": case.get("project_id", TEST_PROJECT_ID),
                },
            ) as response:
                if response.status_code != 200:
                    per_query.append({
                        "id": case["id"],
                        "ndcg_at_10": 0.0,
                        "http_status": response.status_code,
                    })
                    continue
                completed = await parse_sse_stream(response)

            citations = completed.get("citations") or []
            run = QueryRun(
                query_id=case["id"],
                expected_substrings=case.get("expected_answer_contains") or [],
                returned_citation_ids=[
                    str(c.get("citation_id") or c.get("source_chunk_id") or "")
                    for c in citations
                ],
                returned_passage_texts=[
                    str(c.get("snippet") or c.get("passage_text") or "")
                    for c in citations
                ],
            )
            ndcg = score_query(run)
            per_query.append({
                "id": case["id"],
                "ndcg_at_10": ndcg,
                "citations": len(citations),
            })

    fleet_mean = (
        sum(q["ndcg_at_10"] for q in per_query) / len(per_query)
        if per_query else 0.0
    )

    summary = {
        "ndcg_at_10_mean": fleet_mean,
        "queries": per_query,
        "queries_total": len(per_query),
    }
    output_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


def compare_to_baseline(
    baseline_path: Path, current: dict[str, Any]
) -> int:
    """Compare current run to a baseline, exit 1 on regression > 2%."""
    baseline = json.loads(baseline_path.read_text())
    baseline_mean = float(baseline.get("ndcg_at_10_mean", 0.0))
    current_mean = float(current.get("ndcg_at_10_mean", 0.0))
    delta = current_mean - baseline_mean
    print(
        f"baseline NDCG@10 = {baseline_mean:.4f}\n"
        f"current  NDCG@10 = {current_mean:.4f}\n"
        f"delta            = {delta:+.4f}"
    )
    return 1 if delta < -0.02 else 0


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--baseline", type=Path, default=None)
    args = ap.parse_args()

    summary = asyncio.run(run_harness(args.output))
    if args.baseline:
        return compare_to_baseline(args.baseline, summary)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
