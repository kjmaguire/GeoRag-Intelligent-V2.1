#!/usr/bin/env python3
"""Every field on Settings must be read by something.

WHY
    A settings field with no reader is not harmless dead code. It is a
    control that looks like it works. An operator raising
    AZURE_FOUNDRY_MAX_TOKENS to widen the model's output ceiling gets no
    change at all, because the Foundry path reads LLM_MAX_OUTPUT_TOKENS —
    a differently-named setting. The knob turns; nothing behind it moves.

    22 of 141 fields were in that state when this check was written. Four
    were set in docker-compose.yml, and AZURE_FOUNDRY_MAX_TOKENS was set
    on the live fastapi-cc container app.

DELETING A FIELD IS A TWO-PART CHANGE, AND THE SECOND PART IS NOT OPTIONAL
    Settings sets extra="forbid". Measured behaviour of that setting:

      * an unknown OS ENVIRONMENT variable is ignored — pydantic-settings
        queries os.environ once per declared field, so a variable matching
        no field is never looked at;
      * an unknown key in a .env FILE is REJECTED — the file is read
        whole, so every key in it is a candidate, and an unmatched one
        raises at startup.

    So removing a field while .env.example still lists it does not produce
    dead config; it produces a crash for anyone who copied that file.
    Remove the field and its .env.example / .env.production.example /
    docker-compose.yml entries together.

Usage:
    python scripts/check_settings_have_readers.py
    python scripts/check_settings_have_readers.py --list   # names only
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FASTAPI = REPO / "src" / "fastapi"

# Directories searched for readers.
SEARCH_ROOTS = [
    FASTAPI / "app",
    FASTAPI / "tests",
    FASTAPI / "scripts",
    REPO / "scripts",
]

# config.py is NOT excluded, only its declaration LINES are (see
# has_reader). Excluding the whole file was this checker's first version
# and it was wrong in the expensive direction: Settings has computed
# properties, and `effective_max_context_tokens` reads
# MAX_CONTEXT_TOKENS_ANTHROPIC and MAX_CONTEXT_TOKENS_AZURE from inside
# the class. Both were reported dead, both were deleted, and 25 tests
# went red. A property on the same class is a reader like any other.
EXCLUDE_FILES: set[Path] = set()

# Lines matching this are declarations, not reads.
_DECLARATION = re.compile(r"^\s*[A-Z][A-Z0-9_]*\s*:")

# Fields with no in-tree reader for a reason that is not "we forgot".
# Every entry needs the reason, because the reason is the only thing
# separating this list from the bug it exists to permit.
ALLOWED_WITHOUT_READERS: dict[str, str] = {
    # Consumed by pydantic-settings itself / framework plumbing rather
    # than by application code.
}


def settings_fields() -> list[str]:
    """Field names on Settings, read from the source rather than imported.

    Importing app.config is the obvious approach and is wrong twice over.
    It constructs the module-level `settings` singleton, which requires
    FASTAPI_SERVICE_KEY and friends to be present — so the check would
    depend on CI's environment being fully populated to answer a question
    about static declarations. And Settings sets env_file=".env", a
    RELATIVE path: run from the repo root it reads the LARAVEL .env
    sitting there, whose keys match no field, and extra="forbid" raises on
    every one of them. The failure reads as "Settings is broken" and
    actually means "you were standing in the wrong directory".

    An AST walk needs no environment, no pydantic, and no cwd discipline.
    """
    import ast

    config = FASTAPI / "app" / "config.py"
    tree = ast.parse(config.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return sorted(
                statement.target.id
                for statement in node.body
                if isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id.isupper()
            )
    sys.exit(f"no `class Settings` found in {config}")


def has_reader(name: str) -> list[str]:
    """Files that mention `name`, excluding the declaration site.

    Deliberately a plain textual search rather than an AST walk: settings
    are read through getattr(settings, "NAME", default) and through
    os.environ in places, and an import-graph analysis would miss both.
    A false NEGATIVE here (calling a live field dead) is the expensive
    direction, so the search is broad on purpose.
    """
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")
    hits = []
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in (".py", ".sh", ".toml", ".cfg"):
                continue
            if path in EXCLUDE_FILES or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line in text.splitlines():
                if not pattern.search(line):
                    continue
                stripped = line.strip()
                # A comment that merely names the setting is not a
                # reader. This exact case produced the audit's one
                # false positive: QDRANT_DENSE_TOP_K appeared only in
                # "# ... (the QDRANT_DENSE_TOP_K_MAX setting)".
                if stripped.startswith("#"):
                    continue
                # Nor is the field's own declaration. Skipping the LINE
                # rather than the whole file is what lets a computed
                # property elsewhere in config.py still count.
                if _DECLARATION.match(stripped) and stripped.startswith(name):
                    continue
                hits.append(f"{path.relative_to(REPO)}:{stripped[:100]}")
                break
    return hits


def env_files_mentioning(name: str) -> list[str]:
    """Config files that would break if the field were deleted alone."""
    targets = [
        REPO / ".env.example",
        REPO / ".env.production.example",
        REPO / "docker-compose.yml",
    ]
    found = []
    pattern = re.compile(r"^\s*(?:-\s*)?" + re.escape(name) + r"\s*[:=]", re.MULTILINE)
    for path in targets:
        if path.exists() and pattern.search(
                path.read_text(encoding="utf-8", errors="ignore")):
            found.append(path.name)
    return found


def main() -> int:
    fields = settings_fields()
    if "--list" in sys.argv:
        print("\n".join(fields))
        return 0

    dead = []
    for name in fields:
        if name in ALLOWED_WITHOUT_READERS:
            continue
        if not has_reader(name):
            dead.append(name)

    print(f"Settings fields: {len(fields)}")
    print(f"Allowed without readers: {len(ALLOWED_WITHOUT_READERS)}")
    print(f"Unread: {len(dead)}")

    if not dead:
        print("\nEvery Settings field has a reader.")
        return 0

    print("\nFAIL: these fields are declared but nothing reads them.\n",
          file=sys.stderr)
    for name in dead:
        also = env_files_mentioning(name)
        suffix = ("  [also set in: " + ", ".join(also) + "]") if also else ""
        print(f"  - {name}{suffix}", file=sys.stderr)
    print(
        "\nA field with no reader is a control that looks like it works.\n"
        "Either wire it up, or delete it TOGETHER WITH its entries in the\n"
        "files noted above — Settings sets extra=\"forbid\", so a key left\n"
        "behind in a .env file is a startup crash, not dead config.",
        file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
