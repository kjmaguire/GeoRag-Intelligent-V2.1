"""Kestra HTTP dispatcher for support_packet bundles.

When `KESTRA_URL` is unset, the dispatcher is a no-op — the
support_packet agent still returns the bundle dict to the cockpit UI,
but no outbound execution fires. Flip `KESTRA_URL` on once the
operator has provisioned a Kestra flow at
`${KESTRA_FLOW_NAMESPACE}.${KESTRA_FLOW_ID}` to receive the bundle.

The flow is expected to accept a multipart-style execution input
shape per Kestra's REST API:
    POST /api/v1/executions/{namespace}/{flowId}
with the bundle JSON serialised under the `payload` form field. The
auth token (if set) is passed as a Bearer token.

Result envelope:
    {
        "dispatched": bool,
        "reason": str | None,
        "execution_id": str | None,   # populated on dispatch=True
        "url": str | None,
        "status_code": int | None,
        "error": str | None,
    }
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def dispatch_support_packet_to_kestra(
    bundle: dict[str, Any],
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST a support_packet bundle to its configured Kestra flow.

    Args:
        bundle: The dict the support_packet agent assembled (ticket
            row + recent audit anchors + recent answer_runs + recent
            workflow_runs).
        http_client: Optional injected client (used by tests). When
            None we build a per-request client with the configured
            timeout.

    Returns:
        Result envelope (see module docstring). Never raises.
    """
    base_url = (getattr(settings, "KESTRA_URL", "") or "").strip()
    if not base_url:
        return {
            "dispatched": False,
            "reason": "kestra_disabled",
            "execution_id": None,
            "url": None,
            "status_code": None,
            "error": None,
        }

    namespace = getattr(settings, "KESTRA_FLOW_NAMESPACE", "georag.support")
    flow_id = getattr(settings, "KESTRA_FLOW_ID", "support_packet_received")
    timeout_s = float(getattr(settings, "KESTRA_HTTP_TIMEOUT_S", 5.0))
    auth_token = getattr(settings, "KESTRA_FLOW_AUTH_TOKEN", "") or ""

    url = f"{base_url.rstrip('/')}/api/v1/executions/{namespace}/{flow_id}"
    headers: dict[str, str] = {"X-Kestra-Trigger-Source": "support_packet"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    # Kestra accepts inputs as multipart form fields; we send the
    # bundle JSON-encoded under "payload". Downstream the flow can
    # decode + branch on bundle.ticket_id / bundle.workspace_id.
    data = {"payload": json.dumps(bundle, default=str)}

    async def _do_post(client: httpx.AsyncClient) -> dict[str, Any]:
        try:
            resp = await client.post(url, data=data, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "dispatch_support_packet_to_kestra: network failure url=%s err=%s",
                url, type(exc).__name__,
            )
            return {
                "dispatched": False,
                "reason": "kestra_network_error",
                "execution_id": None,
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
                "dispatch_support_packet_to_kestra: upstream %d body=%r url=%s",
                status, body_excerpt, url,
            )
            return {
                "dispatched": False,
                "reason": "kestra_http_error",
                "execution_id": None,
                "url": url,
                "status_code": status,
                "error": body_excerpt,
            }

        # Kestra responds with the execution descriptor (UUID + state).
        execution_id: str | None = None
        try:
            execution_id = (resp.json() or {}).get("id")
        except Exception:
            execution_id = None

        logger.info(
            "dispatch_support_packet_to_kestra: ok status=%s execution_id=%s",
            status, execution_id,
        )
        return {
            "dispatched": True,
            "reason": None,
            "execution_id": execution_id,
            "url": url,
            "status_code": status,
            "error": None,
        }

    if http_client is not None:
        return await _do_post(http_client)

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return await _do_post(client)
