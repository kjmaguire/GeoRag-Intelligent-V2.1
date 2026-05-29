# Phase 50 Handoff — Master-plan §3 Step 2 (silver schema migrations)

**Document version:** 1.0
**Status:** Doc-phase 50 complete. Doc-phase 51 inheriting.
**Predecessors:** `docs/phase49_handoff.md`, `docs/phase3_master_plan_kickoff.md`,
`docs/adr/0002-04p-stack-replaces-ragflow.md`.

---

## 1. What doc-phase 50 delivered

Step 2 of the master-plan §3 implementation kickoff. Schema-only
phase: the eight §9.3 + §9.6 silver tables now exist with RLS,
indexes, and FK targets. No application code lands in this phase.

| # | Table | §-ref | Key | Purpose |
|---|---|---|---|---|
| 1 | `silver.ocr_page_quality` | §9.6 | (report_id, page) | per-page OCR + layout + table confidence |
| 2 | `silver.document_ingestion_quality` | §9.6 | report_id | per-document quality summary + recommended_action |
| 3 | `silver.table_extraction_quality` | §9.6 | (report_id, page, table_id) | per-table extraction confidence |
| 4 | `silver.parser_run_artifacts` | §9.6 | run_id | per-parser-invocation audit trail (also powers Step 7 shadow comparison) |
| 5 | `silver.low_confidence_page_reviews` | §9.6 | review_item_id | Silver Review queue rows |
| 6 | `silver.ingest_extractions` | §9.3 | (report_id, page, region) | per-region text extraction output |
| 7 | `silver.ingest_layouts` | §9.3 | (report_id, page, region) | per-region Docling layout classification |
| 8 | `silver.ingest_ocr_results` | §9.3 | (report_id, page, region) | per-region OCR text + char confidences |

All eight have:
- `workspace_id UUID NOT NULL` with FK to `silver.workspaces(workspace_id)` ON DELETE CASCADE
- `report_id UUID NOT NULL` with FK to `silver.reports(report_id)` ON DELETE CASCADE
- RLS enabled + forced + `tenant_isolation` policy keyed on `current_setting('app.workspace_id')`
- Indexes on `(workspace_id)`; `(report_id, page)` indexes on the per-page tables
- CHECK constraints on enum-shaped columns (`parser_used`, `source_method`, `recommended_action`, `reason`, `status`, `layout_label`) and bounded-numeric columns (confidence in [0,1], retry_count in [0,5])

---

## 2. Files of record

### New (10 files)
- `database/migrations/2026_05_12_180000_create_silver_ocr_page_quality.php`
- `database/migrations/2026_05_12_180001_create_silver_document_ingestion_quality.php`
- `database/migrations/2026_05_12_180002_create_silver_table_extraction_quality.php`
- `database/migrations/2026_05_12_180003_create_silver_parser_run_artifacts.php`
- `database/migrations/2026_05_12_180004_create_silver_low_confidence_page_reviews.php`
- `database/migrations/2026_05_12_180005_create_silver_ingest_extractions.php`
- `database/migrations/2026_05_12_180006_create_silver_ingest_layouts.php`
- `database/migrations/2026_05_12_180007_create_silver_ingest_ocr_results.php`
- `database/migrations/2026_05_12_180008_enable_rls_phase3_silver_tables.php`
- `scripts/phase3_master_plan_step2_verify.sh`

---

## 3. Verifier status

```
[check1] PASS — all 8 silver tables exist
[check2] PASS — RLS enabled + forced on all 8 tables
[check3] PASS — tenant_isolation policy on all 8 tables
[check4] PASS — page + region composite keys present
[check5] PASS — workspace_id FK to silver.workspaces on all 8 tables
[check6] PASS — Laravel migrations table records all 9 entries (8 tables + RLS)

=== Phase 3 master-plan Step 2 verifier summary ===
  6/6 checks passed
```

---

## 4. Decisions made in this phase

### 4.1 `report_id` not `pdf_id` for the FK column

Master plan §9.3 + §9.6 use `pdf_id` in their example schemas. The
canonical FK target in this codebase is `silver.reports.report_id`
(UUID). To avoid a second naming convention for the same identifier,
all eight new tables use `report_id` matching the existing schema.
Each migration docstring documents the deviation. Single canonical
name across `silver.*` reduces query-time cognitive load.

### 4.2 `region` is an integer ordinal (0-indexed)

§9.3 doesn't specify the type of `region`. Choices considered: TEXT
(e.g. `"region_0"`, `"fig_3"`), INTEGER ordinal, UUID. Chose INTEGER
for the smallest+fastest PK and unambiguous detection-order semantics.
The parser assigns 0..N-1 in reading order on the page; a stable
ordering across re-parses requires deterministic region detection
in Step 5 (Docling's layout pass is deterministic given the same
input).

### 4.3 `bbox` stored as `NUMERIC[]` with shape-check constraint

Alternatives considered: 4 separate columns (x0, y0, x1, y1), or a
PostGIS BOX type, or JSONB. Chose `NUMERIC[4]` because: (a) PDF
bboxes are not geographic; PostGIS would be a category error. (b) 4
separate columns multiplies the schema by 4 across 3 tables. (c)
NUMERIC[] is queryable + indexable + the shape-check constraint
`array_length(bbox, 1) = 4` is enforced at the DB level.

### 4.4 `parser_used` and `source_method` are CHECK-constrained enums

