# GeoRAG Architecture Review — PDF Ingestion + Agents + Workflows

**Date:** 2026-05-22
**Purpose:** Independent architectural review. Please read end-to-end then critique with focus on:

1. **Correctness** — are there silent data-drop paths I missed? Race conditions in the new task topology? Auth/security holes in the Kestra↔FastAPI bridge?
2. **Soundness** — is the parser dispatch tree (docling → fitz → pdfplumber → whole-doc-OCR) genuinely fail-safe, or are there inputs where ALL of them silently return nothing useful?
3. **Operational concerns** — what should we monitor in production? What's the right alarm threshold? What's missing from the runbook?
4. **Architectural smells** — anything that's a workaround / hack that should be cleaned up before it bites?
5. **Performance ceilings** — given the current design, where's the next 2–5× speedup likely to come from?
6. **Anything I'm overconfident about** — call it out if a claim below sounds optimistic or unsubstantiated.

Don't restate the structure back to me. Skip "great architecture, here's what I'd improve" framing. Just go after the weak points.

---

## 1. PDF Ingestion Pipeline

### 1.1 End-to-end flow

```
Browser upload
   ↓ multipart POST /api/v1/projects/{id}/upload  (Laravel, 2 GB cap)
Laravel UploadController → MinIO bronze (figures/, reports/)
   ↓ HTTP POST /internal/v1/shadow/ingest_pdf/trigger (Laravel→FastAPI, signed JWT)
FastAPI → Hatchet engine: dispatch `ingest_pdf` workflow
   ↓ Hatchet routes to "ingestion" pool worker
ingest_pdf workflow (5 tasks):
   preflight → parse → persist → { embed_verify, p04p_dual_write } (parallel)
                                         ↓
                                  embed_pending_passages_wf  (AI pool worker, GPU)
                                         ↓
                                  qdrant_reports (vectors searchable)
```

**File anchors:**
- Upload: `app/Http/Controllers/Api/V1/UploadController.php`
- FastAPI dispatch endpoint: `src/fastapi/app/routers/shadow_trigger.py:47`
- Main workflow: `src/fastapi/app/hatchet_workflows/ingest_pdf.py`
- Parser: `src/dagster/georag_dagster/parsers/pdf_report.py`

### 1.2 The 5 Hatchet tasks inside `ingest_pdf`

| Task | execution_timeout | retries | What it does |
|---|---|---|---|
| `preflight` | 60 s | 2 | sha256 + magic-byte + pikepdf open check + page count. Encrypted check is permissive (only rejects password-protected). |
| `parse` | 60 min | 1 | Runs `parse_pdf_report` in a **subprocess pool** (own GIL → heartbeat safety). Returns ParseOut dict. |
| `persist` | 15 min | 2 | Atomic single transaction: `silver.reports` + `silver.document_passages` + figures (when docling on). Inline embed dispatch. |
| `embed_verify` | 2 min | 1 | Polls `silver.document_passages` for unembedded count for 90 s; re-dispatches `embed_pending_passages` if anything still pending. Closes the BattleNorth-style race where Hatchet retries lose the inline embed dispatch. |
| `p04p_dual_write` | 45 min | 1 | Populates 8 layout-aware silver tables via `app.ocr._ingest_helper.run_p04p_for_ingest`. Reads PDF body from local cache (no S3 re-download). |

### 1.3 Parser dispatch tree (inside `parse_pdf_report`)

```
parse_pdf_report(path)
├─ PDF_PARSER_DOCLING_ENABLED=true?
│    ├─ docling (GPU via onnxruntime-gpu + AcceleratorDevice.CUDA)
│    │    → returns text + tables-with-structure + figure regions
│    ├─ if <200 chars → fall through
│    └─ if exception → fall through
├─ PDF_PARSER_FITZ_ENABLED=true (default)?
│    ├─ fitz (PyMuPDF, CPU) — 5-10× faster than pdfplumber for text
│    ├─ per-page OCR on any page <80 chars (Tesseract, CPU)
│    ├─ if <200 chars total → fall through
│    └─ if exception → fall through
├─ pdfplumber (parallel multiprocessing.Pool, 4 workers default)
│    ├─ per-page OCR on short pages
│    ├─ language detection per page
│    └─ column-aware text extraction
└─ Whole-doc OCR (final safety net, all pages, no cap)
```

After text extraction, regardless of parser:
- `_extract_all_tables_as_sections(path)` runs — **dual-pass (lines + text strategies)**, **scans every page**, dedupes via SHA1 of cells
- `_split_into_sections(full_text, per_page_text)` — splits at NI 43-101 numbered headings; sliding-window if none found; sub-chunks large sections at 1500 chars + 200 overlap
- Page tracking flows through `page_first`/`page_last` columns

