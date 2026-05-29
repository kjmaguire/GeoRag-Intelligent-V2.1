## Doc-phase 95 handoff — Master-plan §10 (Eval harness + Customer Support Cockpit) scope proposal

**Status:** Complete. Sixth scope proposal in the §5-§10 sequence.

## What landed

`docs/master_plan_section10_scope_proposal.md` — reads master-plan
§24 (Eval harness) + §25 (Support Cockpit) + Phase 10 deliverables.

Key findings:
- 19-26 ticks estimated. Smaller than §7/§8/§9; comparable to §6.
- **Two natural sub-phases**:
  - **§10-A** Eval harness (golden questions + evaluate_workspace
    workflow + thresholds + dashboard)
  - **§10-B** Support Cockpit (3 ops.* tables + 5 support agents +
    support_replay workflow + Laravel admin module)
- ~65% backend, ~35% frontend.
- §10 reuses `workflow_runs` + `audit_ledger` + Hatchet pattern +
  LangFuse stack already running. Most "new infrastructure" is
  question content + UI surfaces.
- Autonomous-safe slice: 7 sub-steps (schemas + workflow skeletons +
  agent skeletons) — ~7-9 ticks of scaffolding work.

4 open questions tabled for Kyle: golden-question ownership,
regression threshold mode (warning vs blocking), cockpit access
model, replay cost ceiling.

## Master-plan progress map

| Phase | Status |
|---|---|
| §3 §04p PDF stack | Steps 1-8 functionally done |
| §4 RAG/Answer Graph | v1.49 baseline + R-P11 done |
| §5 Spatial pipeline | scope proposed; substrate done |
| §6 PublicGeo + MapLibre | scope proposed; 3 sub-steps closed |
| §7 Reporting + dashboards | scope proposed; 10/16 closed |
| §8 Target Recommendation Engine | scope proposed; 6/14 closed |
| §9 Geological Reasoning + Decision Intelligence | scope proposed; 12/14 closed (86%) |
| §10 Eval harness + Support Cockpit | **scope proposed (this tick)** |
| §11 DR + perf hardening | pending |
| §12 XGBoost + advanced learning | pending |

## Recommended next ticks

Per scope proposal recommendation:
- **Doc-phase 96** = §10.1 + §10.5 (eval schemas)
- **Doc-phase 97** = §10.4 (`evaluate_workspace` workflow skeleton)
- **Doc-phase 98** = §10.8 ops.* schema + §10.9 5 support agent
  skeletons + §10.10 `support_replay` workflow skeleton
- **Doc-phase 99** = §11 (DR + perf) scope proposal

After §10 scaffolding: §11 (DR + deployment + perf), §12 (XGBoost +
advanced learning).

## Carry-overs

Same blockers as prior ticks:
- Unified image rebuild
- Kyle SME content for §8.3 + §9.3 + §10 golden questions
- Activepieces install status
- 4 new §10 open questions added to the queue
