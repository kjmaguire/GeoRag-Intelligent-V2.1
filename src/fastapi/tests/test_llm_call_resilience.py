"""2026-08-15 — tests for two cost-ceiling / reliability fixes:

  Fix 1 (§35.1 self-expiring suspension): ``assert_workspace_not_suspended``
  now falls back to ``usage.workspace_cost_ceilings.suspended_at`` in
  Postgres whenever the Redis flag is a miss (absent OR expired), instead
  of treating a miss as proof the workspace was never suspended. Before
  this fix, a suspended workspace silently resumed the moment the Redis
  key's hard 1h TTL lapsed, because the watcher stops seeing usage once a
  workspace is blocked and never gets a chance to re-suspend / refresh
  the TTL.

  Fix 2 (Foundry chat-completions retry): the chat-completions call in
  ``_call_openai_compatible_llm`` (the one production LLM path,
  ``LLM_BACKEND=azure`` — a Preview SKU with no SLA) now retries a
  transient pre-stream failure (connection error, 429, 5xx, timeout
  before any token arrives) with async-native exponential backoff. A
  mid-stream failure (after at least one token reached the caller) is
  NOT retried — that must still fail the query outright.

Both suites use fully in-process fakes (fake Redis client, fake asyncpg
pool/connection, fake httpx client) — no live stack required.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import app.agent.llm_calls as llm_calls
from app.config import settings

WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Every test in this file gets a clean slate: the DB-fallback TTL
    cache and the per-query LLM-call counter are both module-level state
    that would otherwise leak between tests exercising the same
    workspace_id / budget edges."""
    llm_calls._suspension_db_cache.clear()
    llm_calls._llm_call_counter.set(0)
    yield
    llm_calls._suspension_db_cache.clear()
    llm_calls._llm_call_counter.set(0)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRedisMiss:
    """Redis client that always reports a miss (key absent / expired)."""

    async def get(self, key: str) -> None:
        return None


class _FakeRedisHit:
    """Redis client that always reports the suspension flag set."""

    async def get(self, key: str) -> str:
        return "1"


class _FakeRedisError:
    """Redis client whose .get() raises — simulates an outage."""

    async def get(self, key: str) -> None:
        raise ConnectionError("redis unreachable")


