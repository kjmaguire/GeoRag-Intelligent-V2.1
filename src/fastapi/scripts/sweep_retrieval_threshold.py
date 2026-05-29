"""Sweep retrieval thresholds over the golden set to pick an F1-maximising
config (R8 / B3 + R8-follow-up reranker axis).

Two knobs today
---------------
1. ``RETRIEVAL_QUALITY_THRESHOLD`` (Qdrant cosine coarse-retrieval floor) —
   the original B3 target. Phase-2 sweep showed this is effectively inert
   between 0.25 and 0.60 on the current golden set; the reranker's logit
   threshold is the actual filter.
2. ``RERANKER_SCORE_THRESHOLD`` (cross-encoder logit floor) — the real
   knob. This sweep axis was the R8-follow-up surfaced in Phase 2.

What it measures
----------------
For each (query, expected_section_substring) pair and each threshold combo:
  - recall  = 1 if expected substring appears in any returned chunk's
              document_title / section_title / section_number / text, else 0
  - returned_count = len(result.chunks) after both thresholds apply
  - precision (per-case) = recall / returned_count (single-hit judgement —
              each golden case has ONE known-relevant section)
  - F1      = harmonic mean of mean precision and mean recall across cases

Output
------
  - Prints a Markdown table to stdout
  - Writes `retrieval_threshold_sweep.csv` to cwd
  - Prints the argmax-F1 combo as the recommendation

Usage
-----
  # Cosine threshold sweep only (default):
  docker exec georag-fastapi python /app/scripts/sweep_retrieval_threshold.py

  # Reranker threshold sweep only (pins cosine at current setting):
  docker exec georag-fastapi python /app/scripts/sweep_retrieval_threshold.py \\
      --thresholds 0.50 \\
      --reranker-thresholds -1.0,0.0,0.5,1.0,2.0,3.0

  # Two-axis grid sweep (cosine × reranker logit):
  docker exec georag-fastapi python /app/scripts/sweep_retrieval_threshold.py \\
      --thresholds 0.30,0.50,0.60 \\
      --reranker-thresholds 0.0,0.5,1.0,2.0

  # Project override:
  docker exec georag-fastapi python /app/scripts/sweep_retrieval_threshold.py \\
      --project 019d74a1-fba8-7165-9ae6-a5bf93eef97d

Reranker score scale
--------------------
The ms-marco-MiniLM-L-6-v2 cross-encoder outputs raw logits (unbounded
real numbers). Typical mineral-report queries produce logits in the
range roughly -6 (irrelevant) to +11 (very relevant). A threshold of 0.5
keeps anything whose sigmoid-mapped probability is ≥ 0.62. Sweep values
of [-1.0, 0.0, 0.5, 1.0, 2.0, 3.0] cover ~27% → ~95% sigmoid-probability
floors, which is the useful operating band.

Notes
-----
- Calls search_documents() directly against the app's pooled clients rather
  than going through the FastAPI HTTP layer. Avoids double-hopping through
  the orchestrator and its caching; isolates the retrieval-quality signal.
- Requires the Qdrant collection + embedding/reranker models to be loaded.
  Run inside the georag-fastapi container so app.state is populated.
- The reranker threshold is monkey-patched onto ``settings.RERANKER_SCORE_THRESHOLD``
  between iterations. search_documents reads it fresh on every call
  (tools.py:841), so no restart needed. Original value is restored on
  teardown.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import statistics
import sys
from dataclasses import dataclass
from typing import Any

# Ensure the repo root is importable when run outside `python -m`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.deps import AgentDeps, ToolContext  # noqa: E402
from app.agent.tools import search_documents  # noqa: E402
from app.config import settings  # noqa: E402


# ---------------------------------------------------------------------------
# Golden retrieval set — mirrors tests/test_retrieval_quality.py RETRIEVAL_CASES
# but tracks a per-case substring list so we catch variant surface forms.
# ---------------------------------------------------------------------------


# Retrieval-precision cases: each has expected_any_of AND negative_terms.
# Precision@k increments when the top chunk contains expected_any_of AND
# contains none of the negative_terms. Lets us catch "retrieves something
# vaguely related but about the wrong subject" — the most common silent
# retrieval failure.
RETRIEVAL_CASES: list[dict[str, Any]] = [
    {
        "id": "ret-001",
        "query": "What is the mineral resource estimate?",
        # Resource estimation lives in Section 13 / 14 of NI 43-101.
        "expected_any_of": ["Section 13", "Section 14", "resource estimate", "mineral resource"],
    },
    {
        "id": "ret-002",
        "query": "Who is the qualified person on the NI 43-101 technical report?",
        "expected_any_of": ["Sarah Thompson", "qualified person", "QP", "NI 43-101"],
    },
    {
        "id": "ret-003",
        "query": "What is the deposit type?",
        "expected_any_of": ["Section 8", "deposit type", "unconformity", "Athabasca"],
    },
    {
        "id": "ret-004",
        "query": "Describe the exploration history of the Patterson Lake South property",
        "expected_any_of": ["Section 6", "Section 9", "exploration history", "Patterson"],
    },
    {
        "id": "ret-005",
        "query": "What exploration programs does the NI 43-101 report recommend?",
        "expected_any_of": ["Section 17", "Section 26", "recommendation", "Phase 1", "Phase 2"],
    },
]


@dataclass
class CaseResult:
    case_id: str
    query: str
    cosine_threshold: float
    reranker_threshold: float
    returned_count: int
    recall: int  # 0 or 1
    top_relevance: float
    match_term: str | None


def _chunk_haystack(chunk: Any) -> str:
    """Assemble the searchable text from a chunk for substring matching."""
    parts = [
        str(getattr(chunk, "document_title", "") or ""),
        str(getattr(chunk, "section_title", "") or ""),
        str(getattr(chunk, "section_number", "") or ""),
        str((getattr(chunk, "text", "") or ""))[:2000],
    ]
    return " ".join(parts).lower()


def _recall_hit(chunks: list[Any], expected: list[str]) -> tuple[int, str | None]:
    """Did any chunk contain any expected substring? Return hit + first match."""
    for chunk in chunks:
        hay = _chunk_haystack(chunk)
        for term in expected:
            if term.lower() in hay:
                return 1, term
    return 0, None


# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------


async def _build_app_and_deps(project_id: str) -> tuple[Any, AgentDeps]:
    """Boot the FastAPI lifespan in-process so app.state pools are populated.

    We build a minimal AsyncExitStack-less wrapper: create the app, enter
    lifespan, return the deps. Caller must call the returned cleanup.
    """
    from contextlib import AsyncExitStack

    from app.main import app, lifespan

    stack = AsyncExitStack()
    await stack.enter_async_context(lifespan(app))

    deps = AgentDeps(
        pg_pool=app.state.pg_pool,
        qdrant_client=app.state.qdrant_client,
        neo4j_driver=app.state.neo4j_driver,
        project_id=project_id,
        embedding_model=app.state.embedding_model,
        reranker=getattr(app.state, "reranker", None),
        redis_client=getattr(app.state, "redis_client", None),
        anthropic_client=getattr(app.state, "anthropic_client", None),
    )
    return stack, deps


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


async def sweep(
    project_id: str,
    cosine_thresholds: list[float],
    reranker_thresholds: list[float],
) -> list[CaseResult]:
    """Two-axis grid sweep over cosine × reranker thresholds.

    When ``reranker_thresholds`` is a single-element list, it behaves as a
    pure cosine-axis sweep (prior Phase-2 behaviour). When
    ``cosine_thresholds`` is a single-element list, it's a pure reranker
    sweep. Otherwise it's a full grid.
    """
    stack, deps = await _build_app_and_deps(project_id)
    results: list[CaseResult] = []
    # Monkey-patch settings.RERANKER_SCORE_THRESHOLD between runs. search_documents
    # reads it fresh on every call, so no restart/reload needed. Capture the
    # original so teardown restores it (defensive — lifespan re-init would
    # overwrite anyway, but we don't want to leak state across sweeps in a
    # long-lived container).
    original_reranker = settings.RERANKER_SCORE_THRESHOLD
    try:
        ctx = ToolContext(deps)
        for rerank_t in reranker_thresholds:
            # Monkey-patch for this row. Pydantic BaseSettings in v2 allows
            # attribute assignment via object.__setattr__ even though the
            # model is technically frozen at class-level.
            object.__setattr__(settings, "RERANKER_SCORE_THRESHOLD", rerank_t)
            for cosine_t in cosine_thresholds:
                for case in RETRIEVAL_CASES:
                    search_result = await search_documents(
                        ctx,  # type: ignore[arg-type]
                        query_text=case["query"],
                        project_id=project_id,
                        limit=settings.RETRIEVAL_TOP_N,
                        score_threshold=cosine_t,
                    )
                    chunks = list(search_result.chunks)
                    hit, match = _recall_hit(chunks, case["expected_any_of"])
                    top = max(
                        (getattr(c, "relevance_score", 0.0) or 0.0 for c in chunks),
                        default=0.0,
                    )
                    results.append(
                        CaseResult(
                            case_id=case["id"],
                            query=case["query"],
                            cosine_threshold=cosine_t,
                            reranker_threshold=rerank_t,
                            returned_count=len(chunks),
                            recall=hit,
                            top_relevance=top,
                            match_term=match,
                        )
                    )
                    print(
                        f"  {case['id']} cos={cosine_t:.2f} rerank={rerank_t:+.2f} → "
                        f"count={len(chunks)} recall={hit} "
                        f"top_score={top:.3f} match={match!r}"
                    )
    finally:
        object.__setattr__(settings, "RERANKER_SCORE_THRESHOLD", original_reranker)
        await stack.aclose()
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _aggregate(
    results: list[CaseResult],
) -> dict[tuple[float, float], dict[str, float]]:
    """Group by (cosine, reranker) pair; compute recall/precision/F1/mean_returned."""
    by_combo: dict[tuple[float, float], list[CaseResult]] = {}
    for r in results:
        by_combo.setdefault((r.cosine_threshold, r.reranker_threshold), []).append(r)

    summary: dict[tuple[float, float], dict[str, float]] = {}
    for combo, rows in by_combo.items():
        total = len(rows)
        mean_recall = sum(r.recall for r in rows) / total if total else 0.0
        mean_returned = (
            sum(r.returned_count for r in rows) / total if total else 0.0
        )
        precisions = [
            (r.recall / r.returned_count) if r.returned_count else 0.0
            for r in rows
        ]
        mean_precision = statistics.mean(precisions) if precisions else 0.0
        f1 = (
            2 * mean_precision * mean_recall / (mean_precision + mean_recall)
            if (mean_precision + mean_recall) > 0
            else 0.0
        )
        summary[combo] = {
            "recall": mean_recall,
            "precision": mean_precision,
            "f1": f1,
            "mean_returned": mean_returned,
        }
    return summary


def _print_markdown_table(
    summary: dict[tuple[float, float], dict[str, float]],
) -> None:
    print()
    print("| cosine | reranker | recall | precision |   F1  | mean returned |")
    print("|--------|----------|--------|-----------|-------|---------------|")
    for cosine_t, rerank_t in sorted(summary):
        s = summary[(cosine_t, rerank_t)]
        print(
            f"|  {cosine_t:.2f}  |  {rerank_t:+.2f}   | "
            f"{s['recall']:.3f}  |   {s['precision']:.3f}   | "
            f"{s['f1']:.3f} |     {s['mean_returned']:5.2f}     |"
        )
    print()


def _write_csv(results: list[CaseResult], path: str) -> None:
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "case_id",
                "query",
                "cosine_threshold",
                "reranker_threshold",
                "returned_count",
                "recall",
                "top_relevance",
                "match_term",
            ],
        )
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "case_id": r.case_id,
                    "query": r.query,
                    "cosine_threshold": f"{r.cosine_threshold:.2f}",
                    "reranker_threshold": f"{r.reranker_threshold:+.2f}",
                    "returned_count": r.returned_count,
                    "recall": r.recall,
                    "top_relevance": f"{r.top_relevance:.4f}",
                    "match_term": r.match_term or "",
                }
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        default=os.environ.get(
            "SWEEP_PROJECT_ID", "019d74a1-fba8-7165-9ae6-a5bf93eef97d"
        ),
        help="Project UUID to query against (default: Lazy Edward Bay demo).",
    )
    parser.add_argument(
        "--thresholds",
        "--cosine-thresholds",
        dest="thresholds",
        default="0.25,0.30,0.40,0.50,0.60,0.70",
        help="Comma-separated cosine floors to sweep "
             "(default: 0.25,0.30,0.40,0.50,0.60,0.70). "
             "Alias: --cosine-thresholds (self-documenting alongside "
             "--reranker-thresholds).",
    )
    parser.add_argument(
        "--reranker-thresholds",
        default=None,
        help="Comma-separated reranker logit floors to sweep. "
             "Omit to pin the reranker at its current config value. "
             "Typical useful range: -1.0,0.0,0.5,1.0,2.0,3.0 (sigmoid "
             "probability equivalents: ~0.27, 0.50, 0.62, 0.73, 0.88, 0.95).",
    )
    parser.add_argument(
        "--csv",
        default="retrieval_threshold_sweep.csv",
        help="CSV output path (default: ./retrieval_threshold_sweep.csv).",
    )
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    cosine_thresholds = sorted(
        {float(t.strip()) for t in args.thresholds.split(",") if t.strip()}
    )
    if args.reranker_thresholds:
        reranker_thresholds = sorted(
            {float(t.strip()) for t in args.reranker_thresholds.split(",") if t.strip()}
        )
    else:
        reranker_thresholds = [float(settings.RERANKER_SCORE_THRESHOLD)]

    print(
        f"Sweeping cosine={cosine_thresholds} "
        f"× reranker={reranker_thresholds} on project {args.project}"
    )
    print(f"Current settings: RETRIEVAL_QUALITY_THRESHOLD={settings.RETRIEVAL_QUALITY_THRESHOLD}")
    print(f"                  RERANKER_SCORE_THRESHOLD={settings.RERANKER_SCORE_THRESHOLD}")
    print()

    results = await sweep(args.project, cosine_thresholds, reranker_thresholds)
    summary = _aggregate(results)

    _print_markdown_table(summary)
    _write_csv(results, args.csv)
    print(f"CSV written → {args.csv}")

    # Argmax F1 — recommendation. Tie-break by picking the HIGHEST threshold
    # at peak F1 (most protective against future noise).
    if summary:
        peak_f1 = max(s["f1"] for s in summary.values())
        peak_combos = [
            combo for combo, s in summary.items()
            if abs(s["f1"] - peak_f1) < 1e-9
        ]
        # Sort by (cosine desc, reranker desc) to pick the most protective combo.
        peak_combos.sort(reverse=True)
        best_combo = peak_combos[0]
        metrics = summary[best_combo]
        cosine_t, rerank_t = best_combo
        print()
        print("=" * 70)
        print(
            f"RECOMMENDATION: cosine={cosine_t:.2f} reranker={rerank_t:+.2f} "
            f"(F1={metrics['f1']:.3f}, recall={metrics['recall']:.3f}, "
            f"precision={metrics['precision']:.3f})"
        )
        cur_cos = settings.RETRIEVAL_QUALITY_THRESHOLD
        cur_rer = settings.RERANKER_SCORE_THRESHOLD
        if abs(cosine_t - cur_cos) > 1e-9:
            print(
                f"Current RETRIEVAL_QUALITY_THRESHOLD={cur_cos:.2f} "
                f"→ suggest bumping to {cosine_t:.2f}."
            )
        if abs(rerank_t - cur_rer) > 1e-9:
            print(
                f"Current RERANKER_SCORE_THRESHOLD={cur_rer:+.2f} "
                f"→ suggest bumping to {rerank_t:+.2f}."
            )
        if abs(cosine_t - cur_cos) <= 1e-9 and abs(rerank_t - cur_rer) <= 1e-9:
            print("Current thresholds already match the recommendation.")
        if len(peak_combos) > 1:
            print(
                f"\n(Tied at peak F1 with {len(peak_combos)} combos — "
                f"picked the most protective; see CSV for full set.)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
