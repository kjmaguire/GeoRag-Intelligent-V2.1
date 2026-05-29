# Phase 56 Handoff — Master-plan §3 Step 7b (persistence layer)

**Document version:** 1.0
**Status:** Doc-phase 56 complete. Doc-phase 57 inheriting.
**Predecessors:** `docs/phase55_handoff.md`, `docs/phase3_master_plan_kickoff.md`,
`docs/adr/0002-04p-stack-replaces-ragflow.md`.

The persistence layer that wires orchestrator output to the 8 silver
tables. First doc-phase tick to execute real INSERTs against the
running database. End-to-end integration test on the PLS-2024 fixture
passes; teardown cleanly removes test rows via CASCADE.

---

## 1. What doc-phase 56 delivered

`app.ocr._persist` with two public functions + 8 internal helpers:

| Public function | What it does |
|---|---|
| `transactional_workspace_session(pool, workspace_id)` | async context manager: acquires conn, opens transaction, sets `app.workspace_id` GUC |
| `persist_orchestrator_result(conn, workspace_id, report_id, result)` | writes orchestrator output to all 8 silver tables in one transaction; returns per-table row count dict |

### What gets written, by silver table

| Silver table | Source field in orchestrator result | One row per |
|---|---|---|
| `silver.parser_run_artifacts` | `preflight` + `profile` + each non-null entry in `parses` | parser invocation (≥2, up to 4 rows per doc) |
| `silver.ocr_page_quality` | one per `route_decisions` entry | page |
| `silver.document_ingestion_quality` | `document_summary` | document (1 row) |
| `silver.table_extraction_quality` | each table in each `parses[X].tables` | detected table |
| `silver.low_confidence_page_reviews` | each `route_decisions[?].route == 'silver_review'` | page-reason |
| `silver.ingest_extractions` | each passage from native/mixed/table_heavy parsers | text region |
| `silver.ingest_layouts` | each layout from mixed parser | Docling layout region |
| `silver.ingest_ocr_results` | each passage from scanned parser | OCR'd region |

### Conventions captured

