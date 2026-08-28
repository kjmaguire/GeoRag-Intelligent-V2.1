#!/usr/bin/env python3
"""Count modules under src/fastapi/app that nothing imports.

WHY THIS EXISTS
    A 2026-08-22 audit found roughly 3,400 lines across ten modules with no
    importer anywhere in app/, tests/ or scripts/ -- on top of ~1,700 lines
    of hallucination layers and ~630 lines of orchestrator prompt modules
    already deleted the same week. Dead code is cheap on its own; what it
    costs is belief. `services/dispatchers/pagerduty.py` has a full passing
    test file, so the CI signal reads "PagerDuty alerting is verified
    working" when nothing dispatches to it.

    Deleting them is a decision with a blast radius (several have tests, and
    CLAUDE.md puts test removal behind approval). Measuring them is not. This
    reports the count so the number either shrinks or is seen not to.

WHAT COUNTS AS DEAD
    A module with no `import` or `from ... import` referencing it anywhere in
    the searched trees, other than from itself. Deliberately crude: the point
    is a trend line, not a proof. Dynamic imports, plugin registries and
    entry-points all defeat it, which is why the exempt list exists and why
    this reports rather than gates.

    Package `__init__.py` files are skipped -- importing the package imports
    them, so they are never orphans in this sense, and their contents are
    covered by whether the package itself is imported.

Exit code is always 0. This is instrumentation.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "src" / "fastapi" / "app"
#: Where a PRODUCTION importer can live. Deliberately not tests/.
#:
#: A module imported only by its own test file is exactly the case this
#: report exists for. The worked example was services/dispatchers/pagerduty.py:
#: a full passing test suite and no caller, so CI read as "PagerDuty alerting
#: verified" while nothing dispatched to it. It was deleted 2026-08-28, which
#: is the outcome this rule exists to produce. Counting a test as an importer
#: would hide the next orphan whose tests make it look alive.
APP_ROOTS = [APP, REPO / "src" / "fastapi" / "scripts"]

#: Searched separately, only to annotate an orphan as test-covered.
TEST_ROOTS = [REPO / "src" / "fastapi" / "tests"]

#: Files that name a module without importing it -- uvicorn targets,
#: container commands, workflow steps. `app.embedding_service` and
#: `app.sparse_service` are real services launched this way and appear in
#: no import statement anywhere.
DEPLOY_SURFACES = [
    REPO / "docker-compose.yml",
    REPO / "docker",
    REPO / "deploy",
    REPO / "charts",
    REPO / ".github",
]

#: Modules that are legitimately uncalled, with the reason.
#:
#: Each entry is a claim someone made deliberately. An entry whose module has
#: since gained an importer is reported as stale, because a stale exemption is
#: how the next orphan hides.
EXEMPT: dict[str, str] = {
    "app/services/qdrant_fallback.py": (
        "documented NOT WIRED with a stated prerequisite (a GIN trigram "
        "index on silver.document_passages that no migration creates); "
        "wiring it without that turns a Qdrant outage into a Postgres one"
    ),
    "app/services/target_recommendation/sme_content/athabasca_uranium.py": (
        "NOT dead -- a dynamic-import target. sme_content/seed_runner.py "
        "resolves SME content with importlib.import_module(module_path) and "
        "its own docstring names this module as the example path; "
        "sme_content/__main__.py passes it on the command line. This is the "
        "exact blind spot the module docstring warns about, and it is listed "
        "here so a future cleanup pass does not delete a reachable module "
        "because a static scan could not see the call"
    ),
    "app/services/silver_dq_flag_writer.py": (
        "documented NOT WIRED, and its own banner records the decision: "
        "'Kept rather than deleted because the helper itself is complete and "
        "correct, and the rules are a real roadmap item rather than "
        "abandoned work.' The five rule families it serves lived in the "
        "Dagster asset graph, deleted 2026-08-28, so silver.data_quality_flags "
        "now has no writer on any path"
    ),
    "app/services/ingest/csv_collar_ingester.py": (
        "documented NO PRODUCTION CALLERS, kept pending an owner decision "
        "its own banner states: Laravel's UploadController still 422s "
        "category=collar/category=assay uploads, and this is the obvious "
        "module to wire a restored direct-upload path to. The banner is "
        "explicit that if that is not the plan, this file and its two test "
        "modules should be deleted -- that call belongs to the SME, not to a "
        "cleanup pass"
    ),
}


def module_name(path: Path) -> str:
    """`app/services/foo.py` -> `app.services.foo`."""
    relative = path.relative_to(APP.parent)
    return ".".join(relative.with_suffix("").parts)


def _package_of(path: Path) -> str:
    """Dotted package a file lives in, e.g. `app.services.ingest`."""
    try:
        relative = path.relative_to(APP.parent)
    except ValueError:
        return ""
    return ".".join(relative.parent.parts)


def imported_names(path: Path) -> set[str]:
    """Every dotted name this file imports, absolute and relative alike.

    RELATIVE IMPORTS ARE THE WHOLE REASON THIS IS NOT THREE LINES
        `ast.ImportFrom` records `from .ocr_quality import x` as
        `module="ocr_quality"`, `level=1` -- a bare name that matches no
        module path. Storing it unresolved made this report claim 50
        orphans across 15,655 lines, `plan_executor.py` and
        `ocr_quality.py` among them, both of which are imported daily.

        This tree uses relative + function-local imports heavily (the
        `# noqa: PLC0415` pattern throughout ingest), so ignoring `level`
        does not lose an edge case, it loses most of the graph.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()

    package = _package_of(path)
    names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from . import x` inside app.services.ingest -> the
                # package itself; `from ..db import y` -> one level up.
                parts = package.split(".") if package else []
                base = ".".join(parts[: len(parts) - (node.level - 1)]) if parts else ""
                prefix = f"{base}.{node.module}" if node.module else base
            else:
                prefix = node.module or ""

            if not prefix:
                continue
            names.add(prefix)
            for alias in node.names:
                names.add(f"{prefix}.{alias.name}")

    return names


def main() -> int:
    candidates = [
        path for path in sorted(APP.rglob("*.py"))
        # __init__.py comes in with its package; __main__.py is an entry
        # point by definition and is never imported by anything.
        if path.name not in {"__init__.py", "__main__.py"}
        and "__pycache__" not in path.parts
    ]

    def scan(roots: list[Path]) -> set[str]:
        found: set[str] = set()
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                found |= imported_names(path)
        return found

    all_imports = scan(APP_ROOTS)
    test_imports = scan(TEST_ROOTS)

    # Entry points named as strings rather than imported.
    deploy_text = ""
    for surface in DEPLOY_SURFACES:
        if surface.is_file():
            deploy_text += surface.read_text(encoding="utf-8", errors="replace")
        elif surface.is_dir():
            for path in surface.rglob("*"):
                if path.is_file() and path.suffix in {
                    ".yml", ".yaml", ".sh", ".Dockerfile", ".json", "",
                }:
                    deploy_text += path.read_text(encoding="utf-8", errors="replace")

    def reachable_in(name: str, imports: set[str]) -> bool:
        # `from app.services import foo` records both `app.services` and
        # `app.services.foo`, so a prefix check covers either form.
        return name in imports or any(
            imported.startswith(name + ".") for imported in imports
        )

    orphans: list[tuple[str, int, bool]] = []
    for path in candidates:
        name = module_name(path)
        if reachable_in(name, all_imports):
            continue
        # Launched rather than imported (uvicorn target, container command).
        if name in deploy_text:
            continue
        relative = path.relative_to(REPO / "src" / "fastapi").as_posix()
        if relative in EXEMPT:
            continue
        lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        orphans.append((relative, lines, reachable_in(name, test_imports)))

    orphans.sort(key=lambda item: -item[1])
    total = sum(lines for _, lines, _ in orphans)

    print("### Dead-module report")
    print()
    print(f"**{len(orphans)} module(s), {total} lines, with no importer.**")
    print()
    if orphans:
        print("| module | lines | has tests |")
        print("| --- | ---: | --- |")
        for relative, lines, tested in orphans[:25]:
            print(f"| `{relative}` | {lines} | {'yes' if tested else 'no'} |")
        if len(orphans) > 25:
            print(f"| … and {len(orphans) - 25} more | | |")
        print()
        tested_count = sum(1 for _, _, tested in orphans if tested)
        if tested_count:
            print(
                f"**{tested_count} of these have passing tests and no "
                "production caller.** That is the worst shape on this list: "
                "green CI reads as a verified capability the platform does "
                "not have."
            )
            print()
        print(
            "Crude by design — dynamic imports and registries defeat it. "
            "Confirm before deleting, and record a deliberate exception in "
            "`EXEMPT` in this script rather than deleting the check."
        )
    else:
        print("Nothing orphaned.")

    # Stale exemptions: an entry that now HAS an importer.
    #
    # A package re-export does not count. `dispatchers/__init__.py` used to
    # carry `from app.services.dispatchers.pagerduty import
    # create_pagerduty_incident` purely to widen the package surface, and that
    # made this check report the pagerduty exemption stale while the module
    # still had no caller -- the exact orphan the report exists to surface,
    # marked resolved by the fact that it sat next to an `__init__.py`. (Both
    # are gone as of 2026-08-28; the rule stays, because the next package to
    # grow an `__init__` re-export would hit it again.) Only importers OUTSIDE
    # the module's own package are evidence of use.
    #
    # This narrower rule is applied here and NOT to the orphan scan above,
    # where a package `__init__` that imports its siblings is often the real
    # registry (`agents/phase0/`), and discounting it would report a large
    # number of live modules as dead.
    def imported_from_outside_own_package(path: Path, name: str) -> bool:
        own_init = path.parent / "__init__.py"
        for root in APP_ROOTS:
            if not root.exists():
                continue
            for source in root.rglob("*.py"):
                if "__pycache__" in source.parts or source == own_init:
                    continue
                if source == path:
                    continue
                if reachable_in(name, imported_names(source)):
                    return True
        return False

    stale = []
    for relative in EXEMPT:
        path = REPO / "src" / "fastapi" / relative
        if not path.exists():
            stale.append(f"{relative} (file gone)")
            continue
        name = module_name(path)
        if imported_from_outside_own_package(path, name):
            stale.append(f"{relative} (now imported by app/)")

    if stale:
        print()
        print("**Stale exemptions — these are no longer orphans:**")
        print()
        for entry in stale:
            print(f"- `{entry}`")

    return 0


if __name__ == "__main__":
    sys.exit(main())
