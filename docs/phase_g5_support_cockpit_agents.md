# Phase G.5 — Support Cockpit support agents (§25.4)

**Status:** Complete. Master-plan §10 deliverable "Support agents
(§25.4)" closes — all five phase10 agents are now production-ready
MVPs. The Support Cockpit page + controller already existed (doc-phase
130); this phase makes the agents real so the cockpit has something
to invoke.

## What graduated (5 agents, ~600 LOC across them)

All previously raised `NotImplementedError`. Each now ships with a
minimum-viable body that produces a real, structured output a
controller can render directly.

### 1. `ticket_triage` — Suggest severity + category from description

* Keyword-driven severity detection (critical / high / medium / low)
  using ~25 regex patterns spanning the full severity gradient.
* Keyword-driven category suggestion against the actual
  `ops.support_tickets.category` enum (`wrong_answer`,
  `failed_ingestion`, `failed_report`, `integration_issue`,
  `performance`, `other`).
* Output: `{ticket_id, current_severity, current_category,
  suggested_severity, suggested_category, severity_evidence,
  category_evidence, should_change}`.

### 2. `support_packet` — Diagnostic bundle assembler

* Pulls the ticket row, last 10 audit-ledger anchors, last 5 answer
  runs, last 5 workflow runs scoped to the ticket's workspace.
* Reports `audit_anchor_count_30d` so operators see whether the
  workspace has been busy lately.
* Graceful degradation: any individual subsystem query that fails
  (e.g. silver.answer_runs missing in the env) is caught and the
  field returns empty rather than crashing the whole bundle.
* SeaweedFS upload of the bundle is deferred to §15.4 follow-up;
  the dict shape is stable so the upload can plug in later.

### 3. `root_cause_investigation` — Deterministic hypothesis narrator

* Queries `workflow.workflow_runs` for failed / cancelled / timed-out
  runs in the 48h window prior to the ticket.
* Cross-references `ops.support_tickets` for similar tickets in the
  same workspace + same category in the last 30 days.
* Composes a confidence-tagged narrative (`low` / `medium` / `high`)
  from the signal density.
* Future LLM pass will rewrite the narrative against Langfuse span
  detail; the signal-collection layer doesn't change.

### 4. `customer_response_drafting` — Template-driven response draft

* Six category-specific templates (one per
  `support_tickets.category` enum value) with a `{resolution}`
  placeholder.
* Never auto-sends — output always carries `ready_to_send=False` +
  a note reminding the operator to review.

### 5. `escalation_routing` — Severity-based routing recommendation

* Maps each severity to (`page` role, `channel`, `sla_minutes`,
  `rationale`).
* Advisory by default; `apply=True` is reserved for the future
  PagerDuty / Opsgenie integration. Always returns `applied=False`
  in Phase G.5.

## Test coverage

`src/fastapi/tests/test_phase10_support_agents.py` — **15 tests**,
all passing:

* 4 pure-function tests for `_suggest_severity` (critical / high / low / default)
* 2 pure-function tests for `_suggest_category` (winner / other)
* 1 schema-coverage test for `_ROUTING_TABLE` (all 4 severities present)
* 2 schema-coverage tests for `_RESPONSE_TEMPLATES` (all 6 categories + each has placeholder)
* 6 DB-roundtrip smoke tests against the live `ops.support_tickets`
  table, one per agent + one for the missing-ticket error path
* DB smoke tests skip cleanly when `POSTGRES_PASSWORD` isn't set
* DB smoke tests invoke agents via `__wrapped__` to bypass the
  `agents.runtime` registration requirement (production startup
  registers it; unit tests don't need to)

Canary suite post-G.5: **244 / 0** (+15 from G.4's baseline, all
existing tests still pass).

## What's still needed for the full §25.4 "shipped" tag

This phase makes the agents real. The remaining wiring needed for
the cockpit to invoke them from the UI:

1. **Laravel proxy routes** — add `POST /admin/support-cockpit/agents/{agent}`
   actions on `SupportCockpitController` that authenticate, forward
   to FastAPI, and return the JSON to the page.
2. **FastAPI public endpoints** — add a `POST /v1/admin/support/agents/{agent}`
   router that calls into each phase10 agent's `__wrapped__` (so the
   request inherits the FastAPI app's pg_pool + agents runtime).
3. **UI buttons** — extend `SupportCockpit.tsx` with a "Run agent"
   panel per ticket row: 5 buttons that POST to the Laravel proxy +
   render the response in a side drawer.
4. **PagerDuty/Opsgenie integration** for real escalation_routing
   `apply=True` behavior.
5. **Tone configuration** for customer_response_drafting (workspace-
   per-tenant tone settings via §11 Presentation Coach Agent).

Items 1–3 are pure plumbing — half a day of work each. Item 4 is
operator-territory (procure + configure the on-call tool). Item 5
gates on Phase 11 settings UI.

## Files added / changed

* `src/fastapi/app/agents/phase10/ticket_triage.py` — full body
* `src/fastapi/app/agents/phase10/support_packet.py` — full body
* `src/fastapi/app/agents/phase10/root_cause_investigation.py` — full body
* `src/fastapi/app/agents/phase10/customer_response_drafting.py` — full body
* `src/fastapi/app/agents/phase10/escalation_routing.py` — full body
* `src/fastapi/tests/test_phase10_support_agents.py` (new — 15 tests)
