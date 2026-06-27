# Chapter 18 — Model Stack Evolution + the 2026-06 Audit Wave

> Captures the wave of changes between 2026-05-29 and 2026-06-26: the
> Qwen3 model swaps, the §04p OCR/VL upgrades, ADRs 0011–0017, the new
> structured-to-NL retrieval corpus, contextual retrieval, project
> lifecycle states, and the new tenancy/observability schema. This is
> the "what changed recently and why" chapter — read it alongside the
> chapters it patches (Ch 02, 05, 08, Appendix G/M).

## 1. ADR roll-up (0011–0017)

| ADR | Status | One-liner | Patches |
|---|---|---|---|
| [0011](../../adr/0011-reranker-domain-adaptation.md) | Proposed (dormant) | Reranker domain adaptation: vocab extension → continued MLM → full fine-tune on `bge-reranker-base`. Superseded in practice by the Qwen3-Reranker swap. | [Ch 08 §3](08-llm-and-ml.md) |
| [0012](../../adr/0012-structured-nl-summary-corpus.md) | Proposed | Structured-to-NL summary corpus: synthesise NL passages from assays/lithology/collars/samples so structured data is retrievable in chat. | [Ch 04](04-ingestion-flow.md), [Appendix G](../appendix/G-rag-retrieval-contract.md) |
| [0013](../../adr/0013-no-pgvector-postgres-extension.md) | **Accepted** | pgvector intentionally NOT installed — Qdrant is the sole vector store. | [Ch 02 §1](02-data-stores.md) |
| [0014](../../adr/0014-workspace-lookup-and-pivot.md) | Proposed | Two-phase workspace scoping for support-context workflows (bootstrap default tenant → discover real tenant → re-scope). | [Ch 11](11-tenancy-and-rls.md) |
| [0015](../../adr/0015-qwen3-vl-8b-migration.md) | Proposed (deploy-gated) | Migrate §04p Stage-6 VL from Qwen2.5-VL-7B → Qwen3-VL-8B, gated on a shadow-eval pass. | [Ch 05](05-pdf-stack.md), [Ch 08 §1](08-llm-and-ml.md) |
| [0016](../../adr/0016-paddleocr-3x-migration.md) | **Accepted** (Ph1) / Proposed (Ph2) | PaddleOCR 2.10 → 3.7 in-place engine upgrade; PaddleOCR-VL-1.6 as a Phase-2 full-page parser. | [Ch 05](05-pdf-stack.md) |
| [0017](../../adr/0017-tesseract-from-source.md) | **Accepted** | Tesseract 5.5.2 built from source in a dedicated Docker stage. | [Ch 05](05-pdf-stack.md) |

## 2. The Qwen3 model swap (2026-06-03) — config/runtime split

The single most important architectural fact in this chapter: the
embedding + reranker models were swapped to the Qwen3 line, **but the
swap reached production through `.env` overrides, not through the code
defaults.** Always read the model stack in two columns.

| Slot | Production (live, env-driven) | Code/compose default (stale) |
|---|---|---|
| Dense embedder | `Qwen/Qwen3-Embedding-0.6B` **1024-dim** ([config.py:899,904](../../../src/fastapi/app/config.py)) | `BAAI/bge-small-en-v1.5` 384-dim ([embedding_service.py:41](../../../src/fastapi/app/embedding_service.py); [docker-compose.yml:959,1298,2694](../../../docker-compose.yml)) |
| Reranker | `Qwen/Qwen3-Reranker-0.6B` (via `RERANKER_MODEL_PATH`) ([config.py:929](../../../src/fastapi/app/config.py)) | `BAAI/bge-reranker-base@2cfc18c9` ([reranker.py:77-82](../../../src/fastapi/app/services/reranker.py)) |
| VL (figures) | `Qwen/Qwen2.5-VL-7B-Instruct` (V2 default; V3=Qwen3-VL-8B gated) ([pdf_vl.py:109,117,118](../../../src/fastapi/app/services/pdf_vl.py)) | same |
| Synthesizer LLM | `Qwen/Qwen3-14B-AWQ` (**unchanged**) ([.env.example:492,612](../../../.env.example)) | same |
| Sparse | SPLADE++ (`naver/splade-cocondenser-ensembledistil`) | same |