---

## 2. CPU vs GPU breakdown

### 2.1 Where things run

| Component | Hardware | Why |
|---|---|---|
| PyMuPDF (fitz) text extraction | CPU | Pure C library, no neural net |
| pdfplumber text extraction | CPU (4-way parallel) | Pure Python |
| Per-page tesseract OCR | CPU | Tesseract is CPU-bound (no CUDA build in repo) |
| pdfplumber table extraction | CPU | Pure Python heuristics |
| docling layout model + TableFormer | **GPU (CUDA via onnxruntime-gpu)** | 5× speedup measured |
| paddleocr (§04p) | CPU | Could be GPU but not configured yet |
| bge-small-en-v1.5 (dense embed) | **GPU** | ~144 chunks/sec; was 3-4 chunks/sec on CPU |
| SPLADE++ (sparse encode) | **GPU** | shared GPU with bge-small |
| bge-reranker-base (chat rerank) | **GPU** | ~5× faster than CPU |
| vLLM (Qwen3-14B-AWQ chat LLM) | **GPU** | 16 GB VRAM at 0.80 util |
| persist (Postgres writes) | CPU | DB-bound |
| qdrant index | CPU/RAM (on-disk) | HNSW search, not neural |

### 2.2 GPU allocation on the single A4500 (20 GB VRAM)

| Container | VRAM | Status |
|---|---|---|
| vLLM (Qwen3-14B-AWQ + KV cache @ 0.80) | ~15.8 GB | Owned |
| hatchet-worker-ai (bge-small + reranker + SPLADE) | ~600 MB | Co-resident |
| hatchet-worker-ingestion (docling models, when on) | ~150 MB | Co-resident |
| Free | ~3.4 GB | Headroom |

### 2.3 Measured wall-clock per PDF (after all optimizations)

