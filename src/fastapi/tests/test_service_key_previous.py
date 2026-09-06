"""X-Service-Key accepts the PREVIOUS key during a rotation (2026-09-06).

The kid-based JWT overlap (V1.5-03) let FastAPI verify tokens signed with
either the current or the outgoing key, but every internal call also sends
the ``X-Service-Key`` header, and ``verify_service_key`` compared that
against the primary alone. So a FASTAPI_SERVICE_KEY rotation was a 401
storm from the moment fastapi-cc restarted until every caller restarted
too — ``ops/runbooks/secret-rotation.md`` § 3 had to document it as
not-zero-downtime. ``service_key_matches`` closes that: primary or
previous, constant-time, and the three router-local ``_check_service_key``
clones delegate to it (they used a plain ``!=`` before).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.services.auth as auth
from app.config import require_min_service_key_bytes, settings

PRIMARY = "primary-key-0123456789abcdef0123456789abcdef"
PREVIOUS = "previous-key-0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY", PRIMARY)
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY_PREVIOUS", "")
    monkeypatch.setattr(auth, "_previous_key_seen", False)


# ---------------------------------------------------------------------------
# service_key_matches
# ---------------------------------------------------------------------------


def test_primary_matches_and_previous_rejected_when_no_rotation() -> None:
    assert auth.service_key_matches(PRIMARY) is True
    assert auth.service_key_matches(PREVIOUS) is False
    assert auth.service_key_matches("wrong") is False


def test_previous_accepted_during_rotation(monkeypatch) -> None:
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY_PREVIOUS", PREVIOUS)
    assert auth.service_key_matches(PRIMARY) is True
    assert auth.service_key_matches(PREVIOUS) is True
    assert auth.service_key_matches("wrong") is False


def test_missing_or_empty_header_never_matches(monkeypatch) -> None:
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY_PREVIOUS", PREVIOUS)
    assert auth.service_key_matches(None) is False
    assert auth.service_key_matches("") is False


def test_no_configured_key_never_matches(monkeypatch) -> None:
    """compare_digest("", "") is True; an unset key must not authenticate."""
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY", "")
    assert auth.service_key_matches("") is False
    assert auth.service_key_matches("anything") is False


def test_previous_key_use_is_logged_once(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY_PREVIOUS", PREVIOUS)
    with caplog.at_level("WARNING", logger=auth.logger.name):
        assert auth.service_key_matches(PREVIOUS)
        assert auth.service_key_matches(PREVIOUS)
        assert auth.service_key_matches(PRIMARY)
    hits = [r for r in caplog.records if "FASTAPI_SERVICE_KEY_PREVIOUS" in r.getMessage()]
    assert len(hits) == 1
    assert PREVIOUS not in hits[0].getMessage()


# ---------------------------------------------------------------------------
# verify_service_key — the canonical dependency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_service_key_accepts_both_during_rotation(monkeypatch) -> None:
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY_PREVIOUS", PREVIOUS)
    await auth.verify_service_key(x_service_key=PRIMARY)
    await auth.verify_service_key(x_service_key=PREVIOUS)
    with pytest.raises(HTTPException) as ei:
        await auth.verify_service_key(x_service_key="wrong")
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_service_key_rejects_previous_outside_rotation() -> None:
    with pytest.raises(HTTPException) as ei:
        await auth.verify_service_key(x_service_key=PREVIOUS)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_service_key_500_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY", "")
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY_PREVIOUS", PREVIOUS)
    with pytest.raises(HTTPException) as ei:
        await auth.verify_service_key(x_service_key=PREVIOUS)
    assert ei.value.status_code == 500


# ---------------------------------------------------------------------------
# The three router-local clones
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    [
        "app.routers.shadow_trigger",
        "app.routers.mv_refresh_trigger",
        "app.routers.metrics_ingestion_events",
    ],
)
def test_local_clones_accept_previous_and_reject_missing(module_path, monkeypatch) -> None:
    import importlib

    check = importlib.import_module(module_path)._check_service_key
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY_PREVIOUS", PREVIOUS)

    check(x_service_key=PRIMARY)
    check(x_service_key=PREVIOUS)
    for bad in ("wrong", None):
        with pytest.raises(HTTPException) as ei:
            check(x_service_key=bad)
        assert ei.value.status_code == 401

    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY", "")
    with pytest.raises(HTTPException) as ei:
        check(x_service_key=PREVIOUS)
    assert ei.value.status_code == 500


# ---------------------------------------------------------------------------
# Settings floor applies to the previous key too
# ---------------------------------------------------------------------------


def test_previous_key_floor() -> None:
    assert require_min_service_key_bytes(PREVIOUS, "X") == PREVIOUS
    with pytest.raises(ValueError, match="FASTAPI_SERVICE_KEY_PREVIOUS is 5 bytes"):
        require_min_service_key_bytes("short", "FASTAPI_SERVICE_KEY_PREVIOUS")
    # The Settings validator lets an empty previous key through — that is
    # the steady state — and applies the floor to anything else.
    assert settings.__class__._validate_previous_service_key_length("") == ""
    with pytest.raises(ValueError):
        settings.__class__._validate_previous_service_key_length("short")
