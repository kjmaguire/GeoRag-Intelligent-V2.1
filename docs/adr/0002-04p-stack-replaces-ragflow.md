# ADR 0002: §04p PDF stack replaces RAGFlow as the canonical parser

- **Date**: 2026-05-12
- **Status**: Accepted
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: 2026-04-20 RAGFlow-canonical decision
  documented in `src/dagster/georag_dagster/parsers/pdf_report.py:4-11`

## Context

Master plan v2.4.2 §3 (the "Phase 3" master-plan phase, distinct from
doc-phase 3 which delivered the Kestra migration) calls for a literal
replacement of v1.49 RAGFlow with the §04p stack:

- qpdf + pikepdf — preflight, repair, normalize
- pypdfium2 — rendering for image extraction
- pdfminer.six + pdfplumber — text and table extraction
- Docling — layout intelligence
- PaddleOCR PP-OCRv5 / PP-StructureV3 — OCR fallback for scanned pages

Plus five OCR-quality tables (§9.6), the LangGraph OCR Quality Graph
(§9.7), an XGBoost OCR-quality classifier (§9.8, trained on the first
1,000 reviewed pages), and a Silver Review queue UI extended with
per-page evidence + parser + confidence breakdown.

A divergent operational decision was made 2026-04-20: RAGFlow v0.17.2
was made the canonical PDF parser; the v1.49 `unstructured + pdfplumber`
parser at `src/dagster/georag_dagster/parsers/pdf_report.py` was
relegated to fallback-only. The §04p stack was not built. The
master-plan §3 was not opened.

Four facts forced reconsideration as the project enters the next
master-plan phase:

1. **Doc-phase numbering diverged from master-plan numbering.** Doc-phases
   1–48 covered master-plan §0–§2 plus §4 (RAG + Answer Graph) plus a
   long hardening tail. Master-plan §3 (§04p PDF stack + OCR quality)
   was never opened. The retrospective phase audit at doc-phase 48
   surfaced this gap.

2. **The five §3 OCR-quality tables are load-bearing for §9.8 and §9.7,
   not just §3.** The XGBoost classifier in §9.8 trains on the first
   1,000 reviewed pages — the labeled training data must accumulate
   from doc-phase ticks onward, not begin at master-plan §9. Without
   `silver.ocr_page_quality` and `silver.parser_run_artifacts`
   populating from every PDF ingest, the §9.8 cold-start is impossible.

3. **GPU contention with vLLM is a real architectural concern.**
   Measured 2026-05-12 at idle: A4500 has 372 MiB free with vLLM at
   `--gpu-memory-utilization 0.92`. The free pool oscillates between
   ~300 MiB and ~1 GiB depending on active inference load (KV cache
   allocation is transient). Co-locating PaddleOCR PP-StructureV3
   (~1.5–2 GiB peak) and Docling (~1–2 GiB peak) on the same GPU is
   not viable in any regime without lowering vLLM's KV-cache budget.

4. **The §04p stack libraries are already installed in `georag/fastapi:latest`**
   (verified 2026-05-12 during Step 1 pre-work). The image is already
   11.1 GB; `pikepdf`, `pypdfium2`, `pdfminer.six`, `pdfplumber`,
   `docling`, `paddlepaddle`, `paddleocr` all import cleanly inside
   both `georag-fastapi` and `georag-hatchet-worker-ingestion`
   (which runs the same image). The original revision of this ADR
   proposed a new `georag-ocr` container to keep the FastAPI image
   lean and isolate PaddlePaddle — both premises were false. The
   image is not lean, and PaddlePaddle is not isolated. A separate
   container would duplicate ~3 GB of already-paid-for dependencies
   and add a service-management cost for no benefit.

## Options considered

