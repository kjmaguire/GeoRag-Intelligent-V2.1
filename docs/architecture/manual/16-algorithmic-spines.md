# Chapter 16 — Algorithmic Spines + Canonical Corpus Consolidation

Two major architectural moves landed 2026-05-27 → 2026-05-29 that re-shape
the retrieval surface. Both are flag-gated and rolling out behind the
shadow path.

> Source ADRs: [ADR-0009](../../adr/0009-algorithmic-spines-rollout.md)
> (algorithmic spines, 2026-05-27) and
> [ADR-0010](../../adr/0010-document-passages-canonical-chunked-corpus.md)
> (canonical chunked corpus, 2026-05-29).

## 1. ADR-0009 — Spine A (context preparation)

A pure-function library composition for plan §3 (context preparation),
landed behind feature flags default off. Lives at
[src/fastapi/app/agent/context_prep.py](../../../src/fastapi/app/agent/context_prep.py).

Composition order:
1. **§3a** — typed evidence (input shape contract)
2. **§3b** — authority rank (recency + source-trust + document-version
   primacy from [silver.document_versions](#3-adr-0010--canonical-chunked-corpus))
3. **§3c** — source-diversity rerank (avoid 10 paragraphs from the same
   report)
4. **§3f** — budget enforcer (prompt-token cap)

Output: a deterministic ordered evidence list that the answer-assembly
node sees.

### Companion modules (also ADR-0009)

| Module | Plan § | Purpose |
|---|---|---|
| [agent/multi_turn_resolver.py](../../../src/fastapi/app/agent/multi_turn_resolver.py) | §3e | Pronoun / demonstrative / comparative resolution against conversation history |
| [agent/entity_resolver.py](../../../src/fastapi/app/agent/entity_resolver.py) | §2c | `silver.entity_aliases` lookup + `silver.entity_gaps` writes when unresolved |
| [agent/geospatial_planner.py](../../../src/fastapi/app/agent/geospatial_planner.py) | §2g | PostGIS query planner |
| [agent/tools_geospatial.py](../../../src/fastapi/app/agent/tools_geospatial.py) | §2g | Tool-surface wire for the planner |

## 2. ADR-0009 — Spine B (repair loop)

Plan §4b dispatcher mapping `GuardErrorCode` → `RepairStrategy`, plus
§4c death-loop detector. Shadow-mode wire writes per-query repair
strategy + guard code + evidence kind to `silver.query_traces`.

- Dispatcher: integrated into the agentic-retrieval LangGraph nodes
  (consumes the `GuardErrorCode` enum at
  [agent/guards.py:49](../../../src/fastapi/app/agent/guards.py)).
- Death-loop detector: prevents repair-fire / re-guard / repair-fire
  cycles within a single turn.
- Nightly aggregator: Hatchet workflow
  [repair_shadow_aggregate](../../../src/fastapi/app/hatchet_workflows/repair_shadow_aggregate.py).
  - Cron `15 2 * * *` UTC (15 minutes after `audit_ledger_verify` to
    avoid DB-connection contention).
  - Reads `silver.query_traces` shadow rows; writes per-workspace daily
    rollups to `gold.repair_shadow_daily`.
  - Pre-aggregates so the Grafana dashboard renders in <100 ms.
  - Workspace-scoped via `set_config('georag.workspace_id', …)` (note
    the schema-prefixed GUC name).
  - Sizing input for Stage 2 (terminal enable) and Stage 3 (low-cost
    loop enable) per [repair_loop_spec.md §8](../repair_loop_spec.md).

### Sentry tags

Shadow telemetry also flows to Sentry per
[shadow_telemetry_sentry_tags.md](../shadow_telemetry_sentry_tags.md) —
each repair candidate is tagged with `guard_code`, `repair_strategy`,
`evidence_kind` so production refusal spikes can be sliced quickly.

## 3. ADR-0010 — Canonical Chunked Corpus

### The mismatch ADR-0010 closes

Three silver-tier tables held post-ingest chunked text and disagreed on
which was canonical:

| Source | Rows / coverage | Backed Qdrant? | Owner asset |
|---|---|---|---|
| `silver.reports.sections_text` (JSONB sections per report) | 86 of 1,211 reports have content (7 %) | ✅ `georag_reports` (15,413 points) | `index_reports` |
| `silver.document_passages` (§1b parent-child chunker output) | 7,065 rows / 1,159 docs ≥200 chars | ❌ `georag_chunks` was empty | `index_document_passages` (new) |
| `silver.ingest_extractions` (§04p PDF stack) | 246 rows / 51 ≥200 chars / 3 docs | ❌ | `silver_ingest_extractions` (ADR-0002 era) |

The reranker-label asset chain had been reading `silver.ingest_extractions`
— which has 47× less content than `document_passages` — making any
generated training set useless.

### The decision

**`silver.document_passages` is the canonical chunked-content corpus.**
- New Qdrant collection: **`georag_chunks`** ([index_document_passages.py:64](../../../src/dagster/georag_dagster/assets/index_document_passages.py)).
- `index_reports` → `georag_reports` stays during the cutover; eventual deprecation.
- The reranker-label chain re-targets `silver.document_passages`
  ([test_reranker_uses_document_passages_canonical.py](../../../src/dagster/tests/test_reranker_uses_document_passages_canonical.py) pins the contract).

### Parent-child chunking

The §1b spec ([parent_child_chunker_spec.md](../parent_child_chunker_spec.md))
introduces a two-level chunk hierarchy on `silver.document_passages`:

- `parent_chunk_id` column added by [2026_05_28_030000](../../../database/migrations/2026_05_28_030000_add_parent_chunk_id_to_document_passages.php).
- `chunk_kind` enum extended for parent + child by [2026_05_29_180000](../../../database/migrations/2026_05_29_180000_extend_document_passages_chunk_kind_for_parent_child.php).
- Retrieval fetches child chunks (high precision); the assembly node
  rehydrates with the parent for context (high recall).

### Backfill

[src/dagster/scripts/_backfill_document_passages_to_qdrant.py](../../../src/dagster/scripts/_backfill_document_passages_to_qdrant.py)
is the one-shot used to seed `georag_chunks` from the existing
`silver.document_passages` rows. Re-runs are idempotent on `passage_id`.

## 4. New silver tables (2026-05-26 → 2026-05-29 batch)

| Table | Created by | Status | Notes |
|---|---|---|---|
| `silver.query_traces` | [2026_05_26_220000](../../../database/migrations/2026_05_26_220000_create_silver_query_traces.php) (+ test-DB sibling 220100, + audit columns [2026_05_28_010000](../../../database/migrations/2026_05_28_010000_add_context_prep_audit_and_multi_turn_resolution_to_query_traces.php)) | Live | Plan §0e trace object — see [Ch 12 §3](12-observability.md). Now carries `context_prep_audit` JSONB + `multi_turn_resolution` JSONB columns. |
| `silver.data_quality_flags` | [2026_05_26_220200](../../../database/migrations/2026_05_26_220200_create_silver_data_quality_flags.php) (+ test-DB sibling 220300) | Live (schema); writers still partial | See [Ch 15](15-design-docs-index.md) row 1. Several DQ asset writers landed: `silver_assay_dq`, `silver_collar_dq`, `silver_crs_dq`, `silver_unit_consistency_dq`. |
| `silver.document_versions` | [2026_05_26_220400](../../../database/migrations/2026_05_26_220400_create_silver_document_versions.php) (+ test-DB sibling 220500) | Live | **Closes [document_versioning_design.md](../document_versioning_design.md).** Powers §3b authority rank in Spine A. |
| `silver.entity_aliases` | [2026_05_26_220600](../../../database/migrations/2026_05_26_220600_create_silver_entity_aliases_and_gaps.php) (+ test-DB sibling 220700) | Live | Lookup table for `entity_resolver.py` (§2c). |
| `silver.entity_gaps` | same migration | Live | Logs unresolved entity-resolution attempts so the SME can backfill aliases. |
| `gold.repair_shadow_daily` | (in flight — written by `repair_shadow_aggregate` workflow) | Partial | Per-workspace daily repair telemetry rollup. |

## 5. New silver columns

| Table | Column | Added | Purpose |
|---|---|---|---|
| `silver.reports` | `report_type` | [2026_05_28_020000](../../../database/migrations/2026_05_28_020000_add_report_type_to_silver_reports.php) | NI 43-101 / internal / regulatory / etc. classifier output |
| `silver.reports` | `license_attribution` | [2026_05_28_180000](../../../database/migrations/2026_05_28_180000_add_license_attribution_to_silver_reports.php) | Per-row license attribution for redistribution |
| `silver.document_passages` | `parent_chunk_id` | [2026_05_28_030000](../../../database/migrations/2026_05_28_030000_add_parent_chunk_id_to_document_passages.php) | Parent–child chunk link |
| `silver.document_passages` | `chunk_kind` (extended) | [2026_05_29_180000](../../../database/migrations/2026_05_29_180000_extend_document_passages_chunk_kind_for_parent_child.php) | Parent / child variant tags |
| `silver.query_traces` | `context_prep_audit`, `multi_turn_resolution` | [2026_05_28_010000](../../../database/migrations/2026_05_28_010000_add_context_prep_audit_and_multi_turn_resolution_to_query_traces.php) | Spine A telemetry |

## 6. New Dagster assets (10+)

Added since Pass 3:
- **Data-quality cluster:** `silver_assay_dq`, `silver_collar_dq`,
  `silver_crs_dq`, `silver_unit_consistency_dq`.
- **Structure pipeline:** `silver_structure_derive`,
  `silver_structure_populate`.
- **Format coverage:** `silver_seismic`, `silver_xlsx`, `silver_xyz`.
- **`smdi_deposits`** asset (was bronze-only before).
- **`sparse_encoder`** — batch SPLADE++ encoding asset.
- **`index_document_passages`** — new Qdrant feeder for `georag_chunks`.

## 7. New Hatchet workflow

- [`repair_shadow_aggregate`](../../../src/fastapi/app/hatchet_workflows/repair_shadow_aggregate.py)
  — Spine B nightly rollup (cron `15 2 * * *` UTC, workspace-scoped).

## 8. Flag gating

Both spines default **off** in production:
- Spine A composition: feature flag `CONTEXT_PREP_ENABLED` (default false).
- Spine B repair loop: feature flag `REPAIR_LOOP_ENABLED` (default false);
  Stage-gated rollout per [repair_loop_spec.md §8](../repair_loop_spec.md).
- Shadow mode: writes telemetry without changing answers — always on.

Per ADR-0009: 471+ tests across the 10 modules; the wires land flagged
off so production answers do not change until rollout decisions are made
from `gold.repair_shadow_daily` evidence.
