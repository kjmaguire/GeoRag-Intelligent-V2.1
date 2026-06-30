"""Plan §5b — golden-query benchmark comparison library.

Pure-function diff over two report dicts produced by
``scripts/run_golden_benchmark.py``. Living here (not in ``scripts/``)
makes the logic importable + unit-testable inside the FastAPI container
without dragging the scripts/ tree onto the bind-mount.

Public surface:

  • :func:`load_report`              — parse + validate one report JSON
  • :func:`build_question_map`       — index a report by question_id
  • :func:`diff_passes`              — partition shared questions into
                                       regressed / improved / unchanged
  • :func:`diff_summary`             — top-line deltas (pass rate,
                                       latency, tokens)
  • :func:`render_text`              — pretty multi-line text report
  • :func:`render_json_diff`         — structured diff for CI consumers
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "load_report",
    "build_question_map",
    "diff_passes",
    "diff_summary",
    "render_text",
    "render_json_diff",
]


def load_report(path: Path) -> dict[str, Any]:
    """Parse + validate one benchmark report.

    Exits the process on parse failure or missing required keys —
    the CLI wraps this so the caller gets a clean error message.
    """
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f"ERROR: malformed JSON in {path}: {e}", file=sys.stderr)
        sys.exit(2)

    for key in ("meta", "summary", "results"):
        if key not in data:
            print(f"ERROR: {path} missing top-level '{key}'", file=sys.stderr)
            sys.exit(2)
    return data


def build_question_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index a report's per-question results by question_id."""
    return {r["question_id"]: r for r in report["results"]}


