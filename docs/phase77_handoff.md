## Doc-phase 77 handoff — master-plan §7 (Reporting + dashboards) scope proposal

**Status:** Complete. Scope proposal doc landed.

## What landed

`docs/master_plan_section7_scope_proposal.md` — third scope proposal
(after §5 and §6). Reads master plan Phase 7 against current v1.49
baseline.

Key findings:
- **§7 is the largest phase by deliverable count** so far. 26-37 ticks
  estimated, comparable to §3+§5 combined.
- Splits naturally into three gates: **§7-A v1** (automated reports —
  4 of the 11 types), **§7-B** (22 dashboards), **§7-A v2** (manual R5
  reports + R4/R5 sign-off + Activepieces delivery).
- ~70% backend, ~30% frontend. Inverse of §6.
- Significant v1.49 baseline overlap: audit ledger hash chain exists,
  `workflow_runs` cross-orchestrator table exists, Grafana stack
  running, Hatchet workflow patterns established.
- Autonomous-safe backend ticks: 7.1 (Report Builder Graph skeleton),
  7.2 (R3 templates), 7.7 (hash chain proof JSON), 7.8 (Export
  Compliance Agent skeleton). Frontend (7.12-7.15) waits for Kyle.

4 open questions tabled for Kyle: sub-phase ordering, WeasyPrint vs
headless Chrome, Activepieces install status, §7/§8 parallelism.

## Recommended next ticks

- **Doc-phase 78** — §7.1 Report Builder Graph skeleton (LangGraph
  state model + node stubs). Backend-only; pattern matches §5 / §6
  skeleton work.
- **Doc-phase 79** — §7.8 Export Compliance Agent skeleton
  (`app/agents/phase7/`); §29.2 10-line checklist; pattern matches
  §6.4 boundary agent.
- **Doc-phase 80** — §7.7 hash chain proof JSON generator. Reads
  existing audit_ledger; pure backend utility.

## Carry-overs

1. `weasyprint` + `python-docx` + `openpyxl` deps for §7.9 — image
   rebuild required (stack accumulates: geopandas/rasterio/mplstereonet
   from §5 + these three).
2. Verify Activepieces install status at start of §7-A v2.
3. Verify `workflow_runs` cross-orchestrator unification per §16.5
   before §7-B dashboards block.
4. R4/R5 sign-off flow scope: per §29.6.1, "staffed-ops work for v1" —
   Export Compliance Agent gates but human verifies QP credential.

## Master-plan §7 progress

| Sub-step | Status |
|---|---|
| 7.1 Report Builder Graph skeleton | pending (next tick) |
| 7.2 Eleven report-type templates | pending |
| 7.3-7.6 4 in-graph agents | pending (skeletons-first) |
| 7.7 Hash chain proof JSON | pending |
| 7.8 Export Compliance Agent | pending (skeleton next) |
| 7.9 PDF/DOCX/XLSX renderers | pending (image rebuild) |
| 7.10 generate_report Hatchet workflow | pending |
| 7.11 Activepieces delivery | pending (gated by Activepieces status) |
| 7.12-7.15 22 dashboards | pending (waits for Kyle) |
| 7.16 TDD acceptance test | pending |

**0 of 16 sub-steps closed. Scope locked. Path forward sketched.**
