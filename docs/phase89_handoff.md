## Doc-phase 89 handoff — Master-plan §9 (Geological Reasoning + Decision Intelligence) scope proposal

**Status:** Complete. Scope proposal doc landed.

## What landed

`docs/master_plan_section9_scope_proposal.md` — fifth scope proposal
in the §5/§6/§7/§8/§9 sequence. Reads master-plan §20 (Geological
Reasoning Layer) + §21 (Decision Intelligence Layer) + Phase 9
deliverables.

Key findings:
- 21-26 ticks estimated (excluding §9.3 ontology population, which
  is multi-week Kyle/SME content work).
- ~75% backend, ~25% frontend (lineage graph UI, hypothesis surface
  in chat, next-best-data on Workspace Health Dashboard).
- **§9 is fundamentally different from §5-§8**: heavily
  content-driven (ontology + deposit-model attributes + analogue
  curation). The scaffolding pattern still works but the SME work is
  substantially larger than in §5-§8.
- Reuses existing infrastructure: Answer Graph (extension only),
  audit ledger (powers lineage UI), citation contract, §7's What
  Changed template (doc-phase 82), §8's target outcomes table,
  Qdrant + Neo4j.

4 open questions tabled for Kyle: ontology ownership, hypothesis UX,
decision capture UX, What Changed cadence.

## Master-plan progress map

| Phase | Status |
|---|---|
| §3 §04p PDF stack | Steps 1-8 functionally done (9-10 = SME) |
| §4 RAG/Answer Graph | v1.49 baseline + R-P11 done |
| §5 Spatial pipeline + drillhole visuals | scope proposed; substrate done |
| §6 PublicGeo + MapLibre | scope proposed; 3 sub-steps closed |
| §7 Reporting + dashboards | scope proposed; 10/16 closed |
| §8 Target Recommendation Engine | scope proposed; 6/14 closed |
| §9 Geological Reasoning + Decision Intelligence | scope proposed (this tick) |
| §10 Eval harness + Customer Support Cockpit | pending |
| §11 DR + deployment + perf | pending |
| §12 XGBoost + advanced learning | pending |

## Recommended next ticks

Per the scope proposal:
- **Doc-phase 90** = §9.1 + §9.2 (ontology schema + seed loader)
- **Doc-phase 91** = §9.4 + §9.5 (hypothesis schema + agent skeleton)
- **Doc-phase 92** = §9.9 + §9.10 (decision intelligence schema +
  facade)
- **Doc-phase 93** = §9.11 + §9.13 (Hatchet workflows)
- **Doc-phase 94** = §9.6 + §9.7 + §9.8 (spatial + next-best-data +
  analogue skeletons)

After §9 skeleton landing: §10 (eval + cockpit) scope proposal.

## Carry-overs

Same blockers as §7/§8:
- Unified image rebuild
- Activepieces install status
- Kyle SME content — §8.3 + §9.3
- 4 new §9 open questions
