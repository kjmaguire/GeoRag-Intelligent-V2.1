## Doc-phase 156 handoff — §7.2 ↔ §9.13 cross-section integration

**Status:** Live + 6/6 pytest cases + 21/21 regression tests + 100/100 substrate verifier.

**Substrate verifier milestone: 100 checks (from 72 at start of run).**

## What landed

First cross-section integration of the run. Wires the doc-phase 147
`what_changed_detector` Hatchet task body into the doc-phase 137
§15.1 Report Builder graph. When a caller requests a `what_changed`
report, the §15.1 `gather_evidence` node now invokes the detector
and uses the structured workspace deltas as the section evidence —
instead of returning synthetic-stub claims.

### Three pieces wired

1. **`app/services/report_builder/state.py`** — added
   `report_window_start` + `report_window_end` fields on
   `ReportBuilderState` so the window threads through the graph.

2. **`app/services/report_builder/whatchanged_integration.py`** — new
   ~190-line integration helper. Calls `what_changed_detector.execute`
   via `aio_mock_run`, maps the structured detector output into
   4 `SectionDraft` rows matching the `what_changed` template's
   sections:
   - **period** — window dates + total audit count
   - **data_changes** — 3 real claims (ingestions, decisions,
     hypotheses) with real numbers from the detector
   - **claim_changes** — pending (silver.claim_ledger schema awaits §9.5)
   - **target_changes** — pending (target-zone delta awaits §18 wiring)

3. **`app/services/report_builder/nodes.py::gather_evidence`** — added
   per-report-type dispatch. `what_changed` reports route through the
   integration; all other report types still use the synthetic stub.
   Falls back to stub when the window is missing.

4. **`app/hatchet_workflows/generate_report.py`** — parses
   `report_window_start_iso` / `report_window_end_iso` from input
   and threads them through to `ReportBuilderState`.

## Tests — 6/6 pytest cases green

| Test | Verifies |
|---|---|
| `test_what_changed_integration_returns_none_for_other_report_types` | Non-what_changed types → None (fall back to stub) |
| `test_what_changed_integration_returns_none_without_window` | Missing window → None |
| `test_what_changed_integration_returns_drafts_with_window` | 4 section drafts; Default Workspace shows real deltas |
| `test_what_changed_integration_threaded_through_gather_evidence` | End-to-end via gather_evidence node |
| `test_what_changed_falls_back_to_stub_without_window` | No window → synthetic_stub tag visible |
| `test_generate_report_task_body_threads_window_through` | Full Hatchet body invocation with window → 4 section drafts |

**21/21 regression tests pass** (test_report_builder_planning_nodes
+ test_hatchet_workflow_bodies + test_langgraph_wirings — confirms
no breakage on the §15.1 graph for the other 10 report types).

## Live signal

When run against the Default Workspace with a 7-day window, the
what_changed report's data_changes section now reads:

```text
## Data Changes

- New ingestions: 1
- New public records: 0
- Updated public records: 0
- Decisions recorded: 0
- Hypotheses generated: 9
- Support tickets opened: 6
```

— matching the actual signal we accumulated across doc-phases 134
(9 hypotheses) + 136 (6 tickets).

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_whatchanged_report_integration.py -v
# → 6 passed in 2.40s

# Regression
docker exec georag-fastapi python -m pytest \
    tests/test_report_builder_planning_nodes.py \
    tests/test_hatchet_workflow_bodies.py \
    tests/test_langgraph_wirings.py
# → 21 passed in 2.44s

bash scripts/autonomous_run_substrate_verify.sh
# → 100/100 checks passed
```

## Cumulative session state — 25 ticks closed

- **Doc-phase ticks this run:** **25** (132 → 156)
- **Sections closed:** §25.4 + §6 (2 of 12)
- **Cross-section integrations live:** 1 (§7.2 ↔ §9.13)
- **§18.2 nodes graduated:** 6 of 12
- **§15.1 nodes graduated:** 4 of 12 (one — gather_evidence —
  now branches by report_type)
- **§21.3 capture hooks wired:** 1 of 8
- **Hatchet workflow skeletons graduated:** 6 of 11
- **Reasoning agent skeletons graduated:** 1
- **LangGraph wirings live:** 2 of 2
- **§6 PublicGeo adapters live:** 9 of 9
- **§25.4 support agents live:** 5 of 5
- **PublicGeo features on map:** 95
- **Live pytest cases:** **219** (up from 66 at start of run)
- **Substrate verifier:** **100/100 PASS**

## What's next

The doc-phase 156 integration is the first of several possible
cross-section wirings. Other productive integration candidates:

- **Wire §10.4 evaluate_workspace into §10.6 promotion gate cron** —
  schedule a daily run that calls the gate
- **Wire §9.10 hypothesis_generator into Answer Graph** — when chat
  classifier flags interpretive question, fire the generator
- **Wire §25.4 ticket_triage into a Laravel ticket-creation endpoint** —
  customers can actually file tickets (writer side)

Or continue with skeleton graduations:
- §18.2 missing 6 nodes are LLM/retrieval/spatial-dependent
- §15.1 missing 8 nodes are LLM/SeaweedFS-dependent
- 5 Hatchet workflow skeletons remaining are data/infra-dependent

## Carry-overs

- The integration's "fall back to stub when no window" branch keeps
  what_changed reports working even when callers forget to pass a
  window. The synthetic stub claims are still tagged so they're
  visible in any QA pass.
- `claim_ledger` + `target_zone_delta` evidence kinds resolve to
  "pending" claims today. When those upstream features land (§9.5
  schema for claims, §18 score-change emission), the integration
  swaps the synthetic notes for real-data claims.
