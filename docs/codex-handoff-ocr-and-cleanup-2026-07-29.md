# Codex handoff — OCR completion, dependency currency, full repo review

**Prepared:** 2026-07-29, by Claude (on `trim/phase-a-deletions`)
**Intended runner:** Codex 5.6, model "Sol", Medium reasoning — run to exhaust the
allotted credit budget. Treat every section below as independently startable
work; there is no requirement to go in order except where a dependency is
called out explicitly.

**Repo / branch:** `trim/phase-a-deletions`, worktree `C:\Users\GeoRAG\Herd\georag-trim`.
Do **not** touch `C:\Users\GeoRAG\Herd\georag` — that is the live stack
(`georagintelligencev10`) and must never be disturbed by this work.

**Ground rules inherited from this project's `CLAUDE.md`** (read it in full
before starting): async-native drivers only in FastAPI, Octane-safe Laravel,
mandatory citations on RAG responses, MapLibre not Mapbox, Neo4j Community
only (already dropped on this branch — see §5), no Streamlit.

**Test recipe** (Windows/Git Bash — the FastAPI `.venv` is Linux-only, unusable
directly from Windows, so tests must run in-container):

```bash
MSYS_NO_PATHCONV=1 docker run --rm --network georag \
  -v "//c/Users/GeoRAG/Herd/georag-trim/src/fastapi:/app" -w /app \
  -u 33:33 -e HOME=/tmp -e XDG_CACHE_HOME=/tmp/xdg_cache -e HF_HOME=/tmp/hf_cache \
  --env-file "C:/Users/GeoRAG/Herd/georag/.env" \
  -e POSTGRES_HOST=postgresql -e POSTGRES_DIRECT_HOST=postgresql \
  georag/fastapi:latest python -m pytest -q
```