Rather than `ENUM` types (PostgreSQL ENUMs are global and hard to
extend), CHECK-constrained VARCHARs. Adding a new parser in Step 4+
requires an `ALTER TABLE ... DROP CONSTRAINT ... ADD CONSTRAINT ...`
migration but that's a small price for the introspectability.

### 4.5 Migration apply path (operational, not architectural)

Discovered that `php artisan migrate` runs as `georag_app` (which has
USAGE on `silver` but not CREATE). Pre-Phase-1 silver migrations had
been run with elevated credentials; that path isn't currently wired
through Laravel's connection config.

Applied the DDL directly via `psql -U georag` (superuser) and recorded
the migration entries in Laravel's `migrations` table manually:

```bash
docker exec georag-laravel-octane php artisan migrate --pretend \
  | awk '/2026_05_12_18000/,/2026_05_12_180009/' \
  | grep -E '^\s+⇂' | sed 's/^\s*⇂\s*//' > /tmp/phase3_step2_migrations.sql
docker cp /tmp/phase3_step2_migrations.sql georag-postgresql:/tmp/
docker exec georag-postgresql psql -U georag -d georag \
  -v ON_ERROR_STOP=1 -f /tmp/phase3_step2_migrations.sql
docker exec georag-postgresql psql -U georag -d georag -c \
  "INSERT INTO migrations (migration, batch) VALUES (...);"
```

This works, but suggests two carry-overs for future doc-phase ticks:

1. **Long-term**: wire a `pgsql_admin` connection in `config/database.php`
   that uses the `georag` superuser, so `php artisan migrate` works
   uniformly across schemas. Out of scope here.
2. **Short-term**: doc-phase 51+ migrations that touch silver/workspace
   schemas should use the same `--pretend → psql → INSERT migrations`
   pattern until (1) lands.

---

## 5. Findings carried over to doc-phase 51+

### 5.1 Migration apply path (see 4.5)

The Laravel migrate command can't directly apply silver/workspace
schema changes under the current `georag_app` connection config.
Track as a separate cleanup item. Not blocking §3.

### 5.2 `silver.reports.report_id` is the canonical PDF reference

Pinned in this phase. All §3 work uses `report_id`. If master plan
v2.4.3 is ever produced, §9.3/§9.6 column references should be
updated to match this convention.

### 5.3 Verifier bug — pg_get_constraintdef and search_path

The Step 2 verifier's first attempt used a `LIKE '%REFERENCES silver.workspaces%'`
substring match against `pg_get_constraintdef()`. PostgreSQL omits
the schema qualifier from the constraint definition when `search_path`
resolves it. Fixed by joining `pg_constraint` to `pg_class` /
`pg_namespace` for the actual target schema name. Useful pattern for
future verifiers that need to assert FK targets across schemas.

---

## 6. Pre-existing carry-overs (unchanged this phase)

From doc-phase 49 handoff (unchanged):
- PaddleOCR fitz workaround locked in `parse_scanned.py` skeleton
- Docling is the slowest path, not PaddleOCR
- WSL2 exposes 6/32 CPUs to the Linux VM
- Windows ↔ WSL dual-tree sync pattern (manual cp for new top-level files)

From doc-phase 48 handoff:
- `phase4_step7` sweep-only flake
- `phase9_step1` docker network name mismatch
- Phase-4 dead-code findings still pending user-driven session

---

## 7. What doc-phase 51 will do

**Master-plan §3 Step 3 — PDF profiler + native parser path.**

First step that lands behaviour. Deliverables:
- `app.ocr.preflight` — qpdf/pikepdf encryption + magic + page count
- `app.ocr.profile` — heuristics for native/scanned/mixed/map_heavy/table_heavy
- `app.ocr.parse_native` — pdfminer.six text + pdfplumber tables
- Per-region rows written to `silver.ingest_extractions`
- Per-page rows written to `silver.ocr_page_quality` with `parser_used='native'`
- Per-document summary in `silver.document_ingestion_quality`

Profiler heuristics need rough thresholds:
- text-extraction density per page (chars/area)
- image-area fraction per page
- detected table count via pdfplumber quick scan
- layout complexity score

The PLS-2024-Technical-Report fixture passes through cleanly. The
50-PDF acceptance corpus (Step 9) will tune thresholds against real
data; Step 3 sets initial values + makes them adjustable via
constants.

Verifier: `scripts/phase3_master_plan_step3_verify.sh` — runs the
existing native fixture through the Hatchet `ingest_pdf` workflow's
parse step (modified to dispatch to `app.ocr.parse_native` when
profile == 'native'); asserts rows appear in the expected tables
with expected confidence values.

---

## 8. Master-plan §3 progress

| Step | Status | Doc-phase tick |
|---|---|---|
| 1. `app/ocr/` scaffolding + smoke-bench | ✅ DONE (3/3 green) | 49 |
| 2. §9.3 + §9.6 silver migrations | ✅ DONE (6/6 green) | 50 |
| 3. PDF profiler + native parser | next | 51 |
| 4. Scanned parser (PaddleOCR CPU image-input) | pending | 52 |
| 5. Mixed + table-heavy parsers (Docling) | pending | 53 |
| 6. LangGraph OCR Quality Graph | pending | 54 |
| 7. Hatchet `ingest_pdf` cutover + shadow comparison | pending | 55 |
| 8. Silver Review UI extension | pending | 56 |
| 9. 50-PDF acceptance corpus + sign-off | pending — needs Kyle labeling | 57 |
| 10. RAGFlow retirement + cleanup | pending | 58 |

2 of 10 steps complete.

---

End of doc-phase 50 handoff. Schema substrate is in place; behaviour
starts landing in doc-phase 51.
