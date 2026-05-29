# Phase G follow-up — Kestra + PagerDuty real dispatchers

**Status:** ✅ Done. Both dispatchers wired into the §10 support
agents behind safe-by-default env knobs. 15/15 new dispatcher tests +
15/15 existing phase10 agent tests pass.

## What landed

### Two new HTTP dispatchers

`src/fastapi/app/services/dispatchers/` (new package):

* `kestra.py` — `dispatch_support_packet_to_kestra(bundle)` POSTs to
  `${KESTRA_URL}/api/v1/executions/{namespace}/{flow_id}` with the
  bundle JSON-encoded under a `payload` form field. Bearer-auth when
  `KESTRA_FLOW_AUTH_TOKEN` is set; anonymous otherwise.
* `pagerduty.py` — `create_pagerduty_incident(ticket_id, severity,
  summary, ...)` POSTs an Events v2 Trigger event to the global
  endpoint. **Idempotent**: `dedup_key = ticket_id`, so re-routing
  the same ticket updates the existing incident rather than creating
  duplicates.

Both share the same shape: a single async function, a result envelope
that always returns (never raises), and a `http_client` parameter for
test injection.

### Settings (all default-empty, safe to deploy)

```python
KESTRA_URL: str = ""                                  # disabled when empty
KESTRA_FLOW_NAMESPACE: str = "georag.support"
KESTRA_FLOW_ID: str = "support_packet_received"
KESTRA_FLOW_AUTH_TOKEN: str = ""
KESTRA_HTTP_TIMEOUT_S: float = 5.0

PAGERDUTY_INTEGRATION_KEY: str = ""                   # disabled when empty
PAGERDUTY_API_URL: str = "https://events.pagerduty.com/v2/enqueue"
PAGERDUTY_HTTP_TIMEOUT_S: float = 5.0
```

When `KESTRA_URL` or `PAGERDUTY_INTEGRATION_KEY` is empty, the
dispatcher short-circuits to a no-op result envelope and the agent
continues unaffected. The cockpit UI sees `kestra_dispatch.dispatched
= False, reason = "kestra_disabled"` (or equivalent for PagerDuty) so
the operator gets explicit feedback rather than silent failure.

### Agent wiring

`support_packet`:
* Builds the bundle as before (ticket row + recent audit anchors +
  recent answer_runs + recent workflow_runs).
* Calls `dispatch_support_packet_to_kestra(bundle)` at the end.
* Attaches the dispatch envelope under `bundle["kestra_dispatch"]`
  before returning.

`escalation_routing`:
* Computes the advisory recommendation as before.
* When called with `apply=True`, fires
  `create_pagerduty_incident(...)` with custom_details carrying
  status, route_to, channel, sla_minutes, rationale.
* Always returns the recommendation; the PagerDuty result lives
  under `pagerduty` in the response. The legacy `applied` flag now
  reflects whether PD actually paged (`pd_result["paged"]`).

### Severity mapping (PagerDuty)

| Cockpit | PagerDuty Events v2 |
|---|---|
| critical | critical |
| high     | error |
| medium   | warning |
| low      | info |
| unknown / null | warning (safe fallback) |

## Test coverage

`tests/test_phase10_dispatchers.py` (new, 15 tests):

* Disabled-by-default (both dispatchers): no-op envelope, no outbound call
* Happy path: 200/202 OK → dispatched=True / paged=True
* Bearer-token auth header (Kestra): set when token configured, absent when empty
* Idempotency contract (PagerDuty): dedup_key = ticket_id, severity = trigger
* Severity mapping: all 4 cockpit severities + unknown fallback
* Upstream 4xx: reason + status + body excerpt recorded
* Network error (ConnectError / ConnectTimeout): reason + error message recorded
* PagerDuty summary truncation: 2000-char input → 1024-char output

`tests/test_phase10_support_agents.py` (existing, 15 tests): all
still pass — the dispatcher integration is transparent to the agents'
existing contracts.

## Operator runbook

1. **Provision Kestra flow.** Create a flow at
   `georag.support.support_packet_received` with one input named
   `payload` (string). Branch on `payload` content downstream
   (Slack notify, SeaweedFS archive, audit-attach).
2. **Provision PagerDuty service.** Create a service with Events API
   v2 enabled; copy the 32-char integration key.
3. **Set env vars** in the FastAPI container:
   ```
   KESTRA_URL=https://kestra.your-domain
   KESTRA_FLOW_AUTH_TOKEN=<bearer if needed>
   PAGERDUTY_INTEGRATION_KEY=<32-char key>
   ```
4. **Restart FastAPI.** `docker compose restart fastapi`.
5. **Test fire.** From the cockpit, click "Run Support Packet" on a
   ticket; the result modal will show
   `kestra_dispatch.dispatched=true, execution_id=...`. For PagerDuty,
   click "Apply Escalation"; the result modal will show
   `pagerduty.paged=true, dedup_key=<ticket-id>`.
6. **Verify idempotency** (PagerDuty): re-apply escalation on the
   same ticket. PagerDuty should update the existing incident in
   place rather than creating a duplicate.

## Failure modes covered

| Scenario | Behavior |
|---|---|
| Kestra URL empty | No-op; `kestra_dispatch.reason = "kestra_disabled"` |
| Kestra returns 4xx | Bundle still returned; `kestra_dispatch.status_code + .error` |
| Kestra unreachable (TCP error) | Bundle still returned; `.reason = "kestra_network_error"` |
| Kestra timeout (5s) | Bundle still returned; `.reason = "kestra_network_error"` |
| PagerDuty key empty | Advisory returned; `pagerduty.reason = "pagerduty_disabled"` |
| PagerDuty 4xx | Advisory returned; `.status_code + .error` |
| PagerDuty network error | Advisory returned; `.reason = "pagerduty_network_error"` |
| apply=False | Advisory returned; `.reason = "apply_not_requested"` |

The cockpit UI never blocks on a flaky upstream. The agent always
returns its core recommendation; the dispatcher result is metadata.

## Files

* `src/fastapi/app/services/dispatchers/__init__.py` (new)
* `src/fastapi/app/services/dispatchers/kestra.py` (new)
* `src/fastapi/app/services/dispatchers/pagerduty.py` (new)
* `src/fastapi/app/agents/phase10/support_packet.py` (+ dispatcher wiring)
* `src/fastapi/app/agents/phase10/escalation_routing.py` (+ dispatcher wiring)
* `src/fastapi/app/config.py` (+ 8 settings)
* `src/fastapi/tests/test_phase10_dispatchers.py` (new, 15 tests)
