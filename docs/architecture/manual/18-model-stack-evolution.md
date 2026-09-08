# Chapter 18 — Model Stack Evolution + the 2026-06 Audit Wave

> **Reconciled 2026-09-07 as a historical record.** This chapter documents
> how the model stack *evolved*, decision by decision, and most of it is
> deliberately a record of choices that were later superseded. Read it that
> way. Two things were corrected because they read as current state rather
> than history: the model table in §2 and the re-index hazard in §2.1, both
> of which described a stack that predates the 2026-07-30 Foundry cutover.
> Corrected again 2026-09-08 for the move to Amazon Bedrock
> ([ADR-0022](../../adr/0022-aws-replaces-azure-as-the-production-cloud.md)),
> for the same reason: a "**Superseded.** Production default is X" cell
> reads as current state no matter what the chapter header says.
> Everything in §3 (PaddleOCR, Docling) was already flagged as superseded.
> [Ch 08](08-llm-and-ml.md) is the current model stack; where the two
> disagree, Ch 08 wins.

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
| Dense embedder | `Qwen/Qwen3-Embedding-0.6B`, 1024-dim | **Superseded.** Production default is Cohere Embed v4 on Bedrock at 1024-dim (Foundry 2026-07-30 → 2026-09-08); the Qwen model runs only in the dev `embedding` sidecar |
| Reranker | `Qwen/Qwen3-Reranker-0.6B` on GPU — validated +13.9 % NDCG@10 over bge (0.7048 vs 0.6188) | **Superseded.** Production default is Cohere Rerank on Bedrock; Qwen3-Reranker runs in the dev `reranker` sidecar. Note the version went BACKWARDS at the cloud move — Foundry served v4, Bedrock serves 3.5 — and `RERANKER_SCORE_THRESHOLD_HOSTED` was measured against v4 |
| VL (figures) | `Qwen/Qwen2.5-VL-7B-Instruct` on vLLM | **Gone.** The vLLM service was deleted 2026-07-30. Page description runs through a Bedrock vision model, off the ingest critical path and inert unless `IMAGE_VERBALIZATION_ENABLED` — and now also unless `BEDROCK_VISION_MODEL_ID` is set, which has no default because Bedrock has no equivalent of the retired `gpt-5-mini` |
| Synthesizer LLM | `Qwen/Qwen3-14B-AWQ` | **Superseded.** Cohere Command A+ on Bedrock (`LLM_BACKEND=bedrock`; `azure` is now a startup error). The Qwen name survives as `LLM_PRIMARY_MODEL`'s default, which applies only to `LLM_BACKEND=vllm` |
| Sparse | SPLADE++ (`naver/splade-cocondenser-ensembledistil`) | **Unchanged and self-hosted** — the one component with no managed equivalent on any cloud, or on Cohere's own API. Self-hosted or the sparse leg of hybrid retrieval does not exist (ADR-0022 decision 4) |

> The VRAM-gating and rollback advice that used to sit here concerned a
> single A4500 shared with vLLM. Neither the GPU contention nor the vLLM
> service exists in production any more; both `EMBEDDING_BACKEND` and
> `RERANKER_BACKEND` default to `bedrock` (they defaulted to `foundry`
> between 2026-09-06 and 2026-09-08, and that value is now rejected at
> startup rather than ignored).

### 2.1 The re-index hazard (closed by deletion)

The hazard was that three Dagster index assets still declared 384-dim
`VectorParams` while production `georag_chunks` was 1024-dim, so re-running
one would recreate the collection at the wrong width and silently drop
every vector.

**It can no longer fire.** `src/dagster/` was deleted on 2026-08-28 and the
assets went with it. The collection is now bootstrapped by
[`scripts/init_qdrant.py`](../../../src/fastapi/scripts/init_qdrant.py) and
written only by `embed_pending_passages`. The FastAPI lifespan checks
dense-dimension parity at startup rather than trusting the writer.

The "action owed" that used to close this section — update the three
Dagster assets — is void.

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

> **Historical record:** the OCR choices in §3.1 were superseded on
> 2026-07-29, again on 2026-09-02 and again on 2026-09-08. Docling and
> PaddleOCR were removed, then Azure Document Intelligence was replaced by
> Cohere Parse (ADR-0019), which moved from Foundry to Bedrock with the
> cloud (ADR-0022); Tesseract remains the last-resort fallback. See
> [Ch 05](05-pdf-stack.md).

