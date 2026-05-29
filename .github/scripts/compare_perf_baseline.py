#!/usr/bin/env python3
"""Module 10 Chunk 10.7 — perf baseline regression check.

Compares the JSON output of `load_test.py --json` against the committed
`ops/baselines/<date>-api-latency.md` baseline. Fails (exit 1) when any
query-class p95 exceeds baseline by more than the threshold percent.

Usage:
    compare_perf_baseline.py
        --results <load_test_output.json>
        --baseline <baseline.md>
        --threshold <pct, e.g. 20>

The baseline doc has YAML frontmatter at the top of the file with
per-class p95 baselines (see `ops/baselines/2026-04-22-api-latency.md` for
the committed shape).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_baseline_yaml(text: str) -> dict[str, float]:
    """Extract `p95_seconds` per query_class from the baseline doc's YAML frontmatter.

    The doc starts with `---` … `---` and contains a block:
        baselines:
          count:    {p95_seconds: 1.20, p50_seconds: 0.50, ...}
          numeric:  {p95_seconds: 1.80, ...}
          ...
    """
    m = re.search(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL | re.MULTILINE)
    if not m:
        sys.stderr.write("baseline: missing YAML frontmatter\n")
        sys.exit(2)

    yaml_text = m.group(1)

    # Parse the per-class p95 lines without dragging in PyYAML.
    # Each line looks like:  count: {p95_seconds: 1.20, ...}
    out: dict[str, float] = {}
    for line in yaml_text.splitlines():
        m = re.match(r"^\s*(\w+):\s*\{[^}]*p95_seconds:\s*([\d.]+)", line)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--results", required=True, type=Path)
    p.add_argument("--baseline", required=True, type=Path)
    p.add_argument("--threshold", type=float, default=20.0,
                   help="regression threshold percent (default 20)")
    args = p.parse_args()

    baseline = parse_baseline_yaml(args.baseline.read_text(encoding="utf-8"))
    if not baseline:
        sys.stderr.write("baseline: no per-class p95 entries found\n")
        return 2

    results = json.loads(args.results.read_text(encoding="utf-8"))
    # load_test.py --json emits {"per_class": {"count": {"p95_seconds": ..}, ...}, ...}
    measured = results.get("per_class", {})
    if not measured:
        sys.stderr.write("results: no per_class block — load_test.py --json output unexpected\n")
        return 2

    threshold_factor = 1.0 + args.threshold / 100.0
    failures: list[str] = []
    for cls, base in baseline.items():
        m = measured.get(cls)
        if m is None:
            sys.stdout.write(f"  {cls:10s}  baseline={base:.2f}s   measured=N/A   (skipped)\n")
            continue
        cur = float(m["p95_seconds"])
        ratio = cur / base if base else float("inf")
        ok = cur <= base * threshold_factor
        marker = "OK " if ok else "FAIL"
        sys.stdout.write(
            f"  {cls:10s}  baseline={base:.2f}s   measured={cur:.2f}s   "
            f"ratio={ratio:.2f}x   [{marker}]\n"
        )
        if not ok:
            failures.append(
                f"{cls}: p95 {cur:.2f}s exceeds baseline {base:.2f}s by "
                f"{(ratio - 1) * 100:.1f}% (threshold {args.threshold:.0f}%)"
            )

    if failures:
        sys.stderr.write("\nPERF REGRESSION:\n  - " + "\n  - ".join(failures) + "\n")
        return 1
    sys.stdout.write("\nPerf baseline OK — all p95s within threshold.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
