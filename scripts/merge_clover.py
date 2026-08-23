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
    """{file path: {line number: hit count}} for one Clover report.

    Raises ValueError when the file is not parseable Clover XML. A PHPUnit
    run killed partway through (the coverage job's steps are
    continue-on-error, so a timeout or an OOM lands here) leaves a
    truncated file, not a missing one — and an unhandled ParseError in a
    step that is NOT continue-on-error fails the job with a stack trace
    instead of the one sentence that explains it.
    """
    per_file: dict[str, dict[int, int]] = defaultdict(dict)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"{path} is not parseable Clover XML: {exc}") from exc
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
    lost: list[str] = []
    for path in inputs:
        if not path.is_file():
            # A missing report means that PHPUnit run did not get far
            # enough to write one. Say so rather than silently reporting a
            # number derived from fewer runs than the caller asked for.
            print(f"WARNING: {path} does not exist -- excluded", file=sys.stderr)
            lost.append(path.name)
            continue
        try:
            collected = _collect(path)
        except ValueError as exc:
            print(f"WARNING: {exc} -- excluded", file=sys.stderr)
            lost.append(path.name)
            continue
        used.append(path.name)
        for name, lines in collected.items():
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
    if lost:
        print(f"| **reports MISSING** | **{', '.join(lost)}** |")
    print(f"| files | {len(merged)} |")
    print(f"| executable lines | {total} |")
    print(f"| covered lines | {covered} |")
    print(f"| **line coverage** | **{pct:.1f}%** |")
    if lost:
        print(
            "|  | figure below the true one -- a run's coverage is missing, "
            "not zero |"
        )

    if lost:
        # Non-zero, and this is the point of the script rather than a
        # detail of it. Both PHPUnit steps are continue-on-error, so a run
        # that died leaves NO other red mark on the job: without this the
        # summary published a headline percentage derived from fewer runs
        # than were asked for, understating in exactly the direction the
        # module docstring says merging exists to avoid, and reading as a
        # real coverage drop for whoever looks next.
        print(
            f"ERROR: {len(lost)} of {len(inputs)} report(s) unreadable "
            f"({', '.join(lost)}) -- the figure above is not the merged one",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
