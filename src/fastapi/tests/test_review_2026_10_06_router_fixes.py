"""Regression tests for the 2026-10-06 FastAPI review fixes.

* extract_user_context fails CLOSED when PyJWT cannot be imported (it used to
  return an empty UserContext, i.e. accept any Bearer string unverified).
* POST /internal/v1/mv-refresh/run rejects a malformed workspace_id / unknown
  triggered_by with a validation error instead of a 500 from `$2::uuid` or the
  gold.mv_refresh_log CHECK constraint.
* /ready reports exception CLASS names, not exception text (which can carry
  hosts / DSN fragments) to an unauthenticated caller.
* GET /v1/evidence graph_edge assembly no longer needs (or touches) a graph
  driver.
* `app.middleware` imports on its own (the package re-export used
  ``importlib.util`` without importing it).
"""

from __future__ import annotations

import builtins
import subprocess
import sys
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.evidence import _assemble_graph_edge
from app.routers.mv_refresh_trigger import MvRefreshRunInput
from app.services.auth import extract_user_context


@pytest.mark.asyncio
async def test_missing_pyjwt_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def _no_jwt(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if name == "jwt":
            raise ImportError("No module named 'jwt'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_jwt)
    req = MagicMock()
    req.url.path = "/v1/answer_runs/x/events"
    with pytest.raises(HTTPException) as exc_info:
        await extract_user_context(request=req, authorization="Bearer not.a.jwt")
    assert exc_info.value.status_code == 500


def test_mv_refresh_input_rejects_malformed_workspace_and_trigger() -> None:
    with pytest.raises(ValidationError):
        MvRefreshRunInput(workspace_id="not-a-uuid")
    with pytest.raises(ValidationError):
        MvRefreshRunInput(triggered_by="cron")


def test_mv_refresh_input_canonicalises_workspace_uuid() -> None:
    wid = uuid.uuid4()
    parsed = MvRefreshRunInput(workspace_id=str(wid).upper())
    assert parsed.workspace_id == str(wid)
    assert MvRefreshRunInput().workspace_id is None


@pytest.mark.asyncio
async def test_ready_does_not_leak_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app, ready

    class _BoomPool:
        def acquire(self):  # noqa: ANN202
            raise RuntimeError("connection to 10.0.3.7:5432 refused (password=hunter2)")

    class _BoomQdrant:
        async def get_collections(self):  # noqa: ANN202
            raise OSError("qdrant.internal:6333 unreachable")

    class _BoomRedis:
        async def ping(self):  # noqa: ANN202
            raise ConnectionError("redis.internal:6379 down")

    monkeypatch.setattr(app.state, "pg_pool", _BoomPool(), raising=False)
    monkeypatch.setattr(app.state, "qdrant_client", _BoomQdrant(), raising=False)
    monkeypatch.setattr(app.state, "redis_client", _BoomRedis(), raising=False)
    monkeypatch.setattr(app.state, "embedding_readiness", None, raising=False)

    with pytest.raises(HTTPException) as exc_info:
        await ready()
    checks = exc_info.value.detail["checks"]
    assert checks["postgres"] == "error: RuntimeError"
    assert checks["qdrant"] == "error: OSError"
    assert checks["redis"] == "error: ConnectionError"
    flat = repr(checks)
    assert "hunter2" not in flat and "10.0.3.7" not in flat


def test_graph_edge_evidence_is_assembled_without_a_graph_driver() -> None:
    evidence_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    payload = _assemble_graph_edge(
        {
            "evidence_id": evidence_id,
            "graph_edge_ref": '{"start_node_id": 1, "end_node_id": 2, "rel_type": "HAS_SAMPLE"}',
        },
        workspace_id,
    )
    assert payload.graph_edge_ref["rel_type"] == "HAS_SAMPLE"
    assert payload.start_node_labels is None and payload.described_in is None
    assert payload.workspace_id == workspace_id


def test_app_middleware_imports_standalone() -> None:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import app.middleware as m; m.BodySizeLimitMiddleware"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_queries_router_never_interpolates_the_workspace_claim_into_sql() -> None:
    import inspect

    import app.routers.queries as queries

    src = inspect.getsource(queries)
    assert "SET LOCAL app.workspace_id" not in src
    assert "bind_workspace_scope(" in src