### 2.1 The live re-index hazard 🔴

The Dagster index assets still declare **384-dim** `VectorParams`
([index_document_passages.py:67,263](../../../src/dagster/georag_dagster/assets/index_document_passages.py),
[index_reports.py:64](../../../src/dagster/georag_dagster/assets/index_reports.py),
[index_public_geoscience.py:82](../../../src/dagster/georag_dagster/assets/index_public_geoscience.py)).
Production `georag_chunks` is now 1024-dim. **Re-running any of those
Dagster assets would recreate the collection at 384-dim and silently
break retrieval.** The 2026-06-04 live re-embed deliberately used a
standalone script ([scripts/reembed_qdrant.py:47-48](../../../src/fastapi/scripts/reembed_qdrant.py))
+ a manual `curl PUT /collections/georag_chunks` for the named sparse
slot, NOT the Dagster path.

**Action owed:** update the three Dagster index assets to read
`EMBEDDING_DIMENSION` from settings (1024) before any re-materialise, OR
fence them behind a guard that refuses to recreate a 1024-dim collection
at 384. Tracked in [Appendix Z](../appendix/Z-roadmap.md).

### 2.2 Cutover learnings (from the baseline doc)

[ops/baselines/qwen3-embedding-cutover-2026-06-04.md](../../../ops/baselines/qwen3-embedding-cutover-2026-06-04.md)
records five learnings worth keeping in the architecture record:

1. **`.env` overrides `config.py`** — the swap required editing the live
   container `.env`, not `config.py`. `config.py` is the parity-check /
   identity layer, not the runtime selector.
2. **Restart vs recreate** — needs `up -d --force-recreate`, not just a
   restart, to re-read the new model env.
3. Two `Settings` fields were added during cutover:
   `EMBEDDING_QUERY_PROMPT_NAME`, `EMBEDDING_DIMENSION`.
4. `init_qdrant.py` doesn't declare the named `text` sparse slot — the
   cutover added it manually.
5. Pre-cutover production was a **`bge-small-domain-ft` local fine-tune**
   (ADR-0008 Option D), not stock bge-small. The Qwen3 swap **discards
   that domain fine-tune** — a real quality trade the team accepted for
   the larger, multilingual Qwen3 backbone.

### 2.3 Serving sidecars (2026-06-24)

Two OOM fixes landed as optional sidecar services:
- **Embedding sidecar** ([embedding_service.py](../../../src/fastapi/app/embedding_service.py))
  — when `EMBEDDING_SERVICE_URL` is set, all workers proxy to one shared
  model copy.
- **Reranker sidecar** ([reranker_service.py](../../../src/fastapi/app/reranker_service.py))
  — same pattern via `RERANKER_SERVICE_URL`. Fixes the "6 uvicorn
  workers each load a reranker copy → OOM on RAG queries"
  ([fastapi resource fixes 2026-06-24](../notes/INDEX.md)).
- Plus `/dev/shm` exhaustion fix (loky semaphore leak → `shm_size 1gb`)
  and memory bump 10g→16g.

## 3. §04p OCR/VL upgrades (ADR-0015/0016/0017)

Patches [Ch 05](05-pdf-stack.md). The §04p pipeline order is unchanged
(Docling primary → PaddleOCR secondary → Tesseract fallback); the
engines under each tier were upgraded.

### 3.1 PaddleOCR 2.10 → 3.7 (ADR-0016, Accepted Phase 1)

