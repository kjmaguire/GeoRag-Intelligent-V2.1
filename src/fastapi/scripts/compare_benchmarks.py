#!/usr/bin/env python3
"""Plan §5b — golden-query benchmark comparison CLI.

Thin wrapper around :mod:`app.services.eval.benchmark_compare` so the
pure-function diff logic lives in the FastAPI namespace (importable +
unit-tested) while the operator-facing entry point stays in scripts/.

Usage::

    python scripts/compare_benchmarks.py \\
        bench_results/2026-05-29T16-30-00Z_33bb26a_pre-§5e.json \\
        bench_results/2026-05-30T16-30-00Z_abc1234_post-§5e.json

    python scripts/compare_benchmarks.py before.json after.json --json

Exit codes:
  0 — pass rate stayed the same or improved
  1 — pass rate regressed (CI gate)
  2 — malformed input
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.eval.benchmark_compare import (  # noqa: E402
    build_question_map,
    diff_passes,
    diff_summary,
    load_report,
    render_json_diff,
    render_text,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diff two §5b benchmark JSONs to measure lift / regression.",
    )
    parser.add_argument("before", type=Path, help="Baseline report path")
    parser.add_argument("after", type=Path, help="Newer report path")
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a JSON diff instead of pretty text.",
    )
    args = parser.parse_args()

    before = load_report(args.before)
    after = load_report(args.after)
    before_map = build_question_map(before)
    after_map = build_question_map(after)

    regressed, improved, unchanged = diff_passes(before_map, after_map)
    summary_delta = diff_summary(before, after)

    if args.json:
        out = render_json_diff(
            args.before, args.after,
            before_map, after_map,
            regressed, improved, unchanged,
            summary_delta,
        )
        print(json.dumps(out, indent=2))
    else:
        print(render_text(before, after, regressed, improved, summary_delta))
        only_in_before = sorted(set(before_map) - set(after_map))
        only_in_after = sorted(set(after_map) - set(before_map))
        if only_in_before:
            print(f"\nNOTE: {len(only_in_before)} question(s) present only in BEFORE — skipped from comparison")
        if only_in_after:
            print(f"NOTE: {len(only_in_after)} question(s) present only in AFTER — skipped from comparison")

    return 1 if summary_delta["pass_rate_delta"] < 0 else 0


if __name__ == "__main__":
    sys.exit(main())
