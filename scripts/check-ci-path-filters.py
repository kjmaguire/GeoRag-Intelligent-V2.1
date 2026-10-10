#!/usr/bin/env python3
"""A CI path filter must not skip a document CI reads as DATA.

Two workflows carry a docs-only path filter so a documentation-only push
costs nothing: ci.yml and docker-build.yml. That is worth real money —
ci.yml alone bills 28 minutes per push on a private repository, and three of
eight pushes on 2026-09-15 were documentation only and ran all 24 checks.

The trap it opens is specific and silent. Four "documents" in this repository
are not prose, they are INPUT:

    deploy/aws/README.md        scripts/check-ecs-secret-keys.py parses its
                                key table, and scripts/operator/aws-preflight.sh
                                derives the go-live key list from the same
                                table rather than restating it. Editing that
                                table can genuinely break the build.
    georag-architecture.html    tests/Unit/ArchitectureDocSchemaParityTest.php
                                fails when the doc names a `schema.table` no
                                migration creates.
    docs/architecture/manual/07-orchestration.md
                                src/fastapi/tests/test_cron_doc_parity.py
                                fails when its cron tables drift from the
                                workflows' `on_crons`.
    docs/adr/0014-workspace-lookup-and-pivot.md
                                src/fastapi/tests/test_lookup_and_rescope.py
                                fails when the ADR loses its option markers.

A filter that ignored any of them would let a real breakage through with a
green PR and nothing to look at — the same absence-as-success shape this
repository keeps finding. The first two sit outside `docs/**` (`*.md` is
root-level only, GitHub path globs do not cross `/`, and the architecture doc
is `.html`). The last two sit INSIDE it, and `paths-ignore` has no negation,
so the workflows use a `paths` filter instead: `!` patterns ignore, and a
later plain pattern puts a file back. Both forms are read here, with GitHub's
semantics: `paths-ignore` skips a file any pattern matches; `paths` runs on a
file unless the last pattern that matches it is a `!` one (or none does).

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

#: Workflows that carry a docs-only filter. codeql.yml was here until
#: 2026-09-15, when it was deleted — code scanning is not available for this
#: repository, so every language failed at the SARIF upload (Ch 07). A name
#: left here would fail this check on a missing file rather than on a bad
#: filter, which is a confusing way to learn a workflow is gone.
WORKFLOWS = ("ci.yml", "docker-build.yml")

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
    "docs/architecture/manual/07-orchestration.md": (
        "src/fastapi/tests/test_cron_doc_parity.py (cron tables vs on_crons)"
    ),
    "docs/adr/0014-workspace-lookup-and-pivot.md": (
        "src/fastapi/tests/test_lookup_and_rescope.py (ADR-0014 option markers)"
    ),
}

#: The line that opens a filter. `paths-ignore` skips a push whose files ALL
#: match a pattern; `paths` runs on a push if ANY file survives its patterns.
_BLOCK = re.compile(r"^\s*(paths-ignore|paths):\s*(?:#.*)?$")
_ENTRY = re.compile(r"^\s*-\s*'([^']+)'\s*$")


def filter_blocks(workflow: Path) -> list[tuple[str, list[str]]]:
    """Every `paths-ignore:` / `paths:` block in one workflow, as (kind, globs).

    One entry per block, NOT merged: a `paths` filter is evaluated in order,
    and ci.yml's `push` and `pull_request` filters are separate decisions.

    Parsed line-by-line rather than with a YAML loader on purpose: PyYAML
    turns the `on:` key into the boolean True, and a checker that silently
    reads nothing because of that quirk would pass by finding no patterns —
    which is the failure mode this file exists to prevent, in itself.
    """
    lines = workflow.read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[str, list[str]]] = []
    kind: str | None = None
    patterns: list[str] = []
    for line in lines:
        header = _BLOCK.match(line)
        if header:
            if kind:
                blocks.append((kind, patterns))
            kind, patterns = header.group(1), []
            continue
        if kind:
            match = _ENTRY.match(line)
            if match:
                patterns.append(match.group(1))
            elif line.strip() and not line.strip().startswith("#"):
                blocks.append((kind, patterns))
                kind = None
    if kind:
        blocks.append((kind, patterns))
    return [(k, p) for k, p in blocks if p]


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


def skipped_by(kind: str, patterns: list[str], path: str) -> str | None:
    """The pattern responsible when this filter skips `path`, else None.

    `paths-ignore`: skipped if any pattern matches. `paths`: GitHub walks the
    patterns in order — a matching plain pattern includes the file, a matching
    `!` pattern removes it again — so the LAST match decides, and a file no
    pattern includes is not run on.
    """
    if kind == "paths-ignore":
        return next((p for p in patterns if matches(p, path)), None)
    included, excluder = False, None
    for pattern in patterns:
        if pattern.startswith("!"):
            if matches(pattern[1:], path):
                included, excluder = False, pattern
        elif matches(pattern, path):
            included, excluder = True, None
    if included:
        return None
    return excluder or "(no pattern includes it)"


def main() -> int:
    failures: list[str] = []
    checked = 0

    for name in WORKFLOWS:
        workflow = REPO / ".github" / "workflows" / name
        if not workflow.is_file():
            failures.append(f"{name} does not exist")
            continue
        blocks = filter_blocks(workflow)
        if not blocks:
            # Not an error. A workflow may legitimately carry no filter — but
            # say so, because "found nothing" and "checked nothing" read the
            # same in a green build otherwise.
            print(f"note  {name} has no paths-ignore or paths block", file=sys.stderr)
            continue
        checked += 1
        for kind, patterns in blocks:
            for doc, reader in MACHINE_READ_DOCS.items():
                why = skipped_by(kind, patterns, doc)
                if why:
                    failures.append(
                        f"{name} ({kind}) ignores {doc!r} via {why!r} — but it is read by "
                        f"{reader}. A change that breaks that would pass with a green PR."
                    )

    # A run that matched no workflow verifies nothing; say so rather than pass.
    if not checked:
        print(
            "FAIL  no workflow carried a paths-ignore or paths block. Either the filters "
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
