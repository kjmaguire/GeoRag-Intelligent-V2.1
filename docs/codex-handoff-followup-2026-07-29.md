# Codex handoff, round 2 — review the new OCR code, finish the rest

**Prepared:** 2026-07-29, by Claude, following up on
`docs/codex-handoff-ocr-and-cleanup-2026-07-29.md`.
**Intended runner:** same as round 1 — Codex 5.6 "Sol", Medium reasoning, run
to exhaust the remaining ~50% of the allotted credit budget.

**Repo / branch:** `trim/phase-a-deletions`, worktree
`C:\Users\GeoRAG\Herd\georag-trim`. Same rules as round 1 — do not touch
`C:\Users\GeoRAG\Herd\georag` (the live stack, `georagintelligencev10`).

**Where round 1 left off:** commit `db6b1ed` ("feat(ocr): complete tiled OCR
and review routing") landed all of round 1's §1 (tiling, confidence scoring,
review-queue routing) plus real pieces of §2 (dependency bumps:
`pikepdf>=10.10`, `pdfminer.six>=20260107`, `pypdf>=6.14.2`,
`azure-ai-documentintelligence>=1.0.2`, explicit `phpstan`/`larastan` pins,
all three lockfiles regenerated) and §3 (`phpstan-baseline.neon` shrank by
216 lines — real fixes, not just suppression). Verified: full FastAPI suite
now at **2499 passed, 0 failed, 25 skipped** (previously 1 known failure;
Codex's edit to `test_document_intelligence_client.py` fixed it too). Task
tracker's #28 ("Swap OCR to Azure Document Intelligence") is now genuinely
closed — the live per-page and whole-document OCR paths use Azure DI first,
tile oversized rasters below the 10,000px limit, and fall back to Tesseract
last-resort, per the updated docstring in `src/fastapi/pyproject.toml`.

New files from round 1 to build context from before doing anything below:
`src/fastapi/app/services/ingest/image_tiling.py` (313 lines),
`src/fastapi/app/services/ingest/ocr_quality.py` (286 lines), plus the
`app/models/review_queue.py` / `app/services/review_lineage_lookup.py`
wiring and changes to `pdf_report.py` / `hatchet_workflows/ingest_pdf.py`.

**Test recipe** — unchanged from round 1:

```bash
MSYS_NO_PATHCONV=1 docker run --rm --network georag \
  -v "//c/Users/GeoRAG/Herd/georag-trim/src/fastapi:/app" -w /app \
  -u 33:33 -e HOME=/tmp -e XDG_CACHE_HOME=/tmp/xdg_cache -e HF_HOME=/tmp/hf_cache \
  --env-file "C:/Users/GeoRAG/Herd/georag/.env" \
  -e POSTGRES_HOST=postgresql -e POSTGRES_DIRECT_HOST=postgresql \
  georag/fastapi:latest python -m pytest -q
```

Current baseline: **2499 passed, 25 skipped, 243 deselected, 0 failed.**
Any new failure after this handoff's work is a real regression — don't wave
it off as "pre-existing" the way round 1 correctly did with the old Azure-
credentials failure (that one's gone now; there is no more known-bad test).

---

## Priority order

Work top to bottom; each item is independently PR-able. If the budget runs
out partway through, stop at a clean item boundary rather than leaving a
half-finished one uncommitted.

### 1. Adversarial review of the new OCR code (highest priority)

`image_tiling.py`, `ocr_quality.py`, and the `pdf_report.py`/`ingest_pdf.py`
wiring are brand-new, sit directly in the ingestion path, and have never
processed production traffic. Round 1 added unit tests
(`test_image_tiling.py`, `test_ocr_quality.py`,
`test_ingest_pdf_ocr_review.py`) but unit tests written by the same pass
that wrote the code are not an independent check. Do a real adversarial
read, specifically hunting for:

- **Tiling correctness.** Off-by-one errors in tile-offset math when
  remapping OCR polygons back to original coordinates. Seam-overlap dedup
  that double-counts or drops text at a tile boundary. What happens to a
  page whose height is an exact multiple of the tile size (edge case: zero-
  height final tile)? What happens to a 1px-wider-than-limit image (barely
  over threshold — does it tile at all, or silently pass through and hit
  Azure's `InvalidContentDimensions`)?
- **Confidence-score correctness.** Read `ocr_quality.py`'s scoring logic
  against the multi-signal spec from round 1's handoff (mean AND median
  confidence, % words below threshold, coverage ratio, empty-output
  detection, seam-duplicate detection, gibberish detection) — confirm all
  of these actually landed and aren't just a renamed single threshold.
  Check the tiered routing bands (catastrophic/mandatory-review/spot-check/
  auto-accept) for sane boundary values, not arbitrary round numbers with
  no justification in a comment.
- **Fail-soft correctness.** Given this session's history (a real bug this
  branch already fixed once: Document Intelligence failing soft internally
  and returning an empty result that the caller accepted as "success"
  because it checked for exceptions, not empty text) — re-verify the same
  failure mode can't reappear anywhere in the new tiling/reconstruction
  path. Specifically: does a tile that fails OCR silently produce an empty
  string that then gets treated as "this region has no text" (wrong) rather
  than triggering the fallback/review-queue path (right)?
- **Resource/memory safety.** `image_tiling.py` presumably loads a
  decompression-bomb-adjacent image (this branch's history includes a real
  incident: Pillow's 178,956,970-pixel default limit breaking on a real
  1940s well-log scan). Confirm any limit override is scoped narrowly
  (per-call, only for known-trusted internal bronze-store bytes) and not a
  blanket `Image.MAX_IMAGE_PIXELS = None` that reopens a real DoS vector for
  any other code path that touches `PIL.Image`.
- **Security.** Any new file-path handling in tiling (temp file writes,
  cleanup on exception) — check for the same patterns already fixed
  elsewhere in this codebase (e.g. the dead `_FIGURE_TEMPDIR_ROOT` cleanup
  removed during the docling pass) don't get reintroduced.

Report findings as a normal review (bug list, not prose) and fix what's
real. If nothing is found, say so explicitly — don't pad the review.

### 2. End-to-end validation against real corpus files

Round 1's own handoff item §1g doesn't look like it happened — the new
tests are unit-level (mocked Azure responses, presumably). If Codex has
access to real Azure Document Intelligence credentials in this environment
(check `.env` for `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`/`_KEY` — they were
provisioned earlier this session) and network access to call them, run the
actual pipeline against a real oversized scan and confirm:
- A `silver.reports` row lands with the correct `parser_used`.
- `silver.document_passages` rows are produced with sane text (spot-check,
  not just "row count > 0").
- A confidence-scored region that should route to review actually produces
  a `silver.review_queue` row (or whatever table `review_queue.py` targets
  — check the model file for the real table name).
- Qdrant `georag_chunks` gets embedded points for the resulting passages.

If there's no real oversized-scan fixture available in this environment,
say so explicitly rather than skipping the item silently — this is exactly
the kind of gap round 1's handoff doc warned against ("no silent caps —
flag what was dropped").

### 3. Hallucination-prevention regression (CLAUDE.md rule 5)

The new confidence scoring in `ocr_quality.py` feeds into text that
eventually reaches the RAG pipeline's numeric-claim verification layer
(§04i Layer 3 — grep `pdf_report.py`/the agent's numeric verifier for how
`extraction_confidence` propagates). Run the golden-query regression suite,
not just the OCR unit tests:

```bash
# inside the same container, after the pytest -q run above
python -m pytest tests/test_golden_queries.py tests/test_golden_query_regression.py tests/test_golden_query_harness.py -q
```

If any of these fail or were already skipped/xfail before this branch,
note the baseline explicitly before touching anything — don't "fix" a
pre-existing skip by deleting it.

### 4. Repo-wide dead-code and orphan sweep (round 1's §3, broadened)

Round 1 only found dead code in the docling/paddleocr blast radius because
that's what it was looking for. Do the same pattern repo-wide:
- Every `.env` flag currently `false`/disabled — grep what it gates, confirm
  zero other callers before concluding the code path is genuinely dead
  (vs. just currently off). Cross-reference against the "do not cut these"
  list in the trim plan (search `docs/` for "Option 2") so you don't flag
  something intentionally kept as if it were an oversight.
- Every route in `src/fastapi/app/main.py` and every Laravel route in
  `routes/` — cross-check the inverse of round 1's finding (an endpoint
  Laravel/frontend calls that no longer exists server-side, not just the
  reverse).
