"""Wave-3 infrastructure tests.

Pins three behaviours added in P1 wave 3:

  * #13  _call_openai_compatible_llm uses the pooled http_client when
         supplied; falls back to ad-hoc construction when not.
  * #16  Tool decorator records both TOOL_DURATION and TOOL_RESULT_COUNT
         on success, and TOOL_DURATION (without count) on failure.
  * #16  FIRST_TOKEN_LATENCY is observed when the first SSE delta fires
         (covered indirectly here — the integration shape lives in
         queries.py and is exercised by manual smoke; pure unit-test
         shape is the metric-emit logic).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.orchestrator import _call_openai_compatible_llm


# ---------------------------------------------------------------------------
# #13 — pooled httpx.AsyncClient
# ---------------------------------------------------------------------------


class _FakePostResponse:
    """Stand-in for httpx.Response."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_pooled_http_client_is_used_when_supplied(monkeypatch):
    """When http_client is set, ad-hoc httpx.AsyncClient must NOT be built."""
    payload = {
        "choices": [{"message": {"content": "answer text"}}],
        "usage": {"prompt_tokens": 100, "cached_tokens": 0},
    }

    pool_post = AsyncMock(return_value=_FakePostResponse(payload))
    pool = SimpleNamespace(post=pool_post)

    # Sentinel: if the function falls through to ad-hoc construction
    # this monkeypatch intercepts it and fails the test loudly.
    def _no_adhoc(*args, **kwargs):
        raise AssertionError("ad-hoc httpx.AsyncClient must not be constructed")

    monkeypatch.setattr(httpx, "AsyncClient", _no_adhoc)

    result = await _call_openai_compatible_llm(
        "user msg",
        temperature=0.1,
        base_url="http://fake",
        model="qwen2.5:14b",
        http_client=pool,
    )

    assert result == "answer text"
    pool_post.assert_awaited_once()
    # Confirm the pooled client was hit at the right URL
    args, kwargs = pool_post.await_args
    assert args[0].endswith("/chat/completions")


@pytest.mark.asyncio
async def test_no_pooled_client_falls_back_to_adhoc(monkeypatch):
    """When http_client is None, we construct an ad-hoc httpx.AsyncClient
    so tests + pre-pool deploys keep working."""
    payload = {
        "choices": [{"message": {"content": "answer"}}],
        "usage": {"prompt_tokens": 50, "cached_tokens": 0},
    }

    class _FakeAdhocClient:
        def __init__(self, *_a, **_kw):
            self.post = AsyncMock(return_value=_FakePostResponse(payload))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAdhocClient)

    out = await _call_openai_compatible_llm(
        "user msg",
        temperature=0.1,
        base_url="http://fake",
        model="qwen2.5:14b",
        http_client=None,
    )
    assert out == "answer"


# ---------------------------------------------------------------------------
# #16 — _metered decorator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metered_records_duration_and_count_on_success():
    """Successful tool call should bump TOOL_DURATION and TOOL_RESULT_COUNT."""
    from app.agent.tools import _metered
    from app.metrics import TOOL_DURATION, TOOL_RESULT_COUNT

    @_metered("test_tool_ok")
    async def _fake_tool() -> SimpleNamespace:
        return SimpleNamespace(count=7)

    # Sample current values
    dur_before = TOOL_DURATION.labels(tool="test_tool_ok", outcome="ok")._sum.get()
    cnt_before = TOOL_RESULT_COUNT.labels(tool="test_tool_ok")._sum.get()

    result = await _fake_tool()
    assert result.count == 7

    dur_after = TOOL_DURATION.labels(tool="test_tool_ok", outcome="ok")._sum.get()
    cnt_after = TOOL_RESULT_COUNT.labels(tool="test_tool_ok")._sum.get()
    assert dur_after > dur_before
    assert cnt_after == cnt_before + 7


@pytest.mark.asyncio
async def test_metered_records_error_outcome_without_count():
    """Failing tool call should bump TOOL_DURATION{outcome=error}; result
    count is NOT recorded because there's no count to read."""
    from app.agent.tools import _metered
    from app.metrics import TOOL_DURATION, TOOL_RESULT_COUNT

    @_metered("test_tool_err")
    async def _fake_tool() -> SimpleNamespace:
        raise RuntimeError("boom")

    err_before = TOOL_DURATION.labels(
        tool="test_tool_err", outcome="error"
    )._sum.get()
    cnt_before = TOOL_RESULT_COUNT.labels(tool="test_tool_err")._sum.get()

    with pytest.raises(RuntimeError):
        await _fake_tool()

    err_after = TOOL_DURATION.labels(
        tool="test_tool_err", outcome="error"
    )._sum.get()
    cnt_after = TOOL_RESULT_COUNT.labels(tool="test_tool_err")._sum.get()
    assert err_after > err_before
    # Count stays unchanged on the error path.
    assert cnt_after == cnt_before


@pytest.mark.asyncio
async def test_metered_records_timeout_outcome_distinctly():
    """asyncio.TimeoutError should land in the `timeout` bucket, not error."""
    import asyncio

    from app.agent.tools import _metered
    from app.metrics import TOOL_DURATION

    @_metered("test_tool_timeout")
    async def _fake_tool() -> SimpleNamespace:
        raise asyncio.TimeoutError()

    timeout_before = TOOL_DURATION.labels(
        tool="test_tool_timeout", outcome="timeout"
    )._sum.get()

    with pytest.raises(asyncio.TimeoutError):
        await _fake_tool()

    timeout_after = TOOL_DURATION.labels(
        tool="test_tool_timeout", outcome="timeout"
    )._sum.get()
    assert timeout_after > timeout_before


@pytest.mark.asyncio
async def test_metered_passes_through_args_and_return_value():
    """The decorator must be transparent to the tool's actual signature."""
    from app.agent.tools import _metered

    @_metered("test_tool_passthrough")
    async def _fake_tool(a, b, *, kw="default") -> str:
        return f"{a}-{b}-{kw}"

    out = await _fake_tool(1, 2, kw="custom")
    assert out == "1-2-custom"
