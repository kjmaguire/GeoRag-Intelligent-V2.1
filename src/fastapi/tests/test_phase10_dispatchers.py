"""Phase G overnight — tests for the surviving PagerDuty dispatcher.

The dispatcher is an HTTP wrapper around an external system. The tests
stub the httpx client with `types.SimpleNamespace` mocks so they run
without network or a live PagerDuty instance.

Coverage matrix:
* disabled-by-default (empty config → no-op result)
* happy path (200 OK → dispatched / paged)
* upstream 4xx (records reason + body excerpt)
* network failure (HTTPError caught + logged + envelope returned)
* PagerDuty severity mapping (critical/high/medium/low → pd severity)
* PagerDuty dedup_key idempotency contract (ticket_id is the dedup_key)
"""
from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest


def test_retired_kestra_dispatcher_is_not_importable() -> None:
    assert importlib.util.find_spec("app.services.dispatchers.kestra") is None


# ─────────────────────── pagerduty ───────────────────────────


@pytest.mark.asyncio
async def test_pagerduty_disabled_when_key_empty() -> None:
    from app.services.dispatchers.pagerduty import create_pagerduty_incident

    with patch("app.services.dispatchers.pagerduty.settings") as m:
        m.PAGERDUTY_INTEGRATION_KEY = ""
        m.PAGERDUTY_API_URL = "https://events.pagerduty.com/v2/enqueue"
        out = await create_pagerduty_incident(
            ticket_id="t1",
            severity="critical",
            summary="Test ticket",
        )

    assert out["paged"] is False
    assert out["reason"] == "pagerduty_disabled"
    assert out["dedup_key"] == "t1"


@pytest.mark.asyncio
async def test_pagerduty_happy_path_uses_ticket_id_as_dedup_key() -> None:
    from app.services.dispatchers.pagerduty import create_pagerduty_incident

    captured: dict = {}

    class _FakeClient:
        async def post(self, url, json=None):
            captured["url"] = url
            captured["payload"] = json
            return SimpleNamespace(status_code=202, text='{"status":"success"}')

    with patch("app.services.dispatchers.pagerduty.settings") as m:
        m.PAGERDUTY_INTEGRATION_KEY = "key-32chars-redacted"
        m.PAGERDUTY_API_URL = "https://events.pagerduty.com/v2/enqueue"
        m.PAGERDUTY_HTTP_TIMEOUT_S = 5.0
        out = await create_pagerduty_incident(
            ticket_id="ticket-uuid-abc",
            severity="critical",
            summary="GeoRAG outage",
            custom_details={"sla_minutes": 15},
            http_client=_FakeClient(),
        )

    assert out["paged"] is True
    assert out["dedup_key"] == "ticket-uuid-abc"
    assert out["pd_severity"] == "critical"
    p = captured["payload"]
    assert p["routing_key"] == "key-32chars-redacted"
    assert p["event_action"] == "trigger"
    assert p["dedup_key"] == "ticket-uuid-abc"
    assert p["payload"]["summary"] == "GeoRAG outage"
    assert p["payload"]["severity"] == "critical"
    assert p["payload"]["source"] == "georag-support-cockpit"
    assert p["payload"]["custom_details"]["sla_minutes"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cockpit_sev,pd_sev",
    [
        ("critical", "critical"),
        ("high",     "error"),
        ("medium",   "warning"),
        ("low",      "info"),
        ("unknown",  "warning"),  # fallback when severity not in map
    ],
)
async def test_pagerduty_severity_mapping(cockpit_sev: str, pd_sev: str) -> None:
    from app.services.dispatchers.pagerduty import create_pagerduty_incident

    captured: dict = {}

    class _FakeClient:
        async def post(self, url, json=None):
            captured["payload"] = json
            return SimpleNamespace(status_code=202, text="ok")

    with patch("app.services.dispatchers.pagerduty.settings") as m:
        m.PAGERDUTY_INTEGRATION_KEY = "key"
        m.PAGERDUTY_API_URL = "https://events.pagerduty.com/v2/enqueue"
        m.PAGERDUTY_HTTP_TIMEOUT_S = 5.0
        await create_pagerduty_incident(
            ticket_id="t1",
            severity=cockpit_sev,
            summary="test",
            http_client=_FakeClient(),
        )

    assert captured["payload"]["payload"]["severity"] == pd_sev


@pytest.mark.asyncio
async def test_pagerduty_records_upstream_4xx() -> None:
    from app.services.dispatchers.pagerduty import create_pagerduty_incident

    class _FakeClient:
        async def post(self, url, json=None):
            return SimpleNamespace(
                status_code=400,
                text='{"status":"invalid event","errors":["bad routing_key"]}',
            )

    with patch("app.services.dispatchers.pagerduty.settings") as m:
        m.PAGERDUTY_INTEGRATION_KEY = "bad-key"
        m.PAGERDUTY_API_URL = "https://events.pagerduty.com/v2/enqueue"
        m.PAGERDUTY_HTTP_TIMEOUT_S = 5.0
        out = await create_pagerduty_incident(
            ticket_id="t1",
            severity="critical",
            summary="test",
            http_client=_FakeClient(),
        )

    assert out["paged"] is False
    assert out["reason"] == "pagerduty_http_error"
    assert out["status_code"] == 400
    assert "bad routing_key" in (out["error"] or "")


@pytest.mark.asyncio
async def test_pagerduty_records_network_error() -> None:
    from app.services.dispatchers.pagerduty import create_pagerduty_incident

    class _FakeClient:
        async def post(self, url, json=None):
            raise httpx.ConnectError("dns resolution failed")

    with patch("app.services.dispatchers.pagerduty.settings") as m:
        m.PAGERDUTY_INTEGRATION_KEY = "key"
        m.PAGERDUTY_API_URL = "https://events.pagerduty.com/v2/enqueue"
        m.PAGERDUTY_HTTP_TIMEOUT_S = 5.0
        out = await create_pagerduty_incident(
            ticket_id="t1",
            severity="critical",
            summary="test",
            http_client=_FakeClient(),
        )

    assert out["paged"] is False
    assert out["reason"] == "pagerduty_network_error"
    assert "ConnectError" in (out["error"] or "")


@pytest.mark.asyncio
async def test_pagerduty_summary_truncates_to_1024_chars() -> None:
    from app.services.dispatchers.pagerduty import create_pagerduty_incident

    captured: dict = {}

    class _FakeClient:
        async def post(self, url, json=None):
            captured["payload"] = json
            return SimpleNamespace(status_code=202, text="ok")

    with patch("app.services.dispatchers.pagerduty.settings") as m:
        m.PAGERDUTY_INTEGRATION_KEY = "k"
        m.PAGERDUTY_API_URL = "https://events.pagerduty.com/v2/enqueue"
        m.PAGERDUTY_HTTP_TIMEOUT_S = 5.0
        await create_pagerduty_incident(
            ticket_id="t1",
            severity="critical",
            summary="x" * 2000,
            http_client=_FakeClient(),
        )

    assert len(captured["payload"]["payload"]["summary"]) == 1024
