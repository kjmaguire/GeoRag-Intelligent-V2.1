"""FastAPI middleware stack — security, observability, and DoS-surface protection.

This module groups every custom middleware in one place so the wiring in
`app/main.py` stays a one-liner per concern. Order matters — middlewares
execute in the reverse order they're added (LIFO), so the body-size guard
needs to be added LAST so it runs FIRST on the request path.

See `docs/RUNBOOK.md::FastAPI middleware stack` for the rationale on
each setting.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FastAPI review #1 — body-size limit
# ---------------------------------------------------------------------------


class BodySizeLimitMiddleware:
    """Reject requests whose body exceeds ``max_bytes``.

    A pure ASGI middleware (not ``BaseHTTPMiddleware``) because it has to
    sit on the ``receive`` channel. Two paths:

      * Fast path — ``Content-Length`` present and over the cap: 413
        immediately, before any body byte is read.
      * Streaming path — no ``Content-Length`` (``Transfer-Encoding:
        chunked``) or a lying one: ``receive`` is wrapped and counts bytes as
        they arrive; the first chunk that crosses the cap raises a 413
        ``HTTPException``.

    The streaming path is new (API-8). The old docstring said chunked bodies
    were capped by "Starlette's ``max_request_size`` set on the app
    instance" — Starlette has no such option and nothing set one. FastAPI
    reads and parses the body BEFORE it resolves dependencies, i.e. before
    ``verify_service_key`` runs, so an unauthenticated chunked POST was
    buffered in full: a straightforward way to OOM a uvicorn worker.

    Why ``HTTPException`` from inside ``receive``: FastAPI's body reader
    re-raises ``HTTPException`` from middleware verbatim (anything else it
    turns into a 400 "error parsing the body"), so the request ends as a
    clean 413 through the normal exception handler. If the exception
    escapes the app instead (a handler that swallows it and keeps going),
    the wrapper below still answers 413 as long as no response has started.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    def _too_large(self, path: str, reason: str) -> JSONResponse:
        logger.warning(
            "BodySizeLimitMiddleware: rejected request — %s max=%d path=%s",
            reason,
            self.max_bytes,
            path,
        )
        return JSONResponse({"detail": "Request body too large"}, status_code=413)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        cl = Headers(scope=scope).get("content-length")
        if cl and cl.isdigit() and int(cl) > self.max_bytes:
            await self._too_large(path, f"content-length={cl}")(scope, receive, send)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    logger.warning(
                        "BodySizeLimitMiddleware: streamed body crossed the cap "
                        "received=%d max=%d path=%s",
                        received,
                        self.max_bytes,
                        path,
                    )
                    raise HTTPException(
                        status_code=413, detail="Request body too large"
                    )
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except HTTPException as exc:
            if exc.status_code != 413 or response_started:
                raise
            await self._too_large(path, f"streamed>{self.max_bytes}")(
                scope, receive, send
            )


# ---------------------------------------------------------------------------
# FastAPI review #2 — global per-request timeout
# ---------------------------------------------------------------------------


class GlobalTimeoutMiddleware(BaseHTTPMiddleware):
    """Backstop hard-timeout for any handler that hangs.

    SSE streaming endpoints opt out via the `Accept: text/event-stream`
    header — those own their own deadline via the orchestrator's
    `TIMEOUT_GATHER_S` and we'd otherwise truncate streams mid-flight.

    Returns 504 with a structured detail when the timeout fires, instead
    of letting the asyncio.TimeoutError bubble through to a 500.
    """

    def __init__(self, app, timeout_s: float) -> None:
        super().__init__(app)
        self.timeout_s = timeout_s

    async def dispatch(self, request: Request, call_next):
        accept = request.headers.get("accept", "")
        if "text/event-stream" in accept:
            return await call_next(request)
        # Lazy import — keeps middleware import cheap.
        import asyncio  # noqa: PLC0415
        try:
            return await asyncio.wait_for(call_next(request), timeout=self.timeout_s)
        except TimeoutError:
            logger.warning(
                "GlobalTimeoutMiddleware: request exceeded %.1fs path=%s method=%s",
                self.timeout_s,
                request.url.path,
                request.method,
            )
            return JSONResponse(
                {"detail": "Request exceeded server-side timeout"},
                status_code=504,
            )


