#!/usr/bin/env python3
"""Plan §5b — golden-query benchmark CLI runner.

Runs the live RAG pipeline against every active question in
``eval.golden_questions`` (or a filtered subset) and emits a JSON report
to ``bench_results/<timestamp>_<git_sha>.json`` that future runs can be
compared against to measure lift or regression.

Why a CLI, not just the Hatchet workflow:

  The existing ``eval_real_rag_nightly`` Hatchet workflow runs the same
  evaluator on a cron schedule, but it (a) only targets
  ``refusal_correctness`` by default and (b) doesn't produce a single
  artifact a developer can ``git diff``. This CLI is the ad-hoc
  operator entry point for before/after measurement around a deploy
  (flip a feature flag, run the bench, compare against the prior
  baseline) — pairs with ``scripts/compare_benchmarks.py``.

Usage::

    python scripts/run_golden_benchmark.py
    python scripts/run_golden_benchmark.py --question-set refusal_correctness
    python scripts/run_golden_benchmark.py --max-questions 20 --label pre-§5e

Output schema (one entry per question)::

    {
      "meta": {
        "timestamp": "2026-05-29T16:30:00Z",
        "git_sha": "33bb26a",
        "label": "pre-§5e-training",
        "question_count": 119,
        "question_set_filter": null,
        "max_questions": null
      },
      "summary": {
        "pass_count": 87,
        "fail_count": 32,
        "pass_rate": 0.731,
        "avg_latency_ms": 4823,
        "total_tokens": 145200,
        "failure_layers": {"refusal": 22, "citation": 7, "numeric": 3}
      },
      "results": [
        {"question_id": "...", "question_set": "...", "passed": false,
         "failure_layer": "refusal", "latency_ms": 5123, "tokens_used": 1200,
         "response_text_first_200": "..."}
      ]
    }

Best-effort: a single question failure logs + continues — the run
produces a partial report rather than aborting. Set ``--strict`` to
exit non-zero on any single failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Adjust path so the script works whether invoked from inside the
# fastapi container (where /app is the cwd) or from host with the
# fastapi source on PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from app.services.eval.real_rag_evaluator import (  # noqa: E402
    evaluate_question_real_rag,
)
from app.services.eval.workspace_evaluator import (  # noqa: E402
    QuestionRecord,
    _dsn,
    _load_active_questions,
)


log = logging.getLogger("georag.bench")


def _git_sha() -> str:
    """Short git SHA of the current HEAD.

    Resolution order:
      1. ``GEORAG_GIT_SHA`` env var — set this when running inside the
         fastapi container so the host's git can be queried and the
         result piped through (the container has no ``.git`` and no
         ``git`` binary in PATH).
      2. ``git rev-parse --short HEAD`` on the host — works for direct
         invocations from a developer's workstation.
      3. ``unknown`` — last-resort, lets the run continue without a
         label rather than hard-failing on the bench entry path.
    """
    env_sha = os.environ.get("GEORAG_GIT_SHA")
    if env_sha:
        return env_sha
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Drive the benchmark; return the assembled report dict.

    Steps:
      1. Connect to PG, load active questions (optionally filtered).
      2. For each question, invoke ``evaluate_question_real_rag`` and
         capture the result. Best-effort per question — a single failure
         logs and continues.
      3. Aggregate pass/fail/latency/tokens + failure-layer histogram.
      4. Return the report dict.
    """
    log.info("bench.start dsn=%s sha=%s label=%s",
             _dsn().replace(os.environ.get("POSTGRES_PASSWORD", "_") or "_", "*****"),
             _git_sha(),
             args.label)

    conn = await asyncpg.connect(_dsn())
    try:
        questions = await _load_active_questions(conn, args.question_set)
    finally:
        await conn.close()

    if args.max_questions and len(questions) > args.max_questions:
        log.info("bench.truncate from=%d to=%d", len(questions), args.max_questions)
        questions = questions[: args.max_questions]

    log.info("bench.questions_loaded count=%d", len(questions))

    if not questions:
        log.warning("bench.no_questions filter=%s", args.question_set)
        return _empty_report(args)

    # Per-question we need a fresh connection to satisfy the evaluator's
    # signature, but the evaluator's heavy `deps` (qdrant, neo4j, vllm)
    # are a process-level singleton built by `_get_or_build_deps` — so
    # connection churn is cheap and the model weights stay warm.
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=4)
    try:
        results: list[dict[str, Any]] = []
        t_start = time.monotonic()
        for i, q in enumerate(questions, start=1):
            log.info("bench.eval %d/%d id=%s set=%s",
                     i, len(questions), q.question_id, q.question_set)
            try:
                async with pool.acquire() as conn:
                    res = await evaluate_question_real_rag(
                        conn, q, timeout_seconds=args.per_question_timeout,
                    )
            except Exception as e:  # noqa: BLE001 — bench must keep going
                log.exception("bench.question_crashed id=%s err=%s",
                              q.question_id, e)
                results.append(_crash_entry(q, e))
                if args.strict:
                    raise
                continue

            results.append(_result_entry(q, res))

        elapsed = time.monotonic() - t_start
        log.info("bench.done wall=%.1fs questions=%d", elapsed, len(results))
    finally:
        await pool.close()

    return _assemble_report(args, results)


