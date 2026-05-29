# v1.49 `ingest_pdf` Pipeline Survey

**Document version:** 1.0
**Status:** Locked Phase 1 Step 3 deliverable. Sourced from a code survey of the WSL repo at `/home/georag/projects/georag` on 2026-05-09.
**Companion to:** `docs/phase1_implementation_kickoff.md` (kickoff Step 3 done definition).
**Author:** Phase 1 Step 3 (read-only Explore pass).

---

## Why this document exists

Phase 1 Step 4 implements the Hatchet `ingest_pdf` workflow as a shadow of the existing v1.49 path. The shadow harness (Step 5) compares both paths' outputs and classifies each as `clean`/`minor`/`divergent`/`fatal`. To do that the comparison logic needs an explicit, locked contract for what "same output" means — that's §10 of this doc, the **Diff contract**.

This doc maps:
- §1 Entry points
- §2 Pipeline stages (in execution order)
- §3 Inputs
- §4 Outputs per successful run (silver tables, audit_ledger, outbox)
- §5 Failure modes + retry behaviour
- §6 Existing instrumentation
- §7 Performance baselines (where measurable)
- §8 Open questions / ambiguities
- §9 Hatchet workflow shape (Step 4 preview)
- **§10 Diff contract — locked**

---

## 1. Entry points

### 1.1 Laravel upload API (production entry)
- **Route:** `POST /api/v1/projects/{project}/upload`
- **Controller:** `app/Http/Controllers/Api/V1/UploadController.php::store()` (lines 59–183)
- **Auth:** Sanctum + `$user->hasProjectAccess($projectId)` (line 67) — 403 if denied
- **Validation (lines 74–78):**
  - `file` required, max 100 MB
  - `category` enum string; for PDFs `category="reports"`
  - file extension must match category (`.pdf`)
  - `vendor_profile_id` optional integer FK to `vendor_profiles`
- **Filename sanitisation (lines 104–116):** strips path traversal; collapses non-alphanumerics to `_`; truncates to 120; prefixes with `{projectId}/{Ymd_His}`. Final S3 key: `reports/{projectId}/{timestamp}_{safeFilename}.pdf`
- **Storage (line 139):** `Storage::disk('s3')->put($minioKey, $handle, $putOptions)` — streams to SeaweedFS bronze bucket (the disk name `s3` predates the SeaweedFS migration; per ADR-0001 the env-var prefix stayed `MINIO_*` for back-compat).
- **Vendor profile metadata (lines 128–131):** if `vendor_profile_id` supplied, attached as S3 object metadata `x-georag-vendor-profile-id`.
- **Response (201):** `{ message, minio_key, size, category, vendor_profile_id? }`.
- **Logging (lines 146–154):** `Log::info('UploadController: file uploaded', { project_id, user_id, category, minio_key, original_filename, size, vendor_profile_id })`

### 1.2 Other entry points — confirmed absent from the v1.49 path
- No FastAPI route directly accepts PDFs in v1.49 (the `pdf_router` discovered in Phase 0 ships rendering / inspection helpers, not ingest entry).
- No Dagster sensor on bucket — the parsing is triggered by an asset chain whose entry point couldn't be precisely located in the survey (see §8 open questions).

---

## 2. Pipeline stages (in execution order)

The actual stage implementation lives in **`src/fastapi/app/services/pdf_report.py`** (NOT the per-stage `pdf_*.py` files in `services/` — those are Phase 0 helpers). The stages are sequential within a single `parse_pdf_report()` call.

