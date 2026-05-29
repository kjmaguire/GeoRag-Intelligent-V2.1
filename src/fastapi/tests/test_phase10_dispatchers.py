"""Phase G overnight — tests for the Kestra + PagerDuty dispatchers.

Both dispatchers are HTTP wrappers around external systems. The tests
stub the httpx client with `types.SimpleNamespace` mocks so they run
without network or a live Kestra / PagerDuty instance.

Coverage matrix:
* disabled-by-default (empty config → no-op result)
* happy path (200 OK → dispatched / paged)
* upstream 4xx (records reason + body excerpt)
* network failure (HTTPError caught + logged + envelope returned)
* PagerDuty severity mapping (critical/high/medium/low → pd severity)
* PagerDuty dedup_key idempotency contract (ticket_id is the dedup_key)
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest


# ─────────────────────── kestra ──────────────────────────────


@pytest.mark.asyncio
async def test_kestra_disabled_when_url_empty() -> None:
    from app.services.dispatchers.kestra import dispatch_support_packet_to_kestra

    with patch("app.services.dispatchers.kestra.settings") as m:
        m.KESTRA_URL = ""
        out = await dispatch_support_packet_to_kestra({"ticket_id": "t1"})
    assert out == {
        "dispatched": False,
        "reason": "kestra_disabled",
        "execution_id": None,
        "url": None,
        "status_code": None,
        "error": None,
    }


@pytest.mark.asyncio
async def test_kestra_happy_path_returns_execution_id() -> None:
    from app.services.dispatchers.kestra import dispatch_support_packet_to_kestra

    captured: dict = {}

    class _FakeClient:
        async def post(self, url, data=None, headers=None):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"id": "exec-abc-123", "state": "RUNNING"},
            )

    with patch("app.services.dispatchers.kestra.settings") as m:
        m.KESTRA_URL = "https://kestra.example.com"
        m.KESTRA_FLOW_NAMESPACE = "georag.support"
        m.KESTRA_FLOW_ID = "support_packet_received"
        m.KESTRA_FLOW_AUTH_TOKEN = "tok-secret"
        m.KESTRA_HTTP_TIMEOUT_S = 5.0
        out = await dispatch_support_packet_to_kestra(
            {"ticket_id": "t1", "workspace_id": "w1"},
            http_client=_FakeClient(),
        )

    assert out["dispatched"] is True
    assert out["execution_id"] == "exec-abc-123"
    assert out["status_code"] == 200
    assert out["reason"] is None
    assert (
        captured["url"]
        == "https://kestra.example.com/api/v1/executions/georag.support/support_packet_received"
    )
    assert captured["headers"]["Authorization"] == "Bearer tok-secret"
    assert captured["headers"]["X-Kestra-Trigger-Source"] == "support_packet"
    # Bundle is JSON-serialised under the `payload` form field.
    assert "payload" in captured["data"]
    decoded = json.loads(captured["data"]["payload"])
    assert decoded["ticket_id"] == "t1"


@pytest.mark.asyncio
async def test_kestra_no_auth_header_when_token_empty() -> None:
    from app.services.dispatchers.kestra import dispatch_support_packet_to_kestra

    captured: dict = {}

    class _FakeClient:
        async def post(self, url, data=None, headers=None):
            captured["headers"] = headers
            return SimpleNamespace(status_code=200, json=lambda: {"id": "x"})

    with patch("app.services.dispatchers.kestra.settings") as m:
        m.KESTRA_URL = "https://kestra.example.com"
        m.KESTRA_FLOW_NAMESPACE = "ns"
        m.KESTRA_FLOW_ID = "flow"
        m.KESTRA_FLOW_AUTH_TOKEN = ""
        m.KESTRA_HTTP_TIMEOUT_S = 5.0
        await dispatch_support_packet_to_kestra(
            {"ticket_id": "t1"}, http_client=_FakeClient()
        )

    assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_kestra_records_upstream_4xx_body() -> None:
    from app.services.dispatchers.kestra import dispatch_support_packet_to_kestra

    class _FakeClient:
        async def post(self, url, data=None, headers=None):
            return SimpleNamespace(
                status_code=422,
                text="flow not found",
                json=lambda: {},
            )

    with patch("app.services.dispatchers.kestra.settings") as m:
        m.KESTRA_URL = "https://kestra.example.com"
        m.KESTRA_FLOW_NAMESPACE = "ns"
        m.KESTRA_FLOW_ID = "missing"
        m.KESTRA_FLOW_AUTH_TOKEN = ""
        m.KESTRA_HTTP_TIMEOUT_S = 5.0
        out = await dispatch_support_packet_to_kestra(
            {"ticket_id": "t1"}, http_client=_FakeClient()
        )

    assert out["dispatched"] is False
    assert out["reason"] == "kestra_http_error"
    assert out["status_code"] == 422
    assert "flow not found" in (out["error"] or "")


@pytest.mark.asyncio
async def test_kestra_records_network_error() -> None:
    from app.services.dispatchers.kestra import dispatch_support_packet_to_kestra

    class _FakeClient:
        async def post(self, url, data=None, headers=None):
            raise httpx.ConnectTimeout("connection timed out")

    with patch("app.services.dispatchers.kestra.settings") as m:
        m.KESTRA_URL = "https://kestra.example.com"
        m.KESTRA_FLOW_NAMESPACE = "ns"
        m.KESTRA_FLOW_ID = "flow"
        m.KESTRA_FLOW_AUTH_TOKEN = ""
        m.KESTRA_HTTP_TIMEOUT_S = 5.0
        out = await dispatch_support_packet_to_kestra(
            {"ticket_id": "t1"}, http_client=_FakeClient()
        )

    assert out["dispatched"] is False
    assert out["reason"] == "kestra_network_error"
    assert "ConnectTimeout" in (out["error"] or "")


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
