"""Lean shared-secret auth + body-size guards for the model sidecars.

The embedding / reranker / sparse sidecars deliberately avoid importing
``app.config`` (Settings) so they stay lean (no DB / full-secret deps). This
module therefore reads ``FASTAPI_SERVICE_KEY`` straight from the environment and
does a constant-time compare against the ``X-Service-Key`` request header — the
same shared secret the main FastAPI service and the Laravel bridge use.

Audit 2026-06-27: before this, ``/embed``, ``/rerank`` and ``/sparse`` performed
NO service-to-service auth (unlike the main FastAPI routers) and accepted
unbounded request bodies (arbitrary ``sentences`` / ``pairs`` / ``texts`` →
trivial memory-exhaustion DoS on the shared model host).

Auth behaviour:
  - ``FASTAPI_SERVICE_KEY`` set   → ``X-Service-Key`` required + must match,
    else HTTP 401.
  - ``FASTAPI_SERVICE_KEY`` unset → check skipped (logged once at import). This
    keeps any deploy that hasn't yet wired the key on the sidecar working; set
    the key (compose already passes it to the sidecars) to enforce.
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

_SERVICE_KEY = (os.environ.get("FASTAPI_SERVICE_KEY") or "").strip()

if not _SERVICE_KEY:
    logger.warning(
        "sidecar_auth: FASTAPI_SERVICE_KEY unset — sidecar endpoints accept "
        "UNAUTHENTICATED requests. Set FASTAPI_SERVICE_KEY on the sidecar "
        "(compose passes it) to enforce service-to-service auth."
    )


async def require_service_key(
    x_service_key: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: enforce the shared X-Service-Key when configured."""
    if not _SERVICE_KEY:
        return  # not configured → skip (logged at import)
    if not x_service_key or not hmac.compare_digest(x_service_key, _SERVICE_KEY):
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


# Client header helper — read once; empty dict when the key is unset so the
# proxies stay backward-compatible with an un-keyed sidecar.
SERVICE_KEY_HEADERS: dict[str, str] = (
    {"X-Service-Key": _SERVICE_KEY} if _SERVICE_KEY else {}
)
