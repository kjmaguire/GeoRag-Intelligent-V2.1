"""Unit and integration tests for the projects router.

Tests
-----
  Unit tests (no live DB) — mock asyncpg and verify handler behaviour:
    - 404 when project row is missing
    - 403 when user has no project_user pivot row
    - Happy-path ProjectRead for an authorised user
    - Happy-path collar list (with and without status filter)
    - Empty collar list when project has no collars

  Integration tests — hit the live FastAPI endpoint (require Docker stack):
    - Marked with @pytest.mark.integration

Architecture references
-----------------------
  Section 07d  — Laravel<->FastAPI contracts (route shapes)
  Section 06   — timeout values
"""

from __future__ import annotations

import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.geological import CollarRead, ProjectRead
from app.routers.projects import router
from app.services.auth import UserContext

# Module 9 Chunk 9.4 — Bearer JWT for TestClient calls (extract_user_context
# now raises 401 on missing Authorization for non-probe paths). The HS256
# secret matches what FastAPI is running with; conftest._mint_test_jwt is
# the canonical mint path for integration tests.
from tests.conftest import (  # type: ignore[import-not-found]
    SERVICE_KEY as _SERVICE_KEY,
    _mint_test_jwt as _mint_jwt,
)
_BEARER = f"Bearer {_mint_jwt()}"


# ---------------------------------------------------------------------------
# Test app fixture (unit tests — no live DB required)
# ---------------------------------------------------------------------------

def _make_test_app() -> FastAPI:
    """Create a minimal FastAPI app with the projects router for unit testing."""
    app = FastAPI()

    # Bypass service-key auth for unit tests.
    from app.routers.projects import router as projects_router  # noqa: PLC0415
    from fastapi import APIRouter  # noqa: PLC0415
    # Re-mount without the verify_service_key dependency by patching it.
    app.include_router(projects_router, prefix="/internal")
    return app


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_PROJECT_ID = UUID("019d74a1-fba8-7165-9ae6-a5bf93eef97d")
_USER_ID = "42"

_PROJECT_ROW: dict[str, Any] = {
    "project_id": _PROJECT_ID,
    "project_name": "Athabasca Basin Uranium",
    "crs_datum": "EPSG:32613",
    "company": "Fission Uranium Corp",
    "magnetic_declination": 5.2,
    "orientation_reference": "BOH",
    "commodity": "Uranium",
    "region": "Saskatchewan, Canada",
    "status": "active",
    "slug": "athabasca-basin-uranium",
    "created_at": datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc),
    "updated_at": datetime.datetime(2026, 1, 2, 0, 0, 0, tzinfo=datetime.timezone.utc),
}

_COLLAR_ROW: dict[str, Any] = {
    "collar_id": UUID("12345678-1234-5678-1234-567812345678"),
    "hole_id": "PLS-22-001",
    "project_id": _PROJECT_ID,
    "easting": 448200.0,
    "northing": 6174300.0,
    "elevation": 520.0,
    "total_depth": 350.0,
    "hole_type": "Diamond",
    "azimuth": 225.0,
    "dip": -60.0,
    "drill_date": datetime.date(2022, 6, 15),
    "status": "Completed",
}


# ---------------------------------------------------------------------------
# Helper: mock asyncpg connection that simulates pivot check + data row
# ---------------------------------------------------------------------------

def _make_conn_mock(
    pivot_row: dict | None,
    data_row: dict | None = None,
    data_rows: list[dict] | None = None,
) -> MagicMock:
    """Return a mock asyncpg connection that responds to fetchrow / fetch.

    ``pivot_row``  — what the project_user SELECT returns (None = no access)
    ``data_row``   — what fetchrow returns for the project SELECT
    ``data_rows``  — what fetch returns for the collar SELECT
    """
    conn = MagicMock()

    # Track calls so we can distinguish the pivot check from the data query.
    call_order: list[str] = []

    async def _fetchrow(query: str, *args, **kwargs):  # noqa: ARG001
        call_order.append("fetchrow")
        if "project_user" in query:
            return pivot_row
        # Second call is the project data query.
        return data_row

    async def _fetch(query: str, *args, **kwargs):  # noqa: ARG001
        return data_rows or []

    conn.fetchrow = _fetchrow
    conn.fetch = _fetch

    # asyncpg connection as async context manager.
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    return conn


