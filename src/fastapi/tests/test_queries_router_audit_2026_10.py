"""Audit 2026-10-04 items 18 and 19: post_query's project guard and workspace.

19  the lifecycle guard fails CLOSED -- a database error is a typed 503, not a
    query that runs on a suspended or archived project.
18  the workspace is resolved once, from the project row, and lands on
    deps.workspace_id so document retrieval, the structured tools, persist and
    the cache key all agree.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.queries as q
from app.models.rag import Citation, GeoRAGResponse
from app.services.auth import UserContext

PROJECT = "00000000-0000-0000-0000-0000000000aa"
PROJECT_WS = "b0000000-0000-0000-0000-000000000002"
OTHER_WS = "c0000000-0000-0000-0000-000000000003"


class _Txn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self, *, lifecycle: str = "active", workspace: str | None = PROJECT_WS,
                 fail: Exception | None = None) -> None:
        self.lifecycle = lifecycle
        self.workspace = workspace
        self.fail = fail
        self.executed: list[str] = []

    def transaction(self) -> _Txn:
        return _Txn()

    async def execute(self, sql: str, *a: Any) -> None:
        self.executed.append(sql)

    async def fetchrow(self, sql: str, *a: Any) -> dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        if "lifecycle_state" in sql:
            return {"lifecycle_state": self.lifecycle}
        return {"workspace_id": self.workspace}


class _Pool:
    def __init__(self, conn: _Conn | None = None, acquire_error: Exception | None = None) -> None:
        self.conn = conn or _Conn()
        self.acquire_error = acquire_error

    def acquire(self) -> Any:
        pool = self

        class _Acq:
            async def __aenter__(self) -> _Conn:
                if pool.acquire_error is not None:
                    raise pool.acquire_error
                return pool.conn

            async def __aexit__(self, *exc: object) -> bool:
                return False

        return _Acq()


def _request(pool: Any | None = None, *, with_pool: bool = True) -> Any:
    state = SimpleNamespace()
    if with_pool:
        state.pg_pool = pool or _Pool()
    return SimpleNamespace(app=SimpleNamespace(state=state), state=SimpleNamespace())


def _body() -> Any:
    return SimpleNamespace(query="how deep is PLS-22-08?", project_id=PROJECT)


def _user(workspace_id: str | None = None) -> UserContext:
    return UserContext(
        user_id="u1", project_id=PROJECT, workspace_id=workspace_id, roles=(),
    )


@pytest.fixture(autouse=True)
def _no_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "MULTI_TENANT_ENFORCEMENT_ENABLED", False, raising=False)


# ---------------------------------------------------------------------------
# Item 19 -- fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("request_factory", [
    lambda: _request(_Pool(acquire_error=OSError("connection refused"))),
    lambda: _request(_Pool(_Conn(fail=RuntimeError("canceling statement due to timeout")))),
    lambda: _request(with_pool=False),
], ids=["acquire-fails", "query-fails", "no-pool-yet"])
async def test_a_failing_lifecycle_check_is_a_typed_503(request_factory: Any) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await q.post_query(_body(), request_factory(), user=_user())
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "project_lifecycle_check_unavailable"
    assert exc_info.value.headers == {"Retry-After": "5"}


@pytest.mark.asyncio
async def test_a_blocked_project_still_gets_its_own_status() -> None:
    request = _request(_Pool(_Conn(lifecycle="hibernated")))
    with pytest.raises(HTTPException) as exc_info:
        await q.post_query(_body(), request, user=_user())
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "project_hibernated"


@pytest.mark.asyncio
async def test_an_active_project_proceeds() -> None:
    response = await q.post_query(_body(), _request(), user=_user())
    assert response is not None


# ---------------------------------------------------------------------------
# Item 18 -- one workspace for every consumer
# ---------------------------------------------------------------------------


def _response() -> GeoRAGResponse:
    return GeoRAGResponse(
        text="PLS-22-08 is 372 m deep [DATA-1].",
        citations=[Citation(
            citation_id="[DATA-1]", citation_type="DATA", source_chunk_id="chunk-1",
            document_title="Collars", relevance_score=0.9,
        )],
        confidence=0.9, sources_used=["chunk-1"],
    )


async def _deps_seen_by_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch, *, user: UserContext | None,
    resolved_workspace_id: str | None,
) -> Any:
    import app.agent.orchestrator as orch

    seen: dict[str, Any] = {}

    async def fake_run(**kwargs: Any) -> GeoRAGResponse:
        seen["deps"] = kwargs["deps"]
        return _response()

    monkeypatch.setattr(orch, "run_deterministic_rag", fake_run)
    app_state = SimpleNamespace(
        pg_pool=None, qdrant_client=None, embedding_model=None, redis_client=None,
    )
    body = q.QueryRequest(query="how deep is PLS-22-08?", project_id=PROJECT)
    from app.agent.event_stamper import EventStamper

    _ = [
        c async for c in q._agent_rag_stream(
            body, app_state, user=user, stamper=EventStamper(answer_run_id=uuid4()),
            resolved_workspace_id=resolved_workspace_id,
        )
    ]
    return seen["deps"]


@pytest.mark.asyncio
async def test_missing_jwt_claim_falls_back_to_the_project_rows_workspace(monkeypatch) -> None:
    deps = await _deps_seen_by_the_orchestrator(
        monkeypatch, user=_user(None), resolved_workspace_id=PROJECT_WS,
    )
    assert deps.workspace_id == PROJECT_WS


@pytest.mark.asyncio
async def test_no_user_at_all_still_gets_the_project_workspace(monkeypatch) -> None:
    deps = await _deps_seen_by_the_orchestrator(
        monkeypatch, user=None, resolved_workspace_id=PROJECT_WS,
    )
    assert deps.workspace_id == PROJECT_WS


@pytest.mark.asyncio
async def test_a_jwt_claim_is_kept(monkeypatch) -> None:
    deps = await _deps_seen_by_the_orchestrator(
        monkeypatch, user=_user(PROJECT_WS), resolved_workspace_id=PROJECT_WS,
    )
    assert deps.workspace_id == PROJECT_WS


@pytest.mark.asyncio
async def test_nothing_resolvable_leaves_workspace_unset(monkeypatch) -> None:
    deps = await _deps_seen_by_the_orchestrator(
        monkeypatch, user=_user(None), resolved_workspace_id=None,
    )
    assert deps.workspace_id is None


@pytest.mark.asyncio
async def test_post_query_resolves_the_workspace_from_the_project_row(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_stream(body, app_state, user=None, stamper=None, resolved_workspace_id=None):
        captured["resolved"] = resolved_workspace_id
        yield "data: {}\n\n"

    monkeypatch.setattr(q, "_agent_rag_stream", fake_stream)
    response = await q.post_query(_body(), _request(), user=_user(None))
    _ = [c async for c in response.body_iterator]
    assert captured["resolved"] == PROJECT_WS


@pytest.mark.asyncio
async def test_a_jwt_workspace_that_does_not_own_the_project_is_rejected() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await q.post_query(_body(), _request(), user=_user(OTHER_WS))
    assert exc_info.value.status_code == 403
    assert "workspace" in exc_info.value.detail.lower()
