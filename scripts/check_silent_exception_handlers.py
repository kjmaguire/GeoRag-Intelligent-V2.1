#!/usr/bin/env python3
"""Ratchet on exception handlers that swallow without a word.

A handler that catches and then neither re-raises nor says anything is a
branch the system can take forever without anyone finding out. The 21 Aug
audit counted 215 of them; an AST pass over the current tree finds more,
because the tree grew.

WHY A RATCHET AND NOT A LINT RULE

ruff has S110 (try-except-pass) and BLE001 (blind except), and turning
either on would flag every existing site at once. The only ways to land
that are to fix 260-odd handlers in one change -- a diff nobody can review
against a pipeline whose failure modes are subtle -- or to sprinkle
`# noqa` over all of them, which converts a real signal into decoration.
Neither improves anything.

So this counts instead, per file, against a committed baseline. New
silent handlers fail the build; existing ones are visible, attributed, and
can be paid down file by file. The baseline is a debt register, not an
allowance: `--update` after a genuine reduction, never to make a red build
green.

WHAT COUNTS AS SILENT

An `except` clause whose body contains no `raise` and no call that could
plausibly tell anyone -- logging, warnings, Sentry capture, print. The
definition is deliberately generous: anything that even looks like it
reports (a name containing "log", "warn", "capture", "report") counts as
speaking. A generous definition means the number here is a FLOOR, and a
handler that trips this check is one nobody can argue about.

Usage:

    python scripts/check_silent_exception_handlers.py           # check
    python scripts/check_silent_exception_handlers.py --update  # re-baseline
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "scripts" / "silent_exception_handlers.json"

ROOTS = (
    "src/fastapi/app",
    "src/georag_geoparsers/georag_geoparsers",
    "src/georag_object_storage/georag_object_storage",
)

# Root object names whose method calls count as reporting.
REPORTING_ROOTS = frozenset(
    {"log", "logger", "logging", "warnings", "sentry_sdk", "traceback", "print"}
)
# Substrings in a called attribute that count as reporting, wherever the
# call hangs off. `self._log_failure(...)`, `ctx.log(...)`, `_report(...)`.
REPORTING_SUBSTRINGS = ("log", "warn", "capture", "report", "alert", "emit")


def _speaks(handler: ast.ExceptHandler) -> bool:
    """True if this handler re-raises or says something."""
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return True
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        attrs: list[str] = []
        while isinstance(func, ast.Attribute):
            attrs.append(func.attr)
            func = func.value
        root = func.id if isinstance(func, ast.Name) else ""
        if isinstance(func, ast.Name):
            attrs.append(func.id)

        if root in REPORTING_ROOTS:
            return True
        joined = " ".join(attrs).lower()
        if any(sub in joined for sub in REPORTING_SUBSTRINGS):
            return True
    return False


def _caught_name(handler: ast.ExceptHandler) -> str:
    """Readable name for what an except clause catches."""
    node = handler.type
    if node is None:
        return "bare except:"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Tuple):
        return "+".join(sorted(_caught_name_of(e) for e in node.elts))
    return "?"


def _caught_name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return "?"


def scan_breakdown() -> dict[str, int]:
    """Silent handlers grouped by what they catch.

    The bald total is not the finding. 262 sounds like 262 hidden bugs; in
    practice a third of them are `except ImportError` around an optional
    metrics import and another third catch a narrow ValueError/TypeError
    from a parse that has a sensible default. The ones that matter are the
    handlers that catch bare `Exception` and then say nothing -- those hide
    anything at all, including the failure you are looking for.
    """
    breakdown: dict[str, int] = {}
    for root in ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and not _speaks(node):
                    name = _caught_name(node)
                    breakdown[name] = breakdown.get(name, 0) + 1
    return breakdown


def scan() -> dict[str, int]:
    counts: dict[str, int] = {}
    for root in ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                print(f"WARNING: could not parse {path}: {exc}", file=sys.stderr)
                continue
            silent = sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.ExceptHandler) and not _speaks(node)
            )
            if silent:
                counts[path.relative_to(REPO_ROOT).as_posix()] = silent
    return counts


def main(argv: list[str]) -> int:
    current = scan()
    total = sum(current.values())

    if "--update" in argv:
        BASELINE_PATH.write_text(
            json.dumps(
                {
                    "_comment": (
                        "Per-file count of exception handlers that neither "
                        "re-raise nor report. A debt register, not an "
                        "allowance -- regenerate with `python "
                        "scripts/check_silent_exception_handlers.py --update` "
                        "only after a real reduction."
                    ),
                    "total": total,
                    "by_exception_caught": dict(
                        sorted(scan_breakdown().items(), key=lambda kv: -kv[1])
                    ),
                    "files": current,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="",
        )
        print(f"baseline written: {total} silent handlers across {len(current)} files")
        return 0

    if not BASELINE_PATH.is_file():
        print(f"missing baseline: {BASELINE_PATH}", file=sys.stderr)
        print("run with --update to create it", file=sys.stderr)
        return 2

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8")).get("files", {})

    regressions = [
        (path, baseline.get(path, 0), count)
        for path, count in sorted(current.items())
        if count > baseline.get(path, 0)
    ]
    improvements = sum(
        max(0, baseline.get(path, 0) - current.get(path, 0)) for path in baseline
    )

    breakdown = scan_breakdown()
    broad = breakdown.get("Exception", 0) + breakdown.get("bare except:", 0)
    print(f"silent exception handlers: {total} (baseline {sum(baseline.values())})")
    # The number that matters. A silent `except ImportError` around an
    # optional metrics import is not the same defect as a silent
    # `except Exception` wrapped around a database write.
    print(f"  of which catch bare Exception and say nothing: {broad}")
    if improvements:
        print(f"  {improvements} fewer than baseline -- re-run with --update")

    if not regressions:
        return 0

    print("\nNew silent handlers. Each catches and then tells nobody:", file=sys.stderr)
    for path, was, now in regressions:
        print(f"  {path}: {was} -> {now}", file=sys.stderr)
    print(
        "\nAdd a log line (logger.debug(..., exc_info=True) is enough) or a "
        "re-raise. If the handler genuinely has nothing to say, say that in a "
        "comment and log at debug anyway -- the next person debugging this "
        "path is the reason.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
