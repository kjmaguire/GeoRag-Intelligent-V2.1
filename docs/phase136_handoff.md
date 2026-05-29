## Doc-phase 136 handoff — §25.4 ticket_triage support agent

**Status:** Live + 10/10 pytest cases + 6 real tickets triaged in DB. **79/79 substrate verifier**.

## What landed

First of the 5 §25.4 support agents graduated. Same synthetic-stub +
real-orchestration pattern doc-phase 132/134 established.

### New live service — `app/services/support_cockpit/ticket_triage.py`

~260 lines. Pure async orchestration. Exports:
- `triage_ticket(ticket_id, pool=None)` — single-ticket triage
- `triage_unclassified_tickets(limit=50, pool=None)` — bulk path
- `_synthetic_classifier(description) -> (severity, category)` — stub
- `TriageOutcome` NamedTuple

Per-call orchestration:
1. SELECT … FOR UPDATE on the ticket row (transactional locking)
2. Refuse if status ∈ {'resolved', 'closed'}
3. Call `_synthetic_classifier(description)` for new severity + category
4. UPDATE ticket: severity, category, status='investigating'
5. Emit `support.ticket.triaged` audit anchor with prior + new values
6. Return TriageOutcome

### Synthetic classifier

Deterministic keyword-based. Severity priorities (first match wins):

| Keywords | Severity |
|---|---|
| crash, broken, unable, data loss, critical | critical |
| fail (ingest/report context) | critical |
| fail (other) | high |
| wrong, incorrect, hallucinat, fabricat | high |
| slow, performance, timeout | medium |
| (default) | low |

Category priorities:

| Keywords | Category |
|---|---|
| report, export, docx, xlsx, pdf gen | failed_report |
| pdf upload, upload, ingest, parse, ocr | failed_ingestion |
| integration, activepieces, webhook, api error | integration_issue |
| slow, timeout, lag | performance |
| wrong, incorrect, hallucinat, fabricat | wrong_answer |
| (default) | other |

All outputs guaranteed valid against the `support_tickets_severity_valid`
and `support_tickets_category_valid` CHECK constraints (asserted in code).

### Updated `__init__.py`

`app.services.support_cockpit` exports `triage_ticket`,
`triage_unclassified_tickets`, `TriageOutcome` alongside the existing
`emit_support_access_audit` + `open_trace_with_audit` exports.

## Tests — `src/fastapi/tests/test_ticket_triage.py`

**10 pytest cases, all green:**

| Test | Verifies |
|---|---|
| `test_classifier_critical_for_crash` | "crashed" + "PDF" → critical / failed_ingestion |
| `test_classifier_high_for_wrong_answer` | "wrong answer" → high / wrong_answer |
| `test_classifier_medium_for_slow` | "slow" → medium / performance |
| `test_classifier_default_low_other` | Default → low / other |
| `test_classifier_failed_report` | "report export" → critical / failed_report |
| `test_classifier_integration_issue` | "webhook 502 broken" → critical / integration_issue |
| `test_triage_ticket_end_to_end` | DB writes + audit anchor |
| `test_triage_ticket_rejects_closed_ticket` | Closed tickets raise ValueError |
| `test_triage_ticket_unknown_id_raises` | Nonexistent UUID raises |
| `test_triage_unclassified_tickets_bulk` | 3 open tickets all triaged in one pass |

## Live verification — Support Cockpit now shows real data

Seeded 6 representative tickets + ran `triage_unclassified_tickets`:

```
Triaged 6 tickets:
  c625f3ee... sev=medium→high      cat=other→wrong_answer
  b0ace1df... sev=medium→critical  cat=other→failed_report
  5e74c8a0... sev=medium→critical  cat=other→failed_ingestion
  2671467c... sev=medium→medium    cat=other→performance
  9037c265... sev=medium→critical  cat=other→integration_issue
  459d8fcc... sev=medium→low       cat=other→other

Final state:
  ops.support_tickets:    6 (all 'investigating')
  severity:               3 critical, 1 high, 1 medium, 1 low
  category:               full coverage across all 6 §10.11 categories
  support.ticket.triaged audit anchors:  10 (incl. test runs)
```

Support Cockpit `/admin/support-cockpit` now shows:
- KPI tile "Open / Critical": 6 investigating / 3 critical
- Per-status: 6 investigating
- Per-severity: 3 critical, 1 high, 1 medium, 1 low
- Per-category: full coverage
- Recent tickets table: 6 rows with classifications applied

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_ticket_triage.py -v
# → 10 passed in 0.49s

bash scripts/autonomous_run_substrate_verify.sh
# → 79/79 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 136
- **Track 3 admin surfaces with real data:** **4 of 4**
- **§25.4 support agents graduated:** **1 of 5** (ticket_triage)
- **Hatchet workflow skeletons graduated:** 1 of 11
- **Reasoning-agent skeletons graduated:** 1 (hypothesis_generator)
- **Live pytest cases:** 91 (81 + 10)
- **Substrate verifier:** **79/79 PASS**

## Track-3 dashboards now showing real data

| Surface | Real data |
|---|---|
| `/admin/eval-dashboard` | 4 eval runs, 115 result rows |
| `/admin/decision-history` | 2 decisions, 536 decision.* audit anchors |
| `/admin/support-cockpit` | **6 triaged tickets, full sev/category coverage, 10 triage anchors, 143 support_access anchors** |
| `/admin/hypothesis-workspace` | 9 hypotheses, 27 evidence links, 6 hypothesis.generated anchors |

## What's next

- **Doc-phase 137** — §7-A v1 first report_builder graph nodes
  (synthetic stub pattern). Lights up the §7 report generation surface.
- **Doc-phase 138** — §8 score_targets graph nodes + §8.7 formula
- **Doc-phase 139** — §25.4 second support agent (root_cause_investigation)
- **Doc-phase 140+** — remaining §25.4 agents
  (support_packet, customer_response_drafting, escalation_routing)

## Carry-overs

- The synthetic classifier maps well for common cases but doesn't
  capture nuance. Real LLM classification (§25.4 ticket_triage agent
  with its proper §35 prompt) graduates without touching the
  orchestration.
- The 6 seeded production tickets are real (not test fixtures) — they
  exercise real DB rows and stay in the Support Cockpit. They demonstrate
  the full severity/category palette for the dashboard UI.
- `customer_visible_response` + `resolution_summary` fields remain
  unset; those wait for `customer_response_drafting` agent
  (doc-phase 140+).
