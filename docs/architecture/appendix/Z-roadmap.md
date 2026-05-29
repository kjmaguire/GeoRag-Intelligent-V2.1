# Appendix Z — Implementation-Grade Roadmap

The chapters and earlier appendices in this manual cover what GeoRAG **is
today**. This appendix tracks what we still owe to make this an
*implementation-grade* spec — the full ChatGPT-review punch list, broken
into deliverables with owner, priority, and acceptance criteria.

Each entry below maps back to one ChatGPT-review section. Items already
shipped earlier in this manual link directly; items not yet implemented
include a sketch of what the chapter or appendix will contain.

---

## Z.1 Direct inconsistencies — **DONE**

| Item | Status |
|---|---|
| `00-overview` "eight" rules → "nine" | ✅ fixed |
| GUC name canonicalisation (`app.workspace_id`) | ✅ fixed |
| `georag` SUPERUSER framing | ✅ documented in [Ch 02 §1.1](../manual/02-data-stores.md#11-known-security-issue--georag-role-is-superuser) + [Ch 11 §3](../manual/11-tenancy-and-rls.md) + [Appendix C §2](C-security-posture.md#2-tenant-isolation) |
| Martin `martin_ro` promoted to tracked security item | ✅ [Ch 02 §1.2](../manual/02-data-stores.md) + [Appendix C §2](C-security-posture.md) |
| Qdrant prod auth posture | ✅ [Ch 02 §3](../manual/02-data-stores.md) + [Appendix C §6](C-security-posture.md#6-qdrant-access-control) |
| Anthropic profile-gate | ✅ tracked in [Appendix C §5](C-security-posture.md#5-external-llm-data-egress); implementation **planned** |
| `persist_node` best-effort | ✅ documented + fix proposed in [Ch 06 §2.1](../manual/06-retrieval-and-agents.md#21-persistence-is-currently-best-effort--fix-required); implementation **planned** |
| Memory paths → checked-in notes | ✅ [docs/architecture/notes/INDEX.md](../notes/INDEX.md) |

## Z.2 Medallion contract — **DONE (initial cut)**

[Appendix A — Medallion Contract](A-medallion-contract.md) covers the
lineage spine, tier inventory, fan-out, deletion semantics, and test
envelope. Sub-items still open:

- Create `bronze.upload_files` or remove the references.
- Create `bronze.raw_samples` or remove the references.
- Decide on `bronze.manifest` vs `bronze.ingest_manifest` consolidation.
- Promote `gold.significant_intersections` from on-the-fly to persisted.
- Freeze `silver.report_pages / report_figures / report_tables` column
  set with a migration + pgTAP shape test.
- Reconcile `silver.entities` vs `workspace.entities` references.
- Decide on `silver.lithology_intervals` (does not exist — fix all docs
  pointing to it).

## Z.3 Data Hierarchy — **DONE (chapter)**

[Ch 13 — Data Hierarchy](../manual/13-data-hierarchy.md) defines the
geologist-facing taxonomy + the multi-category dataset model. Sub-items
still open:

- Land migrations for `silver.data_categories` + `silver.dataset_categories`.
- Wire the parser-default + agent-suggest + user-override flow.
- Surface the category facet in Lakehouse / Chat / Sources.
- Add the RLS coverage test for `silver.dataset_categories`.

## Z.4 API contract appendix — **DRAFT SHIPPED** ([Appendix D](D-api-contract.md))

Endpoint inventory + auth/idempotency/rate-limit contract + generator
design. Still owed: the actual OpenAPI generator wiring + per-endpoint
schemas. Tracked inside Appendix D §8.

## Z.5 Event + workflow contracts — **DONE (events); workflows owed**

[Appendix B — Event Payloads](B-event-payloads.md) covers Reverb, SSE,
outbox, Kestra webhook+JWT, and the cross-service internal endpoints
that use X-Service-Key.

Still owed:
- Per-Hatchet-workflow input/output schemas with code references for each
  step (Pydantic model + line numbers).
- Per-Dagster-asset input/output type annotations table.

## Z.6 Ingestion format matrix — **DRAFT SHIPPED** ([Appendix E](E-ingestion-format-matrix.md))

19-row matrix with the user-trigger-through-Hatchet decision recorded.
Still owed: per-format end-to-end test fixtures + per-format runbook
for ops handover.

## Z.7 Data dictionary + ERD — **DRAFT SHIPPED** ([Appendix F](F-data-dictionary.md))

Generator design + per-table template + ERD groupings + CI drift guard.
Still owed: actually wire the `data_dictionary_dump` Dagster asset +
SchemaSpy / `eralchemy2` runner.

## Z.8 RAG retrieval contract — **DRAFT SHIPPED** ([Appendix G](G-rag-retrieval-contract.md))

All numeric and structural parameters codified — chunking, embedding,
Qdrant payload, fusion, reranker, citation binding, numeric
verification, confidence formula, golden eval thresholds. Still owed:
per-workspace tuning of the layer-1 threshold + the LoRA reranker bake
pipeline.

## Z.9 Knowledge graph schema — **DRAFT SHIPPED** ([Appendix H](H-knowledge-graph-schema.md))

Node labels, properties, relationships, workspace fence, upsert /
deletion / conflict rules + example Cypher per agent tool. `DrillHole`
canonical capitalisation noted. Still owed: the nightly
`graph_tenant_audit` verifier.

## Z.10 Threat model + security architecture — **DONE (initial cut)**

[Appendix C — Security Posture](C-security-posture.md) covers tenant
isolation, prompt injection, tool abuse, LLM egress, Qdrant, Martin,
object storage, admin panel, secret rotation, backup encryption,
break-glass, audit, data export. Open items embedded inline.

## Z.11 Frontend workflow specs — **DRAFT SHIPPED** ([Appendix I](I-frontend-specs.md))

Acceptance specs for the 9 highest-value pages + cross-cutting rules.
Still owed: lower-tier pages (Hypothesis, Decisions, Report Builder,
Support Cockpit) — their inventory remains in Ch 10.

## Z.12 Testing + evaluation matrix — **DRAFT SHIPPED** ([Appendix J](J-testing-matrix.md))

Per-layer + per-feature test contracts + CI orchestration. Still owed:
the chaos / failure-recovery suite implementation.

## Z.13 Deployment + operations appendix — **DRAFT SHIPPED** ([Appendix K](K-deployment-operations.md))

Fresh install, prod posture, offline bundle, GPU requirements,
.env / port / volume matrices, backup / restore / upgrade / rollback,
scaling, sizing, RPO/RTO, incident playbooks. Still owed:
- ACME-issued TLS verified end-to-end on a live prod stack.
- Workspace migration (move a workspace between hosts) runbook.

## Z.14 Status markers — **DONE**

[Ch 14 — Status Matrix](../manual/14-status-matrix.md) lists every
service / table / workflow / page / agent with Live / Partial /
Planned / Deprecated / Stub / Experimental marker.

---

## Execution priority — implementation-side work that remains

The documentation upgrade is closed. What remains is the **code**
implementation work the appendices specify. Priority order:

1. **Citation persistence hardening** ([Ch 06 §2.1](../manual/06-retrieval-and-agents.md#21-persistence-is-currently-best-effort--fix-required)) — closes a citation-first contract gap.
2. **`georag` role split** + **Martin `martin_ro` switch** ([Appendix C §2](C-security-posture.md#2-tenant-isolation)) — closes the two tenant-isolation latent risks.
3. **Anthropic / external-LLM profile gate + per-workspace policy** ([Appendix C §5](C-security-posture.md#5-external-llm-data-egress)) — closes the data-egress risk.
4. **Bronze + silver table contract clean-up** ([Z.2 sub-items above](#z2-medallion-contract--done-initial-cut)) — closes data-model drift.
5. **`silver.data_categories` migration + UI** ([Ch 13](../manual/13-data-hierarchy.md)) — unblocks geologist-facing hierarchy.
6. **`data_dictionary_dump` Dagster asset** ([Appendix F](F-data-dictionary.md)) — frees future docs from manual table inventories.
7. **OpenAPI generator wiring** ([Appendix D §8](D-api-contract.md#8-generator-wiring-planned)) — closes the API contract loop.
8. **Per-format Hatchet ingest workflows + per-format fixtures** ([Appendix E](E-ingestion-format-matrix.md)).
9. **Lower-tier frontend specs** (the pages not in Appendix I).
10. **Chaos / failure-recovery suite** ([Appendix J §2.10](J-testing-matrix.md)).
11. **Workspace migration runbook + ACME E2E** ([Appendix K](K-deployment-operations.md)).
12. **Graph tenant audit verifier** ([Appendix H §6](H-knowledge-graph-schema.md#6-workspace-isolation-the-fence)).
13. **`silver.data_quality_flags` validation engine** — schema landed [2026_05_26_220200](../../../database/migrations/2026_05_26_220200_create_silver_data_quality_flags.php). **Partial as of Pass 4:** four DQ writers landed (`silver_assay_dq`, `silver_collar_dq`, `silver_crs_dq`, `silver_unit_consistency_dq`); broader rule engine + ingestion-readiness gate still owed.
14. ~~Document supersession schema + Qdrant payload sync~~ — **Schema DONE 2026-05-26** ([silver.document_versions](../../../database/migrations/2026_05_26_220400_create_silver_document_versions.php)); drives Spine A §3b authority rank. Qdrant payload sync still owed.
15. **Finish wiring structured-answer prompt** — prompt module live at [structured_answer_format.py](../../../src/fastapi/app/agent/prompts/structured_answer_format.py); confirm it has fully superseded OIUR in production. ([structured_answer_format_spec.md](../structured_answer_format_spec.md))
16. ~~Apply §0e trace-object migration + write-path~~ — **DONE 2026-05-26.** Migration + buffered writer + persist_node hook all live. Extended 2026-05-28 with `context_prep_audit` + `multi_turn_resolution` columns. ([trace_logging_design.md](../trace_logging_design.md))
17. **User-facing `GuardErrorCode` catalog wiring** — enum live at [guards.py:49](../../../src/fastapi/app/agent/guards.py); Spine B repair-loop dispatcher consumes it (Ch 16 §2). End-to-end user-facing catalog mapping (per-code message + UI surface + follow-up action) still owed.
18. **Build golden-question YAML loader** — fully owed. ([golden_question_seed_loader_design.md](../golden_question_seed_loader_design.md))

### Pass 4 additions (2026-05-29)

19. ~~Spine A — context preparation library~~ — **DONE 2026-05-27** ([ADR-0009](../../adr/0009-algorithmic-spines-rollout.md), [context_prep.py](../../../src/fastapi/app/agent/context_prep.py)). Production wire behind `CONTEXT_PREP_ENABLED` (default off).
20. ~~Spine B — repair loop shadow wire + nightly aggregator~~ — **DONE 2026-05-27** ([repair_shadow_aggregate.py](../../../src/fastapi/app/hatchet_workflows/repair_shadow_aggregate.py) writes `gold.repair_shadow_daily`). Stage 2 (terminal enable) + Stage 3 (low-cost loop enable) per [repair_loop_spec.md §8](../repair_loop_spec.md) still owed.
21. ~~Multi-turn resolver + entity resolver + geospatial planner~~ — **DONE 2026-05-27** ([multi_turn_resolver.py](../../../src/fastapi/app/agent/multi_turn_resolver.py), [entity_resolver.py](../../../src/fastapi/app/agent/entity_resolver.py), [geospatial_planner.py](../../../src/fastapi/app/agent/geospatial_planner.py)).
22. ~~Canonical chunked corpus + Qdrant `georag_chunks`~~ — **DONE 2026-05-29** ([ADR-0010](../../adr/0010-document-passages-canonical-chunked-corpus.md), [index_document_passages.py](../../../src/dagster/georag_dagster/assets/index_document_passages.py)). Deprecation of `index_reports` + `georag_reports` collection still owed; backfill script ships ([\_backfill_document_passages_to_qdrant.py](../../../src/dagster/scripts/_backfill_document_passages_to_qdrant.py)).
23. **Stage 2 + Stage 3 repair-loop production enable** — gated on Stage 1 telemetry from `gold.repair_shadow_daily`.
24. **Cutover from `index_reports` → `index_document_passages`** — both run today; flip the reranker chain + retrieval default once `georag_chunks` is fully populated.
25. **`gold.repair_shadow_daily` Grafana dashboard** — workflow writes rows; dashboard JSON still owed.
26. **`silver.entity_aliases` SME backfill loop** — gap rows accrue in `silver.entity_gaps`; SME-facing UI to convert gaps → aliases is owed.
