# Chapter 17b — Master Plan Deep Dive (§§5–12)

> Per-section deep summary of the eight master-plan scope proposals.
> [Ch 17](17-strategic-context.md) is the one-paragraph index; this is
> the full goal / scope-fence / done-test / status / cross-ref pass.
> Source files: [docs/master_plan_section{5..12}_scope_proposal.md](../../).

For each section: **Goal** (the SME-facing pitch verbatim from the
proposal), **Deliverables** (the master-plan-numbered list), **Done
test** (the proposal's acceptance criterion), **Status** (what's live
on main as of 2026-05-29), and **Where to look** (manual cross-refs).

---

## §5 — Spatial pipeline + drillhole visuals

**Doc-phase 69 · authored 2026-05-13**

### Goal

> "Show drillholes on a map and as cross-sections."

§5 takes the drillhole spine — already canonical-ish from earlier
phases — and turns it into something a geologist can see. It is the
spatial / visualisation chapter of the build.

### Deliverables (verbatim from §5)

1. GeoPandas / Rasterio / Shapely fully integrated into FastAPI ingestion paths.
2. Minimum-curvature desurvey producing `silver.drill_traces` cleanly.
3. `gold.drillhole_intervals_visual`, `gold.cross_section_panels`,
   `gold.structure_measurements_visual` materialised via Dagster.
4. First three visualisations: **strip logs, cross-sections, stereonets**
   (Plotly interactive + matplotlib static).
5. Chart export contract enforced (§17.4 — see
   [docs/chart_export_contract_spec.md](../../chart_export_contract_spec.md)).
6. Drillhole Visual QA Agent + Visual Readiness Agent
   ([Appendix M §3](../appendix/M-agents-and-ml-catalog.md)).

### Done test

A drillhole array can be visualised as strip logs + cross-sections with
provenance + chart export contract metadata; the Visual Readiness Agent
correctly explains when a visualisation is/isn't possible.

### Status (Pass 4 close)

