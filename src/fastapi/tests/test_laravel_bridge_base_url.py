"""LARAVEL_INTERNAL_URL resolution — the 2026-08-18 silent-misconfig incident.

The variable was never set on fastapi-cc or hatchet-worker-cc, so every
callback into Laravel fell back to the Herd-local default (http://laravel.test)
and died on DNS. Because each helper swallows its own exception, the only
symptom was a per-call warning reading ``err=[Errno -2] Name or service not
known`` — indistinguishable from a transient network blip. The whole real-time
layer was dead for weeks with nothing pointing at the cause.

These lock the two halves of the fix: the fallback still works (local dev must
not break) but taking it now reports ERROR, once, naming the variable.
"""
from __future__ import annotations

import logging

import pytest

from app.services import laravel_bridge


@pytest.fixture(autouse=True)
def _reset_warning_latch():
    """The latch is module state; each test needs a clean one."""
    laravel_bridge._warned_unset_base = False
    yield
    laravel_bridge._warned_unset_base = False


def test_configured_url_is_used_and_trailing_slash_stripped(monkeypatch, caplog):
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-octane-cc/")

    with caplog.at_level(logging.ERROR):
        assert laravel_bridge._laravel_base() == "http://laravel-octane-cc"

    assert caplog.records == []


def test_unset_url_falls_back_but_reports_the_variable_by_name(monkeypatch, caplog):
    monkeypatch.delenv("LARAVEL_INTERNAL_URL", raising=False)

    with caplog.at_level(logging.ERROR):
        assert laravel_bridge._laravel_base() == "http://laravel.test"

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    # Naming the variable is the entire point — a warning that only says
    # "connection failed" is what let this hide.
    assert "LARAVEL_INTERNAL_URL" in message
    assert caplog.records[0].levelno == logging.ERROR


def test_empty_string_counts_as_unset(monkeypatch, caplog):
    # An env var declared with an empty value is a misconfiguration, not a
    # deliberate choice of "" as a base URL.
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "")

    with caplog.at_level(logging.ERROR):
        assert laravel_bridge._laravel_base() == "http://laravel.test"

    assert len(caplog.records) == 1
    assert "LARAVEL_INTERNAL_URL" in caplog.records[0].getMessage()


def test_fallback_is_reported_once_not_once_per_call(monkeypatch, caplog):
    # cost_burn_watcher fires every 5 minutes; a per-call ERROR would be its
    # own noise problem.
    monkeypatch.delenv("LARAVEL_INTERNAL_URL", raising=False)

    with caplog.at_level(logging.ERROR):
        for _ in range(5):
            laravel_bridge._laravel_base()

    assert len(caplog.records) == 1
