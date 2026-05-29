# Phase 49 Handoff — Master-plan §3 Step 1 (`app/ocr/` skeleton + verifier)

**Document version:** 1.0
**Status:** Doc-phase 49 complete. Doc-phase 50 inheriting.
**Predecessors:** `docs/phase48_handoff.md`, `docs/adr/0002-04p-stack-replaces-ragflow.md`,
`docs/phase3_master_plan_kickoff.md`.

This is the first doc-phase tick that lands inside master-plan §3
(§04p PDF stack + OCR quality). Per the numbering convention locked
in the kickoff, doc-phase tick numbers continue from doc-phase 48
forward (49, 50, 51, …) while the *master-plan* phase being executed
is §3. ADR-0002 is now Accepted.

---

## 1. What doc-phase 49 delivered

Step 1 of the master-plan §3 implementation kickoff. Skeleton-only
phase: the §04p parser interface contract is locked, behavioural
implementations land in doc-phases 50+.

| Deliverable | Output | Verifier |
|---|---|---|
| 1 | `app/ocr/` package with 8 typed-async skeleton modules + top-level re-exports | `scripts/phase3_master_plan_step1_verify.sh` check 1 |
| 2 | Import-boundary lint enforcing ADR-0002's rule that route handlers + `app/main.py` cannot import `app.ocr` | `scripts/phase3_master_plan_step1_import_boundary.sh` |
| 3 | 17-test pytest module asserting each function imports, is async, raises `NotImplementedError`, and is re-exported via `__all__` | `tests/test_ocr_module_imports.py` |
| 4 | CPU-OCR smoke-bench wired into the Step 1 verifier as the third check; uses the existing report from doc-phase 48 pre-work | `scripts/phase3_master_plan_step1_verify.sh` check 3 |

---

## 2. Files of record

### New
- `src/fastapi/app/ocr/__init__.py` — package + re-exports
- `src/fastapi/app/ocr/preflight.py` — Step 3 placeholder
- `src/fastapi/app/ocr/profile.py` — Step 3 placeholder
- `src/fastapi/app/ocr/parse_native.py` — Step 3 placeholder
- `src/fastapi/app/ocr/parse_scanned.py` — Step 4 placeholder (notes
  the pre-render-via-pypdfium2 image-input pattern that avoids
  PaddleOCR's `fitz` dependency)
- `src/fastapi/app/ocr/parse_mixed.py` — Step 5 placeholder
- `src/fastapi/app/ocr/parse_table_heavy.py` — Step 5 placeholder
- `src/fastapi/app/ocr/render.py` — Step 4 placeholder
- `src/fastapi/app/ocr/quality_graph.py` — Step 6 placeholder
- `src/fastapi/tests/test_ocr_module_imports.py` — 17 tests, all passing
- `scripts/phase3_master_plan_step1_import_boundary.sh`
- `scripts/phase3_master_plan_step1_verify.sh`

### Modified
- `docs/adr/0002-04p-stack-replaces-ragflow.md` — Status flipped
  Proposed → Accepted (2026-05-12)

---

## 3. Verifier status

```
[check1] PASS — 8/8 app.ocr modules importable + async + re-exported
[check2] PASS — import-boundary lint clean
[check3] PASS — smoke-bench gates all pass (report: ocr_cpu_smoke_1778628470.json)

=== Phase 3 master-plan Step 1 verifier summary ===
  3/3 checks passed
```

Pytest module (`tests/test_ocr_module_imports.py`):
- 17/17 tests pass
- One trivial pytest cache warning about `/app/.pytest_cache` permissions
  (pre-existing pattern in this project, not a Step 1 regression)

---

## 4. Behaviour locked in this phase

### Interface contract (the only locked decision)

Each of the 8 `app.ocr.*` functions is an async coroutine. Signatures:

```python
async def preflight(pdf_path: Path) -> dict[str, Any]
async def profile(pdf_path: Path) -> dict[str, Any]
async def parse_native(pdf_path: Path, pages: Sequence[int] | None = None) -> dict[str, Any]
async def parse_scanned(pdf_path: Path, pages: Sequence[int] | None = None) -> dict[str, Any]
async def parse_mixed(pdf_path: Path, pages: Sequence[int] | None = None) -> dict[str, Any]
async def parse_table_heavy(pdf_path: Path, pages: Sequence[int] | None = None) -> dict[str, Any]
async def render_page(pdf_path: Path, page: int, scale: float = 2.0) -> bytes
async def route_page(parse_result: dict[str, Any], page: int, profile: str) -> dict[str, Any]
```

Output schemas are deliberately `dict[str, Any]` placeholders. Each
docstring lists the minimum keys that the schema-pinning step (3, 4,
5, or 6) must include — those become typed dataclasses when behaviour
lands. Premature locking would just be churn.

### Import-boundary rule (the architectural decision)

Per ADR-0002:
- Allowed importers of `app.ocr`:
  - `src/fastapi/app/hatchet_workflows/ingest_pdf.py`
  - anything under `src/fastapi/app/ocr/` (internal)
  - anything under `src/fastapi/tests/`
- All other importers (route handlers, `app/main.py`, middleware,
  agent code, services) are rejected by
  `scripts/phase3_master_plan_step1_import_boundary.sh`.

The rule keeps PaddleOCR + Docling out of the user-facing FastAPI
process's resident memory. Both processes (`georag-fastapi` and
`georag-hatchet-worker-ingestion`) run the same 11.1 GB image; the
difference is which modules each loads at runtime.

---

## 5. Findings carried over to doc-phase 50+

### 5.1 PaddleOCR fitz dependency — implementation gotcha