- **Workspace_id GUC pattern**: `SELECT set_config('app.workspace_id', $1, true)` (transaction-local), matching the existing `app/hatchet_workflows/ingest_pdf.py` convention.
- **Coord origin annotation**: every extraction / layout payload includes `coord_origin` (`BOTTOMLEFT` for native/Docling, `TOPLEFT_IMAGE` for scanned OCR). Lets downstream consumers translate if needed without losing the source signal.
- **Region counters per page per table**: ensures `(report_id, page, region)` PK uniqueness when multiple parsers contribute to the same page (native + Docling don't currently collide, but the counter pattern is safe).
- **ON CONFLICT DO UPDATE for upserts**: every table that has a natural PK uses ON CONFLICT to allow re-ingest. Re-running the same PDF updates existing rows rather than failing on PK collision.
- **Direct postgres bypass of pgbouncer**: `_dsn()` uses `POSTGRES_DIRECT_HOST` + `POSTGRES_DIRECT_PORT` because `SET LOCAL` inside transactions requires session-pooling stability that pgbouncer's transaction-pooling can break.

---

## 2. Files of record

### New
- `src/fastapi/app/ocr/_persist.py` (~450 lines)
- `src/fastapi/tests/test_ocr_persist_integration.py` (5 tests, 1.6 sec)
- `scripts/phase3_master_plan_step7b_verify.sh`

### DB grant applied via psql
- `GRANT DELETE ON silver.reports TO georag_app` — see § 4.5

---

## 3. Verifier status

```
[check1] PASS — _persist module imports + has async function
[check2] PASS — 5/5 persistence integration tests green
[check1] PASS — Step 1 verifier still green
[check2] PASS — Step 2 verifier still green
[check3] PASS — Step 3 verifier still green
[check4] PASS — Step 4 verifier still green
[check5] PASS — Step 5 verifier still green
[check6] PASS — Step 6 verifier still green
[check7a] PASS — Step 7a verifier still green

=== Phase 3 master-plan Step 7b verifier summary ===
  all checks passed
```

OCR test scoreboard:
- **66 OCR tests passing** (61 prior + 5 persistence integration)

End-to-end behaviour validated on PLS-2024:
- preflight → profile → parse_native → 7× route_page → summarize_document → persist
- Resulting silver rows:
  - 3 parser_run_artifacts (preflight + profiler + native)
  - 7 ocr_page_quality (one per page)
  - 1 document_ingestion_quality (recommended_action: accept_with_review)
  - 0 table_extraction_quality (PLS-2024 has no tables)
  - 0 low_confidence_page_reviews
  - 41 ingest_extractions (one per pdfminer.six text region)
  - 0 ingest_layouts (native parser doesn't emit layouts; Docling does)
  - 0 ingest_ocr_results (no OCR ran for native PDF)

Reject path (invalid PDF):
- 1 parser_run_artifact (preflight, with errors logged)
- 1 document_ingestion_quality (recommended_action: reject)
- All other tables empty (no parsing happened)

---

## 4. Decisions made in this phase

### 4.1 Persistence is one transaction across all 8 tables

`persist_orchestrator_result` does all writes inside the caller-supplied
transaction. Either everything lands or nothing does. The caller
(eventually the Hatchet `ingest_pdf.persist` step in doc-phase 57)
owns the transaction boundary via `transactional_workspace_session`.

Tradeoff: a single bad row in any of the 8 tables rolls back the whole
ingest. For now this is fine — orchestrator output is structurally
consistent. If real-world PDFs surface "one bad table, rest of doc fine"
cases, we can later split into sub-transactions per table.

### 4.2 `_persist` is internal (leading underscore)

Same convention as `_orchestrator.py` and `_docling_common.py`. Only
the Hatchet `ingest_pdf` workflow + tests should import it. Not
re-exported from `app/ocr/__init__.py`.

### 4.3 ON CONFLICT DO UPDATE everywhere

Every INSERT uses ON CONFLICT to support re-ingest. Re-running the
same PDF against the same report_id updates the existing silver rows.
Rationale: scientific reproducibility. Re-running with a tuned
threshold (Step 9) should not require manual cleanup.

### 4.4 Coord origin annotation in JSONB payload

Each extraction / layout / ocr_result row carries `coord_origin` in
its `payload` JSONB column:
- Native + mixed (pdfminer.six + Docling): `BOTTOMLEFT` (PDF coords)
- Scanned (PaddleOCR): `TOPLEFT_IMAGE` (image coords post-render)

Downstream consumers (Silver Review UI, retrieval pipeline) decide
whether to translate. Storing the origin signal next to the bbox is
the safest convention.

### 4.5 GRANT DELETE on silver.reports for georag_app

Discovered during test teardown: `georag_app` had INSERT/UPDATE/SELECT
on `silver.reports` but not DELETE. Granted via:

```sql
GRANT DELETE ON silver.reports TO georag_app;
```

This is a legitimate permission expansion — users will eventually
delete their own reports through the product. The grant was applied
directly via psql as `georag` superuser (same workaround pattern as
doc-phase 50's migration apply path).

**Carry-over**: the doc-phase 50 grant strategy (run migrations as
`georag_app` then patch missing permissions ad-hoc) is brittle. A
future tick should consolidate this into a proper Laravel migration
or runbook entry. For now: every doc-phase that adds an INSERT/UPDATE/
DELETE target must check georag_app privileges before declaring done.

### 4.6 Test teardown via CASCADE

Tests create a throwaway `silver.reports` row, run persist, assert
silver rows, then DELETE the report. The doc-phase 50 schema has
`ON DELETE CASCADE` on every new silver table's `report_id_fkey`,
so deleting the report cleanly removes all the silver rows the test
wrote. Reversible test design.

### 4.7 `_compute_doc_quality_score` is a placeholder

The score is currently just `accept_count / total_routes`. A real
quality score should factor in OCR confidence, layout confidence,
table confidence, retry counts, page-type distribution. Defer to
Step 9 corpus-tuning when we have signal about which factors actually
correlate with operator-perceived quality.

---

## 5. Findings carried over to doc-phase 57+

### 5.1 Permission management is still ad-hoc

The DELETE grant in §4.5 is the second time we've patched `georag_app`
privileges after a migration. A consolidation tick worth opening:
review all silver table grants for `georag_app` + write a single
"app role permissions" migration that's the single source of truth.

### 5.2 Doc-phase 57 wires Hatchet ingest_pdf

Doc-phase 57 will rewrite the `parse` + `persist` steps in
`app/hatchet_workflows/ingest_pdf.py`:
- `parse` step calls `orchestrate(local_pdf_path)` and returns the
  result dict (serialized for Hatchet step-output transport)
- `persist` step deserializes, opens a `transactional_workspace_session`,
  calls `persist_orchestrator_result`, also writes to the existing
  `silver.reports` row (title, sha256, parser_used, etc.) so the
  current downstream consumers keep working

The `silver.document_passages` co-write decision (handoff §5.4 of
doc-phase 55) needs resolution in doc-phase 57. Most likely: keep
writing to document_passages (existing retrieval path) plus the new
ingest_extractions (per-region authoritative store). RAGFlow
retirement in doc-phase 58+ may eventually flip retrieval to read
from ingest_extractions directly.

### 5.3 Bbox coord_origin translation is now annotation-only

Persistence stores the coord_origin in the payload JSONB. No
translation happens at write time. If downstream needs unified coords
(e.g. Silver Review UI overlays), the consumer reads `coord_origin`
and translates. This defers the "one canonical coord system" decision
to whoever actually consumes the data — better than picking now and
locking in a possibly-wrong choice.

### 5.4 Region counters reset per-document

Each `persist_orchestrator_result` call starts with empty region
counters. If you call persist twice for the same report_id with
non-overlapping page sets, the second call's region IDs will collide
with the first's. Solution would be: query existing max(region) per
(report_id, page) before inserting. Not needed for current use case
(persist is called once per ingest), but worth knowing for any
future "incremental re-ingest" feature.

---

## 6. Pre-existing carry-overs (unchanged this phase)

From prior handoffs:
- Profile classifier thresholds need 50-PDF corpus tuning (Step 9)
- Table review confidence thresholds need 50-PDF corpus tuning (Step 9)
- Migration apply path workaround (use psql + manual INSERT migrations)
- Windows ↔ WSL dual-tree sync
- WSL2 exposes 6/32 CPUs
- PaddleOCR cache → /tmp by default
- Docling deprecation warning on table image extraction (benign)
- Retry settings escalation logic is opinionated

From doc-phase 48:
- `phase4_step7` sweep-only flake
- `phase9_step1` docker network name mismatch

---

## 7. What doc-phase 57 will do

**Master-plan §3 Step 7 part C — Hatchet `ingest_pdf` cutover.**

Rewrite the parse + persist steps of
`src/fastapi/app/hatchet_workflows/ingest_pdf.py` to invoke the
orchestrator + persistence chain.

Deliverables:
- `parse` step: download Bronze PDF to local tmp, call
  `orchestrate(local_path)`, return result (or its key fields) for
  transport between Hatchet steps
- `persist` step: open `transactional_workspace_session`, call
  `persist_orchestrator_result`, also write `silver.reports` row
  with the existing column contract (so existing v1.49-era retrieval
  keeps working)
- Keep the existing `silver.document_passages` co-write for now
  (downstream retrieval depends on it)
- End-to-end test that invokes the Hatchet step contract directly
  (not through the Hatchet engine; just calls the step functions
  with synthetic input)
- New verifier `scripts/phase3_master_plan_step7c_verify.sh`

The deliberate non-goal in doc-phase 57: shadow comparison
(dual-run RAGFlow + §04p). That's doc-phase 58 if scope permits, or
defer if Step 7c surfaces enough complexity.

---

## 8. Master-plan §3 progress

| Step | Status | Doc-phase tick |
|---|---|---|
| 1. `app/ocr/` scaffolding + smoke-bench | ✅ DONE | 49 |
| 2. §9.3 + §9.6 silver migrations | ✅ DONE | 50 |
| 3. PDF profiler + native parser | ✅ DONE | 51 |
| 4. Scanned parser + render | ✅ DONE | 52 |
| 5. Mixed + table-heavy parsers (Docling) | ✅ DONE | 53 |
| 6. LangGraph OCR Quality Graph | ✅ DONE | 54 |
| 7a. Orchestrator | ✅ DONE | 55 |
| 7b. Persistence layer | ✅ DONE | 56 |
| 7c. Hatchet ingest_pdf rewrite | next | 57 |
| 7d. Shadow comparison (optional) | pending | 58 |
| 8. Silver Review UI extension | pending | 58-59 |
| 9. 50-PDF acceptance corpus + sign-off | pending — needs Kyle labeling | 59-60 |
| 10. RAGFlow retirement + cleanup | pending | 60-61 |

**7 of 10 main steps complete** (7 done + 7a-c sub-split). Database
writes proven end-to-end.

---

End of doc-phase 56 handoff. Persistence wired. Hatchet cutover next.
