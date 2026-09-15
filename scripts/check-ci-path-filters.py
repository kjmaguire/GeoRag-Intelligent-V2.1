#!/usr/bin/env python3
"""A CI path filter must not skip a document CI reads as DATA.

Three workflows carry `paths-ignore` so a documentation-only push costs
nothing: ci.yml, codeql.yml and docker-build.yml. That is worth real money —
ci.yml alone bills 28 minutes per push on a private repository, and three of
eight pushes on 2026-09-15 were documentation only and ran all 24 checks.

The trap it opens is specific and silent. Two "documents" in this repository
are not prose, they are INPUT:

    deploy/aws/README.md        scripts/check-ecs-secret-keys.py parses its
                                key table, and scripts/operator/aws-preflight.sh
                                derives the go-live key list from the same
                                table rather than restating it. Editing that
                                table can genuinely break the build.
    georag-architecture.html    tests/Unit/ArchitectureDocSchemaParityTest.php
                                fails when the doc names a `schema.table` no
                                migration creates.

A filter that ignored either would let a real breakage through with a green
PR and nothing to look at — the same absence-as-success shape this repository
keeps finding. `*.md` is deliberately root-level only (GitHub path globs do
not cross `/`), and the architecture doc is `.html`, so neither is matched
today. This asserts that stays true.

It is NOT a general "is this file important" checker. It holds one list,
maintained by hand, of documents something in CI reads. Adding a new one
means adding it here.

Run:  python3 scripts/check-ci-path-filters.py
"""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Workflows that carry a docs-only filter.
WORKFLOWS = ("ci.yml", "codeql.yml", "docker-build.yml")

#: Documents CI parses as data, and what reads each. The reader is named so a
#: future maintainer can check the claim rather than trust it.
MACHINE_READ_DOCS = {
    "deploy/aws/README.md": (
        "scripts/check-ecs-secret-keys.py (key table) and "
        "scripts/operator/aws-preflight.sh (A-10 go-live key list)"
    ),
    "georag-architecture.html": (
        "tests/Unit/ArchitectureDocSchemaParityTest.php (schema.table parity)"
    ),
}

_PATHS_IGNORE = re.compile(r"^\s*paths-ignore:.*$", re.MULTILINE)
_ENTRY = re.compile(r"^\s*-\s*'([^']+)'\s*$")


def ignore_patterns(workflow: Path) -> list[str]:
    """Every glob under every `paths-ignore:` block in one workflow.

    Parsed line-by-line rather than with a YAML loader on purpose: PyYAML
    turns the `on:` key into the boolean True, and a checker that silently
    reads nothing because of that quirk would pass by finding no patterns —
    which is the failure mode this file exists to prevent, in itself.
    """
    lines = workflow.read_text(encoding="utf-8").splitlines()
    patterns: list[str] = []
    inside = False
    for line in lines:
        if _PATHS_IGNORE.match(line):
            inside = True
            continue
        if inside:
            match = _ENTRY.match(line)
            if match:
                patterns.append(match.group(1))
            elif line.strip() and not line.strip().startswith("#"):
                inside = False
    return patterns


def matches(pattern: str, path: str) -> bool:
    """GitHub path-glob semantics, narrowly.

    `**` crosses directory separators; a single `*` does not. fnmatch's `*`
    crosses `/`, which would make this checker MORE permissive than GitHub
    and report a match that will not happen — a false alarm, not a false
    pass, but still wrong. So a pattern without `**` is anchored per segment.
    """
    if "**" in pattern:
        return fnmatch.fnmatch(path, pattern.replace("**", "*"))
    if "/" in pattern:
        return fnmatch.fnmatch(path, pattern) and path.count("/") == pattern.count("/")
    # Root-level only: `*.md` must not match `deploy/aws/README.md`.
    return "/" not in path and fnmatch.fnmatch(path, pattern)


def main() -> int:
    failures: list[str] = []
    checked = 0

    for name in WORKFLOWS:
        workflow = REPO / ".github" / "workflows" / name
        if not workflow.is_file():
            failures.append(f"{name} does not exist")
            continue
        patterns = ignore_patterns(workflow)
        if not patterns:
            # Not an error. A workflow may legitimately carry no filter — but
            # say so, because "found nothing" and "checked nothing" read the
            # same in a green build otherwise.
            print(f"note  {name} has no paths-ignore block", file=sys.stderr)
            continue
        checked += 1
        for doc, reader in MACHINE_READ_DOCS.items():
            for pattern in patterns:
                if matches(pattern, doc):
                    failures.append(
                        f"{name} ignores {doc!r} via {pattern!r} — but it is read by "
                        f"{reader}. A change that breaks that would pass with a green PR."
                    )

    # A run that matched no workflow verifies nothing; say so rather than pass.
    if not checked:
        print(
            "FAIL  no workflow carried a paths-ignore block. Either the filters "
            "were removed (then remove this check too) or the parser stopped "
            "finding them (then this check has been passing by accident).",
            file=sys.stderr,
        )
        return 1

    if failures:
        for failure in failures:
            print(f"FAIL  {failure}", file=sys.stderr)
        return 1

    print(
        f"CI path filters: {checked} workflow(s) filtered; none skips any of the "
        f"{len(MACHINE_READ_DOCS)} document(s) CI parses as data."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