PaddleOCR's `ocr(pdf_path_string)` path requires PyMuPDF (`fitz`).
`fitz` is NOT installed in the FastAPI image; installing it is
discouraged (AGPL license, conflicts with the project's
MIT/BSD/Apache 2.0 license rule per `feedback_free_licensing.md`).

**Pattern to use in Step 4 (parse_scanned):** pre-render PDF pages
to numpy arrays via pypdfium2, then pass arrays to `PaddleOCR.ocr()`.
The smoke-bench's `bench_scanned_parse()` in
`ops/validation/ocr_cpu_smoke.py` is the canonical reference shape —
the `parse_scanned.py` skeleton docstring already calls this out.

### 5.2 Docling is the slowest path, not PaddleOCR

Measured (2026-05-12 smoke-bench, 6-CPU WSL2 container on Threadripper
5955WX):
- Native: 60 ms/page
- Scanned warm (PaddleOCR PP-OCRv5 image-input): 6.1 sec/page
- Mixed (Docling layout-first): 11.8 sec/page

Mixed PDFs are the slowest. The 50-PDF acceptance corpus's mixed
category (Step 9) will dominate wall-clock time. Step 5
implementation should consider whether Docling's full table-structure
model is needed on every page or only on pages flagged as
table-bearing by pdfplumber.

### 5.3 WSL2 is exposing only 6 CPUs to the Linux VM

`cpu_count()` inside the hatchet-worker-ingestion container = 6.
Threadripper Pro 5955WX has 32 logical cores. No `cpus:` limit set
on the worker service in `docker-compose.yml` — so the throttle is
coming from WSL2 / Docker Desktop config.

Out of scope for §3, but a 2-4× ingest throughput gain is on the
table by raising the WSL2 CPU allocation in `.wslconfig`. Worth a
separate doc-phase tick or runbook entry — not blocking §3.

### 5.4 Windows ↔ WSL sync pattern (recurring)

Per the `project_vllm_migration.md` memory pattern: Windows-tree
edits don't auto-appear in the WSL canonical tree where the
containers read from. The Step 1 verifier was 2/3 until the new
files (`app/ocr/`, `tests/test_ocr_*.py`, scripts) were copied to
`/home/georag/projects/georag/`.

Recipe used in doc-phase 49:
```bash
cp -r /mnt/c/Users/GeoRAG/Herd/georag/src/fastapi/app/ocr \
      /home/georag/projects/georag/src/fastapi/app/
cp /mnt/c/Users/GeoRAG/Herd/georag/src/fastapi/tests/test_ocr_module_imports.py \
   /home/georag/projects/georag/src/fastapi/tests/
cp /mnt/c/Users/GeoRAG/Herd/georag/scripts/phase3_master_plan_step1_*.sh \
   /home/georag/projects/georag/scripts/
chmod +x /home/georag/projects/georag/scripts/phase3_master_plan_step1_*.sh
```

The canonical `ops/setup/sync_windows_to_wsl.sh` script covers the
existing surface area; if doc-phases 50+ add new top-level
directories (e.g. `src/fastapi/app/ocr/<subdir>/`) the sync script
needs a corresponding entry.

---

## 6. Pre-existing carry-overs (unchanged this phase)

From the doc-phase 48 handoff, still open:

- `phase4_step7` sweep-only flake (intermittent, docker contention)
- `phase9_step1` docker network name mismatch (documented exclusion)
- Phase-4 dead-code findings: `dagster/hooks/shadow_v149.py` log spam,
  `ShadowRouter.php` load-bearing review pending

None affect master-plan §3 work.

---

## 7. What doc-phase 50 will do

**Master-plan §3 Step 2 — §9.3 + §9.6 silver tables (migrations).**

Eight Laravel migrations creating:
- §9.6 quality tables: `silver.ocr_page_quality`,
  `silver.document_ingestion_quality`,
  `silver.table_extraction_quality`, `silver.parser_run_artifacts`,
  `silver.low_confidence_page_reviews`
- §9.3 per-region extraction tables: `silver.ingest_extractions`,
  `silver.ingest_layouts`, `silver.ingest_ocr_results`

All schemas verbatim from master plan §9.3 + §9.6. RLS workspace-scoped.
Indexes on `(pdf_id, page)` and `(pdf_id, page, region)`.

Verifier: `scripts/phase3_master_plan_step2_verify.sh` — confirms each
table exists, RLS enabled, indexes present, migrations are idempotent
(roll back + re-run clean).

No app code lands in doc-phase 50 — schemas only. Doc-phase 51 picks
up Step 3 (PDF profiler + native parser path implementations) which
needs these tables as the write target.

---

## 8. Master-plan §3 progress

| Step | Status | Doc-phase tick |
|---|---|---|
| 1. `app/ocr/` scaffolding + smoke-bench | **DONE** (3/3 green) | 49 |
| 2. §9.3 + §9.6 silver migrations | next | 50 |
| 3. PDF profiler + native parser | pending | 51 |
| 4. Scanned parser (PaddleOCR CPU image-input) | pending | 52 |
| 5. Mixed + table-heavy parsers (Docling) | pending | 53 |
| 6. LangGraph OCR Quality Graph | pending | 54 |
| 7. Hatchet `ingest_pdf` cutover + shadow comparison | pending | 55 |
| 8. Silver Review UI extension | pending | 56 |
| 9. 50-PDF acceptance corpus + sign-off | pending — needs Kyle labeling | 57 |
| 10. RAGFlow retirement + cleanup | pending | 58 |

Doc-phase tick numbers above are estimates; some steps may span
multiple ticks per the autonomous-loop cadence ("budget more for
infra-fix phases; revert wrong attempts before landing right fix").

---

End of doc-phase 49 handoff. The §04p interface contract is locked.
Behaviour starts landing at doc-phase 51.
