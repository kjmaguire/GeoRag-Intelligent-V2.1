#!/usr/bin/env python3
"""Merge Clover coverage reports and print a line-coverage summary.

PHPUnit is run twice in .github/workflows/coverage.yml, because neither
run alone covers the suite:

  * `phpunit.xml`        forces DB_CONNECTION=sqlite, so every test using
    Tests\\Concerns\\RequiresPostgres -- the RLS, tenancy and PostGIS
    regression coverage -- self-skips.
  * `phpunit.pgsql.xml`  runs those, but its `<testsuites>` list is
    hand-authored and narrower than the whole tree.

Reporting either number on its own would be wrong in a specific and
flattering direction: the sqlite run understates coverage of everything
tenancy-related, and the pgsql run understates coverage of everything
else. Merging is the only way to get a figure that means what the word
means.

Merge rule: a line is covered if ANY run covered it. Clover records a
per-line hit count, so the merge is a max() over runs keyed by
(file, line). Lines one run never saw at all are simply absent from its
report and contribute nothing -- which is why this takes the union of
files rather than the intersection.

Usage:

    python scripts/merge_clover.py out.xml in1.xml in2.xml [...]

Writes a merged Clover file and prints a Markdown table on stdout, ready
to append to $GITHUB_STEP_SUMMARY.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def _collect(path: Path) -> dict[str, dict[int, int]]:
    """{file path: {line number: hit count}} for one Clover report."""
    per_file: dict[str, dict[int, int]] = defaultdict(dict)
    root = ET.parse(path).getroot()
    for file_el in root.iter("file"):
        name = file_el.get("name")
        if not name:
            continue
        lines = per_file[name]
        for line_el in file_el.iter("line"):
            num = line_el.get("num")
            count = line_el.get("count")
            if num is None or count is None:
                continue
            n, c = int(num), int(count)
            # max(): covered by any run wins. Absent from a run is not the
            # same as zero in that run -- but zero is the identity here, so
            # both behave correctly.
            lines[n] = max(lines.get(n, 0), c)
    return per_file


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2

    out_path = Path(argv[1])
    inputs = [Path(p) for p in argv[2:]]

    merged: dict[str, dict[int, int]] = defaultdict(dict)
    used: list[str] = []
    for path in inputs:
        if not path.is_file():
            # A missing report means that PHPUnit run did not get far
            # enough to write one. Say so rather than silently reporting a
            # number derived from fewer runs than the caller asked for.
            print(f"WARNING: {path} does not exist -- excluded", file=sys.stderr)
            continue
        used.append(path.name)
        for name, lines in _collect(path).items():
            target = merged[name]
            for num, count in lines.items():
                target[num] = max(target.get(num, 0), count)

    if not used:
        print("ERROR: no Clover reports could be read", file=sys.stderr)
        return 1

    total = sum(len(lines) for lines in merged.values())
    covered = sum(sum(1 for c in lines.values() if c > 0) for lines in merged.values())
    pct = (covered / total * 100.0) if total else 0.0

    root = ET.Element("coverage", {"generated": "0"})
    project = ET.SubElement(root, "project", {"timestamp": "0"})
    for name in sorted(merged):
        file_el = ET.SubElement(project, "file", {"name": name})
        for num in sorted(merged[name]):
            ET.SubElement(
                file_el,
                "line",
                {"num": str(num), "type": "stmt", "count": str(merged[name][num])},
            )
    ET.SubElement(
        project,
        "metrics",
        {"statements": str(total), "coveredstatements": str(covered)},
    )
    out_path.write_bytes(ET.tostring(root, encoding="utf-8"))

    print("| metric | value |")
    print("| --- | --- |")
    print(f"| reports merged | {', '.join(used)} |")
    print(f"| files | {len(merged)} |")
    print(f"| executable lines | {total} |")
    print(f"| covered lines | {covered} |")
    print(f"| **line coverage** | **{pct:.1f}%** |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
