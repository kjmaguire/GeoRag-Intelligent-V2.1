"""No SQL in src/fastapi/app may cast the workspace GUC to uuid bare.

`current_setting('app.workspace_id', true)::uuid` raises SQLSTATE 22P02 when
the GUC holds the '' sentinel -- what a pooled backend reports after a
transaction that set it has ended, and what Laravel's BindWorkspaceRlsContext
binds when it resolves no workspace. Without missing_ok it raises 42704 on a
backend that never set it. Either way the statement errors (and aborts the
enclosing transaction) instead of matching no rows.

Use `NULLIF(current_setting('app.workspace_id', true), '')::uuid`. The PHP half
of this guard, over the raw SQL files and migrations, is
tests/Unit/RawRlsEmptyGucCastTest.php.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

_GUC = r"current_setting\(\s*'{1,2}app\.workspace_id'{1,2}\s*(?:,\s*(?:true|false)\s*)?\)"
BARE_CAST = re.compile(rf"{_GUC}\s*::\s*uuid\b", re.IGNORECASE)
BARE_CAST_FUNCTION = re.compile(rf"\bcast\(\s*{_GUC}\s+as\s+uuid\b", re.IGNORECASE)


def _offenders(label: str, source: str) -> list[str]:
    found = []
    for pattern in (BARE_CAST, BARE_CAST_FUNCTION):
        for match in pattern.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            found.append(f"{label}:{line}: {' '.join(match.group(0).split())}")
    return found


def test_app_sql_never_casts_the_workspace_guc_to_uuid_bare() -> None:
    paths = sorted(APP_ROOT.rglob("*.py"))
    assert paths, f"no Python files under {APP_ROOT} -- the path is wrong"

    offenders = [
        hit
        for path in paths
        for hit in _offenders(str(path.relative_to(APP_ROOT.parent)), path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        "A bare workspace GUC cast raises 22P02 when the GUC is ''. Use "
        "NULLIF(current_setting('app.workspace_id', true), '')::uuid.\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "sql",
    [
        "current_setting('app.workspace_id', true)::uuid",
        "current_setting('app.workspace_id', TRUE)::UUID",
        "current_setting('app.workspace_id')::uuid",
        "current_setting('app.workspace_id', true) :: uuid",
        "current_setting('app.workspace_id', true)\n    ::uuid",
        "CAST(current_setting('app.workspace_id', true) AS uuid)",
    ],
)
def test_pattern_catches_bare_cast(sql: str) -> None:
    assert _offenders("fixture", sql)


@pytest.mark.parametrize(
    "sql",
    [
        "NULLIF(current_setting('app.workspace_id', true), '')::uuid",
        "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid",
        "current_setting('app.workspace_id', true) = ''",
    ],
)
def test_pattern_accepts_guarded_cast(sql: str) -> None:
    assert not _offenders("fixture", sql)
