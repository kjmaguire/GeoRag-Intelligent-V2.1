"""Regression guard: legacy `:Drillhole` (lowercase h) label must not appear
in production code.

The 2026-04-27 D2 migration canonicalised every drill-hole node to PascalCase
(`:DrillHole`, capital H — Global Invariant 4 of §04f). Any code writing the
legacy lowercase-h form is a regression because:

  * It bypasses the Cypher allowlist (`_validate_cypher_label`) when written
    from ingestion code rather than from a query path.
  * Constraints/indexes created against `:Drillhole` will never apply to the
    canonical `:DrillHole` nodes the migration produces.
  * Counters and traversals against `:Drillhole` silently return zero rows
    on a post-migration database, masking real coverage gaps.

This static test scans production Python and Cypher files for the legacy
form. It is exempt for:

  * Test files that *intentionally* assert the legacy form returns 0 rows or
    is rejected by validators.
  * The D2 migration script itself, which references both forms during the
    rename.
  * Documentation lines describing the legacy form (matched by keyword).

The test runs in the fast suite (no marker; typical wall time well under
1 s on this codebase).

Origin: caught 2026-05-07 in `gold_public_geoscience.py` after the D2
migration shipped without a CI guard. Five lines had drifted to the legacy
form across one file (constraint, two indexes, count cypher, docstring).

Architecture reference: §04f Global Invariant 4, docs/kyle-decisions.md D2,
ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher,
docs/04f-public-geoscience-addendum.md (D2-followup section).
"""

from __future__ import annotations

import re
from pathlib import Path

# Match the literal label form `:Drillhole` followed by a non-word character.
# The `\b` ensures we do not flag identifiers like `:Drillholes` or property
# names like `:DrillholeId` (neither is currently used, but the boundary makes
# the matcher resilient to future additions).
_LEGACY_LABEL_RE = re.compile(r":Drillhole\b")

# Paths (relative to repo root, POSIX form) that are allowed to reference the
# legacy form. Each entry needs a one-line justification — never add a
# production code path here.
_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        # The migration itself flips :Drillhole → :DrillHole and includes
        # rollback / pre-flight queries against the legacy form.
        "ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher",
        # Asserts that the legacy form returns 0 rows post-migration and that
        # the allowlist rejects it.
        "src/fastapi/tests/test_neo4j_drillhole_label.py",
        # Allowlist test pins the rejection of every wrong casing.
        "src/fastapi/tests/test_cypher_allowlist.py",
        # This regression test (defines the rule, must reference the form to
        # match against it).
        "src/fastapi/tests/test_no_legacy_drillhole_label.py",
    }
)

# A line is treated as documentation *about* the legacy form (rather than
# code *using* it) if any of these substrings appear, case-insensitive.
# Keep this list short — broadening it weakens the guard.
_LEGACY_DOC_MARKERS: tuple[str, ...] = (
    "legacy",
    "spelling",
    "lowercase",
    "rename",
    "deprecated",
)

# Directory components that should never be scanned regardless of where we
# are walking. Keeps virtualenvs, vendored deps, and build artefacts out.
_SKIP_DIR_PARTS: frozenset[str] = frozenset(
    {".venv", "venv", "__pycache__", "node_modules", ".git", "vendor"}
)

# Where to look. Only production-relevant directories. Tests under src/ are
# included so the exemption list above gates them, not the scan boundary.
_SCAN_SUBDIRS: tuple[tuple[str, ...], ...] = (
    ("src",),
    ("docker", "neo4j"),
    ("ops", "migrations", "neo4j"),
)

_FILE_GLOBS: tuple[str, ...] = ("*.py", "*.cypher")


def _repo_root() -> Path:
    """Walk up from this file's location until we find a directory that looks
    like the repo root (contains `composer.json` and `georag-architecture.html`).

    Doing the walk rather than hard-coding `parents[3]` keeps the test robust
    if someone restructures the test directory layout.

    Phase H — when this test is run from inside the FastAPI container
    (`docker compose exec fastapi pytest …`), `/app` is the bind-mounted
    `src/fastapi/` subset of the repo, so composer.json + architecture
    HTML simply aren't present at any ancestor. Skip rather than fail —
    the host-side test discipline (CI / pre-push hook) is where this
    regression test belongs. Raising in the container is a false-positive
    that pollutes the green-suite signal.
    """
    import pytest as _pytest  # noqa: PLC0415

    here = Path(__file__).resolve()
    for ancestor in (here, *here.parents):
        if (ancestor / "composer.json").is_file() and (
            ancestor / "georag-architecture.html"
        ).is_file():
            return ancestor
    _pytest.skip(
        f"_repo_root: no ancestor of {here} contains both composer.json "
        f"and georag-architecture.html — likely running inside the "
        f"fastapi container which only mounts src/fastapi/. This "
        f"regression scanner is host-side / CI-only."
    )