class _FakeConn:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row
        self.fetchrow_calls = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls += 1
        return self._row


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePgPool:
    """Stand-in for asyncpg.Pool — supports `async with pool.acquire() as conn`."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.conn = _FakeConn(row)

    def acquire(self) -> _FakeAcquireCtx:
        return _FakeAcquireCtx(self.conn)


class _FakePgPoolRaises:
    """Pool whose .acquire() blows up — simulates a DB outage."""

    def acquire(self) -> Any:
        raise ConnectionError("db unreachable")


class _FakePgPoolMustNotBeTouched:
    """Pool that fails the test loudly if the DB fallback is reached at all."""

    def acquire(self) -> Any:
        raise AssertionError("Postgres fallback must not run — Redis already decided")


# ===========================================================================
# Fix 1 — assert_workspace_not_suspended: Postgres fallback on a Redis miss
# ===========================================================================


@pytest.mark.asyncio
async def test_db_fallback_blocks_when_redis_flag_expired() -> None:
    """The core regression: Redis has NO opinion (miss — could be 'never
    suspended' or 'TTL expired while still suspended'), but Postgres says
    suspended_at IS NOT NULL and no admin override — the call must be
    blocked, not silently allowed through."""
    pg_pool = _FakePgPool({"suspended_at": "2026-08-15T00:00:00Z", "admin_override_enabled": False})

    with pytest.raises(llm_calls.WorkspaceQuotaExceeded):
        await llm_calls.assert_workspace_not_suspended(
            WORKSPACE_ID, redis_client=_FakeRedisMiss(), pg_pool=pg_pool,
        )
    assert pg_pool.conn.fetchrow_calls == 1


@pytest.mark.asyncio
async def test_db_fallback_allows_when_no_row() -> None:
    """A workspace with no workspace_cost_ceilings row at all (never
    configured) must NOT be blocked."""
    pg_pool = _FakePgPool(None)
    await llm_calls.assert_workspace_not_suspended(
        WORKSPACE_ID, redis_client=_FakeRedisMiss(), pg_pool=pg_pool,
    )  # no raise


@pytest.mark.asyncio
async def test_db_fallback_respects_admin_override() -> None:
    """suspended_at is set, but an admin has explicitly re-enabled the
    workspace via admin_override_enabled — must NOT be blocked."""
    pg_pool = _FakePgPool({"suspended_at": "2026-08-15T00:00:00Z", "admin_override_enabled": True})
    await llm_calls.assert_workspace_not_suspended(
        WORKSPACE_ID, redis_client=_FakeRedisMiss(), pg_pool=pg_pool,
    )  # no raise


@pytest.mark.asyncio
async def test_redis_hit_short_circuits_before_touching_postgres() -> None:
    """When Redis is authoritative (flag present), the DB fallback must
    not even be attempted — it's the fast path for a reason."""
    with pytest.raises(llm_calls.WorkspaceQuotaExceeded):
        await llm_calls.assert_workspace_not_suspended(
            WORKSPACE_ID,
            redis_client=_FakeRedisHit(),
            pg_pool=_FakePgPoolMustNotBeTouched(),
        )


@pytest.mark.asyncio
async def test_redis_error_falls_through_to_postgres() -> None:
    """A Redis outage (not just a clean miss) must also fall through to
    the DB check rather than failing open unconditionally."""
    pg_pool = _FakePgPool({"suspended_at": "2026-08-15T00:00:00Z", "admin_override_enabled": False})
    with pytest.raises(llm_calls.WorkspaceQuotaExceeded):
        await llm_calls.assert_workspace_not_suspended(
            WORKSPACE_ID, redis_client=_FakeRedisError(), pg_pool=pg_pool,
        )


@pytest.mark.asyncio
async def test_no_redis_no_pgpool_fails_open() -> None:
    """Neither data source supplied at all — soft-contract fail-open,
    same as the pre-fix behavior for a fully unconfigured call site."""
    await llm_calls.assert_workspace_not_suspended(WORKSPACE_ID)  # no raise


@pytest.mark.asyncio
async def test_db_error_fails_open() -> None:
    """A Postgres outage on top of a Redis miss must not take the whole
    chat product down — same soft-contract as the Redis-unavailable path."""
    await llm_calls.assert_workspace_not_suspended(
        WORKSPACE_ID, redis_client=_FakeRedisMiss(), pg_pool=_FakePgPoolRaises(),
    )  # no raise


@pytest.mark.asyncio
async def test_missing_workspace_id_is_noop() -> None:
    await llm_calls.assert_workspace_not_suspended(
        None, redis_client=_FakeRedisMiss(), pg_pool=_FakePgPoolMustNotBeTouched(),
    )  # no raise, no DB touch


@pytest.mark.asyncio
async def test_db_fallback_result_is_cached_within_ttl() -> None:
    """A single user query can fan out to MAX_LLM_CALLS_PER_QUERY separate
    _call_llm invocations; the DB fallback must not become a DB round trip
    on every single one of them within the short cache TTL."""
    pg_pool = _FakePgPool({"suspended_at": "2026-08-15T00:00:00Z", "admin_override_enabled": False})

    for _ in range(5):
        with pytest.raises(llm_calls.WorkspaceQuotaExceeded):
            await llm_calls.assert_workspace_not_suspended(
                WORKSPACE_ID, redis_client=_FakeRedisMiss(), pg_pool=pg_pool,
            )
    assert pg_pool.conn.fetchrow_calls == 1


@pytest.mark.asyncio
async def test_call_llm_forwards_pg_pool_to_suspension_check(monkeypatch) -> None:
    """`_call_llm` must thread its `pg_pool` kwarg into
    `assert_workspace_not_suspended` — without this, the DB fallback is
    unreachable from the real call path no matter how correct the
    function itself is."""
    captured: dict[str, Any] = {}

    async def _fake_check(workspace_id, *, redis_client=None, pg_pool=None):
        captured["workspace_id"] = workspace_id
        captured["redis_client"] = redis_client
        captured["pg_pool"] = pg_pool

    monkeypatch.setattr(llm_calls, "assert_workspace_not_suspended", _fake_check)

    async def _fake_openai_call(*args, **kwargs):
        return "stub answer"

    monkeypatch.setattr(llm_calls, "_call_openai_compatible_llm", _fake_openai_call)
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")

    sentinel_pool = object()
    sentinel_redis = object()
    await llm_calls._call_llm(
        query="q",
        context="ctx",
        workspace_id=WORKSPACE_ID,
        redis_client=sentinel_redis,
        pg_pool=sentinel_pool,
        audit_label="test",
    )

    assert captured["workspace_id"] == WORKSPACE_ID
    assert captured["redis_client"] is sentinel_redis
    assert captured["pg_pool"] is sentinel_pool


@pytest.mark.asyncio
async def test_suspended_workspace_raises_and_never_reaches_http_layer(monkeypatch) -> None:
    """A workspace flagged suspended must fail BEFORE any HTTP call to
    the LLM backend — cost-ceiling enforcement, not merely logged."""
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")

    post_calls = {"n": 0}

    async def _post(*args: Any, **kwargs: Any) -> Any:
        post_calls["n"] += 1
        raise AssertionError("HTTP layer must not be reached")

    fake_client = SimpleNamespace(post=_post)

    with pytest.raises(llm_calls.WorkspaceQuotaExceeded):
        await llm_calls._call_llm(
            query="q",
            context="ctx",
            openai_http_client=fake_client,
            workspace_id=WORKSPACE_ID,
            redis_client=_FakeRedisHit(),
            audit_label="test",
        )
    assert post_calls["n"] == 0


# ===========================================================================
# Fix 2 — pre-stream retry on the chat-completions call
# ===========================================================================


def _ok_response(content: str = "answer text") -> httpx.Response:
    request = httpx.Request("POST", "http://fake/chat/completions")
    return httpx.Response(
        200,
        request=request,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        },
    )