def diff_passes(
    before_map: dict[str, dict[str, Any]],
    after_map: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition shared question IDs into regressed / improved / unchanged.

    Only questions present in BOTH reports are compared. Questions added
    or removed between runs are surfaced separately by the caller via the
    set difference on ``before_map`` / ``after_map`` keys.

    Returns:
        ``(regressed, improved, unchanged)`` — each element is a delta
        dict with both before + after metadata for the question, so the
        caller can render side-by-side comparisons without re-indexing.
    """
    regressed: list[dict[str, Any]] = []
    improved: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []

    for qid, before in before_map.items():
        after = after_map.get(qid)
        if after is None:
            continue
        b_pass = bool(before.get("passed"))
        a_pass = bool(after.get("passed"))
        delta = {
            "question_id": qid,
            "question_set": after.get("question_set"),
            "question_first_120": after.get("question_text_first_120"),
            "before_pass": b_pass,
            "after_pass": a_pass,
            "before_failure_layer": before.get("failure_layer"),
            "after_failure_layer": after.get("failure_layer"),
            "before_latency_ms": before.get("latency_ms"),
            "after_latency_ms": after.get("latency_ms"),
        }
        if b_pass and not a_pass:
            regressed.append(delta)
        elif not b_pass and a_pass:
            improved.append(delta)
        else:
            unchanged.append(delta)

    return regressed, improved, unchanged


def diff_summary(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Top-line summary deltas: pass rate, pass count, latency, tokens.

    Latency deltas are None when either side is missing the
    measurement (e.g. empty result set).
    """
    b = before["summary"]
    a = after["summary"]
    return {
        "pass_rate_delta": round(a["pass_rate"] - b["pass_rate"], 4),
        "pass_count_delta": a["pass_count"] - b["pass_count"],
        "avg_latency_ms_delta": (
            (a["avg_latency_ms"] - b["avg_latency_ms"])
            if a.get("avg_latency_ms") is not None
            and b.get("avg_latency_ms") is not None
            else None
        ),
        "p95_latency_ms_delta": (
            (a["p95_latency_ms"] - b["p95_latency_ms"])
            if a.get("p95_latency_ms") is not None
            and b.get("p95_latency_ms") is not None
            else None
        ),
        "total_tokens_delta": a["total_tokens"] - b["total_tokens"],
    }


def render_text(
    before: dict[str, Any],
    after: dict[str, Any],
    regressed: list[dict[str, Any]],
    improved: list[dict[str, Any]],
    summary_delta: dict[str, Any],
) -> str:
    """Pretty multi-line text report for human review.

    Truncates the regressed/improved lists at 20 entries with a "use
    --json for full list" hint so the terminal output stays scannable
    even when a deploy moves the needle on hundreds of questions.
    """
    lines: list[str] = []
    lines.append("=== benchmark comparison ===")
    lines.append(
        f"  BEFORE: {before['meta']['timestamp']} sha={before['meta']['git_sha']} "
        f"label={before['meta'].get('label') or '-'}"
    )
    lines.append(
        f"  AFTER:  {after['meta']['timestamp']} sha={after['meta']['git_sha']} "
        f"label={after['meta'].get('label') or '-'}"
    )
    lines.append("")
    lines.append(
        f"  questions before/after: "
        f"{before['meta']['question_count']} / {after['meta']['question_count']}"
    )
    lines.append(
        f"  pass_rate: {before['summary']['pass_rate']:.4f} → "
        f"{after['summary']['pass_rate']:.4f}  "
        f"(Δ {summary_delta['pass_rate_delta']:+.4f})"
    )
    lines.append(
        f"  pass_count: {before['summary']['pass_count']} → "
        f"{after['summary']['pass_count']}  "
        f"(Δ {summary_delta['pass_count_delta']:+d})"
    )
    if summary_delta["avg_latency_ms_delta"] is not None:
        lines.append(
            f"  avg_latency_ms: {before['summary']['avg_latency_ms']} → "
            f"{after['summary']['avg_latency_ms']}  "
            f"(Δ {summary_delta['avg_latency_ms_delta']:+d} ms)"
        )
    if summary_delta["p95_latency_ms_delta"] is not None:
        lines.append(
            f"  p95_latency_ms: {before['summary']['p95_latency_ms']} → "
            f"{after['summary']['p95_latency_ms']}  "
            f"(Δ {summary_delta['p95_latency_ms_delta']:+d} ms)"
        )
    lines.append(
        f"  total_tokens: {before['summary']['total_tokens']} → "
        f"{after['summary']['total_tokens']}  "
        f"(Δ {summary_delta['total_tokens_delta']:+d})"
    )
    lines.append("")

    if regressed:
        lines.append(f"REGRESSED ({len(regressed)}) — were passing, now failing:")
        for d in regressed[:20]:
            lines.append(f"  - [{d['question_set']}] {d['question_first_120']!r}")
            lines.append(f"      now fails on layer={d['after_failure_layer']}")
        if len(regressed) > 20:
            lines.append(f"  ... +{len(regressed) - 20} more (use --json for full list)")
        lines.append("")

    if improved:
        lines.append(f"IMPROVED ({len(improved)}) — were failing, now passing:")
        for d in improved[:20]:
            lines.append(f"  + [{d['question_set']}] {d['question_first_120']!r}")
            lines.append(f"      was failing on layer={d['before_failure_layer']}")
        if len(improved) > 20:
            lines.append(f"  ... +{len(improved) - 20} more (use --json for full list)")
        lines.append("")

    before_layers = before["summary"].get("failure_layers", {})
    after_layers = after["summary"].get("failure_layers", {})
    all_layers = sorted(set(before_layers) | set(after_layers))
    if all_layers:
        lines.append("failure-layer histogram:")
        for layer in all_layers:
            b = before_layers.get(layer, 0)
            a = after_layers.get(layer, 0)
            delta = a - b
            lines.append(f"  {layer:>30s}: {b} → {a}  (Δ {delta:+d})")
        lines.append("")

    if summary_delta["pass_rate_delta"] < 0:
        lines.append("VERDICT: REGRESSION — pass rate dropped")
    elif summary_delta["pass_rate_delta"] > 0:
        lines.append("VERDICT: IMPROVEMENT — pass rate increased")
    else:
        lines.append("VERDICT: NEUTRAL — pass rate unchanged")

    return "\n".join(lines)


def render_json_diff(
    before_path: Path,
    after_path: Path,
    before_map: dict[str, dict[str, Any]],
    after_map: dict[str, dict[str, Any]],
    regressed: list[dict[str, Any]],
    improved: list[dict[str, Any]],
    unchanged: list[dict[str, Any]],
    summary_delta: dict[str, Any],
) -> dict[str, Any]:
    """Structured diff payload — for CI / dashboards.

    Includes the asymmetric sets (questions present only in one report)
    so callers can detect when the question pool itself changed.
    """
    only_in_before = sorted(set(before_map) - set(after_map))
    only_in_after = sorted(set(after_map) - set(before_map))
    return {
        "before_path": str(before_path),
        "after_path": str(after_path),
        "summary_delta": summary_delta,
        "regressed_count": len(regressed),
        "improved_count": len(improved),
        "unchanged_count": len(unchanged),
        "questions_only_in_before": only_in_before,
        "questions_only_in_after": only_in_after,
        "regressed": regressed,
        "improved": improved,
    }