### 3.1 PaddleOCR 2.10 → 3.7 (ADR-0016, Accepted Phase 1)

- Pin: `paddleocr[doc-parser]>=3.7,<4.0` ([pyproject.toml:230](../../../src/fastapi/pyproject.toml)).
- `paddlepaddle>=3.1,<3.3` — the `<3.3` cap dodges the 3.3.x CPU oneDNN
  PIR regression (Paddle#77340) that crashes scanned OCR with mkldnn
  ([pyproject.toml:215](../../../src/fastapi/pyproject.toml);
  [paddle pin note](../notes/INDEX.md)).
- Call-site API migrated to 3.x: `use_textline_orientation` (was
  `use_angle_cls`), `device=` (was `use_gpu=`), `.predict()` (was
  `.ocr()`), attribute-based results (`rec_texts`/`rec_scores`/`rec_boxes`):
  - Stage-5 regional-crop worker: [pdf_ocr.py:154,224,239](../../../src/fastapi/app/services/ingest/ocr_engine.py).
  - Scanned-page parser: [parse_scanned.py:152,203,228](../../../src/fastapi/app/services/ingest/ocr_engine.py).
- **Phase 2 (Proposed):** `PaddleOCRVL` 1.6 (96.3 % OmniDocBench v1.6)
  as a parallel full-page parser ([parse_docparser_vl.py:282](../../../src/fastapi/app/services/ingest/ocr_engine.py)),
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

Shipped as two Dagster assets; **now the `nl_summaries` Hatchet workflow**
([`hatchet_workflows/nl_summaries.py`](../../../src/fastapi/app/hatchet_workflows/nl_summaries.py)),
which synthesises and UPSERTs keyed by `uuid5('{table}:{row_id}')` and
writes `chunk_kind='structured_summary'`. The `group_name` drift the
Dagster version had is moot.

`embed_pending_passages` carries these into `georag_chunks` automatically.

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

**Not populated by anything.** These were written by the
`score_answer_quality` Hatchet workflow, which was deleted in 09d1d35
(2026-07-27). Both columns are NULL on every row and will stay NULL.
The migration comment still says "NULL = not yet scored", which reads
as scoring being in progress rather than removed — treat a NULL here
as "never measured". A `WHERE faithfulness_score < x` filter returns
zero rows, which looks like "no low-faithfulness answers" and is the
opposite of the truth.

There is no RAGAS-style continuous quality measurement on production
traffic. Restoring one means restoring a judge that writes these
columns; until then they should be read as dead.

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

| Surface | Pass 4 count | Now (verified 2026-06-26) |
|---|---|---|
| Hatchet workflow files (excl. `worker.py` + `_`-helpers) | 45 | **48** (+ `enrich_passage_context`, `score_answer_quality`, `ingest_zip_archive`) |
| Dagster asset files | 52 | 55 at the time — **all deleted 2026-08-28** |
| FastAPI routers | 31 | **31** (unchanged) |
| Inertia pages (Pages/**/*.tsx) | 96 | **98** |
| Admin pages (Pages/Admin/) | (uncataloged) | **41** — now in [Ch 10 §2a](10-frontend.md) |
| ADRs | 10 | **17** |
| Migrations | ~188 | **202** |
| Compose service blocks | ~60 | **64** |

## 12. What this chapter patches

When reading the older chapters, apply these corrections:
- [Ch 02 §1](02-data-stores.md) — pgvector NOT installed (ADR-0013). ✅ patched.
- [Ch 05](05-pdf-stack.md) — OCR/VL engines upgraded (§3 here).
- [Ch 08 §2-3](08-llm-and-ml.md) — embedding + reranker swapped to Qwen3 (§2 here). ✅ hazard box added.
- [Appendix G §2-4](../appendix/G-rag-retrieval-contract.md) — embedding now 1024-dim Qwen3; collection state.
- [Appendix M §15](../appendix/M-agents-and-ml-catalog.md) — model registry rows.
- [Ch 14](14-status-matrix.md) — new tables + workflows + project lifecycle.