def _status_response(status_code: int) -> httpx.Response:
    request = httpx.Request("POST", "http://fake/chat/completions")
    return httpx.Response(status_code, request=request, json={"error": "boom"})


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every retry test in this file gets asyncio.sleep patched out so the
    2s/4s/8s backoff cadence doesn't actually slow the suite down."""
    monkeypatch.setattr(llm_calls.asyncio, "sleep", AsyncMock())
    yield


@pytest.mark.asyncio
async def test_blocking_call_retries_transient_connect_error_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 60.0)

    calls = {"n": 0}

    async def _post(url: str, *, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return _ok_response("recovered after retry")

    client = SimpleNamespace(post=_post)

    result = await llm_calls._call_openai_compatible_llm(
        user_message="hello",
        temperature=0.0,
        base_url="https://example-foundry.cognitiveservices.azure.com/openai/v1",
        model="Cohere-command-a-plus-05-2026",
        http_client=client,
        enable_thinking=False,
    )

    assert result == "recovered after retry"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_blocking_call_retries_429_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 60.0)

    calls = {"n": 0}

    async def _post(url: str, *, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _status_response(429)
        return _ok_response("second attempt worked")

    client = SimpleNamespace(post=_post)

    result = await llm_calls._call_openai_compatible_llm(
        user_message="hello",
        temperature=0.0,
        base_url="https://example-foundry.cognitiveservices.azure.com/openai/v1",
        model="Cohere-command-a-plus-05-2026",
        http_client=client,
        enable_thinking=False,
    )

    assert result == "second attempt worked"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_blocking_call_exhausts_retries_and_raises_original(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 60.0)

    calls = {"n": 0}

    async def _post(url: str, *, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        calls["n"] += 1
        return _status_response(503)

    client = SimpleNamespace(post=_post)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await llm_calls._call_openai_compatible_llm(
            user_message="hello",
            temperature=0.0,
            base_url="https://example-foundry.cognitiveservices.azure.com/openai/v1",
            model="Cohere-command-a-plus-05-2026",
            http_client=client,
            enable_thinking=False,
        )

    assert exc_info.value.response.status_code == 503
    # 3 attempts total (the default max_attempts) — the wrapper does not
    # retry forever.
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_non_retryable_4xx_is_not_retried(monkeypatch) -> None:
    """A 400 (bad request — e.g. schema violation) is not in the
    retryable status set and must fail on the first attempt."""
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")

    calls = {"n": 0}

    async def _post(url: str, *, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        calls["n"] += 1
        return _status_response(400)

    client = SimpleNamespace(post=_post)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await llm_calls._call_openai_compatible_llm(
            user_message="hello",
            temperature=0.0,
            base_url="https://example-foundry.cognitiveservices.azure.com/openai/v1",
            model="Cohere-command-a-plus-05-2026",
            http_client=client,
            enable_thinking=False,
        )

    assert exc_info.value.response.status_code == 400
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retry_stops_when_call_budget_exhausted(monkeypatch) -> None:
    """Retries must respect MAX_LLM_CALLS_PER_QUERY — a workspace already
    at the per-query LLM-call cap must not get extra attempts smuggled in
    through the retry loop."""
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")
    monkeypatch.setattr(settings, "MAX_LLM_CALLS_PER_QUERY", 2)
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 60.0)
    llm_calls._llm_call_counter.set(2)  # already at the cap

    calls = {"n": 0}

    async def _post(url: str, *, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    client = SimpleNamespace(post=_post)

    with pytest.raises(httpx.ConnectError):
        await llm_calls._call_openai_compatible_llm(
            user_message="hello",
            temperature=0.0,
            base_url="https://example-foundry.cognitiveservices.azure.com/openai/v1",
            model="Cohere-command-a-plus-05-2026",
            http_client=client,
            enable_thinking=False,
        )

    assert calls["n"] == 1  # no retry attempted — budget already exhausted


@pytest.mark.asyncio
async def test_retry_pre_stream_call_stops_when_timeout_budget_insufficient(monkeypatch) -> None:
    """The retry loop must not eat the whole per-query timeout budget on
    its own — when too little of TIMEOUT_GATHER_S remains to plausibly
    fit another backoff + attempt, it gives up and raises the original
    error instead of continuing to retry."""
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 1.0)  # smaller than the 2s first backoff

    attempts = {"n": 0}

    async def _flaky() -> dict:
        attempts["n"] += 1
        raise llm_calls._PreStreamTransientError(httpx.ConnectError("refused"))

    with pytest.raises(httpx.ConnectError):
        await llm_calls._retry_pre_stream_call(_flaky, label="test")

    assert attempts["n"] == 1


class _FakeStreamCtx:
    def __init__(self, resp: Any) -> None:
        self._resp = resp

    async def __aenter__(self) -> Any:
        return self._resp

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeStreamResponse:
    """Minimal stand-in for the object `client.stream(...)` yields."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        lines: list[str] | None = None,
        raise_on_status: Exception | None = None,
        raise_mid_stream: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._lines = lines or []
        self._raise_on_status = raise_on_status
        self._raise_mid_stream = raise_mid_stream

    def raise_for_status(self) -> None:
        if self._raise_on_status is not None:
            raise self._raise_on_status

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        if self._raise_mid_stream is not None:
            raise self._raise_mid_stream


class _FakeStreamClient:
    """`client.stream(...)` returns each queued item in order. An item
    that is an Exception instance is raised synchronously (simulating a
    connection failure before any response is received); otherwise it's
    wrapped in an async-context-manager yielding it as `resp`."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def stream(self, method: str, url: str, *, json: dict[str, Any], headers: Any = None) -> Any:
        self.call_count += 1
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _FakeStreamCtx(item)


async def _collect_tokens(_content: str) -> None:
    return None


@pytest.mark.asyncio
async def test_streaming_call_retries_pre_stream_503_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 60.0)

    request = httpx.Request("POST", "http://fake/chat/completions")
    failing_resp = _FakeStreamResponse(
        status_code=503,
        raise_on_status=httpx.HTTPStatusError(
            "503", request=request, response=httpx.Response(503, request=request),
        ),
    )
    ok_resp = _FakeStreamResponse(
        lines=[
            'data: {"choices":[{"delta":{"content":"hello "}}]}',
            'data: {"choices":[{"delta":{"content":"world"}}],"usage":{"prompt_tokens":5,"completion_tokens":2}}',
            "data: [DONE]",
        ],
    )
    client = _FakeStreamClient([failing_resp, ok_resp])

    result = await llm_calls._call_openai_compatible_llm(
        user_message="hello",
        temperature=0.0,
        base_url="https://example-foundry.cognitiveservices.azure.com/openai/v1",
        model="Cohere-command-a-plus-05-2026",
        http_client=client,
        token_callback=_collect_tokens,
        enable_thinking=False,
    )

    assert result == "hello world"
    assert client.call_count == 2


@pytest.mark.asyncio
async def test_streaming_call_does_not_retry_mid_stream_failure(monkeypatch) -> None:
    """Once at least one token has reached the caller, a subsequent
    failure (connection drop mid-generation) must propagate as-is — NOT
    be retried. Restarting from scratch after tokens already streamed
    would duplicate/corrupt what the user has already seen."""
    monkeypatch.setattr(settings, "LLM_BACKEND", "azure")
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", 60.0)

    mid_stream_failure = _FakeStreamResponse(
        lines=['data: {"choices":[{"delta":{"content":"partial answer"}}]}'],
        raise_mid_stream=httpx.ReadTimeout("dropped mid-stream"),
    )
    client = _FakeStreamClient([mid_stream_failure])

    received: list[str] = []

    async def _capture(content: str) -> None:
        received.append(content)

    with pytest.raises(httpx.ReadTimeout):
        await llm_calls._call_openai_compatible_llm(
            user_message="hello",
            temperature=0.0,
            base_url="https://example-foundry.cognitiveservices.azure.com/openai/v1",
            model="Cohere-command-a-plus-05-2026",
            http_client=client,
            token_callback=_capture,
            enable_thinking=False,
        )

    # Exactly one attempt — no retry after tokens started flowing.
    assert client.call_count == 1
    assert received == ["partial answer"]
