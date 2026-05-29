## Doc-phase 144 handoff — §25.4 escalation_routing agent — §25.4 SUITE CLOSED

**Status:** Live + 12/12 pytest cases + 6 production tickets routed. **86/86 substrate verifier**.

**§25.4 support-agent suite is now 5 of 5 LIVE.** This closes the §25.4 graduation.

## What landed

Fifth of 5 §25.4 support agents (ticket_triage,
root_cause_investigation, support_packet, customer_response_drafting,
**escalation_routing**). Reads a ticket's state + audit chain and
makes a routing decision among 5 outcomes.

### Five routing decisions

| Decision | When |
|---|---|
| `auto_resolve` | Low-severity with full triage + investigation + draft response chain |
| `on_call_engineer` | Critical-severity (always; pages immediately) |
| `sme_review` | wrong_answer with investigation done, OR failed_report with draft response |
| `queue_for_engineer` | High or medium severity (standard queue) |
| `wait_for_more_signal` | wrong_answer without investigation (loop back) |

### New live service — `app/services/support_cockpit/escalation_routing.py`

~225 lines. Pure async. Exports:
- `route_escalation(ticket_id, actor_user_id, assign_to_user_id=None, pool=None)`
- `EscalationOutcome` NamedTuple
- `RoutingDecision` Literal type
- `_synthetic_router()` — decision-tree synthesizer (deterministic stub)

Routing pipeline:
1. SELECT … FOR UPDATE on the ticket
2. Detect prior chain state from audit ledger (triage / investigation /
   response draft signals)
3. Run `_synthetic_router(severity, category, chain_signals)` → decision + rationale
4. Optionally UPDATE `assigned_to_user_id`
5. Emit `support.ticket.escalation_routed` audit anchor with decision + rationale

The router is a deterministic decision tree (first-match-wins).
Real LLM-driven routing replaces `_synthetic_router` without
touching the surrounding orchestration.

## Tests — `src/fastapi/tests/test_escalation_routing.py`

**12 pytest cases, all green:**

Router decision-tree unit (8):
- critical always pages on-call
- critical overrides other signals
- wrong_answer + investigation → sme_review
- wrong_answer without investigation → wait_for_more_signal
- failed_report + draft → sme_review
- high severity → queue_for_engineer
- low + full chain → auto_resolve
- medium fallback → queue_for_engineer

End-to-end DB (4):
- `test_route_escalation_end_to_end` — audit anchor lands
- `test_route_escalation_assigns_user_when_provided` — assignee write
- `test_route_escalation_preserves_existing_assignment` — null assignee passthrough
- `test_route_escalation_unknown_id_raises`

## Live verification — 6 production tickets routed

```text
459d8fcc... [low      other             ] → auto_resolve
c625f3ee... [high     wrong_answer      ] → sme_review
b0ace1df... [critical failed_report     ] → on_call_engineer
5e74c8a0... [critical failed_ingestion  ] → on_call_engineer
2671467c... [medium   performance       ] → queue_for_engineer
9037c265... [critical integration_issue ] → on_call_engineer
```

Decisions track the §25.4 rules: 3 criticals → page on-call; the
wrong_answer ticket → SME review (has investigation chain); the
fully-processed low-severity → auto_resolve (ready for human send);
the medium → standard engineer queue.

## §25.4 suite — now complete end-to-end

For each of the 6 production tickets, the full 5-stage chain has run:

```text
1. created                  → status=open, sev=medium, cat=other
   ↓
2. triage_ticket            → status=investigating, sev/cat classified
                              + support.ticket.triaged
   ↓
3. investigate_ticket       → support_ticket_traces row + trace_summary
                              + support.ticket.investigated
   ↓
4. build_support_packet     → packet anchor with full chain snapshot
                              + support.packet.assembled
   ↓
5. draft_customer_response  → customer_visible_response set
                              + support.ticket.response_drafted
   ↓
6. route_escalation         → routing decision + assignee
                              + support.ticket.escalation_routed
```

Each ticket has **5 distinct audit anchors** linking to the same
target_id, forming the immutable forensic trail per §25 spec.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_escalation_routing.py -v
# → 12 passed in 0.67s

bash scripts/autonomous_run_substrate_verify.sh
# → 86/86 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 144
- **§25.4 support agents graduated:** **5 of 5 — COMPLETE**
- **§18.2 nodes graduated:** 6 of 12
- **§15.1 nodes graduated:** 4 of 12
- **§21.3 capture hooks wired:** 1 of 8
- **Hatchet workflow skeletons graduated:** 1 of 11
- **Reasoning agent skeletons graduated:** 1
- **LangGraph wirings live:** 2 of 2
- **Live pytest cases:** 155 (143 + 12)
- **Substrate verifier:** **86/86 PASS**

## 12-tick run summary (132 → 144)

| Tick | Section | Graduation | Tests |
|---|---|---|---|
| 132 | §10.4 + §10.6 | evaluate_workspace + promotion gate | 8 |
| 133 | §9.12 / §21.3 | Laravel RecordDecision + workflow_enablement hook + RLS retrofit | E2E ✅ |
| 134 | §9.10 | ai_suggested hypothesis emitter | 7 |
| 135 | §6 | jurisdictions + sources foundation seed (5 + 9) | — |
| 136 | §25.4 (1/5) | ticket_triage | 10 |
| 137 | §7-A v1 | 4 of 12 §15.1 nodes | 11 |
| 138 | §8 / §18.2 | §8.7 formula + 6 of 12 §18.2 nodes | 17 |
| 139 | §25.4 (2/5) | root_cause_investigation | 6 |
| 140 | §25.4 (3/5) | support_packet | 5 |
| 141 | §15.1 + §18.2 | LangGraph Pregel wirings (both compile + run) | 4 |
| 142 | UX | Admin nav drawer on AppLayout | — |
| 143 | §25.4 (4/5) | customer_response_drafting | 9 |
| 144 | §25.4 (5/5) | **escalation_routing — §25.4 CLOSED** | 12 |

13 ticks. 89 new pytest cases. Substrate verifier 72 → 86. Four admin
dashboards populated. **§25.4 suite complete — first section to land
end-to-end across the partial-section closeout.**

## What's next

§25.4 closed unlocks productive next moves:
- **Doc-phase 145** — graduate `generate_report` Hatchet task body
  to invoke the doc-phase 141 LangGraph (§15.1 planning pipeline)
- **Doc-phase 146** — graduate `score_targets` Hatchet task body
  to invoke the doc-phase 141 LangGraph (§18.2 scoring pipeline)
- **Doc-phase 147** — graduate `support_replay` Hatchet task body
  using the §25.4 agent suite that just closed

These three workflow-body graduations would bridge the workflow
layer to the graduated graph/agent layers cleanly.

## Carry-overs

- The 5-stage §25.4 chain is sequential today. A future enhancement
  is a `process_ticket_pipeline()` convenience function that runs
  the full chain in one call (triage → investigate → packet → draft
  → route).
- The routing decision is informational today — the `assigned_to_user_id`
  is the only side effect on the ticket row. Future enhancement
  could add a `routing_decision` column to ops.support_tickets for
  faster querying.
- All 5 agents share the synthetic-stub pattern; each `_synthetic_*`
  function is a clear swap-in point for the real LLM equivalent.
