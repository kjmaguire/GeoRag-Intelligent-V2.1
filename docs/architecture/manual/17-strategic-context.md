# Chapter 17 — Strategic Context (Master Plan + Phase Timeline)

> Why what got built, got built. This chapter answers "where does this fit
> in the long-term plan?" — it's intentionally short on implementation
> detail (which the rest of the manual covers) and long on intent.

## 1. Master plan sections — eight scope proposals

The master plan is divided into sections; §§5–12 were authored as
**scope proposals** before kickoff. Each proposal sets goal, scope
fence, and acceptance criteria; the implementation lives in the chapters
+ appendices.

| § | Section | Scope (one-liner) | Implementation status | Doc | Manual cross-ref |
|---|---|---|---|---|---|
| 5 | Spatial pipeline + drillhole visuals | The §B/S/G drillhole build-out — bronze raw imports → silver canonical → gold visual aggregates | Live (~90 %) | [docs/master_plan_section5_scope_proposal.md](../../master_plan_section5_scope_proposal.md) | [Ch 04](04-ingestion-flow.md), [Ch 13](13-data-hierarchy.md), [Appendix A](../appendix/A-medallion-contract.md) |
| 6 | PublicGeo + MapLibre layer packs | Public-geoscience ingestion + Martin layer pack publication + frontend overlay | Live (Tier 1 layers shipped) | [docs/master_plan_section6_scope_proposal.md](../../master_plan_section6_scope_proposal.md) | [Ch 09](09-martin-and-maplibre.md), [data_dict/public_geo.md](../data_dict/public_geo.md) |
| 7 | Reporting + dashboards | NI 43-101 report assembly + 6 dashboards | Partial (Phase 7 agents live; report-builder UI in flight) | [docs/master_plan_section7_scope_proposal.md](../../master_plan_section7_scope_proposal.md) | [Appendix M §5](../appendix/M-agents-and-ml-catalog.md) |
| 8 | Target Recommendation Engine | The drill-target generation + scoring + rationale pipeline | Live (Phase 8 agents + TRG LangGraph) | [docs/master_plan_section8_scope_proposal.md](../../master_plan_section8_scope_proposal.md) | [Appendix M §6](../appendix/M-agents-and-ml-catalog.md), [Appendix N §1.2](../appendix/N-agentic-and-retrieval-catalog.md) |
| 9 | Geological Reasoning + Decision Intelligence | Hypothesis generation + decision-record tracking | Live (Phase 9 agents + `silver.decision_records` family) | [docs/master_plan_section9_scope_proposal.md](../../master_plan_section9_scope_proposal.md) | [Appendix M §7](../appendix/M-agents-and-ml-catalog.md) |
| 10 | Eval harness + Customer Support Cockpit | Golden-query eval + operator SupportCockpit + ticket triage agents | Partial (eval harness live; cockpit + Phase 10 agents experimental) | [docs/master_plan_section10_scope_proposal.md](../../master_plan_section10_scope_proposal.md) | [Appendix J §2.6](../appendix/J-testing-matrix.md), [Appendix M §8](../appendix/M-agents-and-ml-catalog.md) |
| 11 | DR + deployment topologies + performance hardening | Multi-region DR, K8s air-gap, capacity playbooks | Partial (DR drills written; K8s + air-gap installer shipped; perf hardening in flight) | [docs/master_plan_section11_scope_proposal.md](../../master_plan_section11_scope_proposal.md) | [Appendix K](../appendix/K-deployment-operations.md), [Appendix L](../appendix/L-kubernetes-and-airgap.md) |
| 12 | XGBoost + source trust + advanced learning | Graduate Phase 8 weighted scoring to XGBoost+SHAP; source-trust scoring; continuous learning loop | Partial (`train_source_trust` + `train_target_model` Hatchet workflows live; `silver.source_trust_scores` live; XGBoost graduation in flight at §12.x sub-steps) | [docs/master_plan_section12_scope_proposal.md](../../master_plan_section12_scope_proposal.md) | [Appendix M §10](../appendix/M-agents-and-ml-catalog.md) |

### The doc-phase numbering convention

These sections were authored at staggered doc-phases (74 = §6 scope, 77
= §7, 84 = §8, 89 = §9, 95 = §10, 96 = §§11/12). Doc-phases are
**chronological writing milestones**, distinct from the master-plan
**§ section numbers**. A doc-phase handoff packet documents what was
written in that pass; a § section is a thematic slice of the
architecture.

## 2. Phase handoff packets (when an overnight pass ended)

Each handoff documents a specific overnight or focused run. The
substantive ones:

### Phase 0 — Foundation handoff

[docs/phase0_handoff.md](../../phase0_handoff.md). Substantively done at
acceptance **15 / 16** as of 2026-05-09. The single remaining gap was
the `/admin/agent-config/*` Inertia surfaces; landed in a parallel
worktree. Phase 0 deliverables:

- 10 PG extensions installed + 8 schemas
- `audit.audit_ledger` hash-chain
- Phase 0 agents wired (Index Health, Storage Tiering, Store
  Reconciliation, Support Packet, Tenant Isolation Auditor, Lineage
  Reporter, vLLM Security Check, LLM Incident Diagnosis, Model Cost
  Summary, Model Upgrade Watch)
