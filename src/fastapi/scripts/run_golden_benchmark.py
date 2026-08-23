#!/usr/bin/env python3
"""Plan §5b — golden-query benchmark CLI runner.

Runs the live RAG pipeline against every active question in
``eval.golden_questions`` (or a filtered subset) and emits a JSON report
to ``bench_results/<timestamp>_<git_sha>.json`` that future runs can be
compared against to measure lift or regression.

Why a CLI:

  This CLI is the sole operator entry point for before/after measurement
  around a deploy (flip a feature flag, run the bench, compare against
  the prior baseline) — pairs with ``scripts/compare_benchmarks.py``.
  The ``eval_real_rag_nightly`` Hatchet workflow that used to run the
  same evaluator on a cron schedule was removed with the runtime eval
  trim (09d1d35, 2026-07-27); the evaluator modules under
  ``app/services/eval/`` were restored 2026-08-14 specifically to keep
  this bench runnable.

Usage::

    python scripts/run_golden_benchmark.py
    python scripts/run_golden_benchmark.py --question-set refusal_correctness
    python scripts/run_golden_benchmark.py --max-questions 20 --label pre-§5e
    python scripts/run_golden_benchmark.py --per-set 2   # coverage, not volume

Output schema (one entry per question)::

    {
      "meta": {
        "timestamp": "2026-05-29T16:30:00Z",
        "git_sha": "33bb26a",
        "label": "pre-§5e-training",
        "question_count": 119,
        "question_set_filter": null,
        "max_questions": null,
        "sampling": {"strategy": "round_robin", "per_set": null,
                     "seed": 20260821, "sets_represented": {...}},
        "model_stack": {"llm_backend": "azure", "embedding_model": "...",
                        "reranker_model": "...", "qdrant_collection": "..."}
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
import random
import subprocess
import sys
import time
from collections import Counter, OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Adjust path so the script works whether invoked from inside the
# fastapi container (where /app is the cwd) or from host with the
# fastapi source on PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from app.db.dsn import redact_dsn  # noqa: E402
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


#: Default seed for the within-set shuffle. Fixed, so two runs at the same
#: cap draw the same questions and stay comparable; recorded in ``meta`` so a
#: future reader can tell whether two reports sampled alike.
_DEFAULT_SAMPLE_SEED = 20260821


def _set_histogram(questions: list[QuestionRecord]) -> dict[str, int]:
    """How many questions per set — the field that makes a cap auditable."""
    return dict(Counter(q.question_set for q in questions).most_common())


def _stratified_groups(
    questions: list[QuestionRecord], seed: int,
) -> OrderedDict[str, list[QuestionRecord]]:
    """Group by question_set, shuffled deterministically inside each set.

    ``_load_active_questions`` orders by ``question_set, question_id``, so a
    head slice of it is alphabetical twice over — once across sets and once
    within them. Seeding per set removes the second bias while keeping runs
    reproducible; the seed travels in ``meta``.
    """
    groups: OrderedDict[str, list[QuestionRecord]] = OrderedDict()
    for q in questions:
        groups.setdefault(q.question_set, []).append(q)
    for name, bucket in groups.items():
        random.Random(f"{seed}:{name}").shuffle(bucket)
    return groups


def _round_robin(
    groups: OrderedDict[str, list[QuestionRecord]], limit: int,
) -> list[QuestionRecord]:
    """Take one question from each set in turn until ``limit`` is reached."""
    out: list[QuestionRecord] = []
    buckets = list(groups.values())
    longest = max((len(b) for b in buckets), default=0)
    depth = 0
    while depth < longest and len(out) < limit:
        for bucket in buckets:
            if depth < len(bucket):
                out.append(bucket[depth])
                if len(out) == limit:
                    return out
        depth += 1
    return out


def _select_questions(
    questions: list[QuestionRecord],
    max_questions: int | None,
    per_set: int | None,
    seed: int,
) -> tuple[list[QuestionRecord], dict[str, Any]]:
    """Choose which questions to run, keeping every set represented.

    The cap used to be ``questions[: args.max_questions]`` over a list
    ordered by ``question_set, question_id``. With ``--max-questions 10``
    against the 34-question corpus that yields 3 core_chat + 7
    numeric_grounding and nothing else, because 'public_private_boundary'
    and 'refusal_correctness' sort after both. Those two are the sets whose
    validators are non-vacuous, and ``thresholds.py`` declares
    public_private_boundary a §2.9 regulatory anchor with a zero-regression
    cap -- so the only scheduled eval in the system could not observe a
    regression in the one thing it is required to observe. The ten questions
    it did run all carry empty ``expected_citations`` and
    ``expected_numeric_values``, so even the pass rate measured nothing.

    A global cap is now round-robin across sets rather than a head slice,
    which fixes the deployed nightly without the workflow having to change
    its arguments. ``--per-set`` is the explicit form for when the point is
    coverage rather than volume.
    """
    if not max_questions and not per_set:
        return questions, {
            "strategy": "all",
            "per_set": None,
            "max_questions": None,
            "seed": None,
            "sets_represented": _set_histogram(questions),
        }

    groups = _stratified_groups(questions, seed)

    if per_set:
        picked = [q for bucket in groups.values() for q in bucket[:per_set]]
        strategy = "per_set"
        # A global cap on top of --per-set still has to stay stratified:
        # slicing the concatenation would drop whole trailing sets and
        # reintroduce exactly the bug this function exists to remove.
        if max_questions and len(picked) > max_questions:
            picked = _round_robin(_stratified_groups(picked, seed), max_questions)
            strategy = "per_set+round_robin"
    else:
        picked = _round_robin(groups, max_questions or len(questions))
        strategy = "round_robin"

    missing = sorted(set(groups) - {q.question_set for q in picked})
    if missing:
        log.warning(
            "bench.sets_unrepresented sets=%s -- the cap is too small to "
            "reach every question_set; a regression in these is invisible "
            "to this run",
            ",".join(missing),
        )

    return picked, {
        "strategy": strategy,
        "per_set": per_set,
        "max_questions": max_questions,
        "seed": seed,
        "sets_represented": _set_histogram(picked),
        "sets_unrepresented": missing,
    }


def _model_stack_fingerprint() -> dict[str, str]:
    """Which retrieval + generation stack produced this report.

    Two bench reports are comparable only if the same stack produced them.
    ``bench_results_to_commit_baseline.json`` -- the file the nightly diffs
    against -- carries no such record: it is dated 2026-05-28 with git_sha
    "unknown", predating the Qwen3 embedding/reranker swap (2026-06-03), the
    Layer-1 threshold recalibration, the Cohere/Foundry migration and the
    ADR-0010 collection cutover. Nothing in the artefact lets a reader see
    that diffing it against a current run is meaningless, so a green diff
    reads as evidence of stability. Recording the fingerprint makes that
    judgement possible from the file alone.
    """
    from app.config import settings  # noqa: PLC0415 -- keep import cost off the CLI path

    def _env(name: str, fallback: str = "") -> str:
        return ((os.environ.get(name) or fallback).strip()) or "unset"

    embedding_backend = _env("EMBEDDING_BACKEND", "local")
    reranker_backend = _env("RERANKER_BACKEND", "cross_encoder")

    return {
        "llm_backend": settings.LLM_BACKEND or "unset",
        "llm_deployment": (
            settings.AZURE_FOUNDRY_DEPLOYMENT or "unset"
            if settings.LLM_BACKEND == "azure"
            else (settings.VLLM_MODEL or "unset")
        ),
        "embedding_backend": embedding_backend,
        "embedding_model": (
            _env("AZURE_FOUNDRY_EMBED_DEPLOYMENT")
            if embedding_backend == "foundry"
            else (settings.EMBEDDING_MODEL_NAME or "unset")
        ),
        "reranker_backend": reranker_backend,
        "reranker_model": (
            _env("AZURE_FOUNDRY_RERANK_DEPLOYMENT")
            if reranker_backend == "foundry"
            else (settings.RERANKER_MODEL_NAME or "unset")
        ),
        "qdrant_collection": (
            "georag_chunks"
            if settings.RETRIEVAL_USE_DOCUMENT_PASSAGES
            else "georag_reports"
        ),
    }


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
    # See the note at app/main.py's pool log line: redact_dsn strips the
    # password structurally, which CodeQL's taint tracking cannot see.
    log.info("bench.start dsn=%s sha=%s label=%s",
             redact_dsn(_dsn()),  # codeql[py/clear-text-logging-sensitive-data]
             _git_sha(),
             args.label)

    conn = await asyncpg.connect(_dsn())
    try:
        questions = await _load_active_questions(conn, args.question_set)
    finally:
        await conn.close()

    loaded = len(questions)
    questions, sampling = _select_questions(
        questions, args.max_questions, args.per_set, args.sample_seed,
    )
    if len(questions) != loaded:
        log.info(
            "bench.sampled from=%d to=%d strategy=%s sets=%s",
            loaded, len(questions), sampling["strategy"],
            sampling["sets_represented"],
        )

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

    return _assemble_report(args, results, sampling)


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
        # Which layers COULD have checked anything on this question.
        # validate_numeric_claims and validate_entity_resolution both
        # short-circuit to a pass when the ground truth is absent, and the
        # committed corpus is labelled SKELETON with those fields empty — so
        # they returned "pass" on every question and the report presented
        # that as a measurement. Recording the input makes the difference
        # visible in the artefact instead of only in the source.
        "ground_truth": _ground_truth_flags(q),
    }


def _ground_truth_flags(q: QuestionRecord) -> dict[str, bool]:
    """What this question can actually be graded on."""
    return {
        "citations": bool(q.expected_citations),
        "entities": bool(q.expected_entities),
        "numeric": bool(q.expected_numeric_values),
        "refusal": bool(q.expected_refusal),
        "language": bool(
            getattr(q, "expected_language_compliance", None) or [],
        ),
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


def _validator_config_fingerprint() -> dict[str, object]:
    """The validator settings that decide whether a question passes.

    Without this, a pass-rate delta cannot be attributed. The 2026-06-01
    Layer 1 recalibration moved the relevance gate from 0.5 to 0.3 and
    changed "all citations must clear it" to "at least one must"; the two
    bench files eight minutes apart read 20% and 75%, and nothing in either
    artefact says why. Anyone plotting the series sees a product
    improvement.

    Same argument as `_model_stack_fingerprint` above, applied to the half
    of the stack most likely to be tuned: a threshold is exactly the thing
    someone adjusts between two runs they then compare.

    REFUSAL_PATTERNS is hashed rather than inlined. It is the single
    largest determinant of the pass rate — across 23 committed runs, 353
    of 446 recorded failures were a refusal-phrase match — and what a
    reader needs is "did this list change between these two runs", not the
    whole list repeated in every report.
    """
    import hashlib  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415
    from app.services.eval import validators  # noqa: PLC0415

    signature = inspect.signature(validators.validate_retrieval_quality)
    min_relevance = signature.parameters["min_relevance_score"].default

    patterns = list(validators.REFUSAL_PATTERNS)
    digest = hashlib.sha256(
        "\n".join(patterns).encode("utf-8")
    ).hexdigest()[:12]

    return {
        "min_relevance_score": min_relevance,
        "refusal_patterns_sha256_12": digest,
        "refusal_patterns_count": len(patterns),
        "qdrant_document_project_scope": (
            settings.QDRANT_DOCUMENT_PROJECT_SCOPE
        ),
        "retrieval_top_n": getattr(settings, "RETRIEVAL_TOP_N", None),
        "reranker_top_k": getattr(settings, "RERANKER_TOP_K", None),
    }


def _assemble_report(
    args: argparse.Namespace,
    results: list[dict[str, Any]],
    sampling: dict[str, Any] | None = None,
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
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "git_sha": _git_sha(),
            "label": args.label,
            "question_count": len(results),
            "question_set_filter": args.question_set,
            "max_questions": args.max_questions,
            "per_question_timeout": args.per_question_timeout,
            "sampling": sampling or {},
            "model_stack": _model_stack_fingerprint(),
            # Without this a pass-rate delta cannot be attributed to a code
            # change versus a threshold change — see the 2026-06-02 pair,
            # 20% then 75% eight minutes later, from a gate moving 0.5→0.3.
            "validator_config": _validator_config_fingerprint(),
        },
        "summary": {
            "pass_count": pass_count,
            "fail_count": fail_count,
            "pass_rate": round(pass_count / len(results), 4) if results else 0.0,
            "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
            "p95_latency_ms": _p95(latencies),
            "total_tokens": sum(tokens) if tokens else 0,
            "failure_layers": dict(failure_layers.most_common()),
            "coverage": _layer_coverage(results),
        },
        "results": results,
    }


def _layer_coverage(results: list[dict[str, Any]]) -> dict[str, Any]:
    """How much of the pass rate is a measurement.

    The headline number was being read as answer correctness. It is not:
    across 23 committed benchmark runs, 353 of 446 recorded failures were a
    case-insensitive substring match against one of 22 refusal phrases, and
    the two validators that would catch a wrong NUMBER or a wrong ENTITY had
    never failed a single question — because the corpus carries no ground
    truth for them, and both short-circuit to a pass when it is missing.

    So the report now states what each layer was able to check. A reader who
    sees `layer_3_numeric: {"checked": 0, "of": 50}` beside
    `pass_rate: 0.8` cannot mistake the second for a claim about
    correctness.
    """
    total = len(results)
    if not total:
        return {}

    def _checked(kind: str) -> int:
        return sum(1 for r in results if (r.get("ground_truth") or {}).get(kind))

    gradable = [
        r for r in results
        if any(
            (r.get("ground_truth") or {}).get(k)
            for k in ("citations", "entities", "numeric", "language")
        )
    ]
    gradable_passes = sum(1 for r in gradable if r["passed"])

    return {
        "layer_2_citations": {"checked": _checked("citations"), "of": total},
        "layer_3_numeric": {"checked": _checked("numeric"), "of": total},
        "layer_4_entities": {"checked": _checked("entities"), "of": total},
        "layer_6_refusal": {"checked": _checked("refusal"), "of": total},
        # §2.9 required/forbidden language. Reported separately from
        # layer_6_refusal because refusal-phrase presence and
        # withheld-content absence are different claims, and only the
        # second one is a regulatory boundary check.
        "layer_6_language": {"checked": _checked("language"), "of": total},
        # Pass rate over only the questions carrying ground truth for
        # something other than refusal wording. None when there are none,
        # which is itself the finding.
        "gradable_question_count": len(gradable),
        "gradable_pass_rate": (
            round(gradable_passes / len(gradable), 4) if gradable else None
        ),
    }


def _empty_report(args: argparse.Namespace) -> dict[str, Any]:
    """Report shape when no questions matched the filter."""
    return {
        "meta": {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "git_sha": _git_sha(),
            "label": args.label,
            "question_count": 0,
            "question_set_filter": args.question_set,
            "max_questions": args.max_questions,
            "sampling": {},
            "model_stack": _model_stack_fingerprint(),
        },
        "summary": {
            "pass_count": 0, "fail_count": 0, "pass_rate": 0.0, "coverage": {},
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
        help="Global cap on questions run. Applied ROUND-ROBIN across "
             "question_sets, not as a head slice, so every set stays "
             "represented. Use during development to keep iteration "
             "cheap. Default: no cap.",
    )
    parser.add_argument(
        "--per-set", type=int, default=None,
        help="Take N questions from EACH active question_set. Prefer this "
             "over --max-questions when the point is coverage rather than "
             "volume: --per-set 2 always exercises refusal_correctness and "
             "public_private_boundary, the two sets whose validators are "
             "non-vacuous. Default: no per-set cap.",
    )
    parser.add_argument(
        "--sample-seed", type=int, default=_DEFAULT_SAMPLE_SEED,
        help="Seed for the deterministic within-set shuffle, recorded in "
             f"meta.sampling. Default: {_DEFAULT_SAMPLE_SEED}. Change it to "
             "draw a different sample; keep it to compare two runs.",
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
        f"  coverage: {summary.get('coverage')}\n"
    )

    _cov = summary.get("coverage") or {}
    if _cov.get("gradable_question_count") == 0:
        print(
            "WARNING: no question in this run carried expected citations, "
            "entities or numeric values. The pass rate above measures "
            "refusal wording and nothing else — do not quote it as answer "
            "correctness.",
            file=sys.stderr,
        )

    if args.strict and summary["fail_count"] > 0:
        print("STRICT mode + failures present — exiting 1.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
