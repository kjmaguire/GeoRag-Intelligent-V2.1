"""Pre-stream retry pacing shared by the Cohere and Bedrock chat adapters.

VEN-2 / AGT-8 (2026-09-29 audit): both adapters re-sent a 429/5xx
immediately, ignored ``Retry-After`` and charged nothing to the per-query
budgets, while the vLLM path had paced itself since the Foundry era. These
tests pin the shared pacing in ``llm_common`` and that the Bedrock chat loop
now goes through it. The Cohere adapter's own tests are in
``test_cohere_chat_adapter.py``.

Nothing here touches Bedrock or Cohere: the Bedrock client is a fake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

from app.agent import llm_common
from app.agent.llm_calls import _llm_call_counter, llm_call_budget
from app.config import settings


@pytest.fixture(autouse=True)
def slept(monkeypatch: pytest.MonkeyPatch):
    delays: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(llm_common, "_sleep", _fake_sleep)
    with llm_call_budget():
        yield delays


# ---------------------------------------------------------------------------
# Retry-After parsing and the backoff ladder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, None), ("", None), ("5", 5.0), ("1.5", 1.5), ("-3", 0.0), ("soon", None)],
)
def test_retry_after_delta_seconds(header: str | None, expected: float | None) -> None:
    assert llm_common.parse_retry_after(header) == expected


def test_retry_after_http_date() -> None:
    when = datetime.now(UTC) + timedelta(seconds=30)
    parsed = llm_common.parse_retry_after(format_datetime(when, usegmt=True))
    assert parsed is not None
    assert 25.0 <= parsed <= 31.0


def test_the_ladder_doubles_caps_and_jitters_upward_only() -> None:
    for attempt, floor in ((1, 2.0), (2, 4.0), (3, 8.0), (6, 8.0)):
        for _ in range(50):
            delay = llm_common.pre_stream_backoff_s(attempt)
            assert floor <= delay <= floor * 1.25


def test_a_longer_retry_after_wins_over_the_ladder() -> None:
    assert llm_common.pre_stream_backoff_s(1, retry_after_s=9.0) == 9.0
    assert llm_common.pre_stream_backoff_s(1, retry_after_s=0.1) >= 2.0


# ---------------------------------------------------------------------------
# The budgets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_permitted_retry_sleeps_and_charges_the_counter(slept: list[float]) -> None:
    import time

    ok = await llm_common.wait_before_pre_stream_retry(
        label="t", attempt=1, max_retries=2, started_monotonic=time.monotonic()
    )
    assert ok is True
    assert len(slept) == 1
    assert _llm_call_counter.get() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt", "counter", "gather_s", "retry_after"),
    [
        (3, 0, 180.0, None),  # past max_retries
        (1, 8, 180.0, None),  # per-query call budget spent
        (1, 0, 1.0, None),  # the wait does not fit the deadline
        (1, 0, 180.0, 600.0),  # the host asked for longer than a query can wait
    ],
)
async def test_each_budget_stops_the_retry(
    monkeypatch: pytest.MonkeyPatch,
    slept: list[float],
    attempt: int,
    counter: int,
    gather_s: float,
    retry_after: float | None,
) -> None:
    import time

    monkeypatch.setattr(settings, "MAX_LLM_CALLS_PER_QUERY", 8)
    monkeypatch.setattr(settings, "TIMEOUT_GATHER_S", gather_s)
    _llm_call_counter.set(counter)
    ok = await llm_common.wait_before_pre_stream_retry(
        label="t",
        attempt=attempt,
        max_retries=2,
        started_monotonic=time.monotonic(),
        retry_after_s=retry_after,
    )
    assert ok is False
    assert slept == []
    assert _llm_call_counter.get() == counter, "a refused retry must not be charged"


# ---------------------------------------------------------------------------
# The Bedrock chat loop goes through it
# ---------------------------------------------------------------------------


def _throttle(retry_after: str | None = None) -> Exception:
    from botocore.exceptions import ClientError

    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "slow down"},
            "ResponseMetadata": {"HTTPStatusCode": 429, "HTTPHeaders": headers},
        },
        "Converse",
    )


class _FakeClient:
    def __init__(self, outcomes: list[Any], calls: list[int]) -> None:
        self._outcomes = outcomes
        self._calls = calls

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def converse(self, **_request: Any) -> dict[str, Any]:
        self._calls.append(1)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install_bedrock(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[int]:
    import aioboto3

    calls: list[int] = []

    class _Session:
        def client(self, *_a: Any, **_k: Any) -> _FakeClient:
            return _FakeClient(outcomes, calls)

    monkeypatch.setattr(aioboto3, "Session", _Session)
    monkeypatch.setattr(settings, "LLM_BACKEND", "bedrock")
    return calls


_OK = {
    "output": {"message": {"content": [{"text": "fine"}]}},
    "usage": {"inputTokens": 3, "outputTokens": 1},
    "stopReason": "end_turn",
}


@pytest.mark.asyncio
async def test_bedrock_chat_backs_off_and_honours_retry_after(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    from app.agent.llm_bedrock import call_bedrock_llm

    calls = _install_bedrock(monkeypatch, [_throttle("6"), _OK])
    assert await call_bedrock_llm("q", 0.2) == "fine"
    assert len(calls) == 2
    assert slept == [6.0]
    assert _llm_call_counter.get() == 1


@pytest.mark.asyncio
async def test_bedrock_chat_stops_at_the_call_budget(monkeypatch: pytest.MonkeyPatch, slept: list[float]) -> None:
    from botocore.exceptions import ClientError

    from app.agent.llm_bedrock import call_bedrock_llm

    calls = _install_bedrock(monkeypatch, [_throttle(), _throttle(), _OK])
    monkeypatch.setattr(settings, "MAX_LLM_CALLS_PER_QUERY", 3)
    _llm_call_counter.set(3)
    with pytest.raises(ClientError):
        await call_bedrock_llm("q", 0.2)
    assert len(calls) == 1
    assert slept == []