- Pin: `paddleocr[doc-parser]>=3.7,<4.0` ([pyproject.toml:230](../../../src/fastapi/pyproject.toml)).
- `paddlepaddle>=3.1,<3.3` — the `<3.3` cap dodges the 3.3.x CPU oneDNN
  PIR regression (Paddle#77340) that crashes scanned OCR with mkldnn
  ([pyproject.toml:215](../../../src/fastapi/pyproject.toml);
  [paddle pin note](../notes/INDEX.md)).
- Call-site API migrated to 3.x: `use_textline_orientation` (was
  `use_angle_cls`), `device=` (was `use_gpu=`), `.predict()` (was
  `.ocr()`), attribute-based results (`rec_texts`/`rec_scores`/`rec_boxes`):
  - Stage-5 regional-crop worker: [pdf_ocr.py:154,224,239](../../../src/fastapi/app/services/pdf_ocr.py).
  - Scanned-page parser: [parse_scanned.py:152,203,228](../../../src/fastapi/app/ocr/parse_scanned.py).
- **Phase 2 (Proposed):** `PaddleOCRVL` 1.6 (96.3 % OmniDocBench v1.6)
  as a parallel full-page parser ([parse_docparser_vl.py:282](../../../src/fastapi/app/ocr/parse_docparser_vl.py)),
  flag-gated via `PDF_DOCPARSER_BACKEND` (default `docling`). Additive —
  does not replace the per-bbox PP-OCRv5 worker.

### 3.2 Tesseract 5.5.2 from source (ADR-0017, Accepted)

- Built in a dedicated `tesseract-builder` Docker stage
  ([docker/fastapi.Dockerfile:29-77,270-272](../../../docker/fastapi.Dockerfile)),
  `ARG TESSERACT_VERSION=5.5.2`, installed to `/opt/tesseract`,
  `COPY --from` into the runtime stage. English-only `tessdata_fast`.
  Replaces Debian trixie's apt 5.4.x cap.

### 3.3 VL model → Qwen3-VL-8B (ADR-0015, Proposed/gated)

- Default runtime is still **Qwen2.5-VL-7B-Instruct** (`_DEFAULT_MODEL_VERSION="2"`).
  Qwen3-VL-8B activates only when an operator sets `PDF_VL_MODEL_VERSION=3`
  ([pdf_vl.py:125-139](../../../src/fastapi/app/services/pdf_vl.py)).
- **AWQ correction:** the ADR originally named `Qwen3-VL-8B-Instruct-AWQ`
  which **does not exist** (no official Qwen AWQ). `_DEFAULT_MODEL_ID_V3`
  now points at BF16 `Qwen/Qwen3-VL-8B-Instruct` (~17.5 GB) which **does
  not fit the dev A4500 (20 GB)** alongside the main LLM. For constrained
  VRAM, point `PDF_VL_MODEL_ID_V3` at a community W4A16 quant (vet first),
  served via the `vllm-vl` sidecar
  ([VL serving note](../notes/INDEX.md)).
- Deploy gate: shadow-eval machinery landed
  ([services/eval/pdf_vl_shadow.py](../../../src/fastapi/app/services/eval/)) —
  thresholds: schema-valid ≥0.95, figure-link-rate regression ≤2.0pp,
  ≥20 obs. Still pending: a servable Qwen3-VL endpoint + wiring the
  shadow observer across the golden corpus.

## 4. Structured-to-NL retrieval corpus (ADR-0012)

The problem: `silver.document_passages` only held prose from PDFs. When
a geologist asks *"what was the U₃O₈ in PLS-22-11 around 142 m?"*, the
structured-query tool returns numbers but **no chunk surfaces in
`search_documents`** because no passage mentions the sample. ADR-0012
closes this by synthesising NL summaries from the structured silver
tables into `silver.document_passages` with `chunk_kind='structured_summary'`.

New Dagster assets (synthesise + UPSERT keyed by `uuid5('{table}:{row_id}')`):
- [silver_nl_summaries.py](../../../src/dagster/georag_dagster/assets/silver_nl_summaries.py)
  — `silver_assays_v2_nl_summary`, `silver_lithology_nl_summary`,
  `silver_collars_nl_summary`.
- [silver_samples_nl_summary.py](../../../src/dagster/georag_dagster/assets/silver_samples_nl_summary.py)
  — `silver_samples_nl_summary`.

The existing ADR-0010 §A embed cron carries these into `georag_chunks`
automatically. (Minor drift to fix: the two files use inconsistent
`group_name` — `silver_nl_summaries` vs `nl_summaries`.)

## 5. Contextual retrieval (Anthropic-style context headers)

[2026_05_30_100000_add_contextualized_content_to_document_passages.php](../../../database/migrations/2026_05_30_100000_add_contextualized_content_to_document_passages.php)
adds `silver.document_passages.contextualized_content TEXT NULL`.

- Stores an LLM-generated context header prepended to raw passage text
  **before embedding** (Anthropic "contextual retrieval" technique).
- Written by the new **`enrich_passage_context` Hatchet workflow**
  ([enrich_passage_context.py](../../../src/fastapi/app/hatchet_workflows/enrich_passage_context.py))
  — daily 04:30 UTC, before `embed_pending_passages` at 05:45 UTC.
  Calls `services/ingest/context_enricher.py`.
- `passage_embedder.py` then embeds the enriched text in place of raw.
- Work-queue: partial index `WHERE contextualized_content IS NULL AND embedding_id IS NULL`.

## 6. Answer-quality scoring (LLM-as-judge)

[2026_05_30_110000_add_answer_quality_scores_to_query_audit_log.php](../../../database/migrations/2026_05_30_110000_add_answer_quality_scores_to_query_audit_log.php)
adds two columns to `audit.query_audit_log`:
- `faithfulness_score REAL` — Qwen3-as-judge: fraction of answer claims
  supported by retrieved passages.
- `context_precision_score REAL` — fraction of retrieved passages that
  were relevant.

Populated by the new **`score_answer_quality` Hatchet workflow**
([score_answer_quality.py](../../../src/fastapi/app/hatchet_workflows/score_answer_quality.py)).
NULL = not yet scored. This is RAGAS-style continuous quality
measurement on real production traffic.

## 7. Project lifecycle states (CC-03 Item 8 — LANDED)

Previously deferred (blocked on Kyle's pricing decision); **unblocked by
Kyle's 2026-05-29 call** and landed via
[2026_05_30_000001_add_lifecycle_state_to_projects.php](../../../database/migrations/2026_05_30_000001_add_lifecycle_state_to_projects.php).

`silver.projects.lifecycle_state TEXT NOT NULL DEFAULT 'active'`, CHECK
in four values:

| State | Meaning |
|---|---|
| `active` | Normal operation (default) |
| `hibernated` | Soft freeze — ingest, AI queries, user access blocked; **all data preserved** (PG/Qdrant/Neo4j/MinIO); instant reactivation, no re-ingest. Best for long-term RAG quality. |
| `archived` | Permanent freeze, same data-preservation contract; end-of-life. |
| `past_due` | Payment lapse; access suspended. **Billing wiring intentionally NOT built** (still deferred). |

**Critical RLS note** (in the migration's `COMMENT ON COLUMN`):
`lifecycle_state` is **application-layer** access control (FastAPI
middleware + Hatchet guards), NOT RLS. Do **not** add it to any RLS
USING clause — doing so would prevent owners from reactivating their own
hibernated projects. Index: `(workspace_id, lifecycle_state)`.

## 8. New tenancy / observability schema

| Table / column | Migration | Purpose |
|---|---|---|
| `silver.tenant_isolation_audit` | [2026_05_30_000000](../../../database/migrations/2026_05_30_000000_create_silver_tenant_isolation_audit.php) | Z.9 nightly tenant-isolation verifier run log. `auditor` ∈ `postgres_rls`/`neo4j_graph`/`combined`; `pg_violations` + `graph_violations`; aggregates the PG RLS auditor + new Neo4j `graph_tenant_auditor.py`. **RLS off** (admin-gated platform log). |
| `silver.archive_ingest_runs` + `ingest_progress.archive_run_id` | [2026_06_03_040000](../../../database/migrations/2026_06_03_040000_create_silver_archive_ingest_runs.php) | One parent row per ZIP-archive upload — closes the `ingest_zip_archive` silent-failure observability gap (cameco-recovery shape). Status ∈ queued/extracting/fanning_out/completed/failed/partial/cancelled. **RLS-scoped.** |

## 9. RLS sentinel fixes (third + fourth sweep)

Two more RLS-correctness migrations after the May-25 wave:
- [2026_05_29_190000](../../../database/migrations/2026_05_29_190000_replace_broken_chr0_rls_policies.php)
  — `chr(0)` sentinel (PG18 rejects U+0000) on `silver.workspaces` +
  `silver.target_rationales`. psycopg2 failed **closed**, asyncpg masked
  it. Replaced with the canonical `NULLIF(current_setting('app.workspace_id', true), '')`
  empty-string sentinel.
- [2026_05_29_200000](../../../database/migrations/2026_05_29_200000_replace_broken_guc_rls_policies_remaining_silver_tables.php)
  — 5 silver tables (`alias_gaps`, `data_quality_flags`,
  `document_versions`, `entity_aliases`, `query_traces`) had the
  canonical policy NAME but the legacy `georag.workspace_id` GUC inside —
  **fail-open**. Fixed to `app.workspace_id`. Caught by
  `WorkspaceRlsCoverageTest::test_no_policy_references_legacy_georag_gucs`.
- `silver.drill_traces` got RLS enabled + a duplicate policy dropped
  ([2026_05_30_010000](../../../database/migrations/2026_05_30_010000_enable_rls_silver_drill_traces.php) + [020000](../../../database/migrations/2026_05_30_020000_drop_legacy_drill_traces_rls_policy.php)).

See [Ch 11 §5](11-tenancy-and-rls.md) for the full coverage chain; these
extend it.

## 10. Two-phase workspace scoping (ADR-0014)

[ADR-0014](../../adr/0014-workspace-lookup-and-pivot.md) (Proposed) —
the REC#2 Phase-2 sweep collapsed 38 of 56 bespoke
`set_config('app.workspace_id', …)` sites to the canonical
`scoped_connection` / `bind_workspace_scope` helpers. The remaining 6
(5 in `services/support_cockpit/`, 1 in `hatchet_workflows/support_replay.py`)
follow a **two-phase** pattern the helpers don't support: bootstrap the
GUC to the default tenant so the ticket lookup succeeds (caller has only
`ticket_id`), discover the ticket's real workspace, then re-scope. The
ADR proposes a `lookup_and_pivot` helper for this shape. See
[Ch 11](11-tenancy-and-rls.md).

## 11. Updated workflow + asset counts (Pass 5)

| Surface | Pass 4 count | Now |
|---|---|---|
| Hatchet workflow files (excl. `worker.py` + 6 `_`-helpers) | 45 | **~50** (+ `enrich_passage_context`, `score_answer_quality`, `ingest_zip_archive`, `embed_pending_passages_smoke`, `graph_tenant_auditor`-adjacent) |
| Dagster asset files | 52 | **55** (+ `silver_nl_summaries`, `silver_samples_nl_summary`, `data_dictionary_dump`) |
| ADRs | 10 | **17** |
| Migrations | ~188 | **202** |

## 12. What this chapter patches

When reading the older chapters, apply these corrections:
- [Ch 02 §1](02-data-stores.md) — pgvector NOT installed (ADR-0013). ✅ patched.
- [Ch 05](05-pdf-stack.md) — OCR/VL engines upgraded (§3 here).
- [Ch 08 §2-3](08-llm-and-ml.md) — embedding + reranker swapped to Qwen3 (§2 here). ✅ hazard box added.
- [Appendix G §2-4](../appendix/G-rag-retrieval-contract.md) — embedding now 1024-dim Qwen3; collection state.
- [Appendix M §15](../appendix/M-agents-and-ml-catalog.md) — model registry rows.
- [Ch 14](14-status-matrix.md) — new tables + workflows + project lifecycle.