| # | Stage | Where | What | Output added to ReportParseResult |
|---|---|---|---|---|
| 1 | Preflight | `pdf_report.py:1013–1052` | Existence, size ≤100 MB, magic bytes (`%PDF-`), encryption check, SHA-256 of raw bytes | `provenance.sha256`, raises `RuntimeError` on fail |
| 2 | Primary parse — unstructured | `_parse_with_unstructured()` (lines 626–652) | `unstructured.partition_pdf` for text + page count | full text, `parser_used`, `provenance.page_count` |
| 3 | Fallback parse — pdfplumber | `_parse_with_pdfplumber()` (lines 659–720) | Triggered if (2) raises. Page-by-page extract; two-column detection (`_detect_page_columns`); language detect (`_detect_page_language` via langdetect) | full text, language list (BCP-47), per-page warnings, `parser_used` |
| 4 | OCR (scanned-doc fallback) | `_attempt_ocr()` (lines 893–996) | Triggered if extracted text < 200 chars. `pdf2image` → preprocess (greyscale + 2× upscale + adaptive threshold + denoise + sharpen) → Tesseract → postprocess (U3O8/U308 fix, P.Geo. canonicalisation, NI 43-101 formatting). Confidence via `_ocr_page_confidence`. Capped to first 100 pages. Discarded if conf < 0.3 | replaces text if OCR conf ≥ 0.3, otherwise warning only |
| 5 | Metadata extract | `pdf_report.py:1136–1145` | First 2000 chars only. Six pattern-driven extractors: company, filing date (multiple formats → ISO 8601 via `_parse_date_string`), commodity (uranium/gold/copper/lithium/REE/iron/nickel/cobalt), authors (QP patterns), project name, region (Athabasca, Saskatchewan, BC, Yukon, …). | `title, authors, company, filing_date, commodity, project_name, region` |
| 6 | Section split | `_split_into_sections()` (lines 319–367) | Regex `^(\d+)\. (Title)` for sections 1–27. Returns `ReportSection[]` with `(section_number, section_title, text)` | `sections`, `parse_quality_pct = min(count, 17) / 17` |
| 7 | Resource-table extract | `_extract_resource_tables()` (lines 451–524) | Separate pdfplumber pass. Pages whose text contains a `_RESOURCE_TABLE_TRIGGERS` phrase get table-detected; header row classified via `_classify_header` + `_score_header_row`; confidence is `0.3·header_score + 0.3·consistency + 0.4·row_volume` | `resource_tables[]` with `(headers, data_rows, confidence, page_number, trigger_phrase)` |

There are no parallel stages in v1.49 — the entire parse runs sequentially in one Python process. A Hatchet replacement can decompose this into parallel steps where the data permits (e.g. metadata + table extraction can run in parallel after stage 3/4 completes).

---

## 3. Inputs

### 3.1 Workflow input shape (the contract Step 4 must match)

```yaml
# Implicit inputs — derived from the upload + S3 metadata
minio_key:        string    # required — bronze S3 key (reports/{projectId}/...)
project_id:       string    # required — derived from minio_key path
file_size:        int       # bytes
vendor_profile_id: int|null # from x-georag-vendor-profile-id S3 metadata header
```

The Hatchet workflow's `input_validator` should reflect this exactly; the Laravel side passes `{ minio_key, project_id, vendor_profile_id }` and Hatchet derives the rest from S3 GETObject.

### 3.2 Source object
- Bronze S3 bucket via the Laravel `s3` disk (SeaweedFS `minio` service, port 8333 internal)
- Key pattern: `reports/{projectId}/{Ymd_His}_{safeFilename}.pdf`
- File format: PDF, max 100 MB, magic bytes `%PDF-`

### 3.3 Workspace + actor
The current code does NOT propagate `workspace_id` cleanly into the parser — the project_id is the routing key. Phase 1 Step 4 must add `workspace_id` to the workflow input so the audit_ledger entries are workspace-scoped (Phase 0 Step 2 RLS requires workspace_id on every workspace-scoped table).

---

## 4. Outputs per successful run

### 4.1 ReportParseResult (in-memory dataclass — `pdf_report.py:180–198`)

