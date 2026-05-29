## Doc-phase 84 handoff — Master-plan §8 (Target Recommendation Engine) scope proposal

**Status:** Complete. Scope proposal doc landed.

## What landed

`docs/master_plan_section8_scope_proposal.md` — fourth scope proposal
in the §5/§6/§7/§8 sequence. Reads master-plan §18 (Target
Recommendation Engine) + §20.2 (deposit models) + Phase 8 deliverables.

Key findings:
- 22-30 ticks estimated. Comparable to §7 in size; smaller than §3+§5+§7.
- ~85% backend, ~15% frontend (mostly sign-off UI + Target Pack map
  layer). §8 is the most backend-heavy phase so far.
- **§8 reuses §7 substrate heavily** — Target Recommendation Report
  template is already in `templates.py` (doc-phase 82); Export
  Compliance Agent (§7.8) gates the output; Report Builder Graph
  (§7.1) renders it.
- §8's primary new work: Target Recommendation Graph (separate from
  Report Builder Graph), 11 target agents, `targeting.*` schema (10
  tables), weighted scoring formula module.
- SME-dependent ticks (§8.3 Athabasca content, §8.7 scoring weights,
  §8.9 sign-off mechanism) wait for Kyle.

4 open questions tabled for Kyle: §8.3 SME data ownership, constraints
v1 scope, §8/§7-B parallelism, QP credential verification mechanism.

## Master-plan progress

| Master-plan phase | Status |
|---|---|
| §3 (§04p PDF stack + OCR quality) | functionally done (Step 9-10 blocked on SME) |
| §4 (RAG/Answer Graph) | functionally complete (v1.49 baseline) |
| §5 (Spatial pipeline + drillhole visuals) | scope proposed; §5.3-5.5 substrate done; §5.10-5.11 skeletons |
| §6 (PublicGeo + MapLibre) | scope proposed; §6.1, §6.4 skeleton, §6.5 table done |
| §7 (Reporting + dashboards) | scope proposed; 10 of 16 sub-steps closed |
| §8 (Target Engine) | scope proposed (this tick) |
| §9 (Geological Reasoning + Decision Intelligence) | pending |
| §10 (Eval harness + Customer Support Cockpit) | pending |
| §11 (DR + deployment + perf hardening) | pending |
| §12 (XGBoost + advanced learning) | pending |

## Recommended next tick

Doc-phase 85 = §8.1 `targeting.*` schema migrations. 10 tables per
§18.6. Pattern matches §6.5 (RLS + workspace_id + project_id FKs +
JSONB-heavy payloads).

Alternative: doc-phase 85 = §8.4 Target Recommendation Graph state
model + node stubs (mirrors §7.1 doc-phase 80 pattern). Schema-first
or graph-first — either order works. Schema is more concrete /
testable; graph state is more contract-locking.

Default to schema-first: pure backend, pure DDL, immediately verifiable.

## Carry-overs

1. Unified image rebuild still blocking §5 / §7 / §8 graph
   graduation.
2. SME-dependent §8 work (Athabasca content, scoring weights, sign-off
   mechanism) waits for Kyle.
3. Activepieces install status — gates §8.11 + §7.11.
