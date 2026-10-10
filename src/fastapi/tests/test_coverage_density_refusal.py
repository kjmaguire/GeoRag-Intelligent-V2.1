"""GET /coverage/density turns the function's "extent too large" refusal into a 422.

GIS audit 2026-10, finding 14 (and the database audit's unbounded-grid finding).
``silver.coverage_density`` now raises SQLSTATE 54000 (``program_limit_exceeded``)
when a project's extent would need more cells than one local grid can honestly
be drawn in (one record at lon/lat 0,0 stretches it across the map). The route
must answer that with a 422 that carries the reason - not a 500, and not an
empty FeatureCollection, which would read as "no coverage here".

No Postgres: the connection is scripted. The function itself is exercised
against PostGIS in tests/test_coverage_density_pg.py.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.coverage as coverage
from app.services.auth import UserContext, extract_user_context, verify_service_key
from tests._stub_pool import StubConn, StubPool

WS = UUID("a0000000-0000-0000-0000-0000000014c0")
PROJECT = UUID("019d74a1-fba8-7165-9ae6-a5bf93eef97d")

_MESSAGE = (
    "coverage_density: the collars of this project span about 9596 x 7843 km, which needs "
    "about 1161867 cells of 10000 m; the limits are 200000 cells and 2000 km across"
)
_HINT = "Choose a larger cell_size_m, or look for a mis-located record."


class _RaisingConn(StubConn):
    """A connection whose fetch raises what asyncpg raises for the server's RAISE."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("fetch", sql, args))
        raise self._exc


def _server_refusal() -> asyncpg.exceptions.ProgramLimitExceededError:
    exc = asyncpg.exceptions.ProgramLimitExceededError(_MESSAGE)
    # asyncpg fills these from the error response; a constructed one has none.
    exc.message = _MESSAGE  # type: ignore[attr-defined]
    exc.hint = _HINT  # type: ignore[attr-defined]
    return exc


async def _resolve_ws(*_a: Any, **_k: Any) -> UUID:
    return WS


def _client(conn: StubConn, monkeypatch: pytest.MonkeyPatch, *, raise_server_exceptions: bool = True) -> TestClient:
    monkeypatch.setattr(coverage, "resolve_workspace_id", _resolve_ws)
    app = FastAPI()
    app.include_router(coverage.router)
    app.state.pg_pool = StubPool(conn)
    app.state.redis_client = None
    app.dependency_overrides[verify_service_key] = lambda: None
    app.dependency_overrides[extract_user_context] = lambda: UserContext(user_id="1", workspace_id=str(WS))
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_a_refused_extent_is_a_422_with_the_functions_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(_RaisingConn(_server_refusal()), monkeypatch)

    resp = client.get("/coverage/density", params={"project_id": str(PROJECT), "cell_size_m": 10000})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "coverage_extent_too_large"
    assert detail["message"] == _MESSAGE
    assert detail["hint"] == _HINT


def test_a_refusal_is_never_an_empty_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _client(_RaisingConn(_server_refusal()), monkeypatch).get(
        "/coverage/density", params={"project_id": str(PROJECT)}
    )

    assert "features" not in resp.json(), "an empty FeatureCollection would read as 'no coverage'"


def test_any_other_database_error_is_still_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the refusal is translated; a real failure must not be dressed up as a 422."""
    boom = asyncpg.exceptions.UndefinedFunctionError("function silver.coverage_density does not exist")
    client = _client(_RaisingConn(boom), monkeypatch, raise_server_exceptions=False)

    resp = client.get("/coverage/density", params={"project_id": str(PROJECT)})

    assert resp.status_code == 500
