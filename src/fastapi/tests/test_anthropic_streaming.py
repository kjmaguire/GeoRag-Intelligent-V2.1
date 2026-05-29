"""Tests for P0 #5 — real Anthropic streaming via messages.stream.

Prior to P0 #5 the orchestrator called ``client.messages.create`` and
returned only the finished assistant text. The SSE layer then
re-synthesised fake word-level deltas for the UI.

These tests pin the new behaviour:

  1. When ``_call_anthropic_llm`` is invoked WITHOUT a ``token_callback``,
     it still calls the blocking ``messages.create`` endpoint — no
     behavioural regression for cache / batch paths.

  2. When called WITH a ``token_callback``, it opens
     ``messages.stream`` as an async context manager and forwards every
     ``text_delta`` to the callback, accumulating the final message text
     via ``get_final_message``.

  3. A broken ``token_callback`` (raises) MUST NOT break the LLM call —
     the stream consumes the chunks and returns the full text anyway.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.orchestrator import _call_anthropic_llm


# ---------------------------------------------------------------------------
# Fake stream context manager
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal async-context-manager-shaped stand-in for the Anthropic SDK.

    Yields pre-canned text chunks through ``text_stream`` and returns a
    pre-canned final message via ``get_final_message``.
    """

    def __init__(self, chunks: list[str], final_text: str) -> None:
        self._chunks = chunks
        self._final_text = final_text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    @property
    def text_stream(self):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()

    async def get_final_message(self):
        # The orchestrator walks .content blocks to assemble the answer. We
        # mimic the real shape: a list with one text block whose .text is
        # the concatenation of streamed chunks.
        text_block = SimpleNamespace(type="text", text=self._final_text)
        return SimpleNamespace(content=[text_block], usage=None)


def _make_client_blocking(final_text: str):
    """Legacy blocking path — messages.create returns a single message."""
    text_block = SimpleNamespace(type="text", text=final_text)
    fake_msg = SimpleNamespace(content=[text_block], usage=None)
    return SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=fake_msg),
            stream=AsyncMock(side_effect=AssertionError(
                "stream() must not be called when token_callback is None"
            )),
        )
    )


def _make_client_streaming(chunks: list[str], final_text: str):
    """New streaming path — messages.stream returns an async CM."""
    stream_cm = _FakeStream(chunks, final_text)

    def _stream_factory(**kwargs):
        # messages.stream is invoked with kwargs and returns a context
        # manager directly (it's NOT an async def on the real SDK; it's
        # a helper that returns the context object synchronously).
        return stream_cm

    return SimpleNamespace(
        messages=SimpleNamespace(
            stream=_stream_factory,
            create=AsyncMock(side_effect=AssertionError(
                "create() must not be called when token_callback is set"
            )),
        )
    )


@pytest.fixture
def _enable_anthropic(monkeypatch):
    from app.config import settings

    object.__setattr__(settings, "LLM_BACKEND", "anthropic")
    object.__setattr__(settings, "ANTHROPIC_API_KEY", "sk-test")
    object.__setattr__(settings, "REQUIRE_POOLED_ANTHROPIC_CLIENT", False)
    object.__setattr__(settings, "ANTHROPIC_ENABLE_PROMPT_CACHING", False)
    object.__setattr__(settings, "ANTHROPIC_USE_PRIORITY_TIER", False)
    yield settings


# Z.1 — every _call_anthropic_llm test must pass the egress-gate check so
# the SDK code path under test is actually reached. These streaming tests
# pin SDK behaviour, not policy; we wire a permissive workspace + pool so
# the gate returns "flag_enabled" without touching real PG.

_ALLOWED_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


def _permissive_pool():
    """Build a fake asyncpg.Pool whose fetchrow returns allow_external_llm=True."""
    from unittest.mock import MagicMock

    async def _fetchrow(_sql: str, _workspace_id: str):
        return {"extra_payload": {"allow_external_llm": True}}

    conn = SimpleNamespace(fetchrow=_fetchrow)

    class _AcquireCM:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    return SimpleNamespace(acquire=MagicMock(return_value=_AcquireCM()))


@pytest.mark.asyncio
async def test_no_callback_uses_blocking_create(_enable_anthropic):
    client = _make_client_blocking("final answer text")
    out = await _call_anthropic_llm(
        "context + question",
        temperature=0.1,
        client=client,
        workspace_id=_ALLOWED_WORKSPACE_ID,
        pg_pool=_permissive_pool(),
    )
    assert out == "final answer text"
    client.messages.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_streams_chunks(_enable_anthropic):
    chunks = ["Hello, ", "world", "."]
    client = _make_client_streaming(chunks, "Hello, world.")

    received: list[str] = []

    async def _capture(token: str) -> None:
        received.append(token)

    out = await _call_anthropic_llm(
        "context + question",
        temperature=0.1,
        client=client,
        token_callback=_capture,
        workspace_id=_ALLOWED_WORKSPACE_ID,
        pg_pool=_permissive_pool(),
    )

    assert received == chunks
    assert out == "Hello, world."


@pytest.mark.asyncio
async def test_callback_exception_does_not_break_stream(_enable_anthropic):
    """A broken consumer must never fail the LLM call."""
    chunks = ["A", "B", "C"]
    client = _make_client_streaming(chunks, "ABC")

    async def _broken(_token: str) -> None:
        raise RuntimeError("downstream queue closed")

    out = await _call_anthropic_llm(
        "ctx",
        temperature=0.1,
        client=client,
        token_callback=_broken,
        workspace_id=_ALLOWED_WORKSPACE_ID,
        pg_pool=_permissive_pool(),
    )
    # Despite every callback invocation raising, the orchestrator still
    # accumulates the final text via get_final_message.
    assert out == "ABC"


@pytest.mark.asyncio
async def test_empty_chunks_are_skipped(_enable_anthropic):
    """The Anthropic SDK can emit empty text_delta events; ignore them."""
    chunks = ["", "real", "", "text"]
    client = _make_client_streaming(chunks, "realtext")

    received: list[str] = []

    async def _capture(token: str) -> None:
        received.append(token)

    out = await _call_anthropic_llm(
        "ctx",
        temperature=0.1,
        client=client,
        token_callback=_capture,
        workspace_id=_ALLOWED_WORKSPACE_ID,
        pg_pool=_permissive_pool(),
    )
    assert received == ["real", "text"]
    assert out == "realtext"
