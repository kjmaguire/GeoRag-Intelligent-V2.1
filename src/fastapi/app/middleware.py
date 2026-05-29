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

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FastAPI review #1 — body-size limit
# ---------------------------------------------------------------------------


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds ``max_bytes``.

    Two paths:
      * Fast path — Content-Length header present and exceeds the limit:
        return 413 immediately, before reading any body.
      * Slow path — chunked transfer with no Content-Length header:
        let the request proceed; Starlette's body buffer enforces the
        cap via `max_request_size` set on the app instance.

    Why a custom middleware instead of just relying on Starlette's
    `client_max_size`? Two reasons:
      (a) Starlette's setting only kicks in when the body is *consumed*.
          A handler that reads the body in chunks (we don't, but) might
          still process the leading megabytes before erroring.
      (b) An explicit 413 + structured log line is friendlier than the
          generic 500 you'd otherwise get.
    """

    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > self.max_bytes:
            logger.warning(
                "BodySizeLimitMiddleware: rejected request — content-length=%s "
                "max=%d path=%s",
                cl,
                self.max_bytes,
                request.url.path,
            )
            return JSONResponse(
                {"detail": "Request body too large"},
                status_code=413,
            )
        return await call_next(request)


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
        except asyncio.TimeoutError:
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
    their client-side logs with our server-side logs. Laravel's
    `StreamQueryFromFastApi` already forwards the inbound header.
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
            logger.info(
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
