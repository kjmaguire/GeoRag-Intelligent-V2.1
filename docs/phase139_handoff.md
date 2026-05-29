## Doc-phase 139 handoff — §25.4 root_cause_investigation support agent

**Status:** Live + 6/6 pytest cases + 6 production investigations on the Support Cockpit. **82/82 substrate verifier**.

## What landed

Second of 5 §25.4 support agents. Reads a triaged ticket, scans recent
audit + decision signal for the ticket's workspace, synthesizes a
ranked list of probable causes via heuristic pattern matching.

### New live service — `app/services/support_cockpit/root_cause_investigation.py`

~280 lines. Pure async. Exports:
- `investigate_ticket(ticket_id, actor_user_id, lookback_hours=168, pool=None)` —
  end-to-end investigation
- `CATEGORY_AUDIT_PATTERNS` — category → audit action_type prefix map
- `InvestigationResult` NamedTuple

### Investigation pipeline

1. Load ticket (workspace_id, description, severity, category)
2. Map ticket.category → audit action_type prefixes:
   - `failed_ingestion` → `ingest_pdf.*` / `ingest.*` / `ocr.*`
   - `failed_report` → `report.*` / `generate_report.*`
   - `integration_issue` → `external_notification.*` / `public_geoscience.pull.*` / `workflow.jwt_key.*` / `usage.external_notification_sender.*`
   - `wrong_answer` → `decision.*` / `hypothesis.*` + recent decision_records
   - `performance` / `other` → no patterns (synthesizer reports data scarcity)
3. Query `audit.audit_ledger` for matching entries in last 7d
4. (For wrong_answer) Query recent `silver.decision_records`
5. Synthesize `top_causes` (clustered by action_type, ranked by relevance)
6. Generate synthetic `trace_id` (real impl will correlate to Langfuse)
7. Persist via `ops.support_ticket_traces` (ticket_id, trace_id, trace_summary, added_by_user_id)
8. Emit `support.ticket.investigated` audit anchor with full structured payload + trace_id

### Heuristic relevance formula

`relevance = min(1.0, 0.2 + 0.1 * cluster_size)` — clusters with more
hits get higher relevance, capped at 1.0. Top cause is the
highest-relevance cluster. When no clusters exist, the synthesizer
returns a single "data scarcity" cause at relevance 0.1.

Real LLM-driven root-cause analysis replaces `_synthesize_top_causes`
without touching the surrounding flow.

## Tests — `src/fastapi/tests/test_root_cause_investigation.py`

**6 pytest cases, all green:**

Heuristic unit (3):
- `test_category_patterns_cover_all_valid_categories`
- `test_synthesize_top_causes_no_signal_returns_data_scarcity`
- `test_synthesize_top_causes_clusters_audits_by_action_type`

End-to-end DB (3):
- `test_investigate_ticket_end_to_end` — full pipeline with trace link + audit anchor
- `test_investigate_ticket_unknown_id_raises` — ValueError on bad UUID
- `test_investigate_ticket_multiple_runs_create_distinct_traces` — successive investigations don't collide

## Live verification on real data

Investigated all 6 production tickets from doc-phase 136:

```text
c625f3ee... wrong_answer       → "Recent hypothesis.generated events (3× in last 7 days)..."
b0ace1df... failed_report       → "No directly-relevant audit signal found..."
5e74c8a0... failed_ingestion    → "Recent ingest_pdf.parse.complete events (1× in last 7 days)..."
2671467c... performance         → "No directly-relevant audit signal..."
9037c265... integration_issue   → "No directly-relevant audit signal..."
459d8fcc... other               → "No directly-relevant audit signal..."

Final state:
  ops.support_ticket_traces:               6 (one per investigated ticket)
  audit.audit_ledger (support.ticket.investigated):  9 (incl. test runs)
```

The wrong_answer + failed_ingestion tickets surfaced real audit signal
(hypothesis emissions from doc-phase 134; ingest_pdf events from earlier
runs). The other 4 categories show "data scarcity" — which is correct:
no recent ingestion failures, integration errors, or performance events
in the workspace's audit ledger.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_root_cause_investigation.py -v
# → 6 passed in 0.56s

bash scripts/autonomous_run_substrate_verify.sh
# → 82/82 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 139
- **§25.4 support agents graduated:** **2 of 5**
  (ticket_triage, root_cause_investigation)
- **§18.2 nodes graduated:** 6 of 12
- **§15.1 nodes graduated:** 4 of 12
- **§21.3 capture hooks wired:** 1 of 8
- **Hatchet workflow skeletons graduated:** 1 of 11
- **Reasoning agent skeletons graduated:** 1 (hypothesis_generator)
- **Live pytest cases:** 125 (119 + 6)
- **Substrate verifier:** **82/82 PASS**

## What's next

- **Doc-phase 140** — §25.4 support_packet agent (third of 5).
  Assembles a structured packet (ticket info + investigation results
  + relevant audit chain) for handoff to engineering.
- **Doc-phase 141** — LangGraph wiring for the §15.1 + §18.2 graphs
  (thread the graduated nodes into actual Pregel pipelines so
  `generate_report` + `score_targets` Hatchet workflows can be
  graduated end-to-end)
- **Doc-phase 142+** — remaining §25.4 agents
  (customer_response_drafting, escalation_routing)

## Carry-overs

- The `trace_id` generated by the agent (`inv_<16hex>`) is a synthetic
  placeholder. Real impl will correlate to a Langfuse trace id once
  the agent makes LLM calls.
- The heuristic top-cause synthesis is intentionally conservative —
  when no clear signal exists it reports data scarcity rather than
  fabricating causes. Real LLM agent will replace the synthesizer.
- `support_ticket_traces.added_by_user_id` is a FK with ON DELETE
  RESTRICT to public.users. The test fixture handles this with a
  cleanup try/except to mirror doc-phase 124's pattern.