def _make_pool_mock(conn: MagicMock) -> MagicMock:
    """Wrap a mock conn inside a mock asyncpg pool."""
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)
    return pool


# ---------------------------------------------------------------------------
# Helpers for calling the endpoint with mocked state
# ---------------------------------------------------------------------------

def _call_get_project(
    project_id: UUID,
    conn: MagicMock,
    user: UserContext | None = None,
) -> Any:
    """Call GET /internal/projects/{project_id} with a mocked pool.

    Module 9 Chunk 9.4 — extract_user_context now reads request.url.path
    and raises 401 on missing Authorization for /internal/* paths. The
    `patch.object(auth_mod, ...)` approach doesn't reach FastAPI's
    Depends-captured reference. We use `app.dependency_overrides` instead,
    which IS the FastAPI-supported override mechanism.
    """
    app = _make_test_app()
    app.state.pg_pool = _make_pool_mock(conn)

    from app.services.auth import (  # noqa: PLC0415
        extract_user_context,
        verify_service_key,
    )

    resolved_user = user or UserContext()
    app.dependency_overrides[verify_service_key] = lambda: None
    app.dependency_overrides[extract_user_context] = lambda: resolved_user

    try:
        client = TestClient(app, raise_server_exceptions=True)
        return client.get(f"/internal/projects/{project_id}")
    finally:
        app.dependency_overrides.clear()


def _call_get_collars(
    project_id: UUID,
    conn: MagicMock,
    user: UserContext | None = None,
    params: dict | None = None,
) -> Any:
    """Call GET /internal/projects/{project_id}/collars with a mocked pool.

    Uses app.dependency_overrides per Module 9 Chunk 9.4 (see _call_get_project).
    """
    app = _make_test_app()
    app.state.pg_pool = _make_pool_mock(conn)

    from app.services.auth import (  # noqa: PLC0415
        extract_user_context,
        verify_service_key,
    )

    resolved_user = user or UserContext()
    app.dependency_overrides[verify_service_key] = lambda: None
    app.dependency_overrides[extract_user_context] = lambda: resolved_user

    try:
        client = TestClient(app, raise_server_exceptions=True)
        return client.get(
            f"/internal/projects/{project_id}/collars",
            params=params or {},
        )
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Unit tests — GET /internal/projects/{project_id}
# ---------------------------------------------------------------------------


class TestGetProject:
    def test_404_when_project_row_missing(self) -> None:
        """Authorised user but the project row doesn't exist."""
        pivot_row = {"role": "member"}  # access granted
        conn = _make_conn_mock(pivot_row=pivot_row, data_row=None)
        user = UserContext(user_id=_USER_ID, project_id=str(_PROJECT_ID))

        resp = _call_get_project(_PROJECT_ID, conn, user)

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_403_when_no_pivot_row(self) -> None:
        """User has no project_user row — must get 403, not 404."""
        conn = _make_conn_mock(pivot_row=None, data_row=_PROJECT_ROW)
        user = UserContext(user_id=_USER_ID, project_id=str(_PROJECT_ID))

        resp = _call_get_project(_PROJECT_ID, conn, user)

        assert resp.status_code == 403
        assert "access" in resp.json()["detail"].lower()

    def test_happy_path_authorised_user(self) -> None:
        """Authorised user with a valid project row — returns 200 ProjectRead."""
        pivot_row = {"role": "owner"}
        conn = _make_conn_mock(pivot_row=pivot_row, data_row=_PROJECT_ROW)
        user = UserContext(user_id=_USER_ID, project_id=str(_PROJECT_ID))

        resp = _call_get_project(_PROJECT_ID, conn, user)

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == str(_PROJECT_ID)
        assert body["project_name"] == "Athabasca Basin Uranium"
        assert body["status"] == "active"
        assert body["slug"] == "athabasca-basin-uranium"

    def test_no_jwt_skips_pivot_check(self) -> None:
        """Service-key-only request (no JWT user_id) skips the pivot check."""
        # pivot_row=None would normally cause 403, but no user_id means skip.
        conn = _make_conn_mock(pivot_row=None, data_row=_PROJECT_ROW)
        user = UserContext()  # no user_id

        resp = _call_get_project(_PROJECT_ID, conn, user)

        assert resp.status_code == 200
        assert resp.json()["project_id"] == str(_PROJECT_ID)


