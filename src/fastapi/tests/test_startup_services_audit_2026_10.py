"""Audit 2026-10, startup and shared-service findings 13 (client half), 14, 17.

13  the Redis client was built without ``retry=``; redis-py 7's default is three
    retries with exponential jittered backoff, so one Redis blip stalled every
    command for seconds despite ``socket_timeout=0.5``.
14  the lifespan embedding warm-up ran synchronously on the event loop with the
    ingest retry profile, so Cohere throttling could outlast the ECS health grace.
17  clients built from ``qdrant_client_kwargs()`` fell back to httpx's 5 s timeout.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from typing import Any

import numpy as np
import pytest

from app.config import settings
from app.services import embedding as emb
from app.services.qdrant_conn import DEFAULT_CLIENT_TIMEOUT_S, qdrant_client_kwargs


# ---------------------------------------------------------------------------
# Finding 13 -- the Redis client does not retry
# ---------------------------------------------------------------------------
def test_the_redis_client_is_built_with_no_retries() -> None:
    from app.main import build_redis_client

    client = build_redis_client()

    assert client.get_retry() is not None
    assert client.get_retry().get_retries() == 0


def test_the_redis_client_keeps_its_socket_bounds_and_database() -> None:
    from app.main import build_redis_client

    kwargs = build_redis_client().connection_pool.connection_kwargs

    assert kwargs["db"] == 2
    assert kwargs["client_name"] == "georag-fastapi"
    assert kwargs["socket_timeout"] == settings.TIMEOUT_REDIS_S
    assert kwargs["socket_connect_timeout"] == settings.TIMEOUT_REDIS_S


@pytest.mark.asyncio
async def test_a_dead_redis_fails_fast_instead_of_backing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing listens on port 1. With redis-py's default Retry the command
    spent a jittered 0 to 10 s three times over before raising."""
    from redis.exceptions import RedisError

    from app.main import build_redis_client

    monkeypatch.setattr(settings, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "REDIS_PORT", 1)
    client = build_redis_client()

    t0 = time.monotonic()
    with pytest.raises(RedisError):
        await client.ping()
    elapsed = time.monotonic() - t0
    await client.aclose()

    assert elapsed < 1.5, f"a dead Redis cost {elapsed:.1f}s per command"


def test_lifespan_builds_its_client_through_the_factory() -> None:
    from app import main

    source = inspect.getsource(main.lifespan)

    assert "build_redis_client()" in source
    assert "aioredis.Redis(" not in source