# ---------------------------------------------------------------------------
# FastAPI review #5 — structured access log + X-Request-ID propagation
# ---------------------------------------------------------------------------


# Liveness/readiness paths. A SUCCESSFUL request to one of these is
# logged at DEBUG rather than INFO, so it does not reach the log store at
# the default level; a failing one is still logged at INFO, because a
# probe that started failing is the single most useful line in the file.
#
# Measured 2026-08-22: probe requests were 9,758 of fastapi-cc's 98,187
# console lines over two days -- ten percent of the tier's volume spent
# recording that nothing was wrong. (The 21 Aug audit put this at a third
# of the tier; a third is not what the workspace shows.)
_PROBE_PATHS = frozenset({"/health", "/ready", "/healthz", "/readyz", "/up"})


class StructuredAccessLogMiddleware(BaseHTTPMiddleware):
    """Per-request JSON log line + X-Request-ID round-trip.

    Replaces uvicorn's text access log (disabled via `--no-access-log`).
    Emits exactly one `INFO` log per request with low-cardinality fields
    safe for Loki:

      * request_id     — generated UUID4 if X-Request-ID header absent
      * method, path   — HTTP method + URL path (no query string — may have PII)
      * status         — response status code
      * duration_ms    — wall clock from middleware entry to response start
      * client         — request.client.host (real client only when
                         `--proxy-headers --forwarded-allow-ips` is set)

    The X-Request-ID is added to the response so callers can correlate
    their client-side logs with our server-side logs.

    Laravel's `StreamQueryFromFastApi` forwards `traceparent` and
    `X-Request-ID` (the query id) since 2026-08-21. Before that it sent
    neither — this docstring claimed it did, which is exactly why nobody
    noticed that every chat request got a freshly minted, unrelated trace
    id here. If you are debugging a missing join key, check the header
    array in that job rather than trusting this paragraph.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        start = time.perf_counter()
        # Make the request ID visible to downstream handlers via state —
        # they can include it in their own structured logs without
        # re-parsing headers.
        request.state.request_id = request_id

        # Module 10 Chunk 10.6 — W3C Trace Context. Accept the inbound
        # `traceparent` if it matches the v00 spec; mint otherwise.
        # Stored on request.state so handlers + outbound clients can
        # forward the same trace-id.
        traceparent = request.headers.get("traceparent")
        if not _is_valid_traceparent(traceparent):
            traceparent = _mint_traceparent()
        request.state.traceparent = traceparent
        request.state.trace_id = traceparent[3:35]  # 32-hex trace-id slice

        response: Response | None = None
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000.0, 2)
            client_host = request.client.host if request.client else None
            level = (
                logging.DEBUG
                if request.url.path in _PROBE_PATHS and status < 400
                else logging.INFO
            )
            logger.log(
                level,
                "request",
                extra={
                    "request_id": request_id,
                    "traceparent": traceparent,
                    "trace_id": request.state.trace_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": duration_ms,
                    "client": client_host,
                },
            )
            if response is not None:
                response.headers["X-Request-ID"] = request_id
                response.headers["traceparent"] = traceparent


# Module 10 Chunk 10.6 — W3C Trace Context helpers. Mirrors
# app/Http/Middleware/InjectTraceparent.php on the Laravel side; keep the
# regex + mint format byte-for-byte identical so a trace_id minted by
# either service is accepted by the other.
import re as _re  # noqa: E402
import secrets as _secrets  # noqa: E402

_TRACEPARENT_RE = _re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def _is_valid_traceparent(value: str | None) -> bool:
    if value is None:
        return False
    return bool(_TRACEPARENT_RE.match(value))


def _mint_traceparent() -> str:
    """Mint a fresh W3C v00 traceparent.

    Uses `secrets.token_hex` for cryptographically strong trace-id +
    parent-id. Always sets the `01` (sampled) flag — GeoRAG samples 100%
    of internal traces; downstream tail-sampling can drop noise later.
    """
    return f"00-{_secrets.token_hex(16)}-{_secrets.token_hex(8)}-01"