# ---------------------------------------------------------------------------
# Unit tests — GET /internal/projects/{project_id}/collars
# ---------------------------------------------------------------------------


class TestGetProjectCollars:
    def test_403_when_no_pivot_row(self) -> None:
        """User has no project_user row — 403 before touching collar data."""
        conn = _make_conn_mock(pivot_row=None)
        user = UserContext(user_id=_USER_ID, project_id=str(_PROJECT_ID))

        resp = _call_get_collars(_PROJECT_ID, conn, user)

        assert resp.status_code == 403

    def test_empty_list_when_no_collars(self) -> None:
        """Authorised user, project exists, but no collars yet."""
        pivot_row = {"role": "member"}
        conn = _make_conn_mock(pivot_row=pivot_row, data_rows=[])
        user = UserContext(user_id=_USER_ID, project_id=str(_PROJECT_ID))

        resp = _call_get_collars(_PROJECT_ID, conn, user)

        assert resp.status_code == 200
        assert resp.json() == []

    def test_happy_path_returns_collars(self) -> None:
        """Authorised user — returns list of CollarRead dicts."""
        pivot_row = {"role": "member"}
        conn = _make_conn_mock(pivot_row=pivot_row, data_rows=[_COLLAR_ROW])
        user = UserContext(user_id=_USER_ID, project_id=str(_PROJECT_ID))

        resp = _call_get_collars(_PROJECT_ID, conn, user)

        assert resp.status_code == 200
        collars = resp.json()
        assert len(collars) == 1
        assert collars[0]["hole_id"] == "PLS-22-001"
        assert collars[0]["easting"] == pytest.approx(448200.0)
        assert collars[0]["status"] == "Completed"

    def test_no_jwt_skips_pivot_check_for_collars(self) -> None:
        """Service-key-only request skips pivot check for collar endpoint."""
        conn = _make_conn_mock(pivot_row=None, data_rows=[_COLLAR_ROW])
        user = UserContext()  # no user_id

        resp = _call_get_collars(_PROJECT_ID, conn, user)

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_status_filter_param_accepted(self) -> None:
        """Valid status query param is accepted (400 check for invalid values)."""
        pivot_row = {"role": "member"}
        conn = _make_conn_mock(pivot_row=pivot_row, data_rows=[])
        user = UserContext(user_id=_USER_ID)

        resp = _call_get_collars(_PROJECT_ID, conn, user, params={"status": "Completed"})

        assert resp.status_code == 200

    def test_invalid_status_filter_rejected(self) -> None:
        """Invalid status query param value is rejected with 422."""
        pivot_row = {"role": "member"}
        conn = _make_conn_mock(pivot_row=pivot_row, data_rows=[])
        user = UserContext(user_id=_USER_ID)

        resp = _call_get_collars(_PROJECT_ID, conn, user, params={"status": "Invalid"})

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Integration tests — require live Docker stack
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestProjectsIntegration:
    """Integration tests that hit http://localhost:8000.

    Run with:
        cd src/fastapi
        python -m pytest tests/test_projects_router.py -m integration -v
    """

    @pytest.mark.asyncio
    async def test_get_project_403_or_404_for_unknown_uuid(self, rag_client) -> None:
        """Unknown UUID returns 403 (no pivot row) — Module 9 Chunk 9.4 closes
        the enumeration-friendly 404 path. The pivot check fires before the
        project-row lookup, so any UUID the caller has no access to maps to
        403 regardless of whether the row exists. 404 is still acceptable in
        single-tenant deployments where the pivot check is skipped.
        """
        unknown_id = uuid4()
        resp = await rag_client.get(f"/internal/projects/{unknown_id}")
        assert resp.status_code in (403, 404)

    @pytest.mark.asyncio
    async def test_get_project_collars_for_unknown_project(self, rag_client) -> None:
        """Unknown project UUID — accept 403 (no access), 404 (not found),
        or 200+[] (single-tenant skip path).
        """
        unknown_id = uuid4()
        resp = await rag_client.get(f"/internal/projects/{unknown_id}/collars")
        assert resp.status_code in (200, 403, 404)
        if resp.status_code == 200:
            assert resp.json() == []
