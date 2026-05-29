## Doc-phase 96 handoff — §11 + §12 scope proposals (MASTER PLAN FULLY SCOPED)

**Status:** Complete. Final two scope proposals. **The entire
master-plan §3-§12 sequence now has scope coverage.**

## What landed

### §11 (DR + deployment topologies + perf hardening)

`docs/master_plan_section11_scope_proposal.md` — 18-26 ticks; ~80%
ops/infra (Helm charts, Docker Compose hardening, signed-bundle
pipeline, load testing, DR runbooks). Mostly NOT autonomous-safe;
ops/infra decisions need Kyle. Smallest autonomous slice: §11.3
`restore_workspace` Hatchet workflow + §11.10 audit archival
logic skeleton (~3-4 ticks).

4 open questions for Kyle: load test tool choice, air-gapped bundle
v1 scope, cold-tier destination, DR drill cadence.

### §12 (XGBoost + source trust + advanced learning)

`docs/master_plan_section12_scope_proposal.md` — 16-22 ticks; almost
all backend ML. **HARD prerequisite: field-outcome data accumulation
in `targeting.target_outcomes`.** Phase 12 scaffolding is autonomous-
safe; real training waits months/years.

4 open questions for Kyle: synthetic outcomes mode, A/B UI choice,
source-trust decay window, Storage Tiering autonomy.

The §12 scope proposal includes a closing section: **"With §12
scoped, the master plan is fully proposed."** Remaining work splits
into three tracks (image rebuild, Kyle SME content, frontend pass).

## Master-plan progress map — ALL PHASES SCOPED

| Phase | Status |
|---|---|
| §3 §04p PDF stack | Steps 1-8 functionally done; 9-10 = SME |
| §4 RAG/Answer Graph | v1.49 baseline + R-P11 done |
| §5 Spatial pipeline + drillhole visuals | ✅ scope proposed; substrate done |
| §6 PublicGeo + MapLibre | ✅ scope proposed; 3 sub-steps closed |
| §7 Reporting + dashboards | ✅ scope proposed; 10/16 sub-steps closed |
| §8 Target Recommendation Engine | ✅ scope proposed; 6/14 closed |
| §9 Geological Reasoning + Decision Intelligence | ✅ scope proposed; 12/14 closed (86%) |
| §10 Eval harness + Support Cockpit | ✅ scope proposed |
| §11 DR + deployment + perf hardening | ✅ scope proposed |
| §12 XGBoost + advanced learning | ✅ scope proposed |

## Recommended next ticks

The autonomous-safe scoping pass is COMPLETE. Remaining autonomous-
safe work:

- **§10 backend scaffolding** (schemas + Hatchet workflow + 5 support
  agent skeletons) — ~7-9 ticks per scope proposal
- **§11.3** + **§11.10** small skeletons — ~2-3 ticks
- **§12.3-§12.10** scaffolding skeletons — ~8-10 ticks

That's 17-22 more autonomous-safe ticks. After those, the master
plan substrate is fully scaffolded.

**Doc-phase 97** = §10.1 + §10.5 eval schemas (per §10 scope proposal
recommendation).

After all autonomous-safe scaffolding lands, the autonomous run is
genuinely done — every remaining tick needs Kyle SME content, image
rebuild, or frontend work.

## Carry-overs

Unchanged from prior — image rebuild + SME content + Activepieces +
frontend pass. Plus open questions across all 8 scope proposals
(~24-32 questions total) for Kyle's review.

## What this run produced (cumulative)

22 doc-phase handoffs (74-96) + 7 scope proposals (§6/§7/§8/§9/§10/
§11/§12 new this run) + 1 cumulative continuation briefing.

Substrate landed across §6/§7/§8/§9:
- **18 new database tables** (silver.saved_map_views, 10 targeting.*,
  2 ontology, 2 hypothesis, 5 decision_intelligence)
- **34+ agent skeletons** across phase6/phase7/phase8/phase9 packages
- **2 LangGraph state models + 25 node stubs** (Report Builder, Target
  Recommendation)
- **4 Hatchet workflows** (generate_report, score_targets,
  field_outcome_learning, what_changed_detector) registered in AI
  pool
- **6 new service packages** (report_builder, target_recommendation,
  decision_intelligence, geological_ontology, plus
  audit/hash_chain_proof + agent phase packages)
- **11 report templates + 10 deposit-model templates + 12 ontology-
  class slots**

All work import-smoke-tested + verified in running fastapi container.