Omitting `-u 33:33 -e HOME=/tmp` causes false PaddleOCR/paddlex-style
cache-permission failures (a recurring false-positive in this repo's history
— don't chase it as a real bug if it reappears from some other library).
Baseline as of this handoff: **2537 passed, 25 skipped, 243 deselected, 1
pre-existing unrelated failure** (`test_document_intelligence_client.py::
TestOcrPage::test_raises_not_configured_without_credentials` — fails only
because real Azure Document Intelligence credentials are present in this
environment's `.env`; not a real bug, don't "fix" it by breaking the
credential-present case).

For Laravel: `php artisan test --compact` (needs Docker/PG — see
`docs/RUNBOOK.md` for env gotchas). Run `vendor/bin/pint --dirty --format agent`
after any PHP edit.

---

## 0. Blocking dependency — confirm before starting §1–§4

A background agent (task id `a4d3bbbe2f44683f6`) was mid-flight, in an
isolated worktree, removing `docling` entirely from this same branch when
this handoff was written (169 references in `pdf_report.py` alone, plus
`figure_extractor.py`, `pdf_layout.py`, `main.py`, `routers/pdf.py`,
`models/pdf.py`, `tools.py`, `ingest_pdf.py`, the `docling>=2.13` pyproject
pin, and docling-specific tests). **Check whether that work has landed and
merged into `trim/phase-a-deletions` before starting.** If it has not:

- Either wait for it, or
- Take over finishing it yourself using the scope list above — grep for
  `docling`/`DOCLING`/`Docling` across `src/fastapi` first to see what's
  actually left, don't assume the list above is still accurate.

Reason this matters: §2 (dependency currency) and §3 (full review) will both
trip over docling-related dead code and the `docling>=2.13` /
`onnxruntime-gpu` pins if this isn't resolved first, and you'd be reviewing
code that's about to be deleted out from under you.

---

## 1. Remaining OCR work (the approved plan, not yet started)

Full context: this session evaluated and rejected several OCR alternatives
(Tesseract-only, PaddleOCR, Surya v2, docling-native, various
datalab-to/* repos — rejected mainly on licensing grounds: OpenRAIL-M
funding/revenue caps on Surya and Chandra, a non-compete clause on Chandra,
GPL-3.0 on `docling-surya`). **The approved architecture:**

```
TIFF → Pillow/OpenCV tiling → Azure Document Intelligence → multi-signal
confidence validation → manual-review queue for uncertain content
```

with Tesseract kept only as a last-resort fallback (already wired — see
`app/services/ingest/pdf_report.py::_attempt_ocr` and
`document_intelligence_client.py`), and docling removed entirely (§0).

Already done (do not redo):
- `app/services/ingest/document_intelligence_client.py` — Azure DI adapter,
  11 passing unit tests, validated end-to-end against real Azure credentials
  and real historical corpus files (76–99.4% confidence on real content,
  correctly rejects `InvalidContentDimensions` on oversized scans).
- Fallback-to-Tesseract bug fix: `pdf_report.py`'s `_ocr_single_page` and
  `_attempt_ocr_document_intelligence` now check for *empty text*, not just
  exceptions, before accepting a Document Intelligence result as final.
- Full removal of the orphaned PaddleOCR `/pdf/ocr_region` stack (commit
  `ab5f0a2` on this branch, 2026-07-29) — `app/ocr/`, `services/pdf_ocr.py`,
  `routers/ocr_render.py`, `routers/re_ocr_trigger.py`,
  `hatchet_workflows/re_ocr_page.py`, `agent/pdf_tool_results.py`, the
  `paddlepaddle`/`paddleocr` pyproject pins.

**Not yet started — this is the real remaining OCR scope:**

### 1a. Tiling preprocessor
New module, e.g. `app/services/ingest/image_tiling.py`. Azure Document
Intelligence hard-caps at 10,000×10,000 px per side (F0 free tier also caps
at first-2-pages-only + 500 pages/month + 20 calls/min — confirm which tier
is live before assuming full-document processing works). Split any
oversized raster into ≤10,000px (8,000–9,000px recommended margin) vertical
bands with ~150–200px overlap. Preserve tile offsets/IDs so OCR polygons can
be remapped back to original coordinates. The real 1940s well-log TIFF this
session tested against (2,550×16,269px) is a good regression fixture —
copies are at
`C:\Users\GeoRAG\AppData\Local\Temp\claude\C--Users-GeoRAG\61613d47-b296-4975-90be-ee081193c746\scratchpad\ocr_test_samples\`
(may have been cleaned up by the time you run this — re-source from
`C:\Users\GeoRAG\Desktop\028N079W36\028N079W36` if so, per this session's
history; ask Kyle if that path no longer has anything useful).
Needs `opencv-python` and/or a `Pillow` decompression-bomb-limit override —
this session also found Pillow's default 178,956,970-pixel guard breaks on
these same oversized scans; account for that explicitly (raise the limit
deliberately for known-trusted internal files, don't just disable it
globally).

### 1b. Wire tiling into two call sites
- The `tiff_normalize` Hatchet workflow (`app/hatchet_workflows/
  tiff_normalize.py`, ADR-0005) — this is the existing TIFF→PDF conversion
  step; tiling needs to happen here, before the standard `ingest_pdf` /
  `pdf_report.py` pipeline runs. TIFF upload is NOT a new code path — don't
  build one.
- The scanned-PDF-page render path inside `pdf_report.py` itself, for
  oversized embedded raster pages that aren't standalone TIFFs.

### 1c. Coordinate/text reconstruction after tiling
Remap OCR polygons from tile-local to original-image coordinates using the
preserved tile offsets from §1a. Dedupe text in the ~150–200px seam-overlap
zones (don't double-count a word straddling a tile boundary). Preserve
reading order across tile boundaries.

### 1d. Multi-signal confidence scoring
Replace the current flat 0.3 log-only threshold (grep `pdf_report.py` for
the existing single-cutoff check) with multiple signals: mean AND median
confidence, % of words below a per-word threshold, output-coverage-vs-
detected-regions ratio, empty-output detection, seam-duplicate detection
(feeds off §1c), gibberish/repeated-character detection. This was explicitly
requested via ChatGPT-relayed feedback in this session's history and the
user approved it as part of the final plan — don't ship a single-number
threshold as "confidence scoring."

### 1e. Tiered confidence-routing bands
catastrophic-failure / mandatory-review / spot-check / auto-accept. Derive
the actual cutoff numbers from real corpus data where possible (run the
scoring from §1d against a batch of already-ingested `silver.reports` rows
and look at the real confidence distribution) rather than guessing round
numbers.

### 1f. Wire review routing into `silver.review_queue`
This table/pattern already exists from the CC-01 Item 1 drill-upload-
ambiguity flow (see `docs/` for that spec) — reuse its schema and UI
integration pattern, don't invent a parallel review-queue mechanism.
Mandatory-review and spot-check tier hits from §1e should land here.

### 1g. End-to-end validation
Run the full tiling→DI→reconstruction→scoring pipeline against real corpus
files: the 4 already tested this session plus at least one more oversized
scan. Confirm a `silver.reports` row, `silver.document_passages` rows, and
`georag_chunks` Qdrant points land correctly, then ask a real question
through `Foundry/Chat` and confirm a cited answer.

---

## 2. Dependency currency — "latest version of everything we keep"

Standing instruction from Kyle: every OCR tool actually kept must be pinned
to its latest version, and by extension this is a good moment to sweep the
rest of the stack too since a Codex pass is being dedicated to review.

### 2a. Already done this session (verify still current, don't re-churn)
`pypdfium2>=5.12,<6`, `azure-core>=1.41`, `pdf2image>=1.17`, `Pillow>=12.3`
in `src/fastapi/pyproject.toml`. Note the pypdfium2 history: 4.30.1 and
5.0.0b1 were yanked from PyPI for a text-extraction regression; 5.0 removed
`PdfDocument.render()` but this codebase never called that method (only
`page.get_textpage().get_text_bounded()`), so the bump was safe. Don't
"fix" this pin without re-verifying that history yourself first.

### 2b. Needs a real audit — do not assume anything below is current
For every one of the ~80 declared dependencies in `src/fastapi/pyproject.toml`
(and `src/dagster/pyproject.toml` if that tree is still kept — see the
`DORMANT.md` demo-scope decision, 2026-07-29, PDF-only reader core, no
CSV/assay upload confirmed for this demo), check current PyPI latest against
the declared floor/ceiling and bump where safe:
- `azure-ai-documentintelligence` — currently pinned `>=1.0.0,<2.0`; check
  for a newer 1.x with bugfixes.
- `pytesseract`, `pdfplumber`, `pikepdf`, `pypdf` — the fallback/lossless
  extraction stack; low risk to bump.
- `opencv-python` — not yet declared; will be added by §1a. Pin it at whatever
  latest stable is when that code lands, don't pre-pin speculatively.
- Anything still referencing `onnxruntime-gpu` (was docling's dependency —
  check whether §0's docling removal also dropped this; if not, decide
  whether anything else still needs it before removing).
- `laravel/framework` (`^13.0`), `inertiajs/inertia-laravel` (`^3.0`),
  `react` (`^19.0.0`), `@inertiajs/react` (`^3.5.0`), `typescript` (`^6.0.2`),
  `vite` (`^8.0.0`), `tailwindcss` (`^4.3.0`) in root `composer.json` /
  `package.json` — same treatment: check for patch/minor bumps within the
  existing major-version constraint, flag (don't silently apply) any
  available major bump since that's a bigger call than "latest version."

For every bump: check the package's own changelog/release notes for
breaking changes before applying, run the affected test suite, and if a
package has a known-bad version range in this codebase's own history (like
the paddle 3.3.x oneDNN regression, or the pypdfium2 yanked-version episode
above) don't re-introduce it blind — grep this repo's git log and comments
for prior war stories on that package name first.

---

## 3. Full review

"Full review" given the scale of this repo (96 Inertia pages, 101
controllers, 54 Hatchet workflows, 42 agent modules pre-trim — many already
cut on this branch, see `docs/` for the Option-2 trim plan this branch
implements) should be scoped as:

### 3a. Dead-code sweep, same pattern as §0/§1
This session found two real instances of "marked complete in the task
tracker but actually only partially removed" (docling, PaddleOCR) purely by
noticing leftover pyproject.toml pins during unrelated work. Do a
systematic pass: for every flag in the live `.env` that's `false` /
disabled, grep for what it gates and confirm that code path has zero other
callers before concluding it's truly dead vs. just currently-off. Cross-
reference against `docs/master_plan_*` and the trim plan's own Phase A/B
item list (`docs/` — search for "Option 2" or "trim") for anything marked
done that might have the same partial-completion problem. Known clean
spots already verified this session: don't re-litigate #17/A3 (OCR, now
actually done) or the docling removal once §0 lands.

### 3b. Stale docstrings and comments
The same audit that caught the paddle/docling leftovers also found a stale
top-of-file docstring in `pdf_report.py` falsely claiming RAGFlow is the
canonical parser (RAGFlow was replaced per ADR-0002 — the whole point of
the §04p in-process PDF stack existing). That fix was folded into §0's
scope; verify it landed. Broaden the search: grep for "RAGFlow", "Kestra"
(retirement was supposed to be finished per task history — verify no
stragglers), and any other superseded-system name across `docs/` and
`app/` comments.

### 3c. Orphaned-endpoint sweep
Two endpoints (`/pdf/find_legends`, `/pdf/ocr_region`) were found this
session with real backend implementations and zero callers in Laravel or
the frontend. Systematically check every route registered in
`src/fastapi/app/main.py` and every Laravel route in `routes/` for the
inverse problem too (frontend/Laravel calling a FastAPI endpoint that no
longer exists, or vice versa) — a mechanical cross-reference, not a guess.

### 3d. Test-suite health
55 TODO/FIXME/XXX markers currently exist across `src/fastapi/app` (grep
count at handoff time — recount, don't trust this number by the time you
run). Triage: which are real known gaps worth a tracked follow-up vs. stale
noise from a finished task. Also: 25 tests currently skip in the FastAPI
suite (`pytest -q` reports `25 skipped`) — audit why each is skipped and
whether the skip reason is still valid.

### 3e. Stale `__pycache__` artifacts
This session found `.pyc` files in `app/ocr/__pycache__/` for Python source
files that no longer exist anywhere in the repo (`parse_docparser_vl.py`,
`parse_mixed.py`, `parse_table_heavy.py`, `_ingest_helper.py`,
`_orchestrator.py`, `_persist.py` — all deleted in some prior partial
cleanup, cache never cleared). These are gitignored and harmless at
runtime, but their *existence* is a tell that other similarly
partially-finished removals may be lurking elsewhere in the repo. Worth a
`find . -name '__pycache__' -newer pyproject.toml` sweep purely as a
detection signal for more §3a candidates, not because the cache files
themselves need fixing.

---

## 4. Anything else worth doing while a full credit budget is available

These are judgment calls, not instructions — use them if there's budget left
after §1–§3, skip any that don't hold up under Codex's own investigation:

- **`docker-compose.yml` comment drift.** Two comment blocks referencing
  PaddleOCR-specific cache-directory behavior were generalized (not removed
  — the underlying `HOME=/tmp` fix is still needed for numba/matplotlib/HF
  caches) as part of commit `ab5f0a2`. Worth checking whether other env-var
  comments in this same file have gone stale the same way after other
  removals on this branch (Neo4j, monitoring stack, Dagster services,
  Kestra — all dropped in Phase A/B of the trim plan).
- **`PdfProvenance.source_method` Literal enum** in `app/models/pdf.py`
  still lists `"paddle_ocr"` and `"paddle_structure"` as allowed values even
  though nothing can produce them anymore post-§0/PaddleOCR-removal. Left
  in place deliberately this session because it mirrors a DB CHECK
  constraint (`silver.pdf_ocr_results` and friends) — before removing the
  Python-side enum values, check whether those 8 §04p silver tables
  (referenced in the original Option-2 trim plan's A3 item) still exist in
  the schema and whether dropping them is in scope. This is a real,
  slightly deeper follow-up that got deliberately deferred this session —
  don't silently skip it, but don't casually drop DB tables either without
  checking what's already migrated on the live stack.
- **Golden-query / hallucination-prevention regression** — per `CLAUDE.md`
  rule 5, any code touching the RAG pipeline needs the six Section-04i
  layers verified. If §1's confidence-scoring work touches anything the
  agent's numeric-claim-verification layer reads from, run the golden query
  suite, not just unit tests.
- **`opentelemetry`/Sentry currency** — `src/fastapi` has live Sentry
  wiring (`SENTRY_*` env vars, confirmed alive per an earlier audit this
  project has on file); Laravel's `sentry-laravel` package is NOT installed
  (commented out in `.env` per project history) — if a dependency sweep
  touches observability packages, don't accidentally wire Laravel Sentry
  back on as a side effect of a version bump; that needs an explicit
  decision, not an accident.

---

## 5. What NOT to touch

- The live stack (`C:\Users\GeoRAG\Herd\georag`, project name
  `georagintelligencev10`) — trim work happens only in the
  `georag-trim` worktree / `trim/phase-a-deletions` branch.
- Neo4j, the monitoring stack (prometheus/grafana/loki/tempo/otel), Kestra,
  Dagster services, and `martin` are *intentionally* dropped per the
  Option-2 trim plan already executed on this branch — don't reintroduce
  them while doing dependency currency work just because they show up in an
  old `docker-compose.yml` comment or a stale doc.
- `laravel-reverb`, `laravel-horizon`, `hatchet-worker-ai` (embedding
  dispatch), and the `georag_dagster`-originated `pdf_report.py` parser
  (now relocated into FastAPI, not the Dagster tree) are load-bearing —
  see the trim plan's "do not cut these" list in `docs/` if unsure whether
  something is safe to remove.
- Multi-clone Docker safety: any `docker compose` command must use
  `-p georagintelligencev10 --env-file <abspath> --no-deps` and never
  `--remove-orphans`.

---

## 6. Reporting back

When this handoff is worked, the expected artifact is: a PR (or a set of
PRs, one per major section above) against `trim/phase-a-deletions`, each
with its own test evidence (paste the `pytest -q` / `php artisan test
--compact` tail, not just "tests pass"). Flag anything from §1e/§1f/§4's
DB-table question that requires a real decision from Kyle rather than
silently picking an answer — this repo's history has multiple examples
(see `docs/` for the CC-01 Item 2 spatial-uncertainty saga) of an agent
fabricating an "approved" rubric under time pressure instead of escalating
a genuine ambiguity, and it cost real cleanup time later. Don't repeat that
pattern here.
