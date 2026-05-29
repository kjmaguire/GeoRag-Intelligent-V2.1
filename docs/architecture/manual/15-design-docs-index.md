# Chapter 15 — Design Docs (planning artifacts)

These seven documents live at the top of `docs/architecture/` and are
authoritative for **plan intent** — they precede this manual and they
specify behaviour we have not yet shipped. They are not redundant with
the rest of the manual; they are the source for several "Planned" items
in [Ch 14](14-status-matrix.md).

> When a design doc and the manual disagree on what we *should* do, the
> design doc wins until the work lands. When they disagree on what we
> *have*, the manual + code win.

> **Pass 4 update (2026-05-29):** the original seven docs are now joined
> by **seven more** under the ADR-0009 + ADR-0010 work (context_prep,
> repair_loop, multi_turn_resolution, parent_child_chunker,
> reranker_v1_blockers, shadow_telemetry_sentry_tags,
> spatial_chat_card_audit). Several "not implemented" claims have
> shipped — see the verified-status column.

## 1. The seven docs

| # | Doc | Doc header status | **Verified current status** | What it specifies | Reflected in this manual |
|---|---|---|---|---|---|
| 1 | [data_quality_flags_design.md](../data_quality_flags_design.md) | "Schema drafted (not applied); validation rules + gate wiring not implemented" | **Schema live** ([2026_05_26_220200](../../../database/migrations/2026_05_26_220200_create_silver_data_quality_flags.php) + test-DB sibling [2026_05_26_220300](../../../database/migrations/2026_05_26_220300_provision_silver_data_quality_flags_for_test_db.php)). Validation rules + writers **still pending**. | `silver.data_quality_flags` + plan §1g QA/QC validation rules + §0g ingestion readiness gate + §6a document-view UI surface | [Ch 03 §4](03-schemas.md) "QA gates" mention; expand once the rule engine ships |
| 2 | [document_versioning_design.md](../document_versioning_design.md) | "Schema drafted (not applied); supersession detection + Qdrant payload sync not implemented" | **Schema live as of 2026-05-26:** [silver.document_versions](../../../database/migrations/2026_05_26_220400_create_silver_document_versions.php) + test-DB sibling 220500. Drives Spine A §3b authority rank ([Ch 16](16-algorithmic-spines.md)). Qdrant payload sync still pending. | Document supersession (new NI 43-101 deprecates an older one) | [Ch 16 §1](16-algorithmic-spines.md), [Ch 6 §6](06-retrieval-and-agents.md) |
| 3 | [six_subgraphs_spec.md](../six_subgraphs_spec.md) | "Documentary — reconciles plan §0d with shipped implementation" | **Documentary.** Codified by [ADR-0006](../../adr/0006-agentic-retrieval-single-graph.md). | Why we shipped **one** LangGraph + six routed intents rather than six data-store-specific subgraphs | [Ch 06 §2](06-retrieval-and-agents.md) and [ADR-0006](../../adr/0006-agentic-retrieval-single-graph.md) |
| 4 | [structured_answer_format_spec.md](../structured_answer_format_spec.md) | "Spec + draft prompt at `_drafts/structured_answer_format_v1.txt`; NOT wired into production prompts" | **Prompt module live** at [src/fastapi/app/agent/prompts/structured_answer_format.py](../../../src/fastapi/app/agent/prompts/structured_answer_format.py); imported by [agent/orchestrator/__init__.py](../../../src/fastapi/app/agent/orchestrator/__init__.py). Whether it has fully superseded OIUR in production prompts needs SME confirmation. | Plan §4a 8-section structured answer format (extends OIUR); §0b prompt-budget constraints; §4d error-catalog interaction | OIUR is live ([Ch 06 §7](06-retrieval-and-agents.md)); 8-section format is the next iteration |
| 5 | [trace_logging_design.md](../trace_logging_design.md) | "Schema migration drafted (not applied); write-path not implemented" | **Both live as of 2026-05-26.** Migration: [2026_05_26_220000](../../../database/migrations/2026_05_26_220000_create_silver_query_traces.php) + test-DB sibling [2026_05_26_220100](../../../database/migrations/2026_05_26_220100_provision_silver_query_traces_for_test_db.php). Writer: [src/fastapi/app/services/trace_writer.py](../../../src/fastapi/app/services/trace_writer.py) (buffered queue → batched 5-s / 50-trace flush; called from `persist_node`). | Plan §0e trace object — mandatory instrumentation; consumed by §5c dashboard SLA targets | [Ch 12 §3](12-observability.md) covers OTel/Tempo; **add `silver.query_traces` mention** (TODO follow-up) |
| 6 | [user_facing_error_catalog.md](../user_facing_error_catalog.md) | "Specification. Not wired into FastAPI / Laravel yet" | **Partially wired.** `GuardErrorCode` enum lives at [src/fastapi/app/agent/guards.py:49](../../../src/fastapi/app/agent/guards.py) and is consumed by `agentic_retrieval/nodes.py`, `models/rag.py`, `services/trace_writer.py`. User-facing catalog mapping (the spec's main deliverable) likely **still not wired** end-to-end. | Plan §4b 16-code `GuardErrorCode` taxonomy → per-code user-facing message + UI surface + follow-up action; §4c "death loop" response | [Ch 06 §14](06-retrieval-and-agents.md) refusal path; spec upgrades it to a full catalog |
| 7 | [golden_question_seed_loader_design.md](../golden_question_seed_loader_design.md) | "Spec for the loader; not implemented" | **Still spec.** `eval.golden_questions` exists; the YAML→table loader does not. | Bridges SME-edited YAML to the `eval.golden_questions` table | [Appendix J §2.6](../appendix/J-testing-matrix.md#26-rag-golden-queries) RAG golden suite |

## 1b. The seven Pass-4 additions (ADR-0009 + ADR-0010 era)

| # | Doc | Doc header status | Verified current status | What it specifies | Reflected in this manual |
|---|---|---|---|---|---|
| 8 | [context_prep_spec.md](../context_prep_spec.md) | (spec) | **Library live** at [agent/context_prep.py](../../../src/fastapi/app/agent/context_prep.py); production wire behind `CONTEXT_PREP_ENABLED` flag (default off) | Plan §3a-c-f composition: typed evidence → authority rank → source-diversity rerank → budget enforcer | [Ch 16 §1](16-algorithmic-spines.md) (Spine A) |
| 9 | [repair_loop_spec.md](../repair_loop_spec.md) | (spec) | **Shadow wire live**; dispatcher + death-loop detector landed; Stage 1 nightly aggregator workflow (`repair_shadow_aggregate`) ships rollups into `gold.repair_shadow_daily`. Stage 2/3 production enable behind `REPAIR_LOOP_ENABLED` flag (default off) | Plan §4b dispatcher (`GuardErrorCode` → `RepairStrategy`) + §4c death-loop detector | [Ch 16 §2](16-algorithmic-spines.md) (Spine B) |
| 10 | [multi_turn_resolution_spec.md](../multi_turn_resolution_spec.md) | (spec) | **Library live** at [agent/multi_turn_resolver.py](../../../src/fastapi/app/agent/multi_turn_resolver.py); writes `silver.query_traces.multi_turn_resolution` | Plan §3e — pronoun / demonstrative / comparative resolution against turn history | [Ch 16 §1](16-algorithmic-spines.md) |
| 11 | [parent_child_chunker_spec.md](../parent_child_chunker_spec.md) | (spec) | **Live.** `silver.document_passages.parent_chunk_id` + `chunk_kind` extension landed 2026-05-28/29; chunker writes parent/child rows; retrieval rehydrates parents | Plan §1b two-level chunk hierarchy on `silver.document_passages` | [Ch 16 §3](16-algorithmic-spines.md) |
| 12 | [reranker_v1_blockers.md](../reranker_v1_blockers.md) | "blockers list" | **Driving doc for ADR-0010.** Surfaced the three-chunk-table mismatch; `index_document_passages` + the reranker chain re-target are the closures | Lists the topology issues preventing the §5e reranker LoRA pre-flight | [Ch 16 §3](16-algorithmic-spines.md), [ADR-0010](../../adr/0010-document-passages-canonical-chunked-corpus.md) |
| 13 | [shadow_telemetry_sentry_tags.md](../shadow_telemetry_sentry_tags.md) | (spec) | **Wired into shadow path** — Sentry tags `guard_code`, `repair_strategy`, `evidence_kind` per repair candidate | Sentry tag taxonomy for shadow-mode telemetry | [Ch 16 §2](16-algorithmic-spines.md) |
| 14 | [spatial_chat_card_audit_2026_05_29.md](../spatial_chat_card_audit_2026_05_29.md) | (audit) | **Audit artifact** — documents gaps in the spatial chat-card surface against ADR-0007's contract | One-off audit of the ADR-0007 spatial card | [Ch 6 §15](06-retrieval-and-agents.md), [ADR-0007](../../adr/0007-chat-cards-and-intent-expansion.md) |

## 2. Promotion path

Each of the seven docs maps to a future Z-roadmap implementation item
(see [Z execution priority](../appendix/Z-roadmap.md#execution-priority--implementation-side-work-that-remains)):

| Doc | Implementation item | Tracked at |
|---|---|---|
| data_quality_flags | Land `silver.data_quality_flags` + validation engine | Add as Z item #13 |
| document_versioning | Land supersession schema + Qdrant payload sync | Add as Z item #14 |
| six_subgraphs | Already shipped (ADR-0006); doc kept as historical reconciliation | (closed) |
| structured_answer_format | Wire the v1 prompt into the agentic graph | Add as Z item #15 |
| trace_logging | Apply the §0e trace-object migration + write-path | Add as Z item #16 |
| user_facing_error_catalog | Wire the catalog into FastAPI guard responses + frontend | Add as Z item #17 |
| golden_question_seed_loader | Build the YAML → `eval.golden_questions` loader | Add as Z item #18 |

## 3. Plan section ↔ doc index

For navigation by plan §:

| Plan § | Design doc that owns it |
|---|---|
| §0b (prompt budget) | structured_answer_format_spec |
| §0d (subgraph reconciliation) | six_subgraphs_spec |
| §0e (mandatory trace object) | trace_logging_design |
| §0g (ingestion readiness gate) | data_quality_flags_design |
| §1g (QA/QC validation) | data_quality_flags_design |
| §1h (document versioning) | document_versioning_design |
| §3b (multi-document retrieval authority ranking) | document_versioning_design |
| §4a (8-section structured answer) | structured_answer_format_spec |
| §4b (16-code GuardErrorCode) | user_facing_error_catalog |
| §4c (death-loop response) | user_facing_error_catalog |
| §4d (internal→user-facing error mapping) | user_facing_error_catalog |
| §5c (dashboard SLA targets) | trace_logging_design |
| §6a (document-view UI surface) | data_quality_flags_design |

## 4. Conventions for new design docs

When a future feature needs design ahead of implementation:

1. Drop it at `docs/architecture/<topic>_design.md` (top level of the
   architecture dir, not under `manual/` or `appendix/`).
2. Use the same `**Status:**` + `**Plan reference:**` header these seven
   use.
3. Add a row to §1 of this chapter linking it.
4. Add a row to §2 with the implementation item + Z-roadmap pointer.
5. Once shipped, either:
   - Fold the content into the matching chapter / appendix and delete
     the design doc, **or**
   - Keep the doc as historical context with a `**Superseded by:**`
     header pointing at the chapter that now owns the live contract.
