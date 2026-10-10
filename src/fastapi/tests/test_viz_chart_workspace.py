"""POST /v1/viz/chart runs under the caller's workspace, never the default tenant.

GIS audit 2026-10, finding 13. ``render_chart_endpoint`` took its workspace from
``request.state.workspace_id``, which nothing in the app sets (``extract_user_context``
returns a ``UserContext`` and leaves ``request.state`` alone), so every real-data
branch ran as ``LEGACY_DEFAULT_TENANT_UUID``. A caller in any other workspace got the
default tenant's rows - or, when that tenant had none, demo data - presented as
their own. The workspace now comes from the JWT through ``resolve_workspace_id``
(the way ``routers/coverage.py`` does it) and a request that cannot name one is
refused before any query runs.

No Postgres: ``scoped_connection`` is replaced by a recorder that notes the
workspace each query was scoped to and hands back a scripted connection.
"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.visualizations as viz
from app.services.auth import UserContext, extract_user_context, verify_service_key
from tests._stub_pool import StubConn

DEFAULT_TENANT = "a0000000-0000-0000-0000-000000000001"
WS_A = UUID("b0000000-0000-0000-0000-00000000000a")
WS_B = UUID("c0000000-0000-0000-0000-00000000000b")
PROJECT = UUID("019d74a1-fba8-7165-9ae6-a5bf93eef97d")

#: One body per branch of the real-data chain in render_chart_endpoint.
REAL_DATA_BODIES = {
    "long_section": {"chart_kind": "long_section", "project_id": str(PROJECT)},
    "anomaly_map": {"chart_kind": "anomaly_map", "project_id": str(PROJECT)},
    "harker_diagram": {"chart_kind": "harker_diagram", "project_id": str(PROJECT)},
    "grade_tonnage": {"chart_kind": "grade_tonnage", "project_id": str(PROJECT)},
    # Workspace-scoped: no project_id needed, so nothing in the body says "real data".
    "target_heatmap": {"chart_kind": "target_heatmap"},
}


class _ScopedRecorder:
    """Stands in for ``scoped_connection``: records the workspace of every scope."""

    def __init__(self) -> None:
        self.workspaces: list[str] = []
        self.conn = StubConn(
            fetch=[("silver.collars", []), ("silver.assays_v2", []), ("gold.h3_density_mineral", [])],
            fetchval=[("pg_extension", True), ("h3_density_mineral", True)],
        )

    def __call__(self, pool: Any, *, workspace_id: str, site: str = "unknown", **_: Any):  # type: ignore[no-untyped-def]
        self.workspaces.append(workspace_id)
        return self._scope()

    @asynccontextmanager
    async def _scope(self):  # type: ignore[no-untyped-def]
        yield self.conn


@pytest.fixture
def scoped(monkeypatch: pytest.MonkeyPatch) -> _ScopedRecorder:
    recorder = _ScopedRecorder()
    monkeypatch.setattr(viz, "scoped_connection", recorder)
    return recorder


def _client(user: UserContext | None) -> TestClient:
    """``user=None`` leaves ``extract_user_context`` real: no bearer token, no identity."""
    app = FastAPI()
    app.include_router(viz.router)
    app.state.pg_pool = object()
    app.state.redis_client = None
    app.dependency_overrides[verify_service_key] = lambda: None
    if user is not None:
        app.dependency_overrides[extract_user_context] = lambda: user
    return TestClient(app)


def _user(workspace: UUID | None) -> UserContext:
    return UserContext(user_id="7", workspace_id=str(workspace) if workspace else None)


@pytest.mark.parametrize("kind", sorted(REAL_DATA_BODIES))
def test_real_data_is_scoped_to_the_callers_workspace(kind: str, scoped: _ScopedRecorder) -> None:
    resp = _client(_user(WS_A)).post("/v1/viz/chart", json=REAL_DATA_BODIES[kind])

    assert resp.status_code == 200, resp.text
    assert scoped.workspaces, f"{kind}: expected a real-data query"
    assert set(scoped.workspaces) == {str(WS_A)}
    assert DEFAULT_TENANT not in scoped.workspaces


def test_two_callers_get_two_scopes(scoped: _ScopedRecorder) -> None:
    body = REAL_DATA_BODIES["long_section"]

    _client(_user(WS_A)).post("/v1/viz/chart", json=body)
    _client(_user(WS_B)).post("/v1/viz/chart", json=body)

    assert scoped.workspaces == [str(WS_A), str(WS_B)]


@pytest.mark.parametrize("kind", sorted(REAL_DATA_BODIES))
def test_no_bearer_token_is_refused_before_any_query(kind: str, scoped: _ScopedRecorder) -> None:
    resp = _client(None).post("/v1/viz/chart", json=REAL_DATA_BODIES[kind])

    assert resp.status_code == 401
    assert scoped.workspaces == []


@pytest.mark.parametrize("kind", sorted(REAL_DATA_BODIES))
def test_a_token_that_names_no_workspace_is_refused_not_defaulted(kind: str, scoped: _ScopedRecorder) -> None:
    resp = _client(_user(None)).post("/v1/viz/chart", json=REAL_DATA_BODIES[kind])

    assert resp.status_code == 403
    assert scoped.workspaces == [], "the default tenant must not be queried on the caller's behalf"


def test_demo_charts_still_render_and_touch_no_tenant_data(scoped: _ScopedRecorder) -> None:
    resp = _client(_user(WS_A)).post("/v1/viz/chart", json={"chart_kind": "ternary_diagram"})

    assert resp.status_code == 200, resp.text
    assert "data" in resp.json() and "layout" in resp.json()
    assert scoped.workspaces == []


def test_an_unknown_chart_kind_is_still_a_400(scoped: _ScopedRecorder) -> None:
    resp = _client(_user(WS_A)).post("/v1/viz/chart", json={"chart_kind": "not_a_chart"})

    assert resp.status_code == 400
    assert scoped.workspaces == []


def test_the_router_no_longer_carries_a_default_tenant_fallback() -> None:
    """Source pin: no code in the module can fall back to the default tenant again.

    Names are read from the syntax tree, so the docstring and comments are free to
    describe the old behaviour.
    """
    tree = ast.parse(Path(viz.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}

    assert "LEGACY_DEFAULT_TENANT_UUID" not in names
    assert "OptionalWorkspace" not in names, (
        "request.state.workspace_id is never populated, so OptionalWorkspace is always None"
    )
    assert {"resolve_workspace_id", "extract_user_context"} <= names
