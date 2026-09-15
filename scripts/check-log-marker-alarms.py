#!/usr/bin/env python3
"""Every log-marker alarm must watch the log group its emitter writes to.

This deployment has two CloudWatch log groups (services.tf):

    /ecs/georag             the ten ECS services
    /ecs/georag/scheduler   the two nightly sweep tasks

A metric filter only ever sees the group it is attached to, and nothing in
Terraform relates a marker string to the process that prints it. So a filter
can name a real marker, compile, validate, apply, and watch a log group that
marker can never appear in. The alarm then sits at zero forever and reads as
"healthy" — the worst possible failure for an alarm, because it is
indistinguishable from the thing not happening.

That was live. Every marker in alerts.tf was filtered on the services group,
including BEDROCK_ENDPOINT_NOT_INSERVICE, which is emitted only by
deploy/aws/scheduler/startup-sweep.sh and therefore only ever lands in the
scheduler group. alerts.tf describes that one as Sev 1 — "NO chat and NO OCR"
— and it could not have fired.

So: find every marker alerts.tf filters on, find what actually prints it, and
fail when the two disagree.

Deliberately NOT checked: whether the marker string is one the emitter builds
at runtime from parts. Every marker in this repository is a literal, and a
constructed one should be caught in review rather than guessed at here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALERTS = REPO / "deploy" / "aws" / "terraform" / "alerts.tf"

#: Paths whose output goes to the scheduler log group rather than services.
#: Both sweep task definitions set awslogs-group to it (scheduler.tf:53, :92).
SCHEDULER_SOURCES = ("deploy/aws/scheduler/",)

#: Where to look for emitters: the code that actually runs in a container
#: logging to one of the two groups. `scripts/` is deliberately absent — those
#: are CI and operator tools, they log to a terminal, and including them made
#: this file's own description of the bug read as a second emitter.
SEARCH_DIRS = ("src/fastapi/app", "deploy/aws/scheduler", "app")
#: Tests name markers without printing them in production, so they are not
#: evidence either.
SKIP_PARTS = ("/tests/", "/test_", "/.venv/", "__pycache__", "/node_modules/")

#: `key = { ... log_group = "x" ... pattern = "Y" ... }` inside log_markers.
ENTRY = re.compile(
    r"^\s{4}([a-z0-9-]+)\s*=\s*\{(.*?)^\s{4}\}",
    re.MULTILINE | re.DOTALL,
)
LOG_GROUP = re.compile(r'log_group\s*=\s*"([a-z]+)"')
PATTERN = re.compile(r'pattern\s*=\s*"([A-Z0-9_]+)"')


def emitters(marker: str) -> list[str]:
    """Files that contain the marker literal, excluding tests."""
    hits: list[str] = []
    for d in SEARCH_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".sh"}:
                continue
            if path.resolve() == Path(__file__).resolve():
                continue
            rel = "/" + str(path.relative_to(REPO))
            if any(part in rel for part in SKIP_PARTS):
                continue
            try:
                if marker in path.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(str(path.relative_to(REPO)))
            except OSError:
                continue
    return sorted(hits)


def expected_group(sources: list[str]) -> str | None:
    """Which log group these emitters land in, or None if they disagree."""
    groups = {
        "scheduler" if s.startswith(SCHEDULER_SOURCES) else "services"
        for s in sources
    }
    if len(groups) != 1:
        return None
    return groups.pop()


def main() -> int:
    if not ALERTS.is_file():
        print(f"FAIL  {ALERTS} does not exist", file=sys.stderr)
        return 1

    source = ALERTS.read_text(encoding="utf-8")
    start = source.find("log_markers = {")
    if start == -1:
        print(
            "FAIL  no `log_markers = {` block in alerts.tf — this check has "
            "gone stale.",
            file=sys.stderr,
        )
        return 1

    problems: list[str] = []
    checked = 0

    for key, body in ENTRY.findall(source[start:]):
        pattern = PATTERN.search(body)
        group = LOG_GROUP.search(body)
        if not pattern:
            continue
        checked += 1
        marker = pattern.group(1)

        if not group:
            problems.append(
                f"  {key}: no log_group. Every marker must name the group its "
                f"emitter writes to, or the default silently decides for it."
            )
            continue

        found = emitters(marker)
        if not found:
            problems.append(
                f"  {key}: nothing emits {marker}. The alarm is watching for a "
                f"string no code prints, so it can never fire."
            )
            continue

        want = expected_group(found)
        if want is None:
            problems.append(
                f"  {key}: {marker} is emitted from BOTH groups "
                f"({', '.join(found)}). One filter cannot cover both; split "
                f"the marker or the filter."
            )
            continue

        if want != group.group(1):
            problems.append(
                f"  {key}: filters the '{group.group(1)}' log group, but "
                f"{marker} is emitted only by {', '.join(found)}, which logs "
                f"to '{want}'. This alarm cannot fire."
            )

    if problems:
        print(
            "FAIL  log-marker alarms disagree with the code that emits them.\n"
            "      An alarm on the wrong log group sits at zero and reads as "
            "healthy:\n",
            file=sys.stderr,
        )
        print("\n".join(problems), file=sys.stderr)
        return 1

    if checked == 0:
        print("FAIL  parsed no markers out of alerts.tf.", file=sys.stderr)
        return 1

    print(
        f"Log-marker alarms: {checked} marker(s), each filtered on the log "
        "group its emitter writes to."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