| PDF type | Text extraction | Tables | OCR (if needed) | Embed | Total |
|---|---|---|---|---|---|
| 5 MB text-only fact sheet (5-20 pages) | 1-3 s fitz | 5-15 s | 0 s | <1 s | **5-25 s** |
| 10 MB NI 43-101 (200 pages, mixed) | 15-30 s fitz | 60-90 s | 5-30 s | 1-2 s | **80-150 s** |
| 22 MB Madsen PFS (395 pages, 160 tables) | ~37 s fitz | ~90 s | ~50 s | ~3 s | **~170 s (≈2.8 min)** measured |
| 18 MB BattleNorth (575 pages, 84 tables) | ~37 s fitz | ~120 s | ~60 s | ~5 s | **~200 s (≈3.5 min)** projected |
| 50 MB fully-scanned PDF (200 pages) | <1 s fitz | 0 s | ~200 s tesseract | ~2 s | **~3-4 min** |
| Same 50 MB scanned via docling+GPU | ~80 s | inline (with structure) | n/a (docling's path) | ~2 s | **~90 s** |

For a **V3-style 12-PDF batch (146 MB)** with default settings (fitz primary, docling off):
- Theoretical sequential: ~15-20 min
- Hatchet's slots=20 with 4-page parallelism: **~7-10 min wall clock**
- Embed runs in parallel on the AI worker GPU, never blocks parse

---

## 3. 100% data capture — proof tree

Critical correctness requirement: NEVER silently drop data. Audit below.

| Drop path that could lose data | Status |
|---|---|
| Doc-level "OCR-skip" probe (first-10-pages-dense → skip OCR doc-wide) | **REMOVED 2026-05-22** |
| fitz path had no OCR (image pages dropped) | **FIXED** — per-page tesseract on every short page |
| Table-skip probe (probe N pages, skip if no hits) | **REMOVED** — every page is scanned |
| Single-pass table extraction (lines OR text) | **REVERTED to dual-pass** + dedupe — every page gets BOTH strategies, dedupe by cell-signature |
| `MAX_OCR_PAGES = 100` page cap | **REMOVED** — every page OCR'd no matter the count |
| Lost-embed race (BattleNorth) | **FIXED** — `embed_verify` task + every-10-min cron + 24h cron |
| Heartbeat timeout under heavy parse | **FIXED** — parse in subprocess (own GIL) |
| Hatchet retry storms | **FIXED** — atomic persist + parse_timeout = 60 min |
| Encrypted-but-readable PDF rejected (permission flag) | **FIXED** — only password-protected rejected |

**Final safety net:** if every parser returns <200 chars total → whole-doc OCR fires across every page, no cap.

**Trade-offs explicitly kept:**
- 2 GB max upload at preflight (avoids OOM on rogue files; tunable via env)
- Password-protected PDFs rejected (we genuinely can't decrypt without the password)
- Table heuristic filters non-data tables (cover pages, TOC) from the per-table chunk index — but every cell's text still appears in the page-level flowing text, just not as a `Table` section

---

## 4. AI Agents

### 4.1 Agentic Retrieval LangGraph (chat path)

`src/fastapi/app/agent/agentic_retrieval/graph.py` — 7-node async LangGraph compiled once and cached via `lru_cache(maxsize=1)`:

```
START → classify → route → execute → assemble → validate → demote → persist → END
```

Each node is async; the graph picks a tool plan based on intent classification, then runs grounded retrieval, structured response assembly, and citation validation.

### 4.2 Six-intent classifier

`src/fastapi/app/agent/agentic_retrieval/intent_classifier.py`. Keyword-scoring first (cheap), LLM fallback when confidence < 0.6 (Qwen, opt-in). Intents and retrieval profile:

| Intent | Example query | Tools fired |
|---|---|---|
| `factual_lookup` | "Au grade at hole MAD-22-001" | `assay_data`, `search_documents` |
| `synthesis` | "What does this report say about resources?" | `search_documents` heavy, `traverse_knowledge_graph` |
| `hypothesis_generation` | "What else could explain this anomaly?" | `analogue_deposits`, `spatial_correlation` |
| `anomaly_detection` | "Find unusual geochemistry trends" | spatial + statistical tools |
| `uncertainty_quantification` | "How confident are we about Section 14's resource estimate?" | confidence_computer + provenance walk |
| `decision_support` | "Where should we drill next?" | targeting agents (phase8) |

### 4.3 Tools (Pydantic AI)

`src/fastapi/app/agent/tools.py` — `@geo_agent.tool` decorators. Strict timeout discipline so a slow store can't break chat:

| Tool | Backing store | Timeout (env) |
|---|---|---|
| `search_documents` | Qdrant `georag_reports` (workspace + optional project filter) | `TIMEOUT_QDRANT_S=10` |
| `traverse_knowledge_graph` | Neo4j (Cypher with identifier allowlist) | `TIMEOUT_NEO4J_S=3` |
| `query_spatial` | PostGIS | `TIMEOUT_POSTGIS_S=5` |
| `assay_data` | Postgres `silver.assays_v2` | normal pg pool |
| `project_overview` | Postgres + Neo4j summary | normal |

After Qdrant returns, the **bge-reranker-base** cross-encoder runs on GPU to rerank top-N candidates before they reach the LLM.

### 4.4 Hallucination prevention — 6 layers

`src/fastapi/app/agent/hallucination/`. Mandated by `CLAUDE.md` rule #5.

| Layer | Gate |
|---|---|
| 1 `layer1_retrieval.py` | Quality threshold on retrieved chunks; refuse early if <RETRIEVAL_QUALITY_THRESHOLD |
| 2 `layer2_typed_output.py` | Pydantic AI structured outputs (no free-form JSON) |
| 3 `layer3_numerical.py` | Every numerical claim must come from a tool result; LLM-invented numbers fail validation |
| 4 `layer4_entity.py` | Resolve entities to Neo4j nodes; unknown entities flagged |
| 5 `layer5_provenance.py` | Mandatory `source_chunk_id` per claim; no chunk → refusal |
| 6 `layer6_constraints.py` | Geological sanity rules (depth>0, grade ranges, etc.) |

### 4.5 Phase agents (specialized graph runs)

`src/fastapi/app/agents/phaseN/` — separate from chat retrieval; called from Hatchet workflows or admin tools.

| Phase | Job |
|---|---|
| phase0 | Ops diagnostics: vLLM security check, model cost summary, LLM incident triage, index health, storage tier audit, multi-tenant isolation audit, lineage walk, support packet assembly |
| phase5 | Visual QA (drillhole viz readiness) |
| phase6 | Public/private data boundary enforcement |
| phase7 | Report compilation (Builder Graph — `generate_report` workflow) |
| phase8 | Drill target recommendations + scoring + R5 sign-off pause-resume |
| phase9 | Hypothesis generation (alternatives, analogues, gap ID) |
| phase10 | Customer-support response drafting + ticket triage |

---

## 5. Hatchet Workflows

Two worker pools: **ingestion** (PDF/data) and **ai** (LLM/embed/ML). Worker registry at `src/fastapi/app/hatchet_workflows/worker.py:80-194`.

### 5.1 Ingestion pool

| Workflow | Trigger | Purpose |
|---|---|---|
| `ingest_pdf` | Dispatched by upload | The 5-task pipeline above |
| `outbox_dispatcher` | Cron every minute | Drains pending workflow.outbox messages |
| `re_ocr_page` | Manual | Re-OCR a single page with different settings |
| `tiff_ocr_cluster` | Cron | OCR a cluster of TIFF files staged from external sources |
| `storage_tiering_run` | Cron | Move hot data to warm/cold storage |
| `index_health_check` | Cron | Verify qdrant index integrity |
| `store_reconciliation_run` | Cron | Cross-store consistency check |

### 5.2 AI pool (~35 workflows)

High-impact ones for ingestion + chat:

| Workflow | Trigger | Purpose |
|---|---|---|
| `embed_pending_passages` | Cron `*/10 * * * *` + daily 05:45 + inline | Embed unembedded passages to qdrant. Idempotent. |
| `sync_silver_to_kg` | Manual / after cluster ingest | Push silver → Neo4j |
| `mv_refresh_silver` | Nightly | REFRESH MATERIALIZED VIEW for retrieval shortcuts |
| `eval_real_rag_nightly` | 05:15 UTC | Golden-question eval suite |
| `evaluate_workspace` | Manual | Full eval gating before promote |
| `what_changed_weekly` | Mondays 06:00 UTC | Workspace delta digest |
| `field_outcome_learning` | After drilling outcomes | ETL hits/misses → target backtests |
| `train_target_model` / `train_source_trust` | Manual / scheduled | XGBoost retraining |
| `backup_*` (postgres/neo4j/qdrant/redis/seaweedfs) | Cron staggered | Per-store backups |
| `cold_tier_archive` | Cron | Long-term archive |
| `support_packet_assemble` | Triggered by Kestra | Phase 0 support packet builder |
| `bc_minfile_pull`, `nrcan_geo_pull` | Cron | Public geoscience scrapers |

---

## 6. Kestra Flows

Kestra is the **integration edge** (replaces Activepieces). Three flows in `kestra/flows/georag/`.

### 6.1 `external_notification.yaml`

```
External sender → webhook → Kestra → POST /internal/v1/integrations/external_notification/trigger
                                       (Kestra-side JWT, sender-side HMAC)
                                              ↓
                                   Hatchet `external_notification` workflow (AI pool)
                                              ↓
                                   Verify HMAC → notify recipients
```

### 6.2 `public_geoscience_pull.yaml`

```
Cron → Kestra → fetch ArcGIS / WFS feature service
                       ↓
                drop response in MinIO bronze (versioned key)
                       ↓
                POST /internal/v1/integrations/public_geoscience_pull/trigger
                       ↓
                Hatchet `public_geoscience_pull` workflow (AI pool)
                       ↓
                Parse + write to silver/gold layers
```

### 6.3 `support_packet_dispatch.yaml`

```
Hatchet workflow fails (or operator triggers) → POST to Kestra webhook
                                                       ↓
                                    Kestra → POST /internal/v1/agents/support_packet/assemble
                                                       ↓
                                    FastAPI Phase 0 support_packet agent assembles bundle
                                                       ↓
                                    Upload to SeaweedFS warm tier
                                                       ↓
                                    Write silver.support_packets
                                                       ↓
                                    Kestra emails the operator the bundle link (Sendmail task)
```

### 6.4 Kestra ↔ Hatchet pattern

Kestra never directly fires a Hatchet workflow. Always goes through FastAPI's `/internal/v1/integrations/*/trigger` endpoints which:
1. Validate the per-flow JWT (rotated by `scripts/phase3_jwt_rotate.sh`)
2. Verify HMAC where applicable (`external_notification`)
3. Then call `wf.aio_run_no_wait(typed_input)` to dispatch Hatchet

This indirection means Hatchet's auth model is one thing (gRPC tokens, internal) while Kestra's is another (HTTP webhooks, external). FastAPI bridges them.

---

## 7. Recurring lifecycle (typical user upload)

```
T+0s    User uploads 10 MB NI 43-101 PDF
T+1s    Laravel writes to MinIO bronze; POSTs trigger to FastAPI
T+2s    FastAPI dispatches Hatchet `ingest_pdf` workflow
T+2s    ingest_pdf:preflight  → ~1s  (sha256, page count via pikepdf)
T+3s    ingest_pdf:parse      → runs in subprocess
        ├─ fitz extracts text (~15s for 200 pages)
        ├─ short pages get per-page OCR (~5s if any)
        ├─ table dual-pass on every page (~60s)
        └─ section split + sliding-window chunking (~2s)
T+85s   ingest_pdf:persist    → atomic TX into silver.reports + document_passages (~5s)
T+90s   In parallel:
        ├─ embed_verify         → polls until embeds land (~30-90s)
        └─ p04p_dual_write      → §04p tables populated (~30-60s, GPU-accelerated)
T+90s   Inline embed dispatch  → AI worker picks up
T+91s   bge-small + SPLADE on GPU → ~1.5s for ~200 chunks
T+93s   qdrant upsert         → done; vectors searchable
T+93s   USER CAN NOW CHAT against this PDF with page-level citations
```

A **freshly uploaded PDF is in qdrant and chatable in ~90 seconds** for typical NI 43-101 (CPU-bound parse is the floor). Bigger docs (Madsen 22 MB, BattleNorth 18 MB) take 3-5 min. Slide-deck-style PDFs land in 20-30 s.

---

## 8. Known roadmap items / honest gaps

Things NOT done that I want flagged:

- **PyMuPDF doesn't extract tables natively** — the all-page pdfplumber table scan still runs separately. Could be removed if docling becomes the default.
- **`MIN_EXTRACTABLE_TEXT_CHARS=200` total-doc threshold** before fitz falls back. Per-page OCR catches gaps inside fitz, but truly empty docs do fall through to pdfplumber+OCR-everything (safety net works, just adds time).
- **Figure handoff across subprocess boundary** — when docling is on, the figure cache is in the parse subprocess; persist's `_extract_docling_figures` returns empty. Only matters with `PDF_PARSER_DOCLING_ENABLED=true`. Fix is straightforward.
- **paddleocr (§04p)** is still CPU. Could be GPU'd similarly to docling.
- **`unstructured` library** is still imported but dead in the dispatch tree. Could be removed for cleanliness.
- **Hatchet concurrency limit on `embed_pending_passages`** not set; relies on the AI worker's natural 20-slot scheduling. With GPU it rarely matters.
- **No GPU passthrough on the AI worker for docling** — only the ingestion worker got GPU access for docling. AI worker has GPU for bge models. So docling runs in ingestion worker only.
- **Hot-installed libs vs image rebuild** — polars, unstructured[pdf], pytesseract, onnxruntime-gpu are baked into the running image via `docker commit` but not in `pyproject.toml` / Dockerfile. Next clean image rebuild will lose them unless we update pyproject + Dockerfile first.
- **Test coverage on the new code** — none of the new perf/correctness paths have unit tests yet. Tested manually against V3's 12-PDF corpus only.
- **§04p tables not consumed by chat retrieval** — they're populated correctly but chat retrieval (search_documents) still goes to qdrant `georag_reports`. The richer layout data sits unused in `silver.ingest_*` tables.

---

## 9. Specific things I want pushed back on

1. **Embed_verify polling** — I poll every 15s for up to 90s. Is that the right cadence? Under heavy load (12 simultaneous uploads), is 90 s long enough that the inline dispatch could still win? Or should I just always re-dispatch and let the workflow's idempotency be the only safety?

2. **Subprocess pool with max_workers=1** — works for the heartbeat issue but means only ONE parse can use the subprocess at a time on any given worker. Should I use a pool of N workers matching Hatchet's slot count? Or is the single-subprocess design a feature (memory caps)?

3. **Dual-pass table extraction at 60-90s per PDF** — really expensive. Is there a smarter way? Maybe use docling's GPU table model for the lined tables and fall back to pdfplumber text-strategy for the borderless ones?

4. **The figure subprocess handoff** — I have `_DOCLING_FIGURE_CACHE` keyed by sha256 but it doesn't cross the subprocess boundary. Best fix: do figure extraction inside the parse subprocess and return manifest. Or: write figures to /tmp/figures/{sha256}/ in parse subprocess and have persist enumerate. Which is cleaner?

5. **Kestra→FastAPI per-flow JWT** vs **Laravel→FastAPI bearer JWT** — two different auth schemes. Worth unifying? Or is the per-flow rotation a real security win that justifies the complexity?

6. **The "phase agents" naming** — phases 0, 5, 6, 7, 8, 9, 10. No 1-4. What happened to phases 1-4? Are they retired or never built? Should I rename to functional names (`ops`, `viz`, `boundary`, `report`, `targeting`, `hypothesis`, `support`)?

7. **The §04p stack** — what's the long-term plan? It's running but its output isn't consumed by chat retrieval. Is this a stale dual-write or a planned future cutover?

---

**End of review document. Critique away.**
