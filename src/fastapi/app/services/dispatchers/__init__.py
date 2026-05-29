"""Outbound HTTP dispatchers for §10 Customer Support Cockpit agents.

Each dispatcher is a single async function that POSTs a structured
payload to an external system (Kestra, PagerDuty). They share a
common contract:

* Safe-by-default: if the corresponding settings knob is empty, the
  dispatcher returns `{"dispatched": False, "reason": "<short>"}` and
  does NOT raise.
* Bounded: every outbound call has a per-request timeout. We never
  block a support-cockpit invocation waiting on a flaky upstream.
* Non-blocking on failure: HTTP / network errors are caught, logged,
  and returned in the result envelope so the caller (the cockpit UI)
  can still render an answer.
* Idempotent where the upstream supports it (PagerDuty Events v2
  dedup_key = ticket_id).
"""
from app.services.dispatchers.kestra import dispatch_support_packet_to_kestra
from app.services.dispatchers.pagerduty import create_pagerduty_incident

__all__ = [
    "dispatch_support_packet_to_kestra",
    "create_pagerduty_incident",
]
