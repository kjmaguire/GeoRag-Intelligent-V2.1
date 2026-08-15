"""RAG-quality audit 2026-08-14 — finding 5.

The DB CHECK constraint on ``silver.answer_runs.backend_used`` and the
FastAPI ``BackendLiteral`` are two independent sources of truth that have
drifted before ('ollama' lingered in the Literal after the DB CHECK dropped
it; 'azure' — the live default backend — was representable in neither).
This test parses the migration's ``VALUES`` constant directly so any future
edit to one side without the other fails CI immediately instead of
resurfacing as a live persist-time CheckViolationError.

Run with:
    pytest tests/test_backend_enum_contract.py -v
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from app.models.answer_run import BackendLiteral, _KNOWN_BACKENDS, normalize_backend

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "database"
    / "migrations"
    / "2026_08_14_010000_extend_answer_runs_backend_check.php"
)


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
    assert _KNOWN_BACKENDS < set(get_args(BackendLiteral))
    assert "unknown" not in _KNOWN_BACKENDS


def test_normalize_backend_passes_through_known_values() -> None:
    for value in _KNOWN_BACKENDS:
        assert normalize_backend(value) == value


def test_normalize_backend_falls_back_to_unknown() -> None:
    assert normalize_backend(None) == "unknown"
    assert normalize_backend("") == "unknown"
    assert normalize_backend("ollama") == "unknown"  # dropped 2026_06_02_220000
    assert normalize_backend("some-future-backend") == "unknown"
