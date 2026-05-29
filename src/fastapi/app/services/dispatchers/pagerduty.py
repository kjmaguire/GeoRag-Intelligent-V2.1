"""PagerDuty Events API v2 dispatcher for escalation_routing.

When `PAGERDUTY_INTEGRATION_KEY` is empty, the dispatcher is a
no-op — escalation_routing still returns its advisory recommendation
but no outbound page fires. Flip the key on per-service once the
operator has provisioned the PagerDuty integration.

Events API v2 contract:
    POST https://events.pagerduty.com/v2/enqueue
    {
        "routing_key": "<32-char integration key>",
        "event_action": "trigger" | "acknowledge" | "resolve",
        "dedup_key": "<idempotency key — use ticket_id>",
        "payload": {
            "summary": "<short, < 1024 chars>",
            "severity": "critical" | "error" | "warning" | "info",
            "source": "georag-support-cockpit",
            "component": "<optional, e.g. project_id>",
            "group": "<optional, e.g. workspace_id>",
            "class": "<optional, e.g. category>",
            "custom_details": {...}
        }
    }

The cockpit's internal severity vocabulary maps onto PagerDuty's:
    critical -> critical
    high     -> error
    medium   -> warning
    low      -> info
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


_SEVERITY_MAP: dict[str, str] = {
    "critical": "critical",
    "high":     "error",
    "medium":   "warning",
    "low":      "info",
}


async def create_pagerduty_incident(
    *,
    ticket_id: str,
    severity: str,
    summary: str,
    custom_details: dict[str, Any] | None = None,
    component: str | None = None,
    group: str | None = None,
    klass: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Trigger (or update) a PagerDuty incident for one support ticket.

    Idempotent: dedup_key = ticket_id, so re-calling for the same
    ticket updates the existing incident rather than creating a
    duplicate.

    Args:
        ticket_id: The cockpit's ticket_id (UUID-as-string). Used as
            PagerDuty's dedup_key for idempotency.
        severity: Cockpit's severity ("critical" / "high" / "medium" /
            "low"). Mapped onto PagerDuty's vocabulary internally.
        summary: Human-readable short description, surfaced as the
            incident title in the PagerDuty UI.
        custom_details: Optional structured key/value bundle attached
            to the incident (rendered in the PD timeline). Useful
            for SLA timer, route_to, channel, rationale.
        component / group / klass: Optional triage classifiers per
            PagerDuty's payload schema (cardinal: project_id /
            workspace_id / ticket category).
        http_client: Optional injected client for tests.

    Returns:
        {
            "paged": bool,
            "reason": str | None,
            "dedup_key": str,
            "pd_severity": str | None,
            "url": str,
            "status_code": int | None,
            "error": str | None,
        }

    Never raises.
    """
    integration_key = (getattr(settings, "PAGERDUTY_INTEGRATION_KEY", "") or "").strip()
    if not integration_key:
        return {
            "paged": False,
            "reason": "pagerduty_disabled",
            "dedup_key": ticket_id,
            "pd_severity": None,
            "url": getattr(settings, "PAGERDUTY_API_URL", ""),
            "status_code": None,
            "error": None,
        }

    url = getattr(
        settings, "PAGERDUTY_API_URL", "https://events.pagerduty.com/v2/enqueue"
    )
    timeout_s = float(getattr(settings, "PAGERDUTY_HTTP_TIMEOUT_S", 5.0))

    pd_severity = _SEVERITY_MAP.get((severity or "medium").lower(), "warning")

    payload: dict[str, Any] = {
        "routing_key": integration_key,
        "event_action": "trigger",
        "dedup_key": ticket_id,
        "payload": {
            "summary": (summary or "GeoRAG support ticket")[:1024],
            "severity": pd_severity,
            "source": "georag-support-cockpit",
            "custom_details": custom_details or {},
        },
    }
    if component:
        payload["payload"]["component"] = component
    if group:
        payload["payload"]["group"] = group
    if klass:
        payload["payload"]["class"] = klass

    async def _do_post(client: httpx.AsyncClient) -> dict[str, Any]:
        try:
            resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            logger.warning(
                "create_pagerduty_incident: network failure url=%s err=%s",
                url, type(exc).__name__,
            )
            return {
                "paged": False,
                "reason": "pagerduty_network_error",
                "dedup_key": ticket_id,
                "pd_severity": pd_severity,
                "url": url,
                "status_code": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

        status = getattr(resp, "status_code", None)
        if status is not None and status >= 400:
            try:
                body_excerpt = resp.text[:300]
            except Exception:
                body_excerpt = "<body read failed>"
            logger.warning(
                "create_pagerduty_incident: upstream %d body=%r ticket_id=%s",
                status, body_excerpt, ticket_id,
            )
            return {
                "paged": False,
                "reason": "pagerduty_http_error",
                "dedup_key": ticket_id,
                "pd_severity": pd_severity,
                "url": url,
                "status_code": status,
                "error": body_excerpt,
            }

        logger.info(
            "create_pagerduty_incident: ok ticket_id=%s severity=%s pd_severity=%s",
            ticket_id, severity, pd_severity,
        )
        return {
            "paged": True,
            "reason": None,
            "dedup_key": ticket_id,
            "pd_severity": pd_severity,
            "url": url,
            "status_code": status,
            "error": None,
        }

    if http_client is not None:
        return await _do_post(http_client)

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return await _do_post(client)
