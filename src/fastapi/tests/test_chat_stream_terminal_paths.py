"""The SSE producer's side of the chat terminal-path audit (CHAT-6/8/10/17/19).

Each test pins one way the chat used to end without an honest terminal
frame, or with the wrong id on it:

  - CHAT-6   silent phases emitted nothing the browser could see, so a slow
             first token tripped the 120 s idle watchdog;
  - CHAT-17  a run task that died without queueing a sentinel left the
             generator blocked forever;
  - CHAT-19  `completed` fell back to the streaming-session UUID when no
             run row existed, and a cache hit replayed the first asker's
             answer_run_id to every later asker;
  - CHAT-8   the replay ring buffer was written under an id the replay
             endpoint can never be called with;
  - CHAT-10  the Layer 1 hard refusal carried no refusal_payload unless
             REPAIR_LOOP_TERMINAL_ENABLED was on.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.agent.event_stamper import EventStamper
from app.models.rag import Citation, GeoRAGResponse
from app.routers import queries as q


def _response(answer_run_id: Any = None) -> GeoRAGResponse:
    return GeoRAGResponse(
        text="PLS-22-08 averages 0.21% U3O8 [DATA-1].",
        answer_run_id=answer_run_id,
        citations=[
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="chunk-1",
                document_title="Assay table",
                relevance_score=0.9,
            )
        ],
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


def _app_state() -> SimpleNamespace:
    return SimpleNamespace(
        pg_pool=None,
        qdrant_client=None,
        embedding_model=None,
        redis_client=None,
    )


# ---------------------------------------------------------------------------
# answer_run_id on the wire (CHAT-19)
# ---------------------------------------------------------------------------


class TestEffectiveAnswerRunId:
    def test_completed_without_a_persisted_run_carries_null(self) -> None:
        stamper = EventStamper(answer_run_id=uuid4())
        assert q._effective_answer_run_id("completed", {"answer_run_id": None}, stamper) is None

    def test_completed_with_a_persisted_run_carries_it(self) -> None:
        stamper = EventStamper(answer_run_id=uuid4())
        run_id = uuid4()
        assert q._effective_answer_run_id(
            "completed", {"answer_run_id": run_id}, stamper
        ) == str(run_id)

    def test_other_frames_keep_the_stream_id(self) -> None:
        stream = uuid4()
        stamper = EventStamper(answer_run_id=stream)
        assert q._effective_answer_run_id("delta", {"token": "x"}, stamper) == str(stream)
        assert q._effective_answer_run_id("status", {"message": "m"}, stamper) == str(stream)


# ---------------------------------------------------------------------------
# _next_stream_item (CHAT-6 / CHAT-17)
# ---------------------------------------------------------------------------


class TestNextStreamItem:
    async def test_times_out_to_a_heartbeat(self) -> None:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        run = asyncio.create_task(asyncio.sleep(10))
        try:
            assert await q._next_stream_item(queue, run, 0.01) is None
        finally:
            run.cancel()

    async def test_returns_a_queued_item(self) -> None:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        await queue.put(("status", "Synthesizing…"))
        run = asyncio.create_task(asyncio.sleep(10))
        try:
            assert await q._next_stream_item(queue, run, 5) == ("status", "Synthesizing…")
        finally:
            run.cancel()

    async def test_drains_what_a_finished_run_left_before_reporting_it_ended(self) -> None:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def run_body() -> None:
            await queue.put(("done", "result"))

        run = asyncio.create_task(run_body())
        await run
        assert await q._next_stream_item(queue, run, 5) == ("done", "result")
        assert await q._next_stream_item(queue, run, 5) == q._RUN_ENDED_SILENTLY

    async def test_a_run_that_dies_without_a_sentinel_is_reported_not_awaited_forever(self) -> None:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def run_body() -> None:
            await asyncio.sleep(0.01)
            raise asyncio.CancelledError  # a BaseException, not an Exception

        run = asyncio.create_task(run_body())
        item = await asyncio.wait_for(q._next_stream_item(queue, run, 30), timeout=5)
        assert item == q._RUN_ENDED_SILENTLY


# ---------------------------------------------------------------------------
# _agent_rag_stream end to end, orchestrator faked
# ---------------------------------------------------------------------------


async def _collect(
    monkeypatch: pytest.MonkeyPatch,
    fake_run: Any,
    stamper: EventStamper | None = None,
    app_state: SimpleNamespace | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    import app.agent.orchestrator as orch

    monkeypatch.setattr(orch, "run_deterministic_rag", fake_run)
    monkeypatch.setattr(q.settings, "SSE_HEARTBEAT_INTERVAL_S", 0.05, raising=False)
    body = q.QueryRequest(query="how deep is PLS-22-08?", project_id=str(uuid4()))
    chunks = [
        c
        async for c in q._agent_rag_stream(
            body,
            app_state or _app_state(),
            user=None,
            stamper=stamper or EventStamper(answer_run_id=uuid4()),
        )
    ]
    return _frames(chunks)


async def test_silent_phases_emit_status_heartbeats(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_run(**kwargs: Any) -> GeoRAGResponse:
        await kwargs["status_callback"]("Synthesizing answer…")
        await asyncio.sleep(0.3)
        return _response(answer_run_id=uuid4())

    frames = await _collect(monkeypatch, slow_run)

    heartbeats = [d for name, d in frames if name == "status" and d.get("heartbeat")]
    assert heartbeats, "no heartbeat during a silent phase"
    assert all(d["message"] == "Synthesizing answer…" for d in heartbeats)
    assert frames[-1][0] == "completed"


async def test_a_run_that_dies_silently_ends_with_a_failed_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    async def dying_run(**kwargs: Any) -> GeoRAGResponse:
        await asyncio.sleep(0.01)
        raise asyncio.CancelledError

    frames = await asyncio.wait_for(_collect(monkeypatch, dying_run), timeout=10)

    assert frames[-1][0] == "failed"
    assert frames[-1][1]["code"] == "INTERNAL_ERROR"


async def test_completed_without_a_run_row_carries_null_answer_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unpersisted_run(**kwargs: Any) -> GeoRAGResponse:
        return _response(answer_run_id=None)

    stamper = EventStamper(answer_run_id=uuid4())
    frames = await _collect(monkeypatch, unpersisted_run, stamper)

    name, completed = frames[-1]
    assert name == "completed"
    assert completed["answer_run_id"] is None
    assert completed["stream_id"] == str(stamper.answer_run_id)


# ---------------------------------------------------------------------------
# Replay buffer keyed where the replay endpoint reads (CHAT-8 / API-2)
# ---------------------------------------------------------------------------


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, tuple[Any, ...]]] = []

    def rpush(self, key: str, value: str) -> None:
        self.ops.append(("rpush", (key, value)))

    def expire(self, key: str, ttl: int) -> None:
        self.ops.append(("expire", (key, ttl)))

    async def execute(self) -> None:
        self.redis.round_trips += 1
        for op, args in self.ops:
            if op == "rpush":
                self.redis.lists.setdefault(args[0], []).append(args[1])


class _FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.round_trips = 0

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)

    async def copy(self, src: str, dst: str, replace: bool = False) -> bool:
        self.lists[dst] = list(self.lists.get(src, []))
        return True

    async def expire(self, key: str, ttl: int) -> bool:
        return True


async def test_push_is_one_round_trip_per_frame() -> None:
    redis = _FakeRedis()
    stamper = EventStamper(answer_run_id=uuid4())
    await stamper.push_to_redis(redis, "delta", {"token": "a"})
    await stamper.push_to_redis(redis, "delta", {"token": "b"})
    assert redis.round_trips == 2


async def test_completed_aliases_the_buffer_under_the_persisted_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    run_id = uuid4()

    async def persisted_run(**kwargs: Any) -> GeoRAGResponse:
        return _response(answer_run_id=run_id)

    state = _app_state()
    state.redis_client = redis
    await _collect(monkeypatch, persisted_run, app_state=state)

    persisted_key = "georag:answer_run_events:" + str(run_id)
    assert persisted_key in redis.lists, "replay by the persisted id would return []"
    names = [json.loads(e)["event_name"] for e in redis.lists[persisted_key]]
    assert names[0] == "status"
    assert names[-1] == "completed"


# ---------------------------------------------------------------------------
# Cache hit does not reuse another asker's run id (CHAT-19)
# ---------------------------------------------------------------------------


async def test_a_cache_hit_does_not_carry_the_first_askers_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agent.orchestrator as orch

    cached = _response(answer_run_id=uuid4())

    class _CacheRedis:
        async def get(self, key: str) -> str:
            return cached.model_dump_json()

    monkeypatch.setattr(orch.settings, "AGENTIC_RETRIEVAL_V2_ENABLED", True, raising=False)
    monkeypatch.setattr(orch, "_query_response_cache_key", lambda deps, query: "k")
    orch.set_active_context_envelope(None)
    orch.set_active_history(None)
    deps = SimpleNamespace(redis_client=_CacheRedis(), project_id=str(uuid4()))

    result = await orch.run_deterministic_rag(query="how deep?", deps=deps)  # type: ignore[arg-type]

    assert result.text == cached.text
    assert result.answer_run_id is None


# ---------------------------------------------------------------------------
# Layer 1 hard refusal is machine-readable (CHAT-10)
# ---------------------------------------------------------------------------


async def test_layer1_refusal_carries_refusal_payload_with_the_terminal_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent.llm_calls as llm_mod
    from app.agent.agentic_retrieval.nodes import assemble_node
    from app.agent.agentic_retrieval.state import AgenticRetrievalState
    from app.config import settings

    monkeypatch.setattr(settings, "REPAIR_LOOP_TERMINAL_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "RETRIEVAL_QUALITY_GATE_ENABLED", True, raising=False)

    async def must_not_call_llm(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("Layer 1 refused; the LLM must not be called")

    monkeypatch.setattr(llm_mod, "_call_llm", must_not_call_llm)

    state = AgenticRetrievalState(query="what is the grade on Mars?", deps=SimpleNamespace())
    update = await assemble_node(state)

    payload = update["response"].refusal_payload
    assert payload is not None
    assert payload["type"] == "refusal"
    assert payload["reason_code"] == "insufficient_evidence"


# ---------------------------------------------------------------------------
# 3D drill-trace card stays deliverable (CHAT-5, source side)
# ---------------------------------------------------------------------------


def test_trace_points_are_rounded_for_the_wire_only() -> None:
    from app.agent.agentic_retrieval.nodes import _round_trace_points_for_card

    raw = [{"x": -105.123456789012, "y": 42.987654321098, "z": 1834.56789, "depth_m": 12.3456789}]
    card = _round_trace_points_for_card(raw)

    assert card == [{"x": -105.1234568, "y": 42.9876543, "z": 1834.57, "depth_m": 12.35}]
    assert raw[0]["x"] == -105.123456789012, "the tool result itself must not be mutated"
    per_point = len(json.dumps(card[0])) / len(json.dumps(raw[0]))
    assert per_point < 0.85