| Option | Effort | Outcome |
|---|---|---|
| A. Keep RAGFlow canonical (2026-04-20 decision) | Low | §3 reduced to "build quality/review layer on top of RAGFlow output"; permanently glues two parsers; §3 done criterion compromised. Rejected. |
| B. Replace RAGFlow with §04p stack on GPU OCR | High | Matches master plan literally; GPU contention with vLLM requires lowering KV-cache budget, swapping to smaller LLM, or adding a second GPU. Rejected at workstation scale. |
| C. Replace RAGFlow with §04p stack, CPU OCR, **new `georag-ocr` container** | High | Matches master plan literally; defers GPU upgrade decision; was initially chosen until Step 1 pre-work surfaced that the §04p libs are already in `georag/fastapi:latest`. Rejected on duplicated-dependency grounds. |
| D. **Replace RAGFlow with §04p stack, CPU OCR, in-process module in existing `georag-fastapi` image** | High but bounded | Same end state as C; reuses already-installed libs; no new container; Hatchet `ingest_pdf` step calls parsers in-process; user-facing FastAPI never imports the OCR module so query-path stays clean. Chosen. |

## Decision

Replace RAGFlow with the §04p stack as the canonical PDF parser path.
Run PaddleOCR PP-OCRv5 + PP-StructureV3 on **CPU only** for the
foreseeable future; the Threadripper Pro 5955WX (16P/32L) handles
ingest-pool async OCR with acceptable per-page latency.

Host the §04p stack as an **in-process module** at `src/fastapi/app/ocr/`
inside the existing `georag/fastapi:latest` image. The Hatchet
`ingest_pdf` workflow's parse step imports and calls the parsers
directly in-process. No new container. No HTTP IPC. Image stays at
its current ~11 GB.

### What stays the same

- **Hatchet `ingest_pdf` workflow shape**: preflight → parse → persist.
  The three-step contract documented in `docs/phase1_v149_ingest_pdf_survey.md`
  is preserved. Only the internals of the `parse` step change.
- **Silver Bronze contract**: `silver.reports` and
  `silver.document_passages` schemas keep their existing columns. The
  §04p stack writes additional rows into the five new quality tables;
  it does not change existing column types.
- **Bronze intake manifest** (§9.2): unchanged.
- **Citation contract**: `source_chunk_id` continues to anchor every
  cited claim per CLAUDE.md hard rule #4 and §04i. The §04p stack
  produces chunks with stronger per-page provenance, not weaker.
- **`pdf_report.py` v1.49 parser**: stays in tree at
  `src/dagster/georag_dagster/parsers/pdf_report.py` as the audit-trail
  reference until the §04p stack proves equivalent or better on the
  50-PDF acceptance corpus. Archived to `_archived/` only after
  acceptance.

### What changes

- **Hatchet `ingest_pdf` parse step** is rewritten to invoke the §04p
  pipeline. The current call to `parse_pdf_report()` is replaced with
  a profile-routed dispatch:

  ```
  qpdf/pikepdf preflight
    → pypdfium2 page renders + PDF profile classification
    → routed parsing:
        native       → pdfminer.six text + pdfplumber tables
        scanned      → PaddleOCR PP-OCRv5 (CPU) + deskew
        mixed        → Docling layout-first, per-region method
        map-heavy    → page rasterized + Silver Review
        table-heavy  → pdfplumber + Docling table focus
    → page-quality scoring (writes to silver.ocr_page_quality)
    → LangGraph OCR Quality Graph (re-OCR / Silver Review / reject)
    → passage/table normalization to silver.document_passages
    → silver.document_ingestion_quality summary row per PDF
  ```

- **Eight new silver tables**:
  - Five quality tables from §9.6: `silver.ocr_page_quality`,
    `silver.document_ingestion_quality`,
    `silver.table_extraction_quality`, `silver.parser_run_artifacts`,
    `silver.low_confidence_page_reviews`.
  - Three per-region extraction tables from §9.3:
    `silver.ingest_extractions` (text regions),
    `silver.ingest_layouts` (Docling layout regions),
    `silver.ingest_ocr_results` (OCR per-region output). All keyed by
    `(pdf_id, page, region)` with the §9.3 provenance contract
    `(pdf_id, page, bbox, source_method, extraction_confidence)`.

  Schemas land verbatim from §9.6 and §9.3.

- **LangGraph OCR Quality Graph** routes pages by confidence:
  re-OCR (max 2 retries, different engine settings), Silver Review
  (human in the loop), or reject with reason (corrupted PDF,
  password-protected, etc.) per §9.7.

- **PaddlePaddle pinned to CPU wheels**: `paddlepaddle` (CPU), not
  `paddlepaddle-gpu`. Runtime config sets device=cpu explicitly.

