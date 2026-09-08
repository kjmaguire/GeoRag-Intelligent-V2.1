"""RAG-quality audit 2026-08-14 — finding 5, and its 2026-09-08 sequel.

The DB CHECK constraint on ``silver.answer_runs.backend_used`` and the
FastAPI ``BackendLiteral`` are two independent sources of truth that have
drifted before ('ollama' lingered in the Literal after the DB CHECK dropped
it; 'azure' — the live default backend — was representable in neither).
This test parses the migration's ``VALUES`` constant directly so any future
edit to one side without the other fails CI immediately instead of
resurfacing as a live persist-time CheckViolationError.

That agreement check is necessary and was NOT sufficient. When ADR-0022
made ``bedrock`` the default backend, the Literal and the CHECK still
agreed perfectly — both were missing it — so this file stayed green while
every answer run would have persisted ``backend_used = 'unknown'``. No
constraint violation, no error, just the column quietly back to carrying no
information: the same defect finding 5 fixed, through a different door.
``test_configured_default_backend_is_representable`` closes that, by
reading the default out of config.py rather than off either list.

Run with:
    pytest tests/test_backend_enum_contract.py -v
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from app.models.answer_run import _KNOWN_BACKENDS, BackendLiteral, normalize_backend

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "database"
    / "migrations"
    / "2026_09_08_010000_extend_answer_runs_backend_check_for_bedrock.php"
)

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "app" / "config.py"


def _default_llm_backend() -> str:
    """Read ``LLM_BACKEND``'s default out of config.py's source.

    Parsed rather than imported on purpose. Importing ``app.config``
    instantiates ``Settings()`` at module scope, which needs
    FASTAPI_SERVICE_KEY and POSTGRES_PASSWORD in the environment — this
    file is a pure source-agreement test and stays runnable without any.
    It is the same technique the migration is read with, for the same
    reason: the value under test is a literal in a file, and reading the
    file is the most direct way to see it.
    """
    text = _CONFIG_PATH.read_text(encoding="utf-8")
    match = re.search(r'^    LLM_BACKEND: str = "([^"]+)"', text, re.MULTILINE)
    assert match, f"could not find the LLM_BACKEND default in {_CONFIG_PATH}"
    return match.group(1)


def _values_from_migration() -> set[str]:
    """Parse ``private const VALUES = "'a', 'b', ..."`` out of the migration."""
    text = _MIGRATION_PATH.read_text(encoding="utf-8")
    match = re.search(r"private const VALUES = \"(.*?)\";", text)
    assert match, f"could not find VALUES constant in {_MIGRATION_PATH}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_migration_file_exists() -> None:
    assert _MIGRATION_PATH.is_file(), (
        f"expected migration at {_MIGRATION_PATH} — the backend_used CHECK "
        "and BackendLiteral must be defined together"
    )


def test_backend_literal_matches_db_check() -> None:
    """BackendLiteral's members must be EXACTLY the migration's CHECK set.

    Not a subset check: an extra Literal value the DB rejects is just as
    much a drift bug as a DB value the Python side can't emit.
    """
    literal_values = set(get_args(BackendLiteral))
    check_values = _values_from_migration()
    assert literal_values == check_values, (
        f"BackendLiteral {literal_values} != migration CHECK {check_values} "
        "— update both together (src/fastapi/app/models/answer_run.py and "
        f"{_MIGRATION_PATH.name})"
    )


def test_known_backends_are_a_subset_of_the_literal() -> None:
    """_KNOWN_BACKENDS excludes 'unknown' itself (it's the fallback target)."""
    assert set(get_args(BackendLiteral)) > _KNOWN_BACKENDS
    assert "unknown" not in _KNOWN_BACKENDS


def test_normalize_backend_passes_through_known_values() -> None:
    for value in _KNOWN_BACKENDS:
        assert normalize_backend(value) == value


def test_normalize_backend_falls_back_to_unknown() -> None:
    assert normalize_backend(None) == "unknown"
    assert normalize_backend("") == "unknown"
    assert normalize_backend("ollama") == "unknown"  # dropped 2026_06_02_220000
    assert normalize_backend("some-future-backend") == "unknown"


def test_configured_default_backend_is_representable() -> None:
    """The DEFAULT backend must survive a round trip through persistence.

    Reads ``LLM_BACKEND``'s default out of config.py — deliberately not off
    either list under test, because a value missing from both is exactly
    what the agreement test above cannot see. If someone changes the
    default backend without extending the CHECK, this fails here rather
    than showing up months later as a column full of 'unknown'.
    """
    default_backend = _default_llm_backend()

    assert default_backend in _values_from_migration(), (
        f"LLM_BACKEND defaults to {default_backend!r}, which the "
        "answer_runs_backend_valid CHECK does not allow — add a migration "
        "extending the set"
    )
    assert normalize_backend(default_backend) == default_backend, (
        f"normalize_backend({default_backend!r}) returned "
        f"{normalize_backend(default_backend)!r}: the default backend is "
        "normalising away, so every answer run records the wrong value "
        "without raising"
    )
