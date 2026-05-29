## Doc-phase 78 handoff — §7.8 Export Compliance Agent skeleton

**Status:** Complete. Import smoke-tested in `georag-fastapi`.

## What landed

New module `app/agents/phase7/`:
- `export_compliance.py` — `@georag_agent`-decorated; **risk_tier R3**
  (export-blocking; one tier above R2 §6.4 boundary agent). Takes
  `workspace_id`, `export_kind`, optional `report_id`, `export_payload`.
  Returns `checks[] + passed + blocking_failures[] + non_blocking_warnings[]`.
  Skeleton (NotImplementedError body).
- `__init__.py` — re-exports the agent.

Module docstring inventories the future §7 agent set (Report Planner,
Evidence Curator, Claim Validator, Map/Chart Planner, Appendix Builder,
Presentation Coach, Conflict Resolver) so the next ticks know what
lands where.

### The 10-item §29.2 checklist

The export_compliance docstring locks the 10-check contract verbatim
from master-plan §29.2:

1. Citations included
2. CRS metadata included
3. Public/private separated (delegates to §6.4 boundary tags)
4. License notes included
5. Stale evidence flagged
6. Conflicts disclosed
7. User has permission
8. Sign-off complete (R4/R5)
9. QP credential verified (NI 43-101 / CSA)
10. Hash chain recorded

Each check returns `{name, passed, evidence, blocking}` — `blocking`
flag enables future tightening / loosening per workspace policy without
schema change.

### R3 idempotency note

Per `app/agents/wrapper.py:173-176`, R3 requires `ctx.workspace_id` +
`ctx.export_request_id` for idempotency key computation. The skeleton
import works (preconditions check only fires on invocation); the
wrapper integration story locks in §7.1 Report Builder Graph wiring.

## Master-plan §7 progress

| Sub-step | Status |
|---|---|
| 7.0 scope proposal | ✅ DONE (doc-phase 77) |
| 7.1 Report Builder Graph skeleton | pending (next tick) |
| 7.2 Eleven report-type templates | pending |
| 7.3-7.6 4 in-graph agents | pending |
| 7.7 Hash chain proof JSON | pending |
| 7.8 Export Compliance Agent | ✅ skeleton |
| 7.9 PDF/DOCX/XLSX renderers | pending (image rebuild) |
| 7.10 generate_report Hatchet workflow | pending |
| 7.11 Activepieces delivery | pending |
| 7.12-7.15 22 dashboards | pending (waits for Kyle) |
| 7.16 TDD acceptance test | pending |

**1 of 16 §7 sub-steps closed** (plus the scope proposal).

## Recommended next tick

Doc-phase 79 = §7.1 Report Builder Graph state model + node skeletons.
LangGraph state Pydantic model defining `ReportBuilderState`
(report_id, report_type, sections, evidence_ledger, citation_payload,
sign_off_records, hash_chain_proof) + stubbed LangGraph nodes for the
12-step pipeline in §15.1.

Alternative: doc-phase 79 = §7.7 hash chain proof JSON generator. Pure
utility that reads audit_ledger; small + standalone. Easier to land
without requiring graph wiring first.

Will pick at start of next tick based on whether audit_ledger schema
is in place (it is per `docs/audit_ledger_hash_recipe.md`; §7.7 looks
achievable).