- **PaddleOCR + Docling run as in-process Python module** at
  `src/fastapi/app/ocr/` inside the existing `georag/fastapi:latest`
  image. Modules: `app/ocr/preflight.py`, `app/ocr/profile.py`,
  `app/ocr/parse_native.py`, `app/ocr/parse_scanned.py`,
  `app/ocr/parse_mixed.py`, `app/ocr/parse_table_heavy.py`,
  `app/ocr/render.py`, `app/ocr/quality_graph.py`. Each module
  exposes a typed async function; no HTTP layer.

- **Import-boundary rule**: nothing under `src/fastapi/app/routers/`
  or `src/fastapi/app/main.py` may `import app.ocr`. The OCR module
  is imported only by `src/fastapi/app/hatchet_workflows/ingest_pdf.py`
  and by tests. Enforced by a lint check
  (`scripts/phase3_master_plan_step1_import_boundary.sh`) that greps
  for `import app.ocr` outside the allowed callers.

  Rationale: the user-facing FastAPI process never loads PaddleOCR
  or Docling into memory. Only the Hatchet ingestion worker process
  pays the load cost. The two processes share the same image but
  have different runtime memory footprints because they import
  different module trees.

- **No IPC**: Hatchet `ingest_pdf.parse` step function calls
  `app.ocr.<parser>` directly. No HTTP, no port 8500, no separate
  container.

- **Silver Review UI** extended with per-page evidence panel: rendered
  page image, parser used, confidence breakdown (OCR / layout / table),
  reviewer disposition controls. Lands as an addition to the existing
  admin surface rather than a new top-level page.

- **50-PDF acceptance corpus** built and committed to
  `tests/fixtures/phase3_pdf_corpus/` with ground-truth labels for
  routing decisions. Covers native / scanned / mixed / table-heavy /
  map-heavy.

- **RAGFlow v0.17.2 container** removed from `docker-compose.yml`
  after the §04p stack proves out on the acceptance corpus. Not in
  this ADR commit; in a later doc-phase tick.

### GPU contention rationale (specific numbers)

Measured on the A4500 workstation 2026-05-12 (idle, vLLM serving but
no active inference):

| Layer | MiB | Source |
|---|---|---|
| A4500 total | 20,470 | `nvidia-smi` |
| vLLM reserved (0.92 utilization) | ~18,832 | `--gpu-memory-utilization 0.92 × 20,470` |
| Windows desktop compositor | ~1,200 | non-evictable, per `project_vllm_migration.md` |
| Currently free | 372 | observed |
| PaddleOCR PP-OCRv5 (det+rec) | 500–1,000 | upstream docs |
| PaddleOCR PP-StructureV3 (layout+table) | 1,500–2,000 | upstream docs |
| Docling layout model | 1,000–2,000 | upstream docs |

372 MiB free vs ~3 GiB minimum for the §04p OCR stack: co-location
with vLLM at the current config is not viable.

CPU-OCR latency on Threadripper Pro 5955WX (16P/32L, AMD Zen 3) —
**upstream-docs estimates, not measured on this hardware**:

- Native PDFs: 1–5 sec/page (pdfminer.six + pdfplumber; OCR not invoked)
- Scanned PDFs: 5–30 sec/page (PaddleOCR PP-OCRv5 CPU)
- 200-page scanned drill log: 30–90 minutes
- Acceptable because ingest is async via Hatchet `ingestion` pool;
  user-facing query latency is unaffected.

**Gating check in Phase 3 Step 1**: a smoke-bench measures real
per-page latency for one native + one scanned + one mixed PDF
through `georag-ocr` before subsequent steps commit. If measured
latency is >5x the estimate ranges above (catastrophic regression),
this ADR is reopened — options include reducing PP-StructureV3 to
PP-OCRv5-only on mixed pages, swapping Docling for a lighter layout
heuristic, or revisiting GPU OCR via vLLM utilization cut.

## Consequences

### Positive

- **Master-plan §3 done criterion is reachable.** The five OCR-quality
  tables, the LangGraph routing graph, and the 50-PDF acceptance corpus
  all become deliverables of master-plan §3 rather than aspirations
  blocked on RAGFlow's opaque parse internals.
