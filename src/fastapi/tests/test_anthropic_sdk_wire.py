"""_call_anthropic_llm through the real anthropic SDK, over a mock transport.

test_anthropic_streaming.py stands in a fake client, so it pins what the
function passes to ``messages.create`` / ``messages.stream`` but not what
the SDK then puts on the wire, nor whether the SDK still accepts those
arguments. These tests build a real ``AsyncAnthropic`` on an
``httpx2.MockTransport`` and read the request the SDK actually sends.

What they pin, for the SDK major this project is locked to (2.x since
2026-10-27):

  1. The SDK accepts every argument _call_anthropic_llm passes: the
     list-of-blocks system prompt with ``cache_control``, ``extra_headers``
     and a per-request ``timeout``. A removed or renamed parameter fails
     here, offline, instead of in production.

  2. No ``temperature``, ``top_p`` or ``top_k`` reaches the request body.
     API version 2026-10-21 answers them with a 400.

  3. The SDK sends the API version and the key the way the 2.x migration
     guide says: ``anthropic-version: 2026-10-21`` and
     ``Authorization: Bearer <key>``, not ``X-Api-Key``. If a proxy or
     gateway is ever put in front of the API, this is the header it sees.

  4. The response is read back into text on both paths: the plain JSON
     reply for ``messages.create`` and the SSE stream for
     ``messages.stream``.

None of this replaces one live call with a real key; it narrows what that
call can still find to the server's side.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest

from app.agent.orchestrator import _call_anthropic_llm

_KEY = "sk-ant-test-key"
_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"
_SAMPLING_PARAMS = {"temperature", "top_p", "top_k"}


def _message_json(text: str) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-8",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 11,
            "output_tokens": 3,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _sse(chunks: list[str]) -> bytes:
    start = _message_json("")
    start["content"] = []
    events: list[tuple[str, dict]] = [
        ("message_start", {"type": "message_start", "message": start}),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
    ]
    events += [
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": c}},
        )
        for c in chunks
    ]
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": len(chunks)},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def _client(captured: list[httpx2.Request], *, stream_chunks: list[str] | None = None, text: str = "ok"):
    def _handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        if stream_chunks is not None:
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(stream_chunks),
            )
        return httpx2.Response(200, json=_message_json(text))

    return anthropic.AsyncAnthropic(
        api_key=_KEY,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(_handler)),
    )


def _permissive_pool():
    """A fake asyncpg pool on which the egress gate reads allow_external_llm=True."""

    async def _fetchrow(_sql: str, _workspace_id: str):
        return {"extra_payload": {"allow_external_llm": True}}

    async def _execute(_sql: str, *_args):
        return "OK"

    class _TxCM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    conn = SimpleNamespace(fetchrow=_fetchrow, execute=_execute, transaction=lambda: _TxCM())

    class _AcquireCM:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    return SimpleNamespace(acquire=MagicMock(return_value=_AcquireCM()))


@pytest.fixture
def _anthropic_settings():
    from app.config import settings

    overrides = {
        "LLM_BACKEND": "anthropic",
        "ANTHROPIC_API_KEY": _KEY,
        "REQUIRE_POOLED_ANTHROPIC_CLIENT": False,
        # On, so the system prompt goes out as blocks carrying cache_control
        # and the priority-tier header goes out through extra_headers.
        "ANTHROPIC_ENABLE_PROMPT_CACHING": True,
        "ANTHROPIC_USE_PRIORITY_TIER": True,
    }
    saved = {name: getattr(settings, name) for name in overrides}
    for name, value in overrides.items():
        object.__setattr__(settings, name, value)
    yield settings
    for name, value in saved.items():
        object.__setattr__(settings, name, value)


async def _call(client, **kwargs) -> str:
    return await _call_anthropic_llm(
        "context + question",
        temperature=0.1,
        client=client,
        system_prompt="You are a geologist.",
        project_preamble="Project: test.",
        workspace_id=_WORKSPACE_ID,
        pg_pool=_permissive_pool(),
        **kwargs,
    )


def _assert_wire(request: httpx2.Request) -> dict:
    assert request.method == "POST"
    assert request.url.path == "/v1/messages"
    assert request.headers.get("anthropic-version") == "2026-10-21"
    assert request.headers.get("authorization") == f"Bearer {_KEY}"
    assert "x-api-key" not in request.headers
    assert request.headers.get("anthropic-priority-tier") == "priority"

    body = json.loads(request.content)
    assert not _SAMPLING_PARAMS & body.keys(), sorted(body)
    assert body["messages"] == [{"role": "user", "content": "context + question"}]
    assert isinstance(body["system"], list)
    assert any(block.get("cache_control") == {"type": "ephemeral"} for block in body["system"])
    return body


@pytest.mark.asyncio
async def test_create_through_the_real_sdk(_anthropic_settings):
    captured: list[httpx2.Request] = []
    out = await _call(_client(captured, text="final answer"))

    assert out == "final answer"
    assert len(captured) == 1
    body = _assert_wire(captured[0])
    assert "stream" not in body or body["stream"] is False


@pytest.mark.asyncio
async def test_stream_through_the_real_sdk(_anthropic_settings):
    captured: list[httpx2.Request] = []
    received: list[str] = []

    async def _collect(token: str) -> None:
        received.append(token)

    out = await _call(_client(captured, stream_chunks=["Hello, ", "world."]), token_callback=_collect)

    assert received == ["Hello, ", "world."]
    assert out == "Hello, world."
    assert len(captured) == 1
    body = _assert_wire(captured[0])
    assert body["stream"] is True