- TODO/FIXME/XXX triage across `src/fastapi/app` (55 markers at last count,
  before this round's changes — recount) — which are real tracked gaps vs.
  stale noise from a finished task.
- The 25 currently-skipped FastAPI tests — audit each skip reason for
  whether it's still valid.
- `find . -name '__pycache__' -newer src/fastapi/pyproject.toml` as a
  detection signal for orphaned `.pyc` files with no matching `.py` source
  — a tell for more partially-finished removals elsewhere in the repo
  (this is exactly how the PaddleOCR leftovers were found in round 1).

### 5. Laravel/frontend dependency currency (round 1's §2, the part not done)

Round 1's dependency work stayed almost entirely on the FastAPI/Python side.
`composer.json`/`package.json` root-level majors were not audited:
`laravel/framework` (`^13.0`), `inertiajs/inertia-laravel` (`^3.0`), `react`
(`^19.0.0`), `@inertiajs/react` (`^3.5.0`), `typescript` (`^6.0.2`), `vite`
(`^8.0.0`), `tailwindcss` (`^4.3.0`). Check for patch/minor bumps within the
existing major-version constraint (safe, low-risk) and separately flag
(don't silently apply) any available major bump — that's a bigger call than
"latest version" and needs a human decision.

### 6. The deferred DB-table decision

`app/models/pdf.py`'s `PdfProvenance.source_method` Literal still lists
`"paddle_ocr"` and `"paddle_structure"` as allowed values even though
nothing can produce them anymore (round 1 removed the whole PaddleOCR
stack). This mirrors a CHECK constraint on 8 §04p `silver.*` tables
(`pdf_layout_regions`, `pdf_ocr_results`, etc. — grep migrations for the
full list). Two options, pick one and act, don't leave it open a third
time:
   (a) confirm those 8 tables are genuinely unused in the live stack (query
       row counts against the live DB per `docs/RUNBOOK.md`'s read-only
       query guidance) and write a real migration dropping them + the dead
       enum values, or
   (b) if there's any uncertainty about whether they're still referenced
       (e.g. by a reporting job, an admin export, anything outside this
       repo's own code), leave them and write down explicitly *why* in a
       comment so the next pass doesn't re-open this question from zero.
Don't fabricate a "confirmed unused" finding under time pressure — this
repo has a real prior incident (see `docs/` for the CC-01 Item 2
spatial-uncertainty saga) of exactly that happening and costing real
cleanup time later.

---

## What NOT to touch

Same list as round 1: the live stack, Neo4j/monitoring/Kestra/Dagster-
services/Martin (intentionally dropped), `laravel-reverb`/`laravel-horizon`/
`hatchet-worker-ai`/the relocated `pdf_report.py` parser (load-bearing).
Multi-clone Docker safety: `-p georagintelligencev10 --env-file <abspath>
--no-deps`, never `--remove-orphans`.

Additionally for this round: don't touch `phpstan-baseline.neon` items
outside what a real fix requires — round 1 already did a legitimate 216-line
reduction; padding that number further with suppressions-in-disguise would
be a step backward, not forward.

## Reporting back

Same as round 1: PR (or PRs) against `trim/phase-a-deletions`, test evidence
pasted in full (not "tests pass"), and any genuine ambiguity surfaced to
Kyle explicitly rather than silently resolved. If §1's review finds nothing
wrong, say so — a clean bill of health is a valid, useful result, don't
manufacture findings to look thorough.