# ---------------------------------------------------------------------------
# Finding 14 -- the warm-up is off the loop and bounded
# ---------------------------------------------------------------------------
class _SlowModel:
    """encode() blocks its thread, like a throttled Cohere ingest call."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.calls = 0

    def encode(self, *_a: Any, **_k: Any) -> np.ndarray:
        self.calls += 1
        time.sleep(self.seconds)
        return np.zeros(1024, dtype=np.float32)


@pytest.mark.asyncio
async def test_a_slow_warm_up_is_abandoned_and_leaves_the_loop_free() -> None:
    model = _SlowModel(0.6)
    readiness = emb.EmbeddingReadiness()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    t0 = time.monotonic()
    ok = await emb.warm_up_within(model, readiness, timeout_s=0.1)
    waited = time.monotonic() - t0
    task.cancel()

    assert ok is False
    assert waited < 0.5, "startup stopped waiting at the timeout"
    assert ticks >= 5, "the event loop kept running while the warm-up was blocked in its thread"
    assert readiness.state == emb.EMBEDDING_WARMING
    assert not readiness.ready
    assert "exceeded" in (readiness.detail or "")
    assert readiness.failures == 1


@pytest.mark.asyncio
async def test_a_warm_up_that_finishes_late_still_marks_the_embedder_ready() -> None:
    model = _SlowModel(0.25)
    readiness = emb.EmbeddingReadiness()

    assert await emb.warm_up_within(model, readiness, timeout_s=0.05) is False
    await asyncio.sleep(0.5)  # the abandoned thread completes in the background

    assert readiness.ready


@pytest.mark.asyncio
async def test_a_fast_warm_up_reports_success() -> None:
    readiness = emb.EmbeddingReadiness()

    assert await emb.warm_up_within(_SlowModel(0.0), readiness, timeout_s=5.0) is True
    assert readiness.ready


class _BothSurfaces:
    def __init__(self) -> None:
        self.encode_calls = 0
        self.query_calls: list[str] = []

    def encode(self, *_a: Any, **_k: Any) -> np.ndarray:
        self.encode_calls += 1
        return np.zeros(1024, dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        self.query_calls.append(text)
        return np.zeros(1024, dtype=np.float32)


def test_the_warm_up_exercises_the_query_path_when_there_is_one() -> None:
    model = _BothSurfaces()
    readiness = emb.EmbeddingReadiness()

    assert emb.warm_up_once(model, readiness) is True
    assert model.query_calls == ["warm-up"]
    assert model.encode_calls == 0, "encode is the ingest retry profile"


def test_a_model_without_embed_query_is_warmed_with_encode() -> None:
    model = _SlowModel(0.0)
    readiness = emb.EmbeddingReadiness()

    assert emb.warm_up_once(model, readiness) is True
    assert model.calls == 1


def test_a_failing_query_path_warm_up_is_recorded_not_raised() -> None:
    class _Down:
        def embed_query(self, _text: str) -> np.ndarray:
            raise RuntimeError("HTTP 429 from Cohere embed")

    readiness = emb.EmbeddingReadiness()

    assert emb.warm_up_once(_Down(), readiness) is False
    assert readiness.state == emb.EMBEDDING_WARMING
    assert readiness.failures == 1


def test_the_lifespan_uses_the_bounded_warm_up_and_a_threaded_sparse_warm_up() -> None:
    from app import main

    source = inspect.getsource(main.lifespan)

    assert "await warm_up_within(" in source
    assert "warm_up_once(" not in source
    assert re.search(r"await asyncio\.to_thread\(\s*encode_sparse", source)
    assert main._STARTUP_WARMUP_TIMEOUT_S == 30.0


# ---------------------------------------------------------------------------
# Finding 17 -- Qdrant clients get a real timeout
# ---------------------------------------------------------------------------
def test_the_default_timeout_is_long_enough_for_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QDRANT_CLIENT_TIMEOUT_S", raising=False)

    assert qdrant_client_kwargs()["timeout"] == DEFAULT_CLIENT_TIMEOUT_S >= 60


def test_the_default_is_tunable_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_CLIENT_TIMEOUT_S", "120")

    assert qdrant_client_kwargs()["timeout"] == 120


def test_a_caller_can_pick_its_own_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_CLIENT_TIMEOUT_S", "120")

    assert qdrant_client_kwargs(timeout=6)["timeout"] == 6
    assert qdrant_client_kwargs(timeout=0.2)["timeout"] == 1, "never rounds down to no timeout at all"


def test_the_connection_kwargs_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_HOST", "qdrant.internal")
    monkeypatch.setenv("QDRANT_PORT", "443")
    monkeypatch.setenv("QDRANT_HTTPS", "true")
    monkeypatch.setenv("QDRANT_API_KEY", "k")

    kwargs = qdrant_client_kwargs()

    assert (kwargs["host"], kwargs["port"], kwargs["api_key"], kwargs["https"]) == (
        "qdrant.internal", 443, "k", True,
    )


@pytest.mark.asyncio
async def test_main_can_still_build_its_client_with_its_own_timeout() -> None:
    """The old call was ``AsyncQdrantClient(**qdrant_client_kwargs(), timeout=N)``.
    With ``timeout`` now inside the kwargs that is a duplicate-keyword TypeError,
    so main passes it through the helper instead."""
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(**qdrant_client_kwargs(timeout=6), check_compatibility=False)
    await client.close()

    with pytest.raises(TypeError, match="multiple values for keyword argument 'timeout'"):
        AsyncQdrantClient(**qdrant_client_kwargs(), timeout=6, check_compatibility=False)  # type: ignore[misc]


def test_main_passes_its_timeout_through_the_helper() -> None:
    from app import main

    source = inspect.getsource(main.lifespan)

    assert "qdrant_client_kwargs(timeout=int(settings.TIMEOUT_QDRANT_S))" in source
    assert not re.search(r"\*\*qdrant_client_kwargs\(\),\s*timeout=", source)


def test_probes_and_interactive_tools_keep_a_short_timeout() -> None:
    from app.agents.phase0 import index_health
    from app.services.tool_gateway import impls

    assert "qdrant_client_kwargs(timeout=5)" in inspect.getsource(index_health)
    assert "qdrant_client_kwargs(timeout=int(settings.TIMEOUT_QDRANT_S))" in inspect.getsource(impls)
