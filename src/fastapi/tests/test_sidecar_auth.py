"""Tests for the model-sidecar shared-secret auth + batch-size guards.

Audit 2026-06-27: the embedding/reranker/sparse sidecars had no
service-to-service auth and accepted unbounded request bodies.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException


def _reload_auth(monkeypatch, key: str | None):
    """Reimport app.sidecar_auth with FASTAPI_SERVICE_KEY set/unset (it reads
    the env at import time)."""
    if key is None:
        monkeypatch.delenv("FASTAPI_SERVICE_KEY", raising=False)
    else:
        monkeypatch.setenv("FASTAPI_SERVICE_KEY", key)
    import app.sidecar_auth as mod

    return importlib.reload(mod)


@pytest.mark.asyncio
async def test_require_service_key_enforces_when_configured(monkeypatch) -> None:
    mod = _reload_auth(monkeypatch, "s3cr3t")
    # Correct key passes.
    await mod.require_service_key(x_service_key="s3cr3t")
    # Wrong / missing key → 401.
    with pytest.raises(HTTPException) as ei:
        await mod.require_service_key(x_service_key="wrong")
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException):
        await mod.require_service_key(x_service_key=None)
    assert mod.SERVICE_KEY_HEADERS == {"X-Service-Key": "s3cr3t"}


@pytest.mark.asyncio
async def test_require_service_key_skips_when_unset(monkeypatch) -> None:
    mod = _reload_auth(monkeypatch, None)
    # No key configured → check is a no-op (backward compatible), no header.
    await mod.require_service_key(x_service_key=None)
    await mod.require_service_key(x_service_key="anything")
    assert mod.SERVICE_KEY_HEADERS == {}


def test_enforce_batch_limits_rejects_too_many_items(monkeypatch) -> None:
    mod = _reload_auth(monkeypatch, None)
    with pytest.raises(HTTPException) as ei:
        mod.enforce_batch_limits(
            ["x"] * 11, max_items=10, max_total_chars=10_000, label="t"
        )
    assert ei.value.status_code == 413


def test_enforce_batch_limits_rejects_oversized_total(monkeypatch) -> None:
    mod = _reload_auth(monkeypatch, None)
    with pytest.raises(HTTPException) as ei:
        mod.enforce_batch_limits(
            ["a" * 600, "b" * 600], max_items=100, max_total_chars=1000, label="t"
        )
    assert ei.value.status_code == 413


def test_enforce_batch_limits_allows_within_caps(monkeypatch) -> None:
    mod = _reload_auth(monkeypatch, None)
    # Should not raise.
    mod.enforce_batch_limits(
        ["short", "inputs"], max_items=10, max_total_chars=10_000, label="t"
    )


def test_reload_restores_clean_module(monkeypatch) -> None:
    # Leave app.sidecar_auth in its env-default state for other tests.
    _reload_auth(monkeypatch, None)