**Live ~90 %.** B6/B7 cross-section + interval visual landed
2026-05-22 ([notes/INDEX.md#project_bsg_buildout_2026_05_22](../notes/INDEX.md#project_bsg_buildout_2026_05_22)).
The pre-existing `silver_drill_traces` Dagster asset already satisfied
deliverable #2. Remaining: B8/B9 outputs deferred (stereonet polish +
chart export contract last-mile).

### Where to look in this manual

- [Ch 04 — Ingestion flow](04-ingestion-flow.md) — Dagster gold asset map
- [data_dict/gold.md](../data_dict/gold.md) — every gold drillhole table
- [Appendix M §3](../appendix/M-agents-and-ml-catalog.md) — Phase 5 agents
- [Appendix A §4](../appendix/A-medallion-contract.md) — gold materialisation map

---

## §6 — PublicGeo + MapLibre layer packs

**Doc-phase 74**

### Goal

> "The map shows public data, private data, and chat citations together
> — clickable, attributable."

§6 is the public-data + map UX chapter. Backend is mostly already there
(`public_geoscience.*` schema was populated pre-§6); the bulk is React
+ MapLibre work.

### Deliverables (verbatim)

1. All Saskatchewan public sources verified against v2.3 schemas (mostly
   v1.49 baseline).
2. BC MINFILE + NRCan / GEO.ca sources added.
3. Public/Private Boundary Agent enforcing §2.9 language template.
4. Four layer packs: Private Project, PublicGeo, QA, Target placeholder.
5. Evidence Map Mode — citations highlight map features.
6. Feature Inspector with full attribute panel.
7. AOI draw / buffer / cross-section line tools.
8. Saved map views.
9. h3-pg aggregations for density choropleths.

### Done test

Chat answer cites public mineral occurrences within 25 km of project AOI
using the public/private language template; map highlights the cited
occurrences; clicking each shows full provenance with public/workspace
tags.

### Status

**Tier 1 layers live** (`pg_mines`, `pg_mineral_occurrences`,
`pg_drillhole_collars`, `pg_rock_samples`, `pg_assessment_surveys`,
`pg_resource_potential`, `pg_mineral_dispositions`, `pg_bedrock_geology`).
Tier 2/3 layers pre-written but commented out
([Ch 09 §2](09-martin-and-maplibre.md)). Public/Private Boundary Agent
is live ([Appendix M §4](../appendix/M-agents-and-ml-catalog.md)).
Saved map views landed at **doc-phase 105**
([phase105_handoff.md](../../phase105_handoff.md)). h3 density
aggregations live via `gold.h3_density_mineral`.

### Where to look

- [Ch 09 — Martin and MapLibre](09-martin-and-maplibre.md) — every layer
- [data_dict/public_geo.md](../data_dict/public_geo.md) — public-geo tables
- [Ch 13 — Data Hierarchy](13-data-hierarchy.md) — the geologist-facing classification this map respects

---

## §7 — Reporting + dashboards

**Doc-phase 77 · the largest phase by deliverable count**

### Goal

> "The customer can generate a Technical Due Diligence Report
> end-to-end — every section, every citation, every appendix, every
> signature — and the result passes the §29.2 export compliance
> checklist."

The "ship the deliverable" chapter. Three natural sub-phases: **§7-A
Report Builder** (LangGraph + 11 templates + 8 in-graph agents + 5
renderers + delivery layer), **§7-B Dashboards** (22 across 3 tiers),
**§7-C Export Compliance Agent** (10-line checklist, blocking).

### Deliverables (verbatim)

1. Report Builder Graph (§15.1) end-to-end.
2. **Eleven** report types (§15.2) with templates.
3. Report package structure (§15.3) including hash-chain proof JSON.
4. Export Compliance Agent enforcing §29.2 checklist.
5. All product-tier dashboards (§16.1) — **8 dashboards**.
6. Workflow-tier dashboards (§16.2) — **5 dashboards**.
7. Ops-tier dashboards (§16.3) — **9 dashboards** (reuse Grafana).

### Done test

A Technical Due Diligence Report generates end-to-end with all sections,
all citations, all required appendices, the export compliance checklist
passes, and the Reporting Dashboard shows the run with full traceability.

### Status

**Partial.** Phase 7 agents are live as Pydantic-AI shells
([Appendix M §5](../appendix/M-agents-and-ml-catalog.md)) — `report_planner`,
`appendix_builder`, `claim_validator`, `conflict_resolver`,
`evidence_curator`, `export_compliance`, `map_chart_planner`,
`presentation_coach`. The report-builder UI + the 22-dashboard suite are
still in flight. The hash-chain proof JSON has a Phase 7 contract spec
but no wired generator yet.

### Where to look

- [Appendix M §5](../appendix/M-agents-and-ml-catalog.md) — Phase 7 agents
- [services/report_builder/](../../../src/fastapi/app/services/report_builder/) — agent backend
- [Ch 10 §2](10-frontend.md) — frontend pages (Report / ReportView / Reporting dashboard)
- [Ch 14 — Status Matrix](14-status-matrix.md) — per-page status

---

## §8 — Target Recommendation Engine

**Doc-phase 84**

### Goal

> "AI ranks targets. Evidence explains why. Uncertainty is visible.
> Geologist signs off. GeoRAG learns from outcomes." (§18.1 verbatim.)

**Never say "drill here."** The system always frames recommendations
as "highest-ranked untested target zone based on current evidence" —
enforced in the Recommendation Explainer Agent.

### Deliverables (verbatim)

1. All ten deposit model templates (§20.2) loaded with attributes.
2. Athabasca uranium model fully populated (commodity-specific
   factors, host rocks, alteration, structure, geochemistry, geophysics,
   analogues).
3. Target Recommendation Graph (§18.2) end-to-end — **11 nodes**.
4. Phase 8 weighted scoring with explicit per-factor weights.
5. Per-target SHAP-equivalent breakdown via score factor table.
6. Target sign-off ceremony (R5) with QP credential verification (§19.6).
7. Target Pack map layer (target heatmap, ranked target zones).
8. Target Recommendation Report template.

### Done test

An Athabasca uranium project AOI generates ranked target candidates,
each with per-factor explanation + uncertainty + sign-off flow; the
resulting Target Recommendation Report passes the export compliance
checklist.

### Why §8 interlocks with §7

§7's Export Compliance Agent (§7.8) gates the target report; §7's hash
chain proof JSON builder (§7.7) provides regulatory traceability; §7's
Report Builder Graph (§7.1) handles the rendering. §8's job is the
**Target Recommendation Graph** + 11 agents + the `targeting.*` schema.
The two graphs interlock: §8 produces `targeting.target_recommendations`
rows; §7 renders the §15.2 `target_recommendation` template that consumes
them.

### Status

**Live.** The Target Recommendation Graph is the second of the three
LangGraphs documented in [Appendix N §1.2](../appendix/N-agentic-and-retrieval-catalog.md).
The 11 Phase 8 agents are live ([Appendix M §6](../appendix/M-agents-and-ml-catalog.md)).
`targeting.*` schema is live ([data_dict/targeting.md](../data_dict/targeting.md)).
SHAP-equivalent breakdown via `targeting.target_score_factors`. XGBoost
graduation = §12.

### Where to look

- [Appendix M §6](../appendix/M-agents-and-ml-catalog.md) — Phase 8 agents
- [Appendix N §1.2](../appendix/N-agentic-and-retrieval-catalog.md) — TRG LangGraph
- [data_dict/targeting.md](../data_dict/targeting.md) — `targeting.*` tables
- [services/target_recommendation/](../../../src/fastapi/app/services/target_recommendation/) — graph code

---

## §9 — Geological Reasoning + Decision Intelligence

**Doc-phase 89**

### Goal

Per the §20 intro: **"the most original idea in the architecture and
the most defensible differentiator against generic-RAG competitors."**

§9 is part **autonomous-safe engineering** (decision-records schema,
"What Changed" delta workflow, hypothesis schema, lineage UI scaffold),
part **Kyle SME** (ontology population, deposit-model attributes,
analogue list curation). The latter is unblocked but unscheduled.

### Deliverables (verbatim)

1. Geological ontology populated (§20.1 — 11 classes, 50-200 entries each).
2. Competing hypothesis engine (§20.3) integrated into Answer Graph.
3. Spatial geological relationship engine (§20.4) live.
4. Next-best-data recommendations (§20.5) on Workspace Health Dashboard.
5. Analogue finder (§20.6).
6. Decision Intelligence Layer schema (§21.1, §21.2) populated.
7. All eight tracked decision types (§21.3) capturing decisions.
8. Field feedback loop (§21.4) wired.
9. Data lineage graph UI (§21.6).
10. "What Changed" intelligence (§21.7).

### Done test

A chat session with a geologist surfaces competing hypotheses, the
geologist accepts one, the decision is recorded in `decision_records`,
and a "What Changed" report a week later includes that decision in the
workspace narrative.

### Why §9 is fundamentally different from §5–§8

§5–§8 are mostly **engineering** phases. §9 is **content + science**
at the core — ontology curation, deposit-model attributes, competing-
hypothesis prompts — alongside Decision Intelligence engineering. The
engineering side is autonomous-safe; the content side needs SME passes.

### Status

**Live (engineering side).** Phase 9 agents (`analogue_finder`,
`hypothesis_generator`, `next_best_data`, `spatial_relationship`)
shipped ([Appendix M §7](../appendix/M-agents-and-ml-catalog.md)). The
decision-intelligence schema is live (`silver.decision_records`,
`silver.decision_evidence_links`, `silver.decision_options`,
`silver.decision_outcomes`, `silver.decision_lessons_learned`) — see
[Ch 03 §11](03-schemas.md). Hypothesis tracker live
(`silver.hypotheses`). The "What Changed" weekly aggregator is the
new `what_changed_weekly` Hatchet workflow
([Appendix M §11](../appendix/M-agents-and-ml-catalog.md)). Geological
ontology partially populated (`silver.geological_ontology_terms` +
`_synonyms`); SME completion pass pending.

### Where to look

- [Appendix M §7](../appendix/M-agents-and-ml-catalog.md) — Phase 9 agents
- [data_dict/silver.md](../data_dict/silver.md) — decision + hypothesis tables
- [services/geological_reasoning/](../../../src/fastapi/app/services/geological_reasoning/) — hypothesis generator
- [services/decision_intelligence/](../../../src/fastapi/app/services/decision_intelligence/) — recorder + summary

---

## §10 — Eval harness + Customer Support Cockpit

**Doc-phase 95**

### Goal

> "Operational maturity. Without these the system can ship features
> but cannot evolve safely or support customers at scale."

§10 is the "we can ship + we can support" phase — two natural sub-phases
that are operationally rather than product-facing.

### Deliverables (verbatim)

1. Golden questions schema (§24.1) populated with first 100 questions
   across all question sets.
2. Authoring workflow UI in Laravel admin.
3. Hatchet `evaluate_workspace` workflow.
4. Regression thresholds (§24.4) enforced; promotion blocking active.
5. Eval Dashboard.
6. Customer Support Cockpit (§25) deployed.
7. Support agents (§25.4 — 5 of them).
8. Workflow replay capability (`support_replay` Hatchet workflow).

### Done test

A candidate prompt change triggers eval, the eval blocks promotion on a
regression, Kyle can fix or override with logged rationale; a
customer-reported issue ticket traces through the cockpit to root cause
and replays safely.

### Two natural sub-phases

- **§10-A — Eval harness:** `eval.golden_questions` schema + 100
  questions across ~8 question sets (50/50 SME + autonomous) +
  `evaluate_workspace` workflow + regression thresholds + Eval
  Dashboard. Unblocks safe iteration.
- **§10-B — Customer Support Cockpit:** 3 `ops.*` tables + 5 support
  agents (Ticket Triage / Root Cause Investigation / Support Packet /
  Customer Response Drafting / Escalation Routing) + `support_replay`
  workflow + admin module. Unblocks safe customer support at scale.

### Status

**Partial.** Eval harness live: `eval.golden_questions` schema exists,
`evaluate_workspace` Hatchet workflow live
([Appendix M §11](../appendix/M-agents-and-ml-catalog.md)),
`eval_real_rag_nightly` runs the suite nightly. **Loader from YAML
still owed** ([Z roadmap item #18](../appendix/Z-roadmap.md) +
[golden_question_seed_loader_design.md](../golden_question_seed_loader_design.md)).
Support Cockpit: `ops.support_tickets`, `support_ticket_traces`,
`support_replay_runs` live ([data_dict/ops.md](../data_dict/ops.md)).
All 5 Phase 10 agents live as shells
([Appendix M §8](../appendix/M-agents-and-ml-catalog.md));
`support_replay` workflow live; Cockpit frontend ([SupportCockpit.tsx](../../../resources/js/Pages/Foundry/SupportCockpit.tsx))
live. **LangFuse deep-link from SupportCockpit landed doc-phase 104**
([phase104_handoff.md](../../phase104_handoff.md)).

### Where to look

- [Appendix J §2.6](../appendix/J-testing-matrix.md) — golden RAG suite
- [Appendix M §8 + §11](../appendix/M-agents-and-ml-catalog.md) — Phase 10 agents + ML admin surface
- [data_dict/ops.md](../data_dict/ops.md), [data_dict/eval.md](../data_dict/eval.md)
- [services/support_cockpit/](../../../src/fastapi/app/services/support_cockpit/)

---

## §11 — DR + deployment topologies + performance hardening

**Doc-phase 96**

### Goal

> "Production readiness across deployment patterns."

§11 is **fundamentally ops / infra.** Almost no application code — one
Hatchet workflow (`restore_workspace`), five DR runbooks, one CI workflow
file. Everything else is operator runbooks, Helm chart, K8s manifests,
Docker Compose hardening, signed-bundle build pipeline, load-test
harness, audit-ledger archival cron + cold-tier sink.

### Deliverables (verbatim)

1. Backup strategy (§26.2) implemented across all stores.
2. Cross-store consistency restore tested (§26.3).
3. Hatchet `restore_workspace` workflow.
4. DR runbook for all five scenarios (§26.5).
5. Multi-tenant SaaS deployment hardened with Tenant Isolation Auditor
   in CI.
6. Single-tenant cloud reference deployment (Helm chart).
7. Self-host Docker Compose + Kubernetes manifests.
8. Air-gapped bundle build pipeline (signed update packages, offline
   public data bundles, offline tile bundles).
9. Performance load testing against §28 targets.
10. Audit ledger cold-tier archival working.

### Done test

DR drill on staging restores a workspace cleanly within RTO; air-gapped
bundle build pipeline produces verifiable signed package; load tests
validate p95 targets.

### Status

**Partial — but the autonomous-safe slice is DONE** (per
[phase100_handoff.md](../../phase100_handoff.md)). Live:
- All five DR runbooks ([dr-1 → dr-5](../../../ops/runbooks/))
  — landed doc-phase 104.
- Backup strategy across all stores
  ([Ch 02 §9](02-data-stores.md), [Appendix K §9](../appendix/K-deployment-operations.md)).
- `restore_workspace` Hatchet workflow live.
- Helm chart + K8s manifests + air-gap installer
  ([Appendix L](../appendix/L-kubernetes-and-airgap.md)).
- Tenant Isolation Auditor in CI
  ([.github/workflows/tenant-isolation-auditor.yml](../../../.github/workflows/tenant-isolation-auditor.yml))
  — landed doc-phase 104.
- Audit-ledger cold-tier archival landed
  via `cold_tier_archive` workflow.
- Load tests scaffolded under [docs/load_tests/](../../load_tests/).

In flight: signed-bundle GPG signing chain; multi-tenant SaaS hardening
beyond the auditor.

### Where to look

- [Appendix K — Deployment + Ops](../appendix/K-deployment-operations.md)
- [Appendix L — Kubernetes / Helm / Air-gap](../appendix/L-kubernetes-and-airgap.md)
- [ops/runbooks/](../../../ops/runbooks/) — 37 runbooks indexed in Appendix L §9

---

## §12 — XGBoost + source trust + advanced learning

**Doc-phase 96 · final scope proposal — entire master plan (§3–§12) now scope-proposed**

### Goal

> "Advanced ML capabilities once enough field outcomes have accumulated."

### Deliverables (verbatim)

1. XGBoost target scoring trained on accumulated `target_outcomes`.
2. SHAP explanations on every XGBoost score (**mandatory** — "no
   black-box targeting" per §18.3).
3. A/B comparison between weighted and XGBoost scoring; **both stay in
   production**.
4. Source trust scoring (§21.5) trained.
5. Source trust feeding into retrieval ranking.
6. LLM Incident Diagnosis Graph fully wired.
7. Continuous learning loop running on schedule.
8. Storage Tiering Agent (§10.8) live.
9. Lineage Reporter Agent (§10.9) live.

### Done test

Target rankings include both weighted and XGBoost scores with
SHAP-explained per-target factor contributions; source trust scores
influence retrieval ranking; field feedback loop demonstrably improves
model performance over a quarter of operation.

### §12's hard prerequisite

The whole phase depends on **`targeting.target_outcomes` having enough
labelled rows to train on**. Per §8's geological learning loop (§20.8),
the chain is: targets recommended → geologist signs off → drilling
happens (external) → assays imported → `target_outcomes` rows written.
Realistic threshold for first useful XGBoost training: **50–100 labelled
outcomes per deposit model**. For Athabasca uranium alone, that's
months to years of field-feedback accumulation.

So §12 is structurally a "wait for data" phase. **The scaffolding
(model training workflow, SHAP integration, A/B harness) can land
NOW**; the training itself waits for data.

### Status (Pass 4 close)

**Scaffolding landed at 11 / 13 (85 %)** across doc-phases 101 / 102 /
103 ([phase101_handoff.md](../../phase101_handoff.md),
[phase102_handoff.md](../../phase102_handoff.md),
[phase103_handoff.md](../../phase103_handoff.md)). Live:
- `xgboost` + `shap` deps in the FastAPI image.
- `train_target_model` Hatchet workflow
  ([Appendix M §10.3](../appendix/M-agents-and-ml-catalog.md)).
- XGBoost inference path in `target_scoring` agent.
- `train_source_trust` Hatchet workflow.
- `silver.source_trust_scores` + `silver.source_trust_features` live.
- LLM Incident Diagnosis Graph live as the third LangGraph
  ([Appendix N §1.3](../appendix/N-agentic-and-retrieval-catalog.md)).
- Continuous learning loop scaffold (`continuous_learning_loop` +
  `field_outcome_learning` Hatchet workflows).
- Storage Tiering + Lineage Reporter agents live (Phase 0 agents per
  [Appendix M §2](../appendix/M-agents-and-ml-catalog.md)).

Waiting on data: A/B comparison numbers + actual XGBoost training + 
source-trust-into-ranking wire-up.

### Where to look

- [Appendix M §10](../appendix/M-agents-and-ml-catalog.md) — ML training pipelines
- [Appendix N §1.3](../appendix/N-agentic-and-retrieval-catalog.md) — LLM Incident Diagnosis Graph
- [Ch 12 — Observability](12-observability.md) — Storage Tiering + Lineage Reporter (Phase 0 agents)
- [services/source_trust/](../../../src/fastapi/app/services/source_trust/), [services/target_scoring_ml/](../../../src/fastapi/app/services/target_scoring_ml/)

---

## Cross-section: where each master-plan section's code lives

| § | Primary code locations |
|---|---|
| §5 | `src/dagster/georag_dagster/assets/gold_*.py`, `src/dagster/georag_dagster/assets/silver_to_gold/*.py`, `src/fastapi/app/agents/phase5/`, `resources/js/Components/Foundry/SignificantIntersections3DView.tsx` + DrillTrace3D etc. |
| §6 | `database/migrations/2026_04_14_*` public_geo batch, `docker/martin/martin.yaml`, `resources/js/Pages/Foundry/Lakehouse.tsx`, `app/Http/Controllers/Tiles/`, Kestra `public_geoscience_pull.yaml`, `src/fastapi/app/agents/phase6/` |
| §7 | `src/fastapi/app/agents/phase7/`, `src/fastapi/app/services/report_builder/`, `src/fastapi/app/routers/report_builder.py`, `resources/js/Pages/Foundry/Report.tsx` + `ReportView.tsx`, `app/Http/Controllers/Foundry/*` report controllers |
| §8 | `src/fastapi/app/agents/phase8/`, `src/fastapi/app/services/target_recommendation/`, `database/migrations/2026_05_*targeting*`, `resources/js/Pages/Foundry/Targets.tsx`, `routers/target_recommendation_cockpit.py` |
| §9 | `src/fastapi/app/agents/phase9/`, `src/fastapi/app/services/geological_reasoning/`, `src/fastapi/app/services/decision_intelligence/`, `database/migrations/2026_05_13_1[1-3]*` decision + hypothesis + ontology |
| §10 | `src/fastapi/app/agents/phase10/`, `src/fastapi/app/services/support_cockpit/`, `src/fastapi/app/hatchet_workflows/{evaluate_workspace,eval_real_rag_nightly,support_replay}.py`, `database/migrations/2026_05_13_140*` eval + ops schemas |
| §11 | `charts/georag/`, `kubernetes/manifests/`, `airgap/install.sh`, `ops/runbooks/dr-{1..5}-*.md`, `.github/workflows/tenant-isolation-auditor.yml`, `src/fastapi/app/hatchet_workflows/{restore_workspace,backup_*,cold_tier_archive}.py` |
| §12 | `src/fastapi/app/hatchet_workflows/{train_source_trust,train_target_model,continuous_learning_loop,field_outcome_learning}.py`, `src/fastapi/app/services/{source_trust,target_scoring_ml}/`, `src/fastapi/app/services/llm_incident_diagnosis/`, `app/routers/ml_training.py` |