def _result_entry(q: QuestionRecord, res: Any) -> dict[str, Any]:
    """Shape one question result for the report.

    Captures the fields you'd want for a before/after diff: pass/fail,
    failure layer, latency, token usage, and the first 200 chars of
    the LLM response so reviewers can spot-check regressions without
    re-running the bench.
    """
    payload = res.actual_payload if isinstance(res.actual_payload, dict) else {}
    response_text = str(payload.get("response_text") or "")
    return {
        "question_id": str(q.question_id),
        "question_set": q.question_set,
        "question_text_first_120": q.question_text[:120],
        "expected_refusal": q.expected_refusal,
        "passed": bool(res.passed),
        "failure_layer": res.failure_layer,
        "failure_detail_first_200": (res.failure_detail or "")[:200],
        "latency_ms": res.latency_ms,
        "tokens_used": res.tokens_used,
        "evaluator": payload.get("evaluator"),
        "detected_refusal": payload.get("detected_refusal"),
        "response_text_first_200": response_text[:200],
    }


def _crash_entry(q: QuestionRecord, e: Exception) -> dict[str, Any]:
    """Result entry for a question that crashed (eval threw)."""
    return {
        "question_id": str(q.question_id),
        "question_set": q.question_set,
        "question_text_first_120": q.question_text[:120],
        "expected_refusal": q.expected_refusal,
        "passed": False,
        "failure_layer": "evaluator_crashed",
        "failure_detail_first_200": f"{type(e).__name__}: {e}"[:200],
        "latency_ms": None,
        "tokens_used": None,
    }


def _assemble_report(
    args: argparse.Namespace, results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the final report dict from per-question results."""
    pass_count = sum(1 for r in results if r["passed"])
    fail_count = len(results) - pass_count
    latencies = [r["latency_ms"] for r in results if r["latency_ms"] is not None]
    tokens = [r["tokens_used"] for r in results if r["tokens_used"] is not None]
    failure_layers = Counter(
        r["failure_layer"] for r in results
        if not r["passed"] and r["failure_layer"]
    )

    return {
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "git_sha": _git_sha(),
            "label": args.label,
            "question_count": len(results),
            "question_set_filter": args.question_set,
            "max_questions": args.max_questions,
            "per_question_timeout": args.per_question_timeout,
        },
        "summary": {
            "pass_count": pass_count,
            "fail_count": fail_count,
            "pass_rate": round(pass_count / len(results), 4) if results else 0.0,
            "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
            "p95_latency_ms": _p95(latencies),
            "total_tokens": sum(tokens) if tokens else 0,
            "failure_layers": dict(failure_layers.most_common()),
        },
        "results": results,
    }


def _empty_report(args: argparse.Namespace) -> dict[str, Any]:
    """Report shape when no questions matched the filter."""
    return {
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "git_sha": _git_sha(),
            "label": args.label,
            "question_count": 0,
            "question_set_filter": args.question_set,
            "max_questions": args.max_questions,
        },
        "summary": {
            "pass_count": 0, "fail_count": 0, "pass_rate": 0.0,
            "avg_latency_ms": None, "p95_latency_ms": None,
            "total_tokens": 0, "failure_layers": {},
        },
        "results": [],
    }


def _p95(values: list[int]) -> int | None:
    """p95 — simple sort-and-index. None on empty input."""
    if not values:
        return None
    sorted_v = sorted(values)
    idx = int(len(sorted_v) * 0.95)
    return sorted_v[min(idx, len(sorted_v) - 1)]


def _write_report(report: dict[str, Any], output_path: Path | None) -> Path:
    """Persist the report. Defaults to ``bench_results/<ts>_<sha>.json``."""
    if output_path is None:
        repo_root = Path(__file__).resolve().parent.parent
        out_dir = repo_root / "bench_results"
        out_dir.mkdir(exist_ok=True)
        ts = report["meta"]["timestamp"].replace(":", "-")
        sha = report["meta"]["git_sha"]
        suffix = f"_{report['meta']['label']}" if report["meta"].get("label") else ""
        output_path = out_dir / f"{ts}_{sha}{suffix}.json"
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the §5b golden-query benchmark against the live RAG stack.",
    )
    parser.add_argument(
        "--question-set",
        default=None,
        help="Filter to one question_set (e.g. refusal_correctness, "
             "numeric_grounding). Default: all active questions.",
    )
    parser.add_argument(
        "--max-questions", type=int, default=None,
        help="Cap the number of questions run. Use during development to "
             "keep iteration cheap. Default: no cap.",
    )
    parser.add_argument(
        "--per-question-timeout", type=float, default=60.0,
        help="Per-question timeout in seconds. Default: 60.",
    )
    parser.add_argument(
        "--label", default=None,
        help="Free-text label embedded in meta + the output filename. Use "
             "to mark baselines (--label pre-§5e) so the file is easy to "
             "find later.",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Explicit output path. Default: bench_results/<ts>_<sha>[_label].json",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if any question crashes. Default: log + continue.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    report = asyncio.run(_run(args))
    output_path = _write_report(report, args.output)

    summary = report["summary"]
    print(
        f"\n=== bench complete ===\n"
        f"  wrote: {output_path}\n"
        f"  questions: {report['meta']['question_count']}\n"
        f"  pass_rate: {summary['pass_rate']} ({summary['pass_count']}/"
        f"{summary['pass_count'] + summary['fail_count']})\n"
        f"  avg_latency_ms: {summary['avg_latency_ms']}\n"
        f"  p95_latency_ms: {summary['p95_latency_ms']}\n"
        f"  total_tokens: {summary['total_tokens']}\n"
        f"  failure_layers: {summary['failure_layers']}\n"
    )

    if args.strict and summary["fail_count"] > 0:
        print("STRICT mode + failures present — exiting 1.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
