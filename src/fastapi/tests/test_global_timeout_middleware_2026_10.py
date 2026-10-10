"""Audit 2026-10 finding 15: the global request timeout cancels the handler.

``GlobalTimeoutMiddleware`` answered 504 from ``asyncio.wait_for(call_next(...))``.
``call_next`` runs the rest of the stack in a task of its own, which ``wait_for``
does not cancel, so the handler ran to completion behind the 504, holding its
connections and its LLM spend for a response nobody would read.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from app.middleware.http import GlobalTimeoutMiddleware


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _app(routes: list[Route], *, timeout_s: float = 0.1, outer_base_http: bool = False) -> Starlette:
    app = Starlette(routes=routes)
    app.add_middleware(GlobalTimeoutMiddleware, timeout_s=timeout_s)
    if outer_base_http:
        # The production stack has a BaseHTTPMiddleware (the access log)
        # OUTSIDE this one, which runs this middleware in a task of its own.
        class _Outer(BaseHTTPMiddleware):
            async def dispatch(self, request: Any, call_next: Any) -> Any:
                return await call_next(request)

        app.add_middleware(_Outer)
    return app


class _Handler:
    """A handler that records how it ended."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.finished = False
        self.cleaned_up = False

    async def hangs(self, request: Any) -> JSONResponse:
        self.started.set()
        try:
            await asyncio.sleep(5)
            self.finished = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.cleaned_up = True
        return JSONResponse({"ok": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("outer_base_http", [False, True], ids=["bare", "behind-base-http-middleware"])
async def test_a_hung_handler_is_cancelled_not_abandoned(outer_base_http: bool) -> None:
    handler = _Handler()
    app = _app([Route("/slow", handler.hangs)], outer_base_http=outer_base_http)

    async with _client(app) as client:
        resp = await client.get("/slow")

    assert resp.status_code == 504
    assert resp.json() == {"detail": "Request exceeded server-side timeout"}
    assert handler.started.is_set()
    assert handler.cancelled, "the handler was left running behind the 504"
    assert handler.cleaned_up, "its finally blocks must run"
    assert handler.finished is False


@pytest.mark.asyncio
async def test_a_fast_handler_is_untouched() -> None:
    async def quick(request: Any) -> JSONResponse:
        return JSONResponse({"ok": True})

    async with _client(_app([Route("/quick", quick)])) as client:
        resp = await client.get("/quick")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_sse_requests_are_not_bounded() -> None:
    async def slow_stream(request: Any) -> PlainTextResponse:
        await asyncio.sleep(0.3)
        return PlainTextResponse("data: done\n\n")

    async with _client(_app([Route("/s", slow_stream)], timeout_s=0.05)) as client:
        resp = await client.get("/s", headers={"accept": "text/event-stream"})

    assert resp.status_code == 200
    assert resp.text == "data: done\n\n"


@pytest.mark.asyncio
async def test_a_body_that_streams_past_the_deadline_is_not_cut() -> None:
    """The clock stops at http.response.start: a download in progress is not a
    hung handler (and the old call_next version never bounded it either)."""

    async def body_stream(request: Any) -> StreamingResponse:
        async def chunks() -> Any:
            for i in range(4):
                await asyncio.sleep(0.1)
                yield f"chunk{i};".encode()

        return StreamingResponse(chunks(), media_type="text/plain")

    async with _client(_app([Route("/dl", body_stream)], timeout_s=0.15)) as client:
        resp = await client.get("/dl")

    assert resp.status_code == 200
    assert resp.text == "chunk0;chunk1;chunk2;chunk3;"


@pytest.mark.asyncio
async def test_a_timeout_the_application_raised_is_not_answered_as_ours() -> None:
    async def raises(request: Any) -> JSONResponse:
        raise TimeoutError("an upstream call timed out")

    app = _app([Route("/boom", raises)], timeout_s=5.0)

    async with _client(app) as client:
        with pytest.raises(TimeoutError, match="an upstream call timed out"):
            await client.get("/boom")


@pytest.mark.asyncio
async def test_no_second_response_is_started_after_the_handler_responded() -> None:
    sent: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(0.3)  # still "working" after the headers are out
        await send({"type": "http.response.body", "body": b"late"})

    async def recording_send(message: Any) -> None:
        sent.append(message["type"])

    mw = GlobalTimeoutMiddleware(inner, timeout_s=0.05)

    await mw({"type": "http", "path": "/x", "method": "GET", "headers": []}, _never_receive, recording_send)

    assert sent == ["http.response.start", "http.response.body"]


async def _never_receive() -> dict[str, Any]:
    await asyncio.sleep(60)
    return {"type": "http.disconnect"}


@pytest.mark.asyncio
async def test_non_http_scopes_pass_straight_through() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    mw = GlobalTimeoutMiddleware(inner, timeout_s=0.05)
    await mw({"type": "lifespan"}, _never_receive, _never_receive)  # type: ignore[arg-type]

    assert seen == ["lifespan"]
