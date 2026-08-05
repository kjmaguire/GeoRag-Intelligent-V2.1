"""app.services._foundry_retry — pure logic, no network, no real sleeps."""
from __future__ import annotations

import pytest

from app.services import _foundry_retry as retry_mod
from app.services._foundry_retry import with_foundry_retry


class _Resp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every test in this module should run in milliseconds, not seconds."""
    monkeypatch.setattr(retry_mod.time, "sleep", lambda _s: None)


def test_success_first_try_no_retry():
    calls = []

    def do_post():
        calls.append(1)
        return _Resp(200)

    resp = with_foundry_retry(do_post, label="t")
    assert resp.status_code == 200
    assert len(calls) == 1


def test_retries_on_429_then_succeeds():
    responses = iter([_Resp(429), _Resp(429), _Resp(200)])

    def do_post():
        return next(responses)

    resp = with_foundry_retry(do_post, label="t", max_retries=4)
    assert resp.status_code == 200


def test_honors_numeric_retry_after(monkeypatch):
    delays = []
    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: delays.append(s))

    responses = iter([_Resp(429, headers={"retry-after": "7"}), _Resp(200)])

    def do_post():
        return next(responses)

    with_foundry_retry(do_post, label="t")
    assert delays == [7.0]


def test_exhausts_retries_and_raises():
    def do_post():
        return _Resp(503)

    with pytest.raises(RuntimeError):
        with_foundry_retry(do_post, label="t", max_retries=2)


def test_non_retryable_status_raises_immediately():
    calls = []

    def do_post():
        calls.append(1)
        return _Resp(400)

    with pytest.raises(RuntimeError):
        with_foundry_retry(do_post, label="t", max_retries=4)
    assert len(calls) == 1
