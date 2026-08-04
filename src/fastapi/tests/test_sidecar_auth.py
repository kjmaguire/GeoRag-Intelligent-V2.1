"""Tests for the model-sidecar shared-secret auth + batch-size guards.

Audit 2026-06-27: the embedding/reranker/sparse sidecars had no
service-to-service auth and accepted unbounded request bodies.

Audit 2026-07-01: auth is now fail-CLOSED — an unset FASTAPI_SERVICE_KEY
refuses keyed routes with HTTP 503 unless SIDECAR_AUTH_OPTIONAL=true is set
explicitly. The client-side SERVICE_KEY_HEADERS falls back to
app.config.settings so .env-file-only deployments still authenticate.

Behaviour toggles are exercised via monkeypatch.setattr on the module globals
(auto-restored per test) rather than module reloads, so no auth state leaks
into later test modules. The two import-time header-resolution tests that do
need a reload restore the module in a ``finally``.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException

import app.sidecar_auth as sidecar_auth

# ---------------------------------------------------------------------------
# require_service_key — enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_service_key_enforces_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "s3cr3t")
    # Correct key passes.
    await sidecar_auth.require_service_key(x_service_key="s3cr3t")
    # Wrong / missing key → 401.
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key="wrong")
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key=None)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_require_service_key_fails_closed_503_when_unset(monkeypatch) -> None:
    """Audit 2026-07-01: unset key must REFUSE (503), not silently skip."""
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "")
    monkeypatch.setattr(sidecar_auth, "_AUTH_OPTIONAL", False)
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key=None)
    assert ei.value.status_code == 503
    # Even a caller SENDING a key is refused — there is nothing to compare to.
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key="anything")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_explicit_opt_out_allows_unauthenticated(monkeypatch) -> None:
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "")
    monkeypatch.setattr(sidecar_auth, "_AUTH_OPTIONAL", True)
    # Explicit SIDECAR_AUTH_OPTIONAL → no-op, no exception.
    await sidecar_auth.require_service_key(x_service_key=None)
    await sidecar_auth.require_service_key(x_service_key="anything")


@pytest.mark.asyncio
async def test_non_ascii_header_is_401_not_500(monkeypatch) -> None:
    """Audit 2026-07-01: hmac.compare_digest on str raises TypeError for
    non-ASCII input (HTTP headers may carry latin-1) → was an unhandled 500.
    Bytes comparison must yield a clean 401."""
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "s3cr3t")
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key="clé-ключ-ø")
    assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# SERVICE_KEY_HEADERS — client-side resolution (import-time; needs reloads)
# ---------------------------------------------------------------------------


def test_client_headers_from_env(monkeypatch) -> None:
    try:
        monkeypatch.setenv("FASTAPI_SERVICE_KEY", "env-key-abcdefghijklmnopqrstuvwxyz123456")
        mod = importlib.reload(sidecar_auth)
        assert mod.SERVICE_KEY_HEADERS == {
            "X-Service-Key": "env-key-abcdefghijklmnopqrstuvwxyz123456"
        }
        assert mod._SERVICE_KEY == "env-key-abcdefghijklmnopqrstuvwxyz123456"
    finally:
        monkeypatch.undo()
        importlib.reload(sidecar_auth)


def test_client_headers_fall_back_to_settings_when_env_unset(monkeypatch) -> None:
    """Audit 2026-07-01: the main service may get the key via pydantic-settings'
    .env file rather than process env; the client proxies must still send it.
    Server-side enforcement stays env-only (the sidecars can't import Settings).
    """
    from app.config import settings  # importable in the test env

    try:
        monkeypatch.delenv("FASTAPI_SERVICE_KEY", raising=False)
        mod = importlib.reload(sidecar_auth)
        assert {
            "X-Service-Key": settings.FASTAPI_SERVICE_KEY.strip()
        } == mod.SERVICE_KEY_HEADERS
        # Server-side key is env-only → unset here (fail-closed 503 behavior).
        assert mod._SERVICE_KEY == ""
    finally:
        monkeypatch.undo()
        importlib.reload(sidecar_auth)


# ---------------------------------------------------------------------------
# enforce_batch_limits
# ---------------------------------------------------------------------------


def test_enforce_batch_limits_rejects_too_many_items() -> None:
    with pytest.raises(HTTPException) as ei:
        sidecar_auth.enforce_batch_limits(
            ["x"] * 11, max_items=10, max_total_chars=10_000, label="t"
        )
    assert ei.value.status_code == 413


def test_enforce_batch_limits_rejects_oversized_total() -> None:
    with pytest.raises(HTTPException) as ei:
        sidecar_auth.enforce_batch_limits(
            ["a" * 600, "b" * 600], max_items=100, max_total_chars=1000, label="t"
        )
    assert ei.value.status_code == 413


def test_enforce_batch_limits_allows_within_caps() -> None:
    # Should not raise.
    sidecar_auth.enforce_batch_limits(
        ["short", "inputs"], max_items=10, max_total_chars=10_000, label="t"
    )
