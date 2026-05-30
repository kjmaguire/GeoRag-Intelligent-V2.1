"""Unit tests for app.middleware.project_lifecycle — CC-03 Item 8.

Tests
-----
  Unit tests (no live DB) — mock asyncpg.Connection and verify:
    - require_active_project passes silently for 'active' state
    - require_active_project raises HTTPException(403, "project_hibernated")
      for hibernated projects
    - require_active_project raises HTTPException(403, "project_archived")
      for archived projects
    - require_active_project raises HTTPException(402, "project_past_due")
      for past_due projects
    - require_active_project raises HTTPException(404, "project_not_found")
      when the project row is missing
    - Unknown lifecycle states are rejected with 403

Architecture references
-----------------------
  CC-03 Item 8 — project hibernation / soft freeze
  CLAUDE.md    — asyncpg for PostgreSQL; no synchronous drivers
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(lifecycle_state: str | None) -> MagicMock:
    """Return a mock asyncpg.Connection that returns the given lifecycle_state.

    When *lifecycle_state* is None, ``fetchrow`` returns None (simulating a
    missing project row).
    """
    conn = MagicMock()

    async def _fetchrow(query: str, *args: Any, **kwargs: Any):  # noqa: ARG001
        if lifecycle_state is None:
            return None
        row = MagicMock()
        row.__getitem__ = MagicMock(
            side_effect=lambda key: lifecycle_state if key == "lifecycle_state" else None
        )
        return row

    conn.fetchrow = _fetchrow
    # asyncpg connections are used as async context managers in some callers;
    # these tests call the helper directly so __aenter__/__aexit__ are not
    # invoked, but mock them anyway for completeness.
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_project_passes() -> None:
    """An active project should return without raising."""
    from app.middleware.project_lifecycle import require_active_project

    conn = _make_conn("active")
    project_id = str(uuid4())

    # Must not raise.
    await require_active_project(project_id=project_id, conn=conn)


@pytest.mark.asyncio
async def test_hibernated_project_raises_403() -> None:
    """A hibernated project must raise HTTPException(403, 'project_hibernated')."""
    from fastapi import HTTPException

    from app.middleware.project_lifecycle import require_active_project

    conn = _make_conn("hibernated")
    project_id = str(uuid4())

    with pytest.raises(HTTPException) as exc_info:
        await require_active_project(project_id=project_id, conn=conn)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "project_hibernated"


@pytest.mark.asyncio
async def test_archived_project_raises_403() -> None:
    """An archived project must raise HTTPException(403, 'project_archived')."""
    from fastapi import HTTPException

    from app.middleware.project_lifecycle import require_active_project

    conn = _make_conn("archived")
    project_id = str(uuid4())

    with pytest.raises(HTTPException) as exc_info:
        await require_active_project(project_id=project_id, conn=conn)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "project_archived"


@pytest.mark.asyncio
async def test_past_due_project_raises_402() -> None:
    """A past_due project must raise HTTPException(402, 'project_past_due')."""
    from fastapi import HTTPException

    from app.middleware.project_lifecycle import require_active_project

    conn = _make_conn("past_due")
    project_id = str(uuid4())

    with pytest.raises(HTTPException) as exc_info:
        await require_active_project(project_id=project_id, conn=conn)

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail == "project_past_due"


@pytest.mark.asyncio
async def test_missing_project_raises_404() -> None:
    """A project row that does not exist must raise HTTPException(404)."""
    from fastapi import HTTPException

    from app.middleware.project_lifecycle import require_active_project

    conn = _make_conn(None)  # fetchrow returns None
    project_id = str(uuid4())

    with pytest.raises(HTTPException) as exc_info:
        await require_active_project(project_id=project_id, conn=conn)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "project_not_found"


@pytest.mark.asyncio
async def test_unknown_lifecycle_state_raises_403() -> None:
    """An unrecognised lifecycle state must raise HTTPException(403) — fail closed.

    The DB CHECK constraint should prevent this reaching production, but
    defensive coding requires us to fail closed rather than allow access.
    """
    from fastapi import HTTPException

    from app.middleware.project_lifecycle import require_active_project

    conn = _make_conn("suspended_for_audit")  # not a valid DB value
    project_id = str(uuid4())

    with pytest.raises(HTTPException) as exc_info:
        await require_active_project(project_id=project_id, conn=conn)

    assert exc_info.value.status_code == 403
    assert "project_lifecycle_unknown" in exc_info.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state, expected_status, expected_detail",
    [
        ("active", None, None),
        ("hibernated", 403, "project_hibernated"),
        ("archived", 403, "project_archived"),
        ("past_due", 402, "project_past_due"),
    ],
)
async def test_lifecycle_parametrised(
    state: str,
    expected_status: int | None,
    expected_detail: str | None,
) -> None:
    """Parametrised coverage of all four valid lifecycle states."""
    from fastapi import HTTPException

    from app.middleware.project_lifecycle import require_active_project

    conn = _make_conn(state)
    project_id = str(uuid4())

    if expected_status is None:
        # active — no exception
        await require_active_project(project_id=project_id, conn=conn)
    else:
        with pytest.raises(HTTPException) as exc_info:
            await require_active_project(project_id=project_id, conn=conn)
        assert exc_info.value.status_code == expected_status
        assert exc_info.value.detail == expected_detail
