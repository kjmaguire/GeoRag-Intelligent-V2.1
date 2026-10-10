"""Audit 2026-10, query stream findings 5, 12, 13 and 21.

5   ExternalLlmEgressBlocked was reported as INTERNAL_ERROR.
12  a cancelled run was counted as "completed" in the query metrics.
13  every SSE frame awaited a Redis write that, with Redis down, stalled for
    seconds (the replay buffer nothing reads on the hot path).
21  a malformed context envelope was silently dropped, so the user's Field mode
    / data_sources restrictions vanished.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent.egress_gate import ExternalLlmEgressBlocked
from app.agent.errors import USER_MESSAGES, ErrorCode, classify_error
from app.agent.event_stamper import EventStamper
from app.config import settings
from app.models.rag import Citation, GeoRAGResponse
from app.routers import queries as q

PROJECT = "00000000-0000-0000-0000-0000000000aa"


def _response() -> GeoRAGResponse:
    return GeoRAGResponse(
        text="PLS-22-08 averages 0.21% U3O8 [DATA-1].",
        citations=[Citation(citation_id="[DATA-1]", citation_type="DATA", source_chunk_id="chunk-1",
                            document_title="Assay table", relevance_score=0.9)],
        confidence=0.9,
        sources_used=["chunk-1"],
    )


def _frames(chunks: list[str]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for chunk in chunks:
        if chunk.startswith(":"):
            continue
        name_line, data_line = chunk.strip().split("\n", 1)
        out.append((name_line.removeprefix("event: "), json.loads(data_line.removeprefix("data: "))))
    return out


def _state(redis: Any = None) -> SimpleNamespace:
    return SimpleNamespace(pg_pool=None, qdrant_client=None, embedding_model=None, redis_client=redis)


def _counter(outcome: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "georag_queries_total",
        {"tier": "unknown", "outcome": outcome, "backend": settings.LLM_BACKEND},
    ) or 0.0


# ---------------------------------------------------------------------------
# Finding 5 -- the egress gate has its own code
# ---------------------------------------------------------------------------
def test_an_egress_refusal_is_classified_as_egress_blocked_not_internal() -> None:
    code, message = classify_error(ExternalLlmEgressBlocked("w-1", "flag_not_set"))

    assert code is ErrorCode.EGRESS_BLOCKED
    assert code.value == "EGRESS_BLOCKED"
    assert "External LLM access is disabled" in message
    assert "unexpected error" not in message


@pytest.mark.parametrize("reason", ["missing_workspace", "flag_not_set", "flag_disabled", "db_error"])
def test_every_egress_reason_gets_the_same_code(reason: str) -> None:
    assert classify_error(ExternalLlmEgressBlocked(None, reason))[0] is ErrorCode.EGRESS_BLOCKED


def test_the_exceptions_own_message_wins_when_it_carries_one() -> None:
    exc = ExternalLlmEgressBlocked("w-1", "flag_disabled", user_message="Ask Kyle.")

    assert classify_error(exc)[1] == "Ask Kyle."


def test_the_three_copies_of_the_wording_agree() -> None:
    """errors.py, egress_gate.py and lang/en/guard_errors.php say the same
    thing; the PHP side is what the React layer renders for the guard code."""
    from app.agent import egress_gate

    assert USER_MESSAGES[ErrorCode.EGRESS_BLOCKED] == egress_gate._EGRESS_BLOCKED_USER_MESSAGE
    lang = Path(__file__).resolve().parents[3] / "lang" / "en" / "guard_errors.php"
    if lang.exists():
        php = lang.read_text(encoding="utf-8")
        assert "External LLM access is disabled for this workspace. '" in php
        assert "Contact your admin to enable.'" in php


def test_every_error_code_has_a_user_message() -> None:
    assert set(USER_MESSAGES) == set(ErrorCode)


def test_other_runtime_errors_are_still_internal() -> None:
    assert classify_error(RuntimeError("boom"))[0] is ErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_the_failed_frame_carries_the_egress_code_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent.orchestrator as orch

    async def blocked(**_kw: Any) -> GeoRAGResponse:
        raise ExternalLlmEgressBlocked("w-1", "flag_not_set")

    monkeypatch.setattr(orch, "run_deterministic_rag", blocked)
    monkeypatch.setattr(settings, "MULTI_TENANT_ENFORCEMENT_ENABLED", False, raising=False)

    class _Conn:
        def transaction(self) -> Any:
            class _Tx:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *_e: object) -> bool:
                    return False

            return _Tx()

        async def fetchrow(self, sql: str, *_a: Any) -> dict[str, Any]:
            if "lifecycle_state" in sql:
                return {"lifecycle_state": "active"}
            return {"workspace_id": None}

    class _Pool:
        def acquire(self) -> Any:
            class _Acq:
                async def __aenter__(self) -> _Conn:
                    return _Conn()

                async def __aexit__(self, *_e: object) -> bool:
                    return False

            return _Acq()

    from app.services.auth import UserContext

    state = _state()
    state.pg_pool = _Pool()
    request = SimpleNamespace(app=SimpleNamespace(state=state), state=SimpleNamespace())
    body = q.QueryRequest(query="how deep is PLS-22-08?", project_id=PROJECT)
    user = UserContext(user_id="u1", project_id=PROJECT, workspace_id=None, roles=())

    response = await q.post_query(body, request, user=user)
    chunks = [c async for c in response.body_iterator]

    name, data = _frames(chunks)[-1]
    assert name == "failed"
    assert data["code"] == "EGRESS_BLOCKED"
    assert "disabled for this workspace" in data["error"]


# ---------------------------------------------------------------------------
# Finding 12 -- a cancelled run is "cancelled"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_run_cancelled_by_a_client_disconnect_is_not_counted_as_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent.orchestrator as orch

    started = asyncio.Event()

    async def hangs(**kwargs: Any) -> GeoRAGResponse:
        await kwargs["status_callback"]("Retrieving…")
        started.set()
        await asyncio.sleep(30)
        return _response()

    monkeypatch.setattr(orch, "run_deterministic_rag", hangs)
    before_cancelled = _counter("cancelled")
    before_completed = _counter("completed")
    body = q.QueryRequest(query="how deep is PLS-22-08?", project_id=PROJECT)
    stream = q._agent_rag_stream(body, _state(), user=None, stamper=EventStamper(answer_run_id=uuid4()))

    seen = 0
    async for _chunk in stream:
        seen += 1
        if started.is_set():
            break
    await stream.aclose()  # what Starlette does when the client goes away

    assert _counter("cancelled") == before_cancelled + 1
    assert _counter("completed") == before_completed


@pytest.mark.asyncio
async def test_a_run_that_finishes_is_still_counted_as_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent.orchestrator as orch

    async def done(**_kw: Any) -> GeoRAGResponse:
        return _response()

    monkeypatch.setattr(orch, "run_deterministic_rag", done)
    before = _counter("completed")
    body = q.QueryRequest(query="how deep is PLS-22-08?", project_id=PROJECT)

    _ = [c async for c in q._agent_rag_stream(body, _state(), user=None,
                                              stamper=EventStamper(answer_run_id=uuid4()))]

    assert _counter("completed") == before + 1


# ---------------------------------------------------------------------------
# Finding 7 (router half) -- the query's own deadline is published
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_router_publishes_the_deadline_the_llm_calls_read(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent.orchestrator as orch
    from app.agent import llm_common

    seen: list[float | None] = []

    async def fake_run(**kwargs: Any) -> GeoRAGResponse:
        seen.append(llm_common.query_time_remaining_s())
        return GeoRAGResponse(
            text="Hole PLS-22-08 [DATA-1].",
            citations=[Citation(citation_id="[DATA-1]", citation_type="DATA", source_chunk_id="chunk-1",
                                document_title="Collars", relevance_score=0.9)],
            sources_used=["chunk-1"],
            confidence=0.9,
        )

    monkeypatch.setattr(orch, "run_deterministic_rag", fake_run)
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 100.0)
    body = q.QueryRequest(query="how deep is PLS-22-08?", project_id=PROJECT)

    _ = [c async for c in q._agent_rag_stream(body, _state(), user=None, stamper=EventStamper(answer_run_id=uuid4()))]

    assert len(seen) == 1 and seen[0] is not None
    assert 95.0 < seen[0] <= 100.0


# ---------------------------------------------------------------------------
# Finding 13 -- the replay buffer is never allowed to hold a frame up
# ---------------------------------------------------------------------------
class _HangingRedis:
    """Redis with the network down: every round trip outlasts any deadline."""

    def __init__(self) -> None:
        self.pipelines = 0

    def pipeline(self, transaction: bool = True) -> Any:
        self.pipelines += 1

        class _Pipe:
            def rpush(self, *_a: Any) -> None:
                return None

            def expire(self, *_a: Any) -> None:
                return None

            async def execute(self) -> None:
                await asyncio.sleep(30)

        return _Pipe()

    async def copy(self, *_a: Any, **_k: Any) -> bool:
        await asyncio.sleep(30)
        return True

    async def expire(self, *_a: Any, **_k: Any) -> bool:
        await asyncio.sleep(30)
        return True


@pytest.mark.asyncio
async def test_a_hung_redis_write_is_bounded_by_the_redis_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TIMEOUT_REDIS_S", 0.05)
    redis = _HangingRedis()
    stamper = EventStamper(answer_run_id=uuid4())

    t0 = time.monotonic()
    await stamper.push_to_redis(redis, "delta", {"token": "a", "event_seq": 1})

    assert time.monotonic() - t0 < 1.0
    assert redis.pipelines == 1


@pytest.mark.asyncio
async def test_after_one_failure_the_stream_stops_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TIMEOUT_REDIS_S", 0.05)
    redis = _HangingRedis()
    stamper = EventStamper(answer_run_id=uuid4())

    t0 = time.monotonic()
    for i in range(50):
        await stamper.push_to_redis(redis, "delta", {"token": str(i), "event_seq": i})
    await stamper.alias_to(redis, str(uuid4()))

    assert time.monotonic() - t0 < 1.0, "50 frames must cost ONE stall, not fifty"
    assert redis.pipelines == 1


@pytest.mark.asyncio
async def test_a_healthy_redis_keeps_being_written(monkeypatch: pytest.MonkeyPatch) -> None:
    ops: list[str] = []

    class _Pipe:
        def rpush(self, key: str, value: str) -> None:
            ops.append("rpush")

        def expire(self, key: str, ttl: int) -> None:
            ops.append("expire")

        async def execute(self) -> None:
            ops.append("execute")

    class _Redis:
        def pipeline(self, transaction: bool = True) -> _Pipe:
            return _Pipe()

    stamper = EventStamper(answer_run_id=uuid4())
    for i in range(3):
        await stamper.push_to_redis(_Redis(), "delta", {"token": str(i), "event_seq": i})

    assert ops.count("execute") == 3


@pytest.mark.asyncio
async def test_the_whole_stream_is_not_slowed_by_a_dead_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent.orchestrator as orch

    async def chatty(**kwargs: Any) -> GeoRAGResponse:
        for i in range(40):
            await kwargs["token_callback"](f"word{i} ")
        return _response()

    monkeypatch.setattr(orch, "run_deterministic_rag", chatty)
    monkeypatch.setattr(settings, "TIMEOUT_REDIS_S", 0.2)
    redis = _HangingRedis()
    body = q.QueryRequest(query="how deep is PLS-22-08?", project_id=PROJECT)

    t0 = time.monotonic()
    frames = _frames([c async for c in q._agent_rag_stream(
        body, _state(redis), user=None, stamper=EventStamper(answer_run_id=uuid4()))])
    elapsed = time.monotonic() - t0

    assert frames[-1][0] == "completed"
    assert sum(1 for name, _ in frames if name == "delta") == 40
    # Forty-odd frames at 0.2 s each would be 8+ s; one stall is 0.2 s.
    assert elapsed < 3.0
    assert redis.pipelines == 1


# ---------------------------------------------------------------------------
# Finding 21 -- a malformed envelope is a 422, never a silent drop
# ---------------------------------------------------------------------------
VALID_ENVELOPE = {"mode": "field", "data_sources": ["assays"], "reporting_code": "NI 43-101"}


def test_a_valid_envelope_is_accepted() -> None:
    body = q.QueryRequest(query="q", project_id=PROJECT, context_envelope=VALID_ENVELOPE)

    assert body.context_envelope == VALID_ENVELOPE


def test_no_envelope_is_accepted() -> None:
    assert q.QueryRequest(query="q", project_id=PROJECT).context_envelope is None


@pytest.mark.parametrize(
    ("envelope", "named"),
    [
        ({"data_sources": ["seismic"]}, "data_sources"),
        ({"mode": "tablet"}, "mode"),
        ({"crs_epsg": "not-a-number"}, "crs_epsg"),
        ({"reporting_code": "SEC S-K"}, "reporting_code"),
    ],
)
def test_a_malformed_envelope_is_rejected_and_names_the_field(envelope: dict, named: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        q.QueryRequest(query="q", project_id=PROJECT, context_envelope=envelope)

    message = str(exc_info.value)
    assert "context_envelope is not a valid envelope" in message
    assert named in message


def test_the_422_does_not_echo_the_envelope_back() -> None:
    with pytest.raises(ValidationError) as exc_info:
        q.QueryRequest(query="q", project_id=PROJECT,
                       context_envelope={"area_of_interest": "SECRET-LOCATION", "mode": "tablet"})

    assert "SECRET-LOCATION" not in "; ".join(e["msg"] for e in exc_info.value.errors())


@pytest.mark.asyncio
async def test_the_route_answers_422_and_never_starts_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from fastapi import FastAPI

    import app.agent.orchestrator as orch
    from app.services.auth import UserContext, extract_user_context, verify_service_key

    async def must_not_run(**_kw: Any) -> GeoRAGResponse:
        raise AssertionError("a request with an unreadable envelope must not reach the agent")

    monkeypatch.setattr(orch, "run_deterministic_rag", must_not_run)
    app = FastAPI()
    app.state.limiter = q.limiter
    app.include_router(q.router, prefix="/internal")
    app.dependency_overrides[verify_service_key] = lambda: None
    app.dependency_overrides[extract_user_context] = lambda: UserContext(
        user_id="u1", project_id=PROJECT, workspace_id=None, roles=())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/internal/queries",
            json={"query": "q", "project_id": PROJECT, "context_envelope": {"data_sources": ["seismic"]}},
        )

    assert resp.status_code == 422
    assert "data_sources" in resp.text