```python
{
    "title":            str | None,
    "authors":          list[str],            # Qualified Persons
    "company":          str | None,
    "filing_date":      str | None,           # ISO 8601
    "commodity":        str | None,
    "project_name":     str | None,
    "region":           str | None,
    "sections":         list[ReportSection],  # {section_number, section_title, text}
    "parse_quality_pct": float,               # min(detected_sections, 17) / 17
    "parser_used":      str,                  # "unstructured-x.y.z" | "pdfplumber-x.y.z" | "ocr"
    "skipped_elements": list[str],
    "warnings":         list[str],
    "provenance": {
        "sha256":             str,
        "page_count":         int,
        "page_languages":     list[str],      # BCP-47
        "minio_key":          str,
        "vendor_profile_id":  int | None,
    },
    "resource_tables": list[{
        "headers":         list[str],
        "data_rows":       list[list],
        "confidence":      float,             # 0.0–1.0
        "page_number":     int,
        "trigger_phrase":  str,
    }],
}
```

### 4.2 silver tables touched

The mapping from ReportParseResult → silver tables couldn't be precisely identified in the survey (see §8.1). The likely targets, based on the schemas already deployed in Phase 0 Step 2 + the existing `silver.*pdf*` tables:

| silver table | Probable population |
|---|---|
| `silver.reports` | One row per parsed PDF — title, authors, company, filing_date, commodity, project_name, region, parser_used, parse_quality_pct, sha256, minio_key |
| `silver.document_passages` | One row per `ReportSection` — section_number, section_title, text |
| `silver.pdf_text_blocks` | Per-page text + bbox (likely populated by Phase 0 helpers, not v1.49 directly) |
| `silver.pdf_layout_regions` | Layout detection output (also Phase 0 helpers, likely Phase 3 scope) |
| `silver.pdf_ocr_results` | Only populated when stage 4 fires |
| `silver.pdf_table_cells` | Resource-table extraction (stage 7) |
| `silver.review_queue` | Manual-review entries on partial failure (see §5) |

**Step 4 must verify this mapping by running one PDF through v1.49 and querying the silver tables, then locking the schema in this doc.** Until then the diff contract treats the row count + sha256 of the source PDF as the primary equivalence check.

### 4.3 audit_ledger action types

The survey did not find explicit `emit_audit` calls inside `pdf_report.py`. Likely call sites are at the Laravel boundary (UploadController) and at the Dagster-asset boundary (after parse). Conservative expected set per successful run:

```
ingest_pdf.upload.complete         (UploadController, after Storage::put)
ingest_pdf.parse.start             (parse asset entry)
ingest_pdf.parse.complete          (parse asset success)
silver.reports.write               (parse asset → silver row insert)
```

On failure / partial:
```
ingest_pdf.parse.fallback_to_pdfplumber
ingest_pdf.parse.ocr_applied
ingest_pdf.parse.failed
silver.review_queue.write          (manual-review trigger)
```

**Phase 1 Step 4 must declare the canonical set in code.** Step 5's diff harness asserts the SET of action_types (not the order) is equal between v1.49 and Hatchet runs.

### 4.4 outbox propagations (likely)

The v1.49 path may not yet write outbox rows for embedding indexing — those probably ship in a downstream Dagster asset (vector indexing). For Step 4's purpose the Hatchet workflow should write outbox propagations for:
- `qdrant`: vector embedding upsert per passage (one row per `silver.document_passages` row)
- `neo4j`: entity link writes if the parser identifies QPs / projects / regions

**Treat outbox parity as best-effort in Step 5's diff** — focus on silver-row equivalence first.

### 4.5 External side-effects
- No Reverb broadcasts in v1.49 PDF parse path (Reverb fires on chat/answer events, not ingest).
- No Slack notifications on success.
- Manual-review queue entries → Silver Review UI (Phase 4+).

---

## 5. Failure modes + retry behaviour

