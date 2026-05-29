#!/usr/bin/env python3
"""ADR-0010 Session C per-slice comparator.

Diffs two §5b benchmark JSONs at the question_set granularity. A
candidate is considered safe to retire-the-baseline if no slice
regresses more than 2pp pass_rate (Kyle's locked criterion).

Usage::

    python scripts/compare_benchmarks_per_slice.py before.json after.json

Exit codes:
  0  — no slice regression >2pp; safe to retire
  1  — at least one slice regressed >2pp; HOLD
  2  — malformed input
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


REGRESSION_TOLERANCE = 0.02  # 2pp per Kyle's locked retirement criterion


def load_report(path: Path) -> dict:
    with open(path) as fh:
        return json.load(fh)


def per_slice_pass_rate(report: dict) -> dict[str, dict]:
    """Return {question_set: {pass, total, pass_rate}} from a report's
    results array."""
    buckets: dict[str, list] = defaultdict(list)
    for r in report["results"]:
        buckets[r.get("question_set", "(unknown)")].append(bool(r.get("passed")))
    out: dict[str, dict] = {}
    for qset, passes in buckets.items():
        n = len(passes)
        p = sum(passes)
        out[qset] = {"pass": p, "total": n, "pass_rate": p / n if n else 0.0}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Per-slice benchmark diff for ADR-0010 retirement gate.",
    )
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()

    try:
        before = load_report(args.before)
        after = load_report(args.after)
    except (json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"ERROR loading reports: {exc}", file=sys.stderr)
        return 2

    b_slices = per_slice_pass_rate(before)
    a_slices = per_slice_pass_rate(after)

    all_qsets = sorted(set(b_slices) | set(a_slices))
    regressions: list[tuple[str, float, float]] = []
    improvements: list[tuple[str, float, float]] = []

    print(f"=== Per-slice pass_rate diff ===")
    print(f"baseline:  {args.before.name}")
    print(f"candidate: {args.after.name}")
    print()
    print(f"{'question_set':<28} {'baseline':>16} {'candidate':>16} {'delta':>10}")
    print(f"{'-'*28} {'-'*16} {'-'*16} {'-'*10}")
    for qset in all_qsets:
        b = b_slices.get(qset, {"pass": 0, "total": 0, "pass_rate": 0.0})
        a = a_slices.get(qset, {"pass": 0, "total": 0, "pass_rate": 0.0})
        delta = a["pass_rate"] - b["pass_rate"]
        marker = ""
        if delta < -REGRESSION_TOLERANCE:
            regressions.append((qset, b["pass_rate"], a["pass_rate"]))
            marker = "  ⚠ REGRESSION"
        elif delta > REGRESSION_TOLERANCE:
            improvements.append((qset, b["pass_rate"], a["pass_rate"]))
            marker = "  ✓"
        print(f"{qset:<28} "
              f"{b['pass']:>4}/{b['total']:<3} {b['pass_rate']:>7.3f} "
              f"{a['pass']:>4}/{a['total']:<3} {a['pass_rate']:>7.3f} "
              f"{delta:>+9.3f}{marker}")
    print()

    b_total_p = before["summary"]["pass_count"]
    b_total_n = before["meta"]["question_count"]
    a_total_p = after["summary"]["pass_count"]
    a_total_n = after["meta"]["question_count"]
    print(f"OVERALL: baseline={b_total_p}/{b_total_n} ({b_total_p/b_total_n:.3f}) "
          f"candidate={a_total_p}/{a_total_n} ({a_total_p/a_total_n:.3f})")
    print()

    if regressions:
        print(f"VERDICT: HOLD ({len(regressions)} slice(s) regressed >{REGRESSION_TOLERANCE*100:.0f}pp)")
        for qset, b_pr, a_pr in regressions:
            print(f"  - {qset}: {b_pr:.3f} → {a_pr:.3f} ({a_pr - b_pr:+.3f})")
        if improvements:
            print(f"  ({len(improvements)} slice(s) improved >{REGRESSION_TOLERANCE*100:.0f}pp)")
        return 1

    print(f"VERDICT: RETIRE — no slice regressed >{REGRESSION_TOLERANCE*100:.0f}pp")
    if improvements:
        print(f"  ({len(improvements)} slice(s) improved >{REGRESSION_TOLERANCE*100:.0f}pp)")
        for qset, b_pr, a_pr in improvements:
            print(f"  + {qset}: {b_pr:.3f} → {a_pr:.3f} ({a_pr - b_pr:+.3f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
