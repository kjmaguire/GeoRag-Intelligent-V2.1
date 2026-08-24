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


# ---------------------------------------------------------------------------
# OBS-14 — where a cost-burn alert actually goes
# ---------------------------------------------------------------------------


class TestTheEscalationPathThatExists:
    """PagerDuty is not it, and the module now says so.

    `create_pagerduty_incident` above is a complete Events v2 client with
    dedup keys and a severity map, and has never had a caller.
    PAGERDUTY_INTEGRATION_KEY is empty and set on no container app. Read
    without context it implies the platform can page a human; it cannot.

    What exists is the log-marker route: a detector logs a distinctive
    string, a Log Analytics scheduled query rule matches it, and
    `georag-alerts-ag` emails a real address. These tests pin that the
    cost-burn detector is on it, because until 2026-08-22 its "high"
    severity alert — the one that precedes suspending a workspace's LLM
    activity — terminated in a database row.
    """

    def test_the_dispatcher_says_it_is_not_wired(self) -> None:
        from app.services.dispatchers import pagerduty

        assert pagerduty.__doc__ is not None
        assert "NOT WIRED" in pagerduty.__doc__

    def test_the_dispatcher_still_has_no_caller(self) -> None:
        """If someone wires it, this fails — and the NOT WIRED banner
        needs to come off in the same change."""
        from pathlib import Path

        app_dir = Path(__file__).resolve().parent.parent / "app"
        callers = []

        for path in app_dir.rglob("*.py"):
            if "dispatchers" in path.parts:
                continue
            if "create_pagerduty_incident" in path.read_text(
                encoding="utf-8", errors="replace"
            ):
                callers.append(str(path.name))

        assert callers == [], (
            f"{callers} now call the PagerDuty dispatcher. Good — but remove "
            "the NOT WIRED banner from services/dispatchers/pagerduty.py, "
            "and set PAGERDUTY_INTEGRATION_KEY, or it still cannot page."
        )

    def test_the_cost_burn_alert_leaves_the_database(self) -> None:
        import inspect

        from app.hatchet_workflows import cost_burn_watcher

        assert cost_burn_watcher.COST_BURN_ALERT_MARKER

        source = inspect.getsource(cost_burn_watcher)
        assert "COST_BURN_ALERT_MARKER," in source, (
            "the marker is defined but never logged, so the alert still "
            "ends in audit.audit_ledger and reaches nobody"
        )

    def test_the_marker_is_distinctive_enough_to_match_on(self) -> None:
        """It is matched with `Log_s has '<marker>'`. A short or
        lower-case marker would match ordinary log prose and stack
        traces, and the alert would fire on nothing."""
        from app.hatchet_workflows.cost_burn_watcher import (
            COST_BURN_ALERT_MARKER,
        )

        assert COST_BURN_ALERT_MARKER.isupper()
        assert "_" in COST_BURN_ALERT_MARKER
        assert len(COST_BURN_ALERT_MARKER) > 12

    def test_an_alert_rule_matches_the_marker(self) -> None:
        """The marker and the rule are in different repos-worth of file
        and drift silently: the log line keeps being written and nothing
        is listening."""
        from pathlib import Path

        from app.hatchet_workflows.cost_burn_watcher import (
            COST_BURN_ALERT_MARKER,
        )

        repo = Path(__file__).resolve().parents[3]
        script = (repo / "deploy" / "azure" / "alerts" / "create-alerts.sh").read_text(
            encoding="utf-8"
        )

        assert COST_BURN_ALERT_MARKER in script, (
            "no scheduled query rule matches the cost-burn marker, so the "
            "log line goes nowhere"
        )

    def test_the_alert_does_not_carry_query_text(self) -> None:
        """A 30-day log store is not where customer questions belong —
        the same rule sparse_encoder was fixed for."""
        import inspect

        from app.hatchet_workflows import cost_burn_watcher

        source = inspect.getsource(cost_burn_watcher)
        marker_call = source[source.index("COST_BURN_ALERT_MARKER,"):][:400]

        for leaky in ("query_text", "question", "prompt"):
            assert leaky not in marker_call, leaky
