"""Lean shared-secret auth + body-size guards for the model sidecars.

The embedding / reranker / sparse sidecars deliberately avoid importing
``app.config`` (Settings) so they stay lean (no DB / full-secret deps). The
server-side check therefore reads ``FASTAPI_SERVICE_KEY`` straight from the
environment and does a constant-time compare against the ``X-Service-Key``
request header — the same shared secret the main FastAPI service and the
Laravel bridge use.

Audit 2026-06-27: before this, ``/embed``, ``/rerank`` and ``/sparse`` performed
NO service-to-service auth (unlike the main FastAPI routers) and accepted
unbounded request bodies (arbitrary ``sentences`` / ``pairs`` / ``texts`` →
trivial memory-exhaustion DoS on the shared model host).

Auth behaviour (audit 2026-07-01 — fail CLOSED, mirroring the
MULTI_TENANT_ENFORCEMENT_ENABLED / SINGLE_TENANT_MODE loud-failure pattern in
app/config.py):
  - ``FASTAPI_SERVICE_KEY`` set   → ``X-Service-Key`` required + must match,
    else HTTP 401.
  - ``FASTAPI_SERVICE_KEY`` unset → keyed routes REFUSE with HTTP 503 unless
    ``SIDECAR_AUTH_OPTIONAL=true`` is set explicitly. The previous behaviour
    (unset key → auth silently skipped) meant a deployment that delivered the
    key via pydantic-settings' .env file — or simply forgot the env on the
    sidecar — ran the model host wide open with only an import-time log line
    to show for it. Loud failure beats silent insecurity; the operator either
    wires the key (compose already passes it) or opts out in writing.

Client side: ``SERVICE_KEY_HEADERS`` is what the in-process proxies
(_RemoteEmbedding / _RemoteReranker / _remote_encode_sparse) attach to their
requests. It prefers the process env but falls back to
``app.config.settings.FASTAPI_SERVICE_KEY`` — the main FastAPI service may
legitimately receive the key via the .env file rather than process env, and
without the fallback such a deployment would send NO header and 401 against a
keyed sidecar. The guarded import simply fails on the sidecars themselves
(Settings demands DB secrets they don't have), leaving them env-only.
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

# Server-side enforcement key — process env ONLY (the sidecars cannot import
# app.config; see module docstring).
_SERVICE_KEY = (os.environ.get("FASTAPI_SERVICE_KEY") or "").strip()

# Explicit, in-writing opt-out of sidecar auth (e.g. an air-gapped single-host
# dev loop). Without this, an unset key fails closed with HTTP 503.
_AUTH_OPTIONAL = (
    os.environ.get("SIDECAR_AUTH_OPTIONAL", "").strip().lower()
    in ("1", "true", "yes", "on")
)

if not _SERVICE_KEY:
    if _AUTH_OPTIONAL:
        logger.warning(
            "sidecar_auth: FASTAPI_SERVICE_KEY unset and SIDECAR_AUTH_OPTIONAL "
            "is set — sidecar endpoints accept UNAUTHENTICATED requests "
            "(explicit operator opt-out)."
        )
    else:
        logger.critical(
            "sidecar_auth: FASTAPI_SERVICE_KEY unset — sidecar model routes "
            "will refuse every request with HTTP 503. Set FASTAPI_SERVICE_KEY "
            "on the sidecar (compose passes it), or set "
            "SIDECAR_AUTH_OPTIONAL=true to explicitly run unauthenticated."
        )


async def require_service_key(
    x_service_key: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: enforce the shared X-Service-Key.

    Fail-closed when no key is configured (503) unless SIDECAR_AUTH_OPTIONAL
    is set. Comparison is constant-time over the UTF-8 bytes — comparing the
    ``str`` values directly would raise TypeError (→ 500) on a non-ASCII
    header, which HTTP permits (audit 2026-07-01).
    """
    if not _SERVICE_KEY:
        if _AUTH_OPTIONAL:
            return  # explicit opt-out (logged at import)
        raise HTTPException(
            status_code=503,
            detail=(
                "sidecar auth not configured: FASTAPI_SERVICE_KEY is unset on "
                "this sidecar. Set it (or SIDECAR_AUTH_OPTIONAL=true to "
                "explicitly run unauthenticated)."
            ),
        )
    if not x_service_key or not hmac.compare_digest(
        x_service_key.encode("utf-8"), _SERVICE_KEY.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid or missing X-Service-Key")


def enforce_batch_limits(
    items: list,
    *,
    max_items: int,
    max_total_chars: int,
    label: str,
) -> None:
    """Reject oversized batches (HTTP 413) before they hit the model.

    ``max_total_chars`` is summed over ``str(item)`` so it bounds both very long
    single inputs and very large batches. A sidecar serves the per-query path
    (a handful of short inputs); the bulk Dagster path uses its own in-process
    model, so these caps are generous for legitimate traffic.
    """
    if len(items) > max_items:
        raise HTTPException(
            status_code=413,
            detail=f"{label}: batch of {len(items)} exceeds max {max_items}",
        )
    total = 0
    for it in items:
        total += len(it) if isinstance(it, str) else len(str(it))
        if total > max_total_chars:
            raise HTTPException(
                status_code=413,
                detail=f"{label}: total payload exceeds {max_total_chars} chars",
            )


# Body-size ceiling for sidecar requests. Sized to comfortably fit the largest
# legitimate batch the char-based guards allow (RERANK_MAX_TOTAL_CHARS is 4M;
# JSON escaping/overhead roughly doubles that in the worst case).
_MAX_BODY_BYTES = int(os.environ.get("SIDECAR_MAX_BODY_BYTES", str(16 * 1024 * 1024)))


def install_body_size_limit(app, max_bytes: int = _MAX_BODY_BYTES) -> None:
    """Reject oversized request bodies by Content-Length (HTTP 413).

    ``enforce_batch_limits`` runs only AFTER pydantic has parsed the JSON body,
    which means a multi-GB body is fully read and decoded before any guard
    fires. This middleware refuses on the declared Content-Length header
    before the route handler ever reads the body. Bodies without the header
    (chunked) fall through to the post-parse guards.
    """
    from starlette.responses import JSONResponse  # noqa: PLC0415

    @app.middleware("http")
    async def _body_size_limit(request, call_next):  # noqa: ANN001, ANN202
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                return JSONResponse(
                    status_code=400, content={"detail": "invalid Content-Length"}
                )
            if declared > max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": f"request body of {declared} B exceeds {max_bytes} B"
                    },
                )
        return await call_next(request)


def _client_service_key() -> str:
    """Resolve the key the CLIENT proxies should send.

    Process env first; falls back to app.config settings (which may have read
    the key from the .env file — audit 2026-07-01: env-only resolution made
    .env-file-only deployments send no header and 401 against a keyed
    sidecar). The guarded import fails harmlessly on the sidecars themselves.
    """
    key = (os.environ.get("FASTAPI_SERVICE_KEY") or "").strip()
    if key:
        return key
    try:
        from app.config import settings  # noqa: PLC0415

        return (settings.FASTAPI_SERVICE_KEY or "").strip()
    except Exception:
        return ""


# Client header helper — read once; empty dict when no key is resolvable so the
# proxies stay well-formed against an explicitly-unauthenticated sidecar.
_CLIENT_KEY = _client_service_key()
SERVICE_KEY_HEADERS: dict[str, str] = (
    {"X-Service-Key": _CLIENT_KEY} if _CLIENT_KEY else {}
)