- **§9.8 XGBoost classifier substrate accumulates from day one.** Every
  PDF ingest writes `silver.ocr_page_quality` and
  `silver.parser_run_artifacts` rows; the 1,000-page training threshold
  becomes a function of ingest volume, not a separate labeling project.
- **License-compliant.** pypdfium2 (Apache 2.0/BSD-3), Docling (MIT),
  PaddleOCR (Apache 2.0), pdfminer.six (MIT), pdfplumber (MIT), qpdf
  (Apache 2.0), pikepdf (MPL 2.0). All within the project's
  MIT/BSD/Apache 2.0 license rule.
- **No permanent two-parser glue.** The end state is a single
  authoritative PDF parser stack; the v1.49 `pdf_report.py` retires
  cleanly to `_archived/`.
- **vLLM live chat path untouched.** CPU OCR runs in the Hatchet
  ingestion pool; query-path GPU utilization is unaffected.
- **Per-page provenance gets stronger.** §04p produces
  `(pdf_id, page, bbox, source_method, extraction_confidence)` for
  every region; citations gain a stronger anchor than RAGFlow's
  document-level confidence.

### Negative

- **Initial parse quality regression risk vs RAGFlow's NI 43-101
  tuning.** RAGFlow v0.17.2 has been validated on this project's
  technical reports; the §04p composition is greenfield. Mitigation:
  50-PDF acceptance corpus + multiple tuning passes before RAGFlow
  retires.
- **PaddlePaddle CPU wheel install on Linux/WSL still has sharp edges**
  (specific protobuf / numpy version constraints). Expect 1–2
  doc-phase ticks of environment debugging.
- **Image size unchanged but already large.** The §04p libs are
  already in `georag/fastapi:latest` (11.1 GB image). This ADR adds
  no image-size delta but also does not solve the pre-existing
  bloat. Defer a "split worker images" exercise to a later
  master-plan phase if image size becomes an operational pain point;
  out of scope here.
- **Scanned PDF throughput is slower on CPU than GPU.** A 200-page
  1970s drill log takes 30–90 min CPU vs ~5–15 min GPU. Acceptable
  for async ingest; would be a problem if interactive OCR were ever
  added to the user path (it is not).
- **Existing already-ingested PDFs in `silver.reports`** parsed by
  RAGFlow are not automatically re-ingested. A re-ingest workflow is
  out of scope for this ADR; doc-phase ticks may add one later. The
  golden test's 30–31/31 pass rate is preserved by holding the
  existing fixture rows; new uploads use §04p.

## Verification (this commit)

This is a planning ADR — no infrastructure or code change to verify
in this commit. The full verification of the §04p stack replacement
is the entire master-plan §3 acceptance test (Step 9 of
`docs/phase3_master_plan_kickoff.md`): 50-PDF acceptance corpus passes
through `georag-ocr` with ground-truth routing decisions matching, and
the existing golden-test 30–31/31 baseline is preserved.

Commit content:
- This ADR
- Master-plan §3 implementation kickoff
  (`docs/phase3_master_plan_kickoff.md`)
- §3 acceptance corpus scaffolding under
  `tests/fixtures/phase3_pdf_corpus/`

## Follow-ups (NOT part of this ADR; tracked separately)

- Master-plan §3 implementation kickoff: `docs/phase3_master_plan_kickoff.md`
  (next deliverable). To disambiguate from doc-phase 3 (Kestra
  migration) the master-plan filename uses the `_master_plan_` infix.
- 50-PDF acceptance corpus construction: labeling work + commit to
  `tests/fixtures/phase3_pdf_corpus/`. Likely 1–2 doc-phase ticks of
  Kyle's SME time, not autonomous-loop work.
- Re-ingest workflow for RAGFlow-parsed `silver.reports` rows: defer
  until §04p stack is proven on the acceptance corpus.
- GPU OCR escape hatch: if production volume justifies dedicated GPU,
  swap PaddlePaddle CPU wheels for GPU wheels; runtime device flag
  flips. Hardware change, not architectural change. Reassess at
  master-plan §11 (DR + deployment topologies).
- `georag-ocr` container scaling: when ingest volume requires it,
  the container is independently horizontally scalable. Hatchet
  ingestion pool worker count is the throttle.
