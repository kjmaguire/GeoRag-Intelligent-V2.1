#!/usr/bin/env python3
"""Keep the scheduler jobs' inline bash identical to its reviewed source.

An Azure Container Apps Job carries its script as an inline `args` string
on the job resource. That makes the deployed body a copy, and a copy of a
shell script inside a YAML string is the kind of thing that gets edited in
the portal at 2am and never makes it back.

So the source of truth is
``deploy/azure/containerapps/scripts/{shutdown,startup}-sweep.sh`` — real
files that ``deploy/azure/containerapps/scripts/tests/run.sh`` executes —
and the YAML holds a generated copy. This checker fails when they differ.

    python scripts/check_scheduler_job_parity.py          # verify
    python scripts/check_scheduler_job_parity.py --write   # regenerate

Neither the YAML nor the script is applied by CD; see the header of
deploy/azure/containerapps/shutdown-job.yaml for the apply command. This
gate only guarantees that what you review is what you would apply.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTAINERAPPS = REPO_ROOT / "deploy" / "azure" / "containerapps"

# (job yaml, script that must appear as its inline args)
PAIRS = (
    (CONTAINERAPPS / "shutdown-job.yaml", CONTAINERAPPS / "scripts" / "shutdown-sweep.sh"),
    (CONTAINERAPPS / "startup-job.yaml", CONTAINERAPPS / "scripts" / "startup-sweep.sh"),
)

# The `- |` block literal under `args:`, and the indent its content sits at.
BLOCK_MARKER = "- |"
BLOCK_INDENT = " " * 12


def _read(path: Path) -> list[str]:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n").split("\n")


def _script_body(script: Path) -> list[str]:
    """The script minus its shebang.

    The shebang is meaningful in the file (it is what makes the script
    directly executable and what shellcheck keys off) and meaningless in
    the YAML, where the job's own `command: [/bin/bash, -c]` decides the
    interpreter. Stripping it here is the one deliberate difference
    between the two copies.
    """
    lines = _read(script)
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _block_bounds(lines: list[str], yaml_path: Path) -> tuple[int, int]:
    """Return [start, end) covering the inline args content."""
    start = None
    for i, line in enumerate(lines):
        if line.strip() == BLOCK_MARKER:
            start = i + 1
            break
    if start is None:
        raise SystemExit(f"{yaml_path}: no `{BLOCK_MARKER}` args block found")

    end = start
    while end < len(lines):
        line = lines[end]
        # A blank line inside a block literal belongs to the block; the
        # block ends at the first non-blank line that is dedented.
        if line.strip() and not line.startswith(BLOCK_INDENT):
            break
        end += 1
    return start, end


def _embedded(lines: list[str], start: int, end: int) -> list[str]:
    out = []
    for line in lines[start:end]:
        out.append(line[len(BLOCK_INDENT):] if line.startswith(BLOCK_INDENT) else line.strip())
    while out and not out[-1].strip():
        out.pop()
    return out


def _render(body: list[str]) -> list[str]:
    # A blank line must stay genuinely blank rather than becoming twelve
    # spaces: trailing whitespace is exactly the kind of invisible diff
    # this gate exists to prevent.
    return [BLOCK_INDENT + line if line.strip() else "" for line in body]


def process(yaml_path: Path, script: Path, write: bool) -> bool:
    lines = _read(yaml_path)
    start, end = _block_bounds(lines, yaml_path)
    embedded = _embedded(lines, start, end)
    body = _script_body(script)

    if embedded == body:
        print(f"OK   {yaml_path.name} matches {script.name}")
        return True

    if write:
        lines[start:end] = _render(body)
        with open(yaml_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
        print(f"WROTE {yaml_path.name} from {script.name}")
        return True

    print(f"DRIFT {yaml_path.name} does not match {script.name}", file=sys.stderr)
    import difflib

    diff = difflib.unified_diff(
        embedded, body, fromfile=f"{yaml_path.name} (inline args)",
        tofile=str(script.relative_to(REPO_ROOT)).replace("\\", "/"), lineterm="",
    )
    for line in list(diff)[:60]:
        print(line, file=sys.stderr)
    print(
        "\nEdit the .sh file, then run:  "
        "python scripts/check_scheduler_job_parity.py --write",
        file=sys.stderr,
    )
    return False


# --- cron/guard invariant ---------------------------------------------
# Container Apps Jobs schedule in UTC, so a local-time window is spelled
# out three times: the cron's two candidate UTC hours, the guard's target
# LOCAL hour, and the two UTC offsets. They have to agree, and nothing
# tells you when they stop agreeing -- the guard exits 0 on a skip, so a
# job that has silently stopped firing looks exactly like one that has
# correctly skipped its off-hour.
#
# That is not hypothetical. The window moved from US-Eastern to
# US-Pacific on 2026-08-21, and a partial edit that changed the guard
# without the cron would have left `0 6,7 * * *` paired with a target of
# 20:00 Eastern -- 06:00 UTC is 02:00 EDT, never 20 -- so neither the
# nightly shutdown nor the morning startup would ever have run again.
#
# The check needs no timezone database: each candidate hour must hit the
# target under exactly one of the two offsets, and both offsets must be
# used. A DST-guarded pair of cron hours has no other correct shape.
_CRON_RE = re.compile(r'^\s*cronExpression:\s*"0 ([0-9,]+) \* \* \*"', re.MULTILINE)
_TARGET_RE = re.compile(r'TARGET_LOCAL_HOUR="\$\{SWEEP_TARGET_LOCAL_HOUR:-(\d+)\}"')
_OFFSET_RE = re.compile(r'^\s*(?:STD|DST)_OFFSET_HOURS=(-?\d+)|OFFSET_HOURS=(-?\d+)\s+#', re.MULTILINE)


def check_cron_matches_guard(yaml_path: Path) -> bool:
    text = "\n".join(_read(yaml_path))

    cron = _CRON_RE.search(text)
    target = _TARGET_RE.search(text)
    offsets = sorted({int(a or b) for a, b in _OFFSET_RE.findall(text)})

    if not (cron and target and len(offsets) == 2):
        print(
            f"SKIP {yaml_path.name}: could not read cron/target/offsets "
            f"(cron={bool(cron)} target={bool(target)} offsets={offsets})",
            file=sys.stderr,
        )
        return False

    hours = sorted(int(h) for h in cron.group(1).split(","))
    want = int(target.group(1))

    if len(hours) != 2:
        print(
            f"BAD  {yaml_path.name}: cron names {len(hours)} candidate hours; "
            "a DST-guarded schedule needs exactly two",
            file=sys.stderr,
        )
        return False

    matched = {}
    for hour in hours:
        hits = [off for off in offsets if (hour + off) % 24 == want]
        if len(hits) != 1:
            print(
                f"BAD  {yaml_path.name}: cron hour {hour:02d}:00 UTC maps to "
                + " and ".join(f"{(hour + off) % 24:02d} (offset {off}h)" for off in offsets)
                + f" -- neither is the target local hour {want:02d}"
                if not hits else
                f"BAD  {yaml_path.name}: cron hour {hour:02d}:00 UTC hits the "
                f"target under BOTH offsets -- the job would fire twice",
                file=sys.stderr,
            )
            return False
        matched[hits[0]] = hour

    if len(matched) != 2:
        print(
            f"BAD  {yaml_path.name}: both cron hours resolve under offset "
            f"{next(iter(matched))}h -- the other half of the year never fires",
            file=sys.stderr,
        )
        return False

    detail = ", ".join(
        f"{hour:02d}:00 UTC @ {off}h" for off, hour in sorted(matched.items(), reverse=True)
    )
    print(f"OK   {yaml_path.name} cron reaches {want:02d}:00 local ({detail})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true",
        help="regenerate the YAML args block from the script",
    )
    args = parser.parse_args()

    ok = True
    for yaml_path, script in PAIRS:
        if not yaml_path.exists():
            print(f"missing: {yaml_path}", file=sys.stderr)
            return 2
        if not script.exists():
            print(f"missing: {script}", file=sys.stderr)
            return 2
        ok &= process(yaml_path, script, args.write)
        ok &= check_cron_matches_guard(yaml_path)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