- `bronze.provenance` lineage spine
- `silver.workspaces` tenancy spine with `data_version`

### Doc-phase 100 handoff — §11.3 + §11.10 autonomous-safe skeletons

[docs/phase100_handoff.md](../../phase100_handoff.md). §11 autonomous-
safe slice complete — DR drills 1–5 skeletons, cross-store divergence
detection, the "what fires when?" alert layer.

### Doc-phase 101 handoff — §12.3 + §12.4 + §12.5 XGBoost scaffolding

[docs/phase101_handoff.md](../../phase101_handoff.md). **The final
autonomous-safe master-plan tick.** XGBoost scaffolding landed at
~3/13 sub-steps — schema, trainer skeleton, eval harness.

### Doc-phase 102 handoff — §12.7–§12.10 extended scaffolding

[docs/phase102_handoff.md](../../phase102_handoff.md). 4 more §12 sub-
steps closed. §12 reached **9/13 (69 %)**.

### Doc-phase 103 handoff — §12.6 A/B + §12.11/§12.12 graduation notes

[docs/phase103_handoff.md](../../phase103_handoff.md). §12 reached
**11/13 (85 %)**. A/B comparison framework for the XGBoost graduation
landed.

### Doc-phase 104 handoff — §10.13 + §11.5 + §11.4

[docs/phase104_handoff.md](../../phase104_handoff.md). 7 deliverables
across 3 sub-steps: LangFuse deep-link from SupportCockpit, Tenant
Isolation CI workflow (now [.github/workflows/tenant-isolation-auditor.yml](../../../.github/workflows/tenant-isolation-auditor.yml)),
DR runbook scaffolds (covers `ops/runbooks/dr-1` → `dr-5`).

### Doc-phase 105 handoff — §6.5 SavedMapView

[docs/phase105_handoff.md](../../phase105_handoff.md). Model + controller
skeleton; Pint passes. Powers `silver.saved_map_views`.

## 3. Cumulative master-plan completion

Approximation as of 2026-05-29:

| § | Title | Status | Notes |
|---|---|---|---|
| 0 | Foundation | ✅ Done (15/16 → 16/16 by end of 2026-05) | Audit ledger + tenancy + Phase 0 agents |
| 1–4 | Existing pre-master-plan work | ✅ Done | Subsumed into §04* chapters of `georag-architecture.html` |
| 5 | Spatial + drillhole visuals | ~90 % | B6/B7 visuals live; B8/B9 deferred |
| 6 | PublicGeo + MapLibre | ✅ Done for Tier 1 | Tier 2/3 layers planned ([Ch 09](09-martin-and-maplibre.md)) |
| 7 | Reporting + dashboards | Partial | Phase 7 agents live; report-builder UI in flight |
| 8 | Target Recommendation | ✅ Done (Phase 8) | XGBoost graduation = §12 |
| 9 | Geological reasoning | ✅ Done | Hypothesis tracker + decision intelligence schema |
| 10 | Eval + SupportCockpit | Partial | Eval live; cockpit + Phase 10 agents experimental |
| 11 | DR + deployment + perf | Partial | DR runbooks + K8s + air-gap done; perf hardening in flight |
| 12 | XGBoost + source trust | 85 % | Schema + trainer + A/B framework + 11/13 sub-steps |

## 4. Reading order for new contributors

A new engineer joining the project should read in this order:

1. [Ch 00 — Overview](00-overview.md) — what GeoRAG is.
2. [Ch 01 — Services](01-services.md) — the system map.
3. [Ch 04 — Ingestion flow](04-ingestion-flow.md) — how data lands.
4. [Ch 06 — Retrieval + agents](06-retrieval-and-agents.md) — how questions get answered.
5. This chapter — **why** the rest exists.
6. The specific § scope proposal for whichever feature they're touching.
7. The relevant phase handoff if they're picking up an unfinished sub-step.
8. The relevant [appendix](../MANUAL.md#appendices) for the contract they're implementing.

## 5. Strategic non-negotiables (from CLAUDE.md hard rules)

These are the architectural commitments the rest of the plan is built on:

1. **No Streamlit** — React + Inertia is the frontend; if external examples use Streamlit, translate.
2. **Async-native FastAPI** — `asyncpg`, `redis.asyncio`, async Qdrant + Neo4j drivers.
3. **Octane-safe Laravel** — no static state leaks between requests.
4. **Citations mandatory** — every RAG claim carries `source_chunk_id`; refusal otherwise.
5. **Hallucination prevention §04i — six layers** apply to every code path touching the RAG pipeline.
6. **Schemas in §04e are contracts** — don't invent fields.
7. **No orchestration overlap** — Laravel queues = user-triggered; Hatchet = durable per-document; Dagster = scheduled bulk; Kestra = integration edge.
8. **MapLibre GL, not Mapbox GL** — licensing for on-prem.
9. **Neo4j Community Edition only** — no Enterprise features (manual warmup + app-level RBAC).

Per CLAUDE.md, when code disagrees with the architecture doc, **the
doc is correct and the code needs fixing**. This chapter is part of
that doc.