def _iter_candidate_files(root: Path) -> list[Path]:
    """Enumerate every file we will scan. Sorted for deterministic output."""
    out: list[Path] = []
    for parts in _SCAN_SUBDIRS:
        scan_dir = root.joinpath(*parts)
        if not scan_dir.is_dir():
            continue
        for glob in _FILE_GLOBS:
            for path in scan_dir.rglob(glob):
                if any(part in _SKIP_DIR_PARTS for part in path.parts):
                    continue
                out.append(path)
    return sorted(out)


def test_no_legacy_drillhole_label_in_production_code() -> None:
    """Scan production Python and Cypher for `:Drillhole` (legacy form).

    A non-empty offender list fails the test with file:line:content for each
    hit, plus a remediation pointer.
    """
    root = _repo_root()
    offenders: list[str] = []

    for path in _iter_candidate_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in _EXEMPT_PATHS:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary-ish or unreadable file — not our concern.
            continue

        for line_no, line in enumerate(text.splitlines(), start=1):
            if not _LEGACY_LABEL_RE.search(line):
                continue
            if any(marker in line.lower() for marker in _LEGACY_DOC_MARKERS):
                continue
            offenders.append(f"{rel}:{line_no}: {line.strip()}")

    assert not offenders, (
        "Legacy `:Drillhole` (lowercase h) label found in production code. "
        "The canonical form is `:DrillHole` (PascalCase) per §04f Global "
        "Invariant 4 and the 2026-04-27 D2 migration.\n\n"
        "Fix the offending line(s), or — only with a strong justification — "
        "add the path to `_EXEMPT_PATHS` in this test file.\n\n"
        "Offenders:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Self-tests for the test infrastructure itself. These guard against the
# regression test silently passing because its scanner is broken.
# ---------------------------------------------------------------------------


def test_repo_root_resolves() -> None:
    """The root finder returns a directory containing both sentinel files."""
    root = _repo_root()
    assert (root / "composer.json").is_file()
    assert (root / "georag-architecture.html").is_file()


def test_scan_finds_at_least_one_file() -> None:
    """If this fails the test is silently a no-op — guard against that."""
    files = _iter_candidate_files(_repo_root())
    assert len(files) > 10, (
        f"Scanner found only {len(files)} files. Either the codebase shrank "
        "drastically or the scan paths drifted from reality — investigate "
        "before trusting the regression test."
    )


def test_legacy_label_pattern_matches_known_form() -> None:
    """Sanity: the regex actually matches what we think it matches."""
    assert _LEGACY_LABEL_RE.search("MATCH (n:Drillhole) RETURN n")
    assert _LEGACY_LABEL_RE.search("FOR (d:Drillhole) REQUIRE d.pg_id IS UNIQUE")
    # Must NOT match the canonical form.
    assert not _LEGACY_LABEL_RE.search("MATCH (n:DrillHole) RETURN n")
    # Must NOT match identifiers that merely contain the substring.
    assert not _LEGACY_LABEL_RE.search("set DrillholeProp = 1")  # no leading colon
    # Word-boundary: ":Drillholes" (plural) should NOT trigger.
    assert not _LEGACY_LABEL_RE.search(":Drillholes")


def test_doc_marker_filter_skips_descriptive_lines() -> None:
    """Lines describing the legacy form must not be flagged."""
    descriptive_lines = (
        "# Renamed all live nodes from the legacy `:Drillhole` spelling.",
        "// :Drillhole is the deprecated lowercase form.",
        "    :Drillhole — the legacy form, retired by D2.",
    )
    for line in descriptive_lines:
        assert _LEGACY_LABEL_RE.search(line), "regex sanity"
        assert any(
            marker in line.lower() for marker in _LEGACY_DOC_MARKERS
        ), f"doc marker did not catch a clearly descriptive line: {line!r}"