| # | Trigger | Effect |
|---|---|---|
| 1 | File not found at S3 key | `RuntimeError` raised; upstream (Dagster asset) records failure; **no retry inside parser** |
| 2 | File size > 100 MB | `RuntimeError` raised; manual-review queue entry |
| 3 | Magic bytes ≠ `%PDF-` | `RuntimeError` raised; corrupted-file flag; manual review |
| 4 | Encrypted PDF (`/Encrypt` dict present) | `RuntimeError` raised; manual review |
| 5 | unstructured parser exception | Falls back to pdfplumber (logged) |
| 6 | pdfplumber also fails | `RuntimeError("Both parsers failed")` raised |
| 7 | Extracted text < 200 chars | Triggers OCR (stage 4) |
| 8 | OCR pdf2image conversion fails | Logs, continues with low-conf primary text |
| 9 | OCR Tesseract unavailable | Same |
| 10 | OCR confidence < 0.3 | Output discarded, falls back to primary text + warning |
| 11 | Empty section list | `parse_quality_pct = 0.0`, manual-review trigger |
| 12 | Metadata field extraction fails individually | Returns `null` for that field, no exception |
| 13 | Resource-table extraction returns empty | `resource_tables = []`, no error |

**Retry policy:** v1.49 parser has NO internal retry. Retries live at the Dagster asset layer (per `dagster.RetryPolicy(max_retries=3, backoff="exponential")` is the typical pattern in this repo, but the actual asset wasn't located — see §8). The Hatchet replacement should declare retries on the Hatchet step decorator: `retries=3, retry_backoff=exponential`.

**Manual-review triggers** (the boundary between "this run succeeded" and "this run needs human eyes"):
- `parse_quality_pct < 0.3` (fewer than ~5 of 17 sections detected)
- Metadata extraction yields < 2 of the 7 expected fields
- OCR applied with confidence < 0.5
- File too large / encrypted / corrupted

---

## 6. Existing instrumentation

### 6.1 Logging
- UploadController INFO logs (lines 146–154) — see §1.1.
- `pdf_report.py` does extensive INFO logging at parse start, OCR progress (every 10 pages), parser fallback transitions.
- No structured (JSON) log format is enforced; logs go to STDERR and are picked up by Loki via Promtail.

### 6.2 Metrics
- **No PDF-specific Prometheus counters/histograms exist today** in `app/metrics.py` or the FastAPI instrumentator config. Step 4 adds them in the Hatchet wrapper.

### 6.3 Tracing
- **No OpenTelemetry spans inside `pdf_report.py` today.** asyncpg + httpx auto-instrumentation captures DB / HTTP child spans wherever they happen, but the parse stages themselves are unspanned. Step 4 wraps each Hatchet step in `@workflow.step` which automatically emits a span per step.

### 6.4 Langfuse
- v1.49 PDF parse does not call vLLM (the Phase 0 `pdf_vl.py` helper does, but it's not in this v1.49 path). No Langfuse traces from PDF ingest in v1.49.

---

## 7. Performance baselines

No live timing data found in the repo (`ops/baselines/`, `ops/validation/reports/`). The numbers below are **expected ranges from code reading**, not measured — Step 4's smoke test populates real numbers.

| PDF class | Pages | File size | Expected duration (sec, full pipeline) |
|---|---|---|---|
| Small native | ≤25 | <5 MB | 2–8 s |
| Medium native | 25–100 | 5–20 MB | 10–40 s |
| Large native | 100–300 | 20–100 MB | 60–240 s |
| Scanned (OCR) | 100 page cap | any | 60–120 s additional |

OCR per page: ~0.6–1.2 s (medium-resolution scan). pdfplumber: ~0.03–0.05 s/MB. unstructured: ~0.05–0.08 s/MB.

---

## 8. Open questions / ambiguities

These items could not be resolved in the read-only survey. **Step 4 must answer each before declaring its workflow contract complete:**

1. **Where does ReportParseResult get persisted?** The survey didn't find the Dagster asset (or other writer) that takes the parser output and inserts into `silver.reports`. Searching for `INSERT INTO silver.reports` and `silver.reports` in `*.py` + `*.php` found nothing — the actual writer might use a Dagster IOManager that's been renamed or refactored. Step 4 implementer: `grep -r 'silver_reports' src/ ops/ ingest_pipeline/` and locate.
2. **What's the canonical audit_ledger action_type set?** §4.3 lists likely names but they need to be confirmed against actual `emit_audit` call sites.
3. **Outbox propagation for embeddings** — is it written by the parse asset, by a downstream embedding asset, or by a deferred queue worker? Step 4 must locate.
4. **Retry policy** at the asset layer — confirm exponential backoff, max 3 attempts.
5. **Manual-review queue schema** — `silver.review_queue` exists from earlier Phase 0 schema; what columns does the v1.49 parser fill on partial-failure?
6. **vendor_profile_id resolution** — the upload attaches it as S3 metadata; how does the parser pick it up and what does it do with it? (Probably passed to a column-mapping step that's downstream of the v1.49 parser surveyed here.)

---

## 9. Hatchet workflow shape (Step 4 preview)

For Step 4, the natural decomposition is:

```python
@hatchet.workflow(name="ingest_pdf", input_validator=IngestPdfInput)
class IngestPdfWorkflow:
    @hatchet.step(retries=2, timeout="60s")
    async def preflight(self, input, ctx) -> PreflightOutput: ...

    @hatchet.step(parents=["preflight"], retries=2, timeout="5m")
    async def primary_parse(self, input, ctx) -> ParseOutput: ...
        # unstructured first, falls back to pdfplumber on exception

    @hatchet.step(parents=["primary_parse"], retries=1, timeout="10m")
    async def ocr_if_needed(self, input, ctx) -> ParseOutput: ...
        # only runs if primary_parse output text < 200 chars

    @hatchet.step(parents=["ocr_if_needed"], retries=2, timeout="30s")
    async def metadata_extract(self, input, ctx) -> MetadataOutput: ...
        # parallel with section_split + resource_tables

    @hatchet.step(parents=["ocr_if_needed"], retries=2, timeout="30s")
    async def section_split(self, input, ctx) -> SectionsOutput: ...

    @hatchet.step(parents=["ocr_if_needed"], retries=2, timeout="2m")
    async def resource_tables(self, input, ctx) -> TablesOutput: ...

    @hatchet.step(parents=["metadata_extract", "section_split", "resource_tables"])
    async def persist(self, input, ctx) -> PersistOutput: ...
        # writes silver.reports, silver.document_passages, silver.pdf_table_cells
        # writes audit_ledger entries
        # writes outbox.pending_propagations for embeddings
        # writes silver.review_queue if manual-review triggers fired
```

Pool: `python-worker-ingestion` (per kickoff Step 2 — already provisioned).

Action prefix: `ingestion:ingest_pdf` (matches the kickoff's pool-affinity pattern).

---

## Diff contract (§10 — locked)

The Phase 1 Step 5 shadow harness compares the v1.49 path's output against the Hatchet path's output and assigns one of four classifications. This is the canonical definition.

Both paths run on the same `minio_key` + same SHA-256 input. Each path produces:
- a ReportParseResult JSON (the in-memory dataclass)
- a set of silver row IDs + counts
- a set of audit_ledger action_types
- a set of outbox propagations

### 10.1 Classification

| Classification | Meaning | Triggers (any single match) |
|---|---|---|
| **`clean`** | Outputs match within tolerance — Hatchet may receive 100% of traffic for this kind of input | All checks below pass simultaneously. |
| **`minor`** | Outputs differ in non-load-bearing ways (timing, spurious whitespace, ordering of equivalent items) | Only "fuzzy text similarity 0.95 ≤ x < 0.99" or "audit_ledger action_type set differs by ≤ 1 non-critical entry" |
| **`divergent`** | Substantive output difference that warrants ops investigation; **traffic ramp paused** | Any single divergent check below |
| **`fatal`** | One path errored where the other succeeded, OR both errored with different reasons; **traffic ramp paused, post-mortem required** | Either path raised an unhandled exception that the other did not, or both raised but with different `RuntimeError` messages |

### 10.2 Per-field equivalence checks

| Field | Equivalence check | If different → |
|---|---|---|
| `provenance.sha256` | Exact match (must be identical — same input file) | **fatal** |
| `provenance.page_count` | Exact match | **divergent** |
| `provenance.minio_key` | Exact match | **fatal** (different inputs) |
| `provenance.page_languages` | Set equality | **minor** |
| `parser_used` | Either side may use a different fallback path (unstructured vs pdfplumber) — **don't compare directly**; just record both | (informational; not a classification trigger) |
| `parse_quality_pct` | `abs(a - b) ≤ 0.10` | **divergent** if greater |
| Sections (count) | Exact match | **divergent** |
| Sections (text per section) | Per-section cosine similarity ≥ 0.95 (using SBERT `BAAI/bge-small-en-v1.5` embeddings — already in the FastAPI image) | `< 0.99` ⇒ **minor**; `< 0.95` ⇒ **divergent** |
| `title` / `company` / `project_name` | Exact match (after Unicode NFC + casefold) | **divergent** |
| `filing_date` | Exact match (both ISO 8601) | **divergent** |
| `commodity` | Exact match | **divergent** |
| `region` | Exact match | **divergent** |
| `authors` (list of QPs) | Set equality (after Unicode NFC + casefold) | **minor** if order differs but set equal; **divergent** if set differs |
| `resource_tables` (count) | Exact match | **divergent** |
| `resource_tables[i].headers` | Exact set equality | **divergent** |
| `resource_tables[i].data_rows` count | Exact match | **divergent** |
| `resource_tables[i].confidence` | `abs(a - b) ≤ 0.10` | **minor** if greater |
| silver row counts per table | Exact match per table | **divergent** if any count differs |
| audit_ledger action_type set | Set equality | **minor** if differs by ≤ 1 non-critical (warning-level); **divergent** otherwise |
| outbox propagation count | Exact match (set: `target_store + source_id`) | **minor** for Phase 1; **divergent** in Phase 2 |
| Wall-clock duration | Both within 2× of each other | **minor** if Hatchet is >2× slower |

### 10.3 Critical action_type set
These audit_ledger action_types MUST appear in both paths' output for `clean` classification — missing any single one in either path is **divergent**:

- `ingest_pdf.parse.complete` (or its v1.49 equivalent — Step 4 confirms the canonical name)
- `silver.reports.write`

Non-critical (warning/info) action_types — ok to differ by ≤ 1:
- `ingest_pdf.parse.fallback_to_pdfplumber`
- `ingest_pdf.parse.ocr_applied`
- `silver.review_queue.write`

### 10.4 Storage of diff results

Each shadow run writes one row to `silver.shadow_runs` (Phase 1 Step 5 schema):
```sql
silver.shadow_runs (
    id uuid PK,
    workspace_id uuid,
    minio_key text,
    classification text CHECK (classification IN ('clean','minor','divergent','fatal','partial')),
    v149_result jsonb,
    hatchet_result jsonb,
    diff_details jsonb,           -- per-field check outcomes
    v149_duration_ms int,
    hatchet_duration_ms int,
    v149_audit_run_id uuid,
    hatchet_audit_run_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
)
```

`partial` is reserved for runs where one or both paths haven't yet completed (used by the diff worker to mark in-flight comparisons).

### 10.5 Daily zero-divergence gate

The kickoff §Step 8 cutover requires "7 consecutive days at 100% traffic with zero `clean` divergence". Operationally:

- A day's shadow runs all classified `clean` ⇒ that day counts toward the streak.
- Any `minor` / `divergent` / `fatal` day breaks the streak; ops investigates and the streak resets after fix.
- The traffic-ramp control reads the streak length and refuses to advance past 1% / 10% / 50% / 100% boundaries unless the prior day was `clean`.

---

## Appendix A — Files referenced

- `app/Http/Controllers/Api/V1/UploadController.php` (Laravel upload entry)
- `src/fastapi/app/services/pdf_report.py` (the v1.49 parse pipeline)
- `database/migrations/*pdf*` + `database/migrations/*passages*` (silver tables touched)
- `docs/phase1_implementation_kickoff.md` (Step 3 done def)
- Phase 0 schema files for the silver/audit/outbox tables that the Hatchet path will share

End of v1.49 ingest_pdf survey.
