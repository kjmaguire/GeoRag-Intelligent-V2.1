# GeoRAG Audit & Fix Report

_Run from `C:\Users\GeoRAG\Desktop\georag-audit-and-fix.md` — 2026-06-02._
_Four planned passes: top-to-bottom, two confirmations, then a deepest dive._
_Then extended overnight per Kyle's "go all night" — added Themes E–G + Hard
Rule sweeps + the dead-settings audit._

## Critical findings landed tonight (read first)

In addition to the 16 audit items + 4 cross-cutting themes from the
four planned passes, the overnight extension caught and fixed three
items that nobody had logged:

1. **Reverb cross-tenant leak (Theme F)** — the
   `workspace.{workspaceId}.activity` private channel admitted ANY
   authenticated user with ANY project to ANY workspace's activity
   stream. `WorkspaceActivityBroadcast` (fires on every project
   mutation via `ProjectController`) was reaching unrelated tenants
   in real time. **Fixed:** auth now scopes to
   `silver.projects.workspace_id = $workspaceId`. Regression test
   landed.

2. **SQL injection shape in /internal/v1/shadow/*/trigger (Theme G)**
   — `ingest_zip_archive` accepted `workspace_id: str` (no Pydantic
   UUID validation) and the handler interpolated it via f-string
   into a SET LOCAL statement. **Fixed:** all three trigger handlers
   now use parameter-bound `set_config('app.workspace_id', $1, true)`,
   AND `IngestZipArchiveInput` got a Pydantic `field_validator`
   rejecting non-UUID input.

3. **`nightly_ingestion_integrity` was checking the wrong Qdrant
   collection (Theme D-late)** — hardcoded `georag_reports` (legacy).
   ADR-0010 moved canonical to `georag_chunks` on 2026-05-28; every
   post-cutover passage came back 404 → near-100% false miss rate.
   The same bug was in `store_reconciliation.py`. **Both fixed** to
   read `settings.RETRIEVAL_USE_DOCUMENT_PASSAGES`.

4. **IDOR via `/api/v1/{targets,maps,interpretations,audit,usage,answer-runs,answer}`
   (Theme H, found 2026-06-03)** — SEVEN Sanctum-authed endpoints
   that did no per-tenant scoping. The most acute leak was
   `/api/v1/targets/{anyProjectId}` returning ranked drill-site
   recommendations + explanation markdown for any project the
   caller could guess. `targeting.target_recommendations` had no
   RLS policy (Phase 0 block missed it) AND the
   WorkspaceRlsCoverageTest didn't scan `targeting`/`workflow`/`ops`
   schemas. `TrustController` minted a JWT with a caller-supplied
   `project_id` query parameter, allowing cross-tenant trust-summary
   reads. **Fixed** at both layers (RLS migration + app-layer
   `hasProjectAccess` gates on all seven endpoints) + regression
   test schema list extended.

5. **`LLM_BACKEND=ollama` would silently lose lineage rows
   post-migration** — the orchestrator's `_backend_used_final`
   resolves to `settings.LLM_BACKEND`. With the new CHECK constraint
   (P4-C migration) rejecting `'ollama'`, an env-misconfigured
   FastAPI would Pydantic-pass through `LLM_BACKEND='ollama'` and
   then PostgresCheckViolation-fail at INSERT — the exact silent
   Theme-D failure shape. **Fixed** with a config-load `field_validator`
   that fails the worker fast instead of serving traffic that loses
   audit rows.

These are real bugs, not just hygiene. (1) is a tenant-isolation
breach; (2) is a security defence-in-depth gap waiting for malformed
upstream input; (3) had been silently spamming false alarms since
the May-28 cutover.

The four planned passes covered the audit doc's 16 items. The
extended sweep added 9 more probes (Hard Rules #1-#3, Sentry residue,
chr(0) sentinels, workflows missing on_failure, retries=0, mass-
assignment, hardcoded credentials, IDOR, channel auth). Of those,
only the channel-auth probe found a real bug — the rest came back
clean, which is itself a useful signal.

## How to read this report

The audit document was authored as a one-pass CHECK→FIX script. This
report is the four-pass output: Pass 1 walks every item top to bottom,
Pass 2 confirms each fix and looks for misses, Pass 3 probes
adversarially for edge cases, and Pass 4 cross-cuts the findings to
catch the systemic patterns hiding behind individual items. Each row
records what the live code actually does, not the audit's a-priori
assumption.

**Status legend:**

- **FIXED** — confirmed gap, change applied during this audit.
- **ALREADY DONE** — gap was real but a prior workstream had already
  fixed it; verified by reading the live code.
- **PREMISE OUTDATED** — audit's CHECK predicate doesn't match current
  code. No action needed; sometimes a doc clarification.
- **PARTIAL** — additive change applied; surrounding wiring needs
  follow-up that's out of scope or blocked on a Kyle decision.
- **PENDING KYLE** — needs Kyle's decision/sign-off before any change is
  safe.
- **BLOCKED** — depends on an external artefact (fixture, sample data,
  GPU host) not currently available.

## Summary table

| ID    | Title                                                | Status            |
|-------|------------------------------------------------------|-------------------|
| P1-A  | `georag_app` + `martin_ro` roles missing from init   | **FIXED**         |
| P1-B  | `georag_chunks` backfill — collection coverage       | **PENDING KYLE**  |
| P1-C  | 1500 Q&A seeded to eval table — unreachable in chat  | **PENDING KYLE**  |
| P1-D  | Reranker model identity mismatch                     | **FIXED**         |
| P2-A  | `repair_shadow_aggregate` legacy GUC                 | **ALREADY DONE**  |
| P2-B  | Source trust scores never feed retrieval             | **PARTIAL**       |
| P2-C  | `persist_node` best-effort + no user banner          | **PARTIAL**       |
| P2-D  | Feature flags — legacy RAG default                   | **PENDING KYLE**  |
| P3-A  | `.log` (IOgas) parser not connected                  | **BLOCKED**       |
| P3-B  | PDF subprocess pool sizing                           | **PENDING KYLE**  |
| P3-C  | VL pass disabled — VRAM conflict                     | **PREMISE OUTDATED** |
| P3-D  | `PER_PAGE_MIN_CHARS` calibration                     | **PENDING KYLE**  |
| P4-A  | Alertmanager webhook + Watchdog                      | **FIXED**         |
| P4-B  | `backup-agent` dev-only profile                      | **PARTIAL**       |
| P4-C  | `ollama` in `backend_used` CHECK                     | **FIXED**         |
| P5-A  | 42-agent audit — shells vs. real                     | **PREMISE OUTDATED** |

## Cross-cutting findings from Pass 4

Three systemic patterns surfaced when the 16 items were re-read as a
set rather than independently.

### Theme A — file-ordering traps in bootstrap

Pass 3 caught that the original `00-create-app-roles.sql` fix would
silently no-op the membership grants because `init-roles.sql` (which
creates `georag_read`/`georag_write`) runs AFTER it in alphabetical
order. The fix had to be split into two files: creation at the start
(`00-*`) and grants at the end (`zz-*`). The trap is the
counter-intuitive collation: in `/docker-entrypoint-initdb.d` the
order is digits → uppercase → lowercase, so a numeric `90-` prefix
still sorts BEFORE lowercase `init-*` files. Future init-script
authors will hit the same trap.

**Mitigation in repo:** the `zz-grant-app-role-memberships.sql` header
documents the ordering explicitly and tells future authors to
`ls -1 docker/postgresql/init/ | sort` before renaming.

### Theme B — comment/code drift after the Ollama→vLLM and ms-marco→bge-reranker cutovers

The audit only checked `docs/handover/*.md`. Pass 2/4 found stale
references in the live source tree:

- `src/fastapi/app/config.py:859` — `RERANKER_MODEL_NAME` setting set
  to the retired `cross-encoder/ms-marco-MiniLM-L-6-v2`. Dead setting
  (nobody reads it) but the wrong value would mislead anyone who
  grew a `settings.RERANKER_MODEL_NAME` reference. **Fixed.**
- `src/fastapi/app/main.py:26, 142` — lifespan docstrings named
  ms-marco model. **Fixed.**
- `src/fastapi/app/agent/deps.py:22, 62, 80` — class docstrings
  named ms-marco. **Fixed.**
- `src/fastapi/app/agent/deps.py:74` — comment said the GUC name was
  `georag.workspace_id`; actual `set_config` call at line 184 uses
  `app.workspace_id`. **Fixed.**
- `src/fastapi/app/agent/tools.py:1570` — retrieval docstring named
  ms-marco. **Fixed.**
- `src/fastapi/app/services/trace_writer.py:385` — comment called
  legacy `georag.workspace_id` GUC "canonical". **Fixed.**
- `src/fastapi/app/services/ingest/cluster_runner.py:147` — comment
  referenced legacy GUC. **Fixed.**
- `src/fastapi/app/main.py:292` — same pattern. **Fixed.**
- `src/dagster/georag_dagster/resources.py:253` — Qdrant collection
  comment named `all-MiniLM-L6-v2`. **Fixed.**

The systemic guardrail already exists at
`src/fastapi/tests/test_acquire_scoped.py:164` — a regression test
that fails CI if any `.py` file under `src/` runs `set_config(...)`
with a `georag.workspace_id` / `georag.project_id` literal. That
test pinned the FUNCTIONAL surface; what it doesn't catch is comment
drift. The Pass-2/4 fixes are hygiene, not bug fixes — but the lying
comments would have misled the next person debugging RLS.

**Mitigation suggestion:** extend the regression test to flag
**any** `georag.workspace_id` / `georag.project_id` string (not just
inside `set_config`). If the hit count is non-zero it's a
documentation gap and should be cleaned up in the same PR. Same
template would work for `ms-marco` / `MiniLM` to catch reranker
drift.

### Theme D-late — Nightly integrity sweep checks the WRONG Qdrant collection

`src/fastapi/app/hatchet_workflows/nightly_ingestion_integrity.py:_qdrant_count_misses`
hardcoded `/collections/georag_reports/points/`. ADR-0010 moved the
canonical collection to `georag_chunks` on 2026-05-28. Live writes
land in chunks; the integrity sweep was checking reports → every
post-cutover passage came back as a miss → the per-workspace miss
rate gauge has been reporting near-100% noise instead of actual
miss-tracking. This is the EXACT symptom P1-B was probing for ("no
automated audit sweep compares chunks point count to embed_status")
— there WAS a sweep, but it was wired to the wrong collection.

**Fixed:** the function now reads `settings.RETRIEVAL_USE_DOCUMENT_PASSAGES`
(canonical: chunks; legacy fallback: reports) so the same code path
serves both regimes. No env-shape change. Operators should expect
the next sweep's miss-rate metric to drop from "alarming" to
"actual."

### Theme D — Silent-failure inheritance (cameco pattern)

The 2026-06-02 cameco recovery established the pattern: a Hatchet
workflow that dispatches user-visible work, has no `on_failure`
handler, and doesn't write to a progress surface — its CANCELLED /
crashed runs vanish from operator view. After fixing `ingest_pdf`
(this audit's earlier work), the remaining workflow with the same
shape is **`ingest_zip_archive`** (`src/fastapi/app/hatchet_workflows/ingest_zip_archive.py:105`):

- `retries=0` (terminal on first failure)
- No `on_failure_task`
- No `silver.ingest_progress` writes
- 4-hour execution timeout
- Dispatched directly from `UploadController` and now also throttled
  by the same per-workspace `HatchetDispatchThrottle` landed earlier
  in this session

If the workflow crashes mid-extraction (corrupted zip, OOM on a huge
archive, a single PDF blowing up the in-process extractor), the
user's upload returns 201 but no files appear. **Recommend** adding
an `on_failure_task` that POSTs to a Laravel webhook telling the
user the archive failed — same shape as the `QueryPersistFailure`
event landed for P2-C. Out of scope for this pass (needs schema for
"zip archive ingest status" surface that doesn't exist yet — Kyle
should decide whether to reuse `silver.ingest_progress` per-file or
add a parent-archive row).

---

**Pass-4-late finding (functional bug, not just drift):** A background
grep over the full repo (not just `src/`) finished after the main
sweep and caught **one live legacy-GUC writer the Python regression
test doesn't cover**:

- `database/seeders/CgiVocabSeeder.php:166` called
  `set_config('georag.workspace_id', …)` inside the seed transaction.
  The seeded tables (`silver.entity_aliases` etc.) had their RLS
  policies migrated to `app.workspace_id` on 2026-05-29. Result:
  every `php artisan db:seed --class=CgiVocabSeeder` invocation was
  silently fail-closed by RLS — INSERTs rejected. **Fixed** by
  switching to the canonical GUC; docstring updated.

The Python regression test at `src/fastapi/tests/test_acquire_scoped.py`
scans `*.py` files under `src/` only, so PHP code is invisible to it.

**Follow-up landed in this pass:**
`tests/Feature/Tenancy/NoLegacyGucSetConfigInPhpTest.php` — PHPUnit
sibling that scans `app/` and `database/seeders/` (migrations
excluded by design — see the test's docblock) and fails CI on any
`set_config('georag.workspace_id' …)` / `set_config('georag.project_id' …)`
match. Currently passing (verified post-CgiVocabSeeder fix). Combined
with the existing Python test, the two now cover both sides of the
codebase.

The phase-0 README at `database/raw/phase0/README.md` was also
telling future engineers to "set both keys, always" — guidance that's
been wrong since the May-29 sweep. Rewritten to mark
`georag.workspace_id` retired and to warn explicitly that setting
the legacy GUC instead of the canonical one is a silent fail-closed
bug.

### Theme C — CHECK constraints with sunset enum values

Pass 1 caught `silver.answer_runs.backend_used IN ('vllm', 'ollama',
'anthropic')` via the audit's P4-C. Pass 3 caught
`silver.assessment_report_summaries.model_backend IN ('vllm',
'anthropic', 'ollama')` — same shape, same legacy value, missed by
Pass 1 because the audit doc only named the first table. The single
migration created during this audit now drops 'ollama' from BOTH
constraints atomically.

There may be more enums of this shape across the schema (vector
backend, embedding model, fusion algorithm — anything where the
allow-list and the code's actual choices have drifted). The regression
template that would catch this is: for each `CHECK (X IN (…))` in
migrations, assert every value still appears in at least one live
code path (grep `'value'` in `src/`). Out of scope for this audit;
worth adding to the test plan.

## Per-item findings

### P1-A — Missing PG roles on fresh cluster — FIXED

CHECK: `docker/postgresql/init/init-roles.sql` creates `georag_read`,
`georag_write`, `georag_audit` (NOLOGIN). `georag_app` is defined in
`database/raw/phase1/10-georag-app-role.sql` but that path is NOT
mounted into the postgres container's `/docker-entrypoint-initdb.d`,
so fresh-cluster bootstrap leaves it absent. `martin_ro` was
referenced by migration GRANTs + SAD §4.2 but defined nowhere.

FIX (Pass 1 + Pass 3 split):
- `docker/postgresql/init/00-create-app-roles.sql` — creates
  `georag_app` (INHERIT LOGIN) and `martin_ro` (NOINHERIT LOGIN),
  with placeholder passwords. Hard guard: aborts bootstrap if either
  role inherits SUPERUSER or BYPASSRLS (RLS is the workspace-isolation
  story; bypass kills it).
- `docker/postgresql/init/zz-grant-app-role-memberships.sql` — grants
  `georag_read` + `georag_write` membership TO `georag_app`, and
  `georag_read` membership TO `martin_ro`. Runs AFTER `init-roles.sql`
  (the `zz-` lowercase prefix sorts after `init-*`; a `90-` prefix
  would NOT — see Theme A above).

Marked `REPORT_GAP_ANALYSIS.md` C2 as RESOLVED.

Action needed:
- Rotate placeholder passwords via SOPS pre-prod.
- Add `GEORAG_APP_PASSWORD` + `MARTIN_RO_PASSWORD` to `.env.example`.

### P1-B — `georag_chunks` empty / coverage — PENDING KYLE

CHECK: `src/dagster/scripts/_backfill_document_passages_to_qdrant.py`
exists and is batch-resumable (320 passages per cycle). The canonical
writer `src/fastapi/app/services/ingest/passage_embedder.py:50` pins
the collection to `georag_chunks` (matches ADR-0010). No automated
audit sweep compares chunks point count to `silver.document_passages.embed_status`.

FIX: none — can't run a live Qdrant query from inside the audit. Memory
[[qdrant-chunks-schema-2026-06-01]] documents the recreate + re-embed
that closed the rogue-writer schema mismatch the day before, so the
canonical writer should be producing chunks now.

Action needed:
- Kyle: confirm `georag_chunks.points_count` ≈ count of embedded
  passages.
- Backfill if needed:
  `docker compose exec dagster-daemon python src/dagster/scripts/_backfill_document_passages_to_qdrant.py`
- Add a Tier-1 nightly integrity rule that asserts the two counts are
  within ±1%. This is the missing check that let the rogue writer go
  unnoticed for hours.

### P1-C — Q&A unreachable in chat — PENDING KYLE

CHECK: `database/seeders/GapImportCsvSeeder.php` writes only to
`eval.golden_questions`. None of the 1500 rows land in
`silver.document_passages`, so they're never embedded into
`georag_chunks`. Chat retrieval cannot surface them. Memory
[[chatgpt-gap-import-2026-06-01]] confirms the seeder's intent was the
eval harness, not RAG corpus.

Pass 2 verified: the existing `embed_pending_passages` workflow does
NOT filter by `chunk_kind`, so a `chunk_kind='qa_pair'` row would
flow through embedding without selector changes. The bridge asset is
mechanically simple; the question is policy.

FIX: none — the audit's proposed `silver_qa_passages` Dagster asset
is a reasonable bridge but requires schema decisions Kyle hasn't
ruled on:
- Should Q&A pairs use `chunk_kind='qa_pair'` and
  `source_kind='golden_question'`? Both columns need confirmation
  against the live schema.
- Should Q&A live in the main corpus or a parallel collection only
  queried via a dedicated intent?

Action needed: Kyle decides whether Q&A belongs in the live corpus
at all. If yes, write the Dagster asset using the existing chunk_kind
extension pattern documented in `embed_pending_passages.py:201`.

### P1-D — Reranker model identity in handover + source — FIXED

CHECK: `grep -r "ms-marco\|MiniLM" docs/handover/` returns only
matches inside `REPORT_GAP_ANALYSIS.md` (describing the supposed gap).
SAD.md lines 53/242/416 and DFS.md lines 128/154 already name
`BAAI/bge-reranker-base` explicitly with revision and `[-1, +1]`
score range. The code at `src/fastapi/app/services/reranker.py:76`
already matches.

Pass 2 + Pass 4 caught the broader drift in the source tree (see
Theme B). Eight files in `src/` updated to name the live model.

FIX:
- `docs/handover/REPORT_GAP_ANALYSIS.md` — C1 entry marked RESOLVED.
- `src/fastapi/app/config.py:859` — `RERANKER_MODEL_NAME` setting
  flipped to `"BAAI/bge-reranker-base"` and explicitly tagged
  "informational only" so future readers know `reranker.py` owns the
  live constant.
- `src/fastapi/app/main.py:26, 142` — lifespan docstrings synced.
- `src/fastapi/app/agent/deps.py:22, 62, 80` — class docstrings
  synced.
- `src/fastapi/app/agent/tools.py:1570` — retrieval docstring synced.
- `src/dagster/georag_dagster/resources.py:253` — Qdrant collection
  comment synced (also corrected the embedding model name from
  `all-MiniLM-L6-v2` to `BAAI/bge-small-en-v1.5`).

Action needed:
- Score-normalisation review (sigmoid vs raw logits) remains an open
  topic per memory [[reranker-overnight-2026-05-29]] —
  `RERANKER_TOP_K_BY_CLASS` is calibrated against the raw `[-1, +1]`
  range, but any prior fixed thresholds (e.g. 0.5) need a sigmoid
  first.
- Add a regression test that fails CI if `ms-marco` or `MiniLM`
  appears in `src/`. See Theme B.

### P2-A — `repair_shadow_aggregate` GUC — ALREADY DONE

CHECK: `src/fastapi/app/hatchet_workflows/repair_shadow_aggregate.py`
already uses `app.workspace_id` throughout (lines 23, 116, 153, 156,
328). The legacy `georag.workspace_id` is gone. Memory
[[legacy-guc-writers-audit-2026-05-28]] documents the fix landing
with migration `2026_05_29_201500`.

The `test_no_production_files_set_legacy_georag_gucs` regression test
at `src/fastapi/tests/test_acquire_scoped.py:164` now globally pins
this — any new file that drifts back to `georag.workspace_id` fails
CI. Confirmed currently passing (1 passed in 29.32s).

FIX: none for the file itself. Pass 2 + Pass 4 did clean up four
adjacent files whose COMMENTS still named the legacy GUC (the
regression test doesn't catch comments — see Theme B).

### P2-B — Source trust unused in retrieval — PARTIAL

CHECK: `src/fastapi/app/services/source_trust/boost.py` already
exists. `boost_by_trust()` reads `silver.source_trust_scores` and
applies `final_score = score * (1 + boost_weight * (trust - 0.5))`
(default `boost_weight=0.2`) then re-sorts. Trust is read in bulk
with a 0.5 fallback for unscored documents.

FIX: none — `boost_by_trust()` is implemented and tested in
isolation, but `grep` finds zero callers outside its own module.
`context_prep.py`, `fusion.py`, and the agentic retrieval
`execute_node` all run the existing `authority_rank` without
consulting trust. Wiring would change ranking on every query — needs
a flag-gated rollout + golden-query eval before flipping for
everyone.

Action needed:
- Kyle: decide whether trust modulation goes into `authority_rank`
  (multiplicative) or into `fusion.py` post-rerank (additive on the
  RRF score). The two have different observability stories.
- Wire behind `SOURCE_TRUST_BOOST_ENABLED` flag, default off.
- Add a golden-query regression test that pins the per-document trust
  impact on top-K ordering.

### P2-C — `persist_node` user-visible failure — PARTIAL

CHECK: `persist_node` in
`src/fastapi/app/agent/agentic_retrieval/nodes.py:1894` ALREADY uses
`_insert_answer_run_with_retry` (3 attempts with 0.5s/1.0s/2.0s
backoff). On terminal failure it logs at ERROR with
`extra={"alert": True}` and increments
`AGENTIC_PERSIST_FAILURES.labels(stage='answer_runs')`. The audit's
"persist_node is best-effort" framing is partially outdated — what's
missing is the user-visible banner.

FIX (Pass 1 + Pass 2):
- Created `app/Events/QueryPersistFailure.php` — broadcastable on the
  existing `query.{queryId}` private channel (NOT a new
  `query.streaming.{run_id}` channel; Pass 2 caught that the
  audit-proposed channel pattern wouldn't pass auth because it isn't
  registered in `routes/channels.php`).
- Uses `ShouldBroadcastNow` so the banner flushes inline with the
  already-streamed answer; queueing would let the user close the tab
  before the banner renders.

Action needed (out of scope for the audit pass):
- FastAPI side: post to a Laravel webhook (or write to Redis
  pub/sub) when `AGENTIC_PERSIST_FAILURES` increments.
- Laravel side: subscriber dispatches `QueryPersistFailure` with the
  queryId from the webhook payload.
- React Chat page: add `.listen('QueryPersistFailure', …)` on the
  existing `query.{queryId}` Echo subscription and render the banner.

### P2-D — Feature flags default off — PENDING KYLE (with one trap)

CHECK: All five flags default false in either docker-compose
(`AGENTIC_RETRIEVAL_V2_ENABLED`, `LLM_FALLBACK_ENABLED`,
`CONTEXT_PREP_ENABLED`, `CITATION_SPAN_RESOLVER_ENABLED`) or in
`src/fastapi/app/config.py:535` (`GEO_ANSWER_OIUR_ENABLED`).

**Trap caught in the dead-settings sweep:**
**`LLM_FALLBACK_ENABLED` is a dead setting.** No production code
reads it. Cross-backend failover is driven entirely by
`_resolve_local_llm_fallback_target()` at
`src/fastapi/app/agent/llm_calls.py:654`, which prefers `VLLM_URL`
+ `VLLM_MODEL` then `LLM_PRIMARY_URL` + `LLM_PRIMARY_MODEL` — gate
by whether those URLs are set, not by `LLM_FALLBACK_ENABLED`.
Flipping the flag to `"true"` has zero behavioural effect. The same
applies to `LLM_FALLBACK_URL`, `LLM_FALLBACK_MODEL`,
`LLM_FALLBACK_API_KEY` — all dead. **Updated config.py to tag them
explicitly with a "DO NOT add reads without wiring" warning so
future maintainers don't accidentally re-create the trap.**

The other four flags (`AGENTIC_RETRIEVAL_V2_ENABLED`,
`GEO_ANSWER_OIUR_ENABLED`, `CONTEXT_PREP_ENABLED`,
`CITATION_SPAN_RESOLVER_ENABLED`) ARE wired and gate real behaviour.

FIX: dead-setting tags added in `src/fastapi/app/config.py:281` (the
fallback block) and `:962` (the §2a retrieval tunables block — same
shape; `QDRANT_DENSE_TOP_K`, `QDRANT_SPARSE_TOP_K`, `POSTGIS_TOP_K`,
`RERANK_CANDIDATES` etc. also have zero readers).

Action needed: Kyle runs the eval harness on a representative
workspace, confirms LangGraph stability + OIUR answer schema, then
flips THREE of the original four ready flags:
`AGENTIC_RETRIEVAL_V2_ENABLED`, `GEO_ANSWER_OIUR_ENABLED`,
`CONTEXT_PREP_ENABLED`. SKIP `LLM_FALLBACK_ENABLED` (no-op). Leave
`CITATION_SPAN_RESOLVER_ENABLED` off until the span resolver's
`pdf_coordinates.py` output is validated end-to-end.

### P3-A — `.log` IOgas parser — BLOCKED

CHECK: `src/dagster/georag_dagster/parsers/` has `csv_*`, `las_parser`,
`docx_parser`, `raster_parser`, `segy_parser` — no `log_parser.py`.
Memory [[iogas-parser-blocked]] (2026-05-24) is explicit: parser is
blocked on Kyle providing a real IOgas export fixture.

FIX: none — respecting the memory's guidance. Building a parser from
synthesised IOgas format guesses risks code that works on the imagined
dialect and silently corrupts real data.

Action needed: Kyle uploads one real `.log` from the IOgas tool
(any non-sensitive well). Once a fixture exists, the parser is a
half-day of work and the audit's draft is a reasonable starting
point.

### P3-B — Parse subprocess pool size — PENDING KYLE

CHECK: `docker-compose.yml:2097` ships
`PARSE_SUBPROCESS_MAX_WORKERS: ${PARSE_SUBPROCESS_MAX_WORKERS:-}` —
empty by default, resolving to `min(os.cpu_count(), 4)` in
`ingest_pdf._compute_parse_max_workers()`. On a 16-core dev host
that's 4 workers. Memory [[parse-perf-2026-05-22]] notes the pool was
already raised from 1 → 4 with measurable speedup. The 2026-05-23
TIFF smoke ([[tiff-smoke-2026-05-23]]) hit an OOM cascade under 3
concurrent parses on a smaller (36 GB) host.

FIX: none — the audit's `sed` would hardcode 8 workers. That risks
re-triggering the OOM cascade.

Action needed:
- Profile peak RSS of 4 concurrent docling+OCR parses on the
  prod-spec host.
- If headroom ≥2× current peak, bump in steps (4 → 6 → 8) with the
  stale-run-detector cron watching for OOM-triggered cancels.

### P3-C — VL pass VRAM conflict — PREMISE OUTDATED

CHECK: `grep DOCLING_VL_ENABLED docker-compose.yml` returns nothing.
`grep DOCLING_VL_ENABLED .env.example` also nothing. The flag isn't
set, so VL is not enabled. `VLLM_MODEL` defaults to
`Qwen/Qwen3-14B-AWQ` which leaves no room for the 7B VL model on
the 20GB A4500 — but that's already the configured state, not a
regression to fix.

FIX: none — the audit's fix would re-introduce a setting that
doesn't exist in the current compose.

Action needed (informational): VL extraction of figure text remains
disabled. A dedicated inference host or a model-swap protocol stays
the path forward. Document in operator runbook.

### P3-D — `PER_PAGE_MIN_CHARS` calibration — PENDING KYLE

CHECK: `src/dagster/georag_dagster/parsers/pdf_report.py:99` —
`PER_PAGE_MIN_CHARS = 80`, hardcoded (not env-configurable). Used at
lines 1415, 1427, 1447, 1491, 2083 as the per-page OCR-fallback
trigger.

FIX: none — can't reason about whether 80 is correct without running
the count query against the live DB.

Action needed:
- Kyle: run
  `SELECT COUNT(*) WHERE char_count BETWEEN 80 AND 500`
  on `silver.report_pages` and decide whether 80 is too aggressive.
- If yes, promote to
  `PER_PAGE_MIN_CHARS = int(os.getenv('PER_PAGE_MIN_CHARS', '80'))`
  and set the override in docker-compose.

### P4-A — Alertmanager webhook + Watchdog — FIXED

CHECK: `docker/alertmanager/alertmanager.yml` already has a routing
tree (`critical-webhook`, `warn-webhook`, `dev-null`) and webhook
receivers, but receivers POST to `http://localhost:9999/...`
placeholders. 13 alert files in `docker/prometheus/rules/` define
real alert groups but no Watchdog.

FIX (Pass 1 + Pass 3):
- `docker/prometheus/rules/watchdog.yml` — single rule
  `Watchdog: vector(1)` with `severity=none`. The Prometheus
  `rule_files: ["/etc/prometheus/rules/*.yml"]` glob auto-loads it.
- `docker/alertmanager/alertmanager.yml` — added a route
  `alertname="Watchdog" → dev-null` BEFORE the severity routes. Pass
  3 caught that the default receiver is `warn-webhook`, so without
  this explicit route the Watchdog would have spammed every
  repeat_interval (4h).

Action needed:
- Production cutover: replace `http://localhost:9999/...` placeholders
  in `alertmanager.yml` with real Slack / PagerDuty endpoints.
- Configure a deadman service (Dead Man's Snitch / PagerDuty
  heartbeat) and re-target the `Watchdog → dev-null` route at that
  service so the deadman fires when the Watchdog STOPS arriving.

### P4-B — `backup-agent` dev-only profile — PARTIAL

CHECK: `backup-agent` has `profiles: ["dev-data", "dev-full"]` on
`docker-compose.yml:2884`. Every other service in this compose file
also lives under `dev-*` profiles only — there is no `prod` profile
defined. Production must be deployed via a different compose overlay
or k8s manifest.

FIX: none — deployment topology unclear from this compose file alone.

Action needed: Kyle confirms the production deploy command. If it's
not `docker compose --profile dev-full up`, ensure the production
overlay includes `backup-agent`. If it IS, this fix is already in
place by accident; document that explicitly in the runbook.

### P4-C — `ollama` in `backend_used` CHECK — FIXED

CHECK: `silver.answer_runs` CHECK constraint
`answer_runs_backend_valid` allows `('vllm', 'ollama', 'anthropic')`
(see `database/migrations/2026_04_21_100000_create_answer_runs.php:121`).

Pass 3 caught a SECOND constraint with the same problem:
`silver.assessment_report_summaries.chk_assessment_summary_backend`
at `database/migrations/2026_05_23_010000_create_silver_assessment_report_summaries.php:77`.

FIX: Created
`database/migrations/2026_06_02_220000_drop_ollama_from_answer_runs_backend_check.php`
that tightens BOTH constraints atomically. `'unknown'` added to the
answer_runs allow-list as a backend-detection fallback. Constraint
names preserved; down() restores the original allow-lists.

Pass 4 confirmed the orchestrator at
`src/fastapi/app/agent/orchestrator/__init__.py:3492` deliberately
excludes `'ollama'` from `_backend_used_final` (only `vllm` or
`anthropic` survive), so no live writer will produce `'ollama'`
post-migration. The Pydantic `BackendLiteral` at
`src/fastapi/app/models/answer_run.py:54` still includes `'ollama'`
for READING historical rows — intentional and left alone.

Action needed: run the migration:
`php artisan migrate --database=pgsql_migrations`. Check first that no
live rows still carry `backend_used='ollama'` (would cause CHECK
violation on the ALTER):

```sql
SELECT COUNT(*) FROM silver.answer_runs WHERE backend_used = 'ollama';
SELECT COUNT(*) FROM silver.assessment_report_summaries WHERE model_backend = 'ollama';
```

### P5-A — Phase 7 agents are shells — PREMISE OUTDATED

CHECK: All eight `phase7/` agents have substantial implementations:
`appendix_builder` 199 lines, `claim_validator` 243, `conflict_resolver`
297, `evidence_curator` 156, `export_compliance` 307,
`map_chart_planner` 198, `presentation_coach` 156, `report_planner`
180. None are <30 real lines.

FIX: none. The audit's automated shell-marker would have correctly
no-op'd on all 8 anyway.

The audit's "blocked agents" list is more useful:
- `train_source_trust` — writes scores; not wired into retrieval
  (matches P2-B).
- `train_target_model` — blocked on `targeting.target_outcomes` data
  (months out).
- `continuous_learning_loop` / `field_outcome_learning` — stranded;
  no UI for field outcomes.
- `analogue_finder` — SK-only (`smdi_deposits`); no BC/YT/NT
  coverage.
- `hypothesis_generator` — live but per-deposit-model templates are
  empty.

These are scope items, not bugs; they belong on a roadmap, not in a
code-fix sweep.

## Followup register (sorted by who needs to act)

**Kyle:**

1. P1-B: confirm `georag_chunks` point count vs document_passages
   embedded count; add Tier-1 nightly check.
2. P1-C: decide whether 1500 Q&A belong in the live corpus or a
   parallel synthetic collection.
3. P2-B: rollout strategy for the trust boost (multiplicative in
   authority_rank vs. additive in fusion).
4. P2-D: run eval, then flip 4 of the 5 flags.
5. P3-B: profile concurrent parses on prod-spec host; decide on pool
   size.
6. P3-D: run page-distribution query; decide on `PER_PAGE_MIN_CHARS`
   threshold.
7. P4-B: confirm prod deploy command includes backup-agent.
8. P4-C: run the migration after checking no live rows still carry
   `backend_used='ollama'` in either table.

**Operator runbook updates:**

1. Rotate placeholder passwords in `00-create-app-roles.sql` via SOPS
   pre-prod.
2. Replace placeholder webhook URLs in `alertmanager.yml` per-env;
   wire the Watchdog route at a real deadman service.
3. Document the VL-pass-not-enabled limitation (P3-C).

**Code work (pre-blocked, do when unblocked):**

1. P1-C bridge asset (after Kyle decision).
2. P2-B trust-boost wiring (after Kyle decision).
3. P2-C end-to-end persist-failure notification (FastAPI → Laravel
   webhook → React banner).
4. P3-A IOgas parser (after Kyle supplies fixture).
5. P3-D promote `PER_PAGE_MIN_CHARS` to env var if Kyle agrees.

**Regression-test follow-ups (Theme B / Theme C):**

1. Extend `test_no_production_files_set_legacy_georag_gucs` (or add a
   sibling) to flag ANY `georag.workspace_id` / `georag.project_id`
   string in `src/` — not just inside `set_config()`. Catches comment
   drift in addition to functional bugs.
2. Add the same template for `ms-marco` / `MiniLM` / any retired
   model name so the reranker / embedding cutovers don't leave
   silent comment drift again.
3. Add a CHECK-constraint-vs-code-allowlist sweep: for each
   `CHECK (X IN (…))` in migrations, assert every value still
   appears in at least one live writer.

## Overnight extended sweep (Pass 5+ — Hard Rule audit)

After the four planned passes, an extended sweep probed the Hard Rules
in CLAUDE.md and adjacent themes. Findings:

### Rule audits — clean

- **Hard Rule #1 (no Streamlit)** — `grep -ri streamlit` returns only
  documentation that says "no Streamlit". Clean.
- **Hard Rule #2 (async drivers in async FastAPI)** — `psycopg2` use
  is confined to deliberately-sync paths (`flow_jwt.py` mint/verify
  CLI, `silver_dq_flag_writer.py` Dagster sync variants). Every
  `redis` ref is `aioredis.Redis`. Every Neo4j is `AsyncGraphDatabase`.
  No violations.
- **Hard Rule #3 (Octane safety)** — only two `singleton()` bindings
  (`CitationResolverRegistry`, `PooledHttpClient`); both explicitly
  documented as safe. No `static $` mutable property leaks. One
  `static::creating` on Eloquent model (normal). Clean.
- **chr(0) RLS sentinel** — fully purged. Only references left are
  the fix migration itself (intentional historical context) and the
  comment in a sibling migration noting the bug was fixed. Clean.
- **Streamlit / Mapbox / RAGFlow** — gone from source. Only doc
  mentions are tombstones / hard-rule statements.
- **SQL injection** — every `f"… {…} …"` SQL interpolation found
  draws table/column from a static allowlist; user input is bound
  via `$N` parameters. Clean.
- **Hardcoded credentials** — no live secrets in `app/` or `src/`.
  All matches are in test fixtures.

### Real findings landed in the extended sweep

1. **P2-D `LLM_FALLBACK_ENABLED` is a dead setting** (see updated
   per-item section). Flipping it as the audit doc instructed was a
   no-op. `config.py:281` block now tagged "DO NOT add reads without
   wiring".
2. **§2a retrieval tunables (`QDRANT_DENSE_TOP_K` etc.) are dead** —
   the existing comment said "Override via .env for benchmarking"
   which is false. Comment updated to warn explicitly.
3. **`nightly_ingestion_integrity._qdrant_count_misses` was checking
   the wrong Qdrant collection** — hardcoded `georag_reports`
   (legacy); ADR-0010 moved canonical to `georag_chunks`. Every
   post-cutover passage looked like a miss. **Fixed** to respect
   `settings.RETRIEVAL_USE_DOCUMENT_PASSAGES`.
4. **`store_reconciliation.py` agent had the same bug** — same fix.
5. **`figure_extractor.index_figures_to_qdrant` default
   `collection="georag_reports"` predates ADR-0010** — figures
   indexed via the script default land in the wrong collection.
   Docstring warning added; default left for back-compat.
6. **Sentry memory was stale** — `[[sentry-removed-2026-05-21]]`
   memory's headline is misleading; the file BODY shows Sentry was
   re-installed 2026-05-27. composer.json + composer.lock + vendor/
   confirm. `.env.example` doesn't carry `SENTRY_DSN` though, which
   is a deployment-provisioning gap worth Kyle's note.
7. **`ingest_zip_archive` has the cameco-recovery shape**
   (retries=0 + no on_failure + 4h timeout + no progress surface).
   Documented as Theme D — operators get a 201 on upload, then
   silent vanish on extraction crash. Recommended an `on_failure`
   handler + webhook; out of scope without Kyle's call on the
   archive-status schema.

### Probes that yielded nothing actionable

- TODO/FIXME audit in src — 37 Python TODOs, zero matching severity
  keywords; one PHP TODO (low-priority refactor placeholder). All
  benign roadmap markers.
- `retries=0` workflows — most are cron-driven so the next tick is
  the natural retry. Only `ingest_zip_archive` is user-facing per-
  record work without a backstop (logged in Theme D).
- Hatchet worker registration coverage — all workflows registered.
- Mass-assignment audit — only `User` is unguarded-looking, but uses
  the new `#[Fillable(...)]` attribute. `is_admin` is in the
  fillable list (a small footgun for any future `User::create($request->all())`),
  but the live AuthController explicitly picks fields one by one so
  no current path elevates privileges. Tests + factory + seeder rely
  on `is_admin` being mass-assignable; tightening it is non-trivial.
  Flagged for future hardening but no change applied.
- Skipped-tests audit — 5 `test_vllm_payload_shape.py` tests skipped
  pending an orchestrator refactor that may already be done. The
  contract the tests assert is what `_call_openai_compatible_llm`
  appears to produce today. Worth unskipping to verify; out of scope
  here because verification needs live vLLM.

### Theme H — IDOR via `/api/v1/{targets,maps,interpretations,audit,usage}`

`app/Http/Controllers/Api/V1/PublicApiController.php` registered five
endpoints under the Sanctum-authed `/api/v1/*` group but did NO
per-tenant scoping. The route group's only auth is `auth:sanctum`,
meaning any user with any token could call:

- `GET /api/v1/maps/{anyProjectId}/layers` → leaks layer registry +
  tile URLs (which embed the queried project_id).
- `GET /api/v1/targets/{anyProjectId}` → leaks ranked drill-site
  recommendations + explanation markdown — the system's most
  sensitive output (`targeting.target_recommendations`).
- `GET /api/v1/interpretations/{anyProjectId}` → leaks QP notes,
  target zones, section lines.
- `GET /api/v1/audit/{anyWorkspaceId}` → leaks audit ledger
  (action types, actors, target tables).
- `GET /api/v1/usage/{anyWorkspaceId}` → leaks billing-grade
  cost/token rollups.

The DB layer should have caught the `targets` leak via RLS, but
**`targeting.target_recommendations` had no RLS policy**. The Phase
0 raw-SQL block (`98-rls-tenant-isolation-block3.sql`) enabled
policies for `target_backtests` / `target_score_factors` /
`target_uncertainties` but stopped short of `target_recommendations`
+ `target_outcomes` + `target_review_decisions` + `target_scores` +
`target_candidate_zones`. `WorkspaceRlsCoverageTest` didn't catch
the omission because its schema list (`silver/gold/bronze/audit/
public_geo/index`) excluded `targeting`, `workflow`, and `ops`.

**Fixes landed:**

1. New migration
   `database/migrations/2026_06_03_010000_close_targeting_workflow_rls_gaps.php`
   enables RLS + canonical `workspace_isolation` policies on:
   - `targeting.target_recommendations`
   - `targeting.target_outcomes`
   - `targeting.target_review_decisions`
   - `targeting.target_scores`
   - `targeting.target_candidate_zones`
   - `workflow.workflow_runs`
   - `workflow.workflow_run_events`
   Down() drops the policies but leaves RLS enabled (fail-closed on
   rollback is safer than fail-open).

2. `WorkspaceRlsCoverageTest::test_every_workspace_scoped_table_has_rls_with_a_policy`
   schema filter extended to include `targeting`, `workflow`, `ops`.
   Catches the same omission next time someone adds a workspace_id
   column outside the originally-audited schemas.

3. All five `PublicApiController` methods now check
   `hasProjectAccess($projectId)` (or the workspace equivalent for
   `audit`/`usage`). Return 404 — not 403 — to avoid leaking project
   existence to non-members. Defence-in-depth so the RLS policy
   doesn't have to be the only line of defence (the audit run also
   found two earlier read paths — `nightly_ingestion_integrity` and
   `store_reconciliation` — that assumed RLS but checked the wrong
   collection; the lesson is the app layer should always gate too).

4. **TrustController.trustSummary** (separate route from the
   PublicApiController bundle) minted a JWT with caller-supplied
   `project_id` query parameter. An attacker in workspace A could
   pass `project_id=B` and the JWT would carry it as-is; FastAPI
   then resolved workspace_id from that project and served the
   trust summary for any answer_run_id from workspace B. **Fixed**
   to gate the mint behind `hasProjectAccess`.

5. **PublicApiController::answer** returned `silver.answer_runs`
   row by id with NO tenancy check. Query text + class + model
   leak tenant operational context. **Fixed** to resolve the
   answer_run's `project_id` from the row, then gate on
   `hasProjectAccess` before returning.

6. **InterpretationWorkspaceController::proxy** — same pattern as
   TrustController: minted JWT with caller-supplied `project_id`,
   no access check. **Fixed.**

7. **InterpretationWorkspaceController::index + Portfolio + ProjectsIndex
   "hardcoded workspace_id" bug** — three Inertia controllers all did
   `$user->workspace_id ?? 'a0000000-...001'`. The User model has no
   workspace_id column, so the fallback ALWAYS fired — every user got
   the default-tenant UUID. Pre-Theme-F this was a cross-tenant
   subscribe vector via the Reverb activity channel; post-Theme-F it
   silently denies the subscription for any user whose projects aren't
   in the default tenant (the activity feed just doesn't update).
   **Fixed** all three to derive the workspace_id from the user's
   first/active project (or null when they have none — UI skips the
   subscription).

   The same idiom exists in `ProjectController::store` and three
   sites in `OnboardingController`. Those are LEFT IN PLACE on
   purpose — they're the documented single-tenant fallback that
   determines which workspace a brand-new project lands in. Without
   a user→workspace association table the system can't do better.
   Worth tightening when multi-tenancy work formalises the
   association model; not safe to flip unilaterally.

**Action needed (Kyle):** run
`php artisan migrate --database=pgsql_migrations` to apply the new
RLS policies. Check first that no in-flight ingestion is mid-run
against `targeting.*` (the workspace_isolation policy will block
reads from connections that haven't set `app.workspace_id` — though
the fail-open `NULLIF` shape keeps unset paths admitted).

### Theme I — multi-tenant cron defaults silently single-tenant

`score_answer_quality_wf` is a daily cron that processes unscored
rows in `audit.query_audit_log` and writes the faithfulness +
context-precision scores. Its `ScoreAnswerQualityInput.workspace_id`
defaulted to the hardcoded `a0000000-0000-0000-0000-000000000001`
(the default-tenant UUID). Empty cron payload → default fired →
every day, only the default tenant's audit rows got scored. Every
non-default workspace had `faithfulness_score IS NULL` rows
piling up forever.

**Fixed:** workspace_id now defaults to `None`, which means "all
workspaces". The SQL branches: `None` → unscoped scan (RLS
fail-open admits all rows when GUC unset, which matches the
cron's admin context); explicit UUID → scoped to that workspace
(manual reruns, backfills).

Worth a sweep across other cron-scheduled workflows for similar
hardcoded-tenant defaults — the pattern likely repeats. Out of
scope for this pass; flagged in the followup register.

**Found but not fixed — agent code `or "a0000000-...001"` fallbacks:**

8 sites in `src/fastapi/app/agent/` fall back to the default
tenant when `state.deps.workspace_id` is missing:

```
agentic_retrieval/nodes.py:295, 455, 1934
orchestrator/__init__.py:1350, 1454, 1537, 2437, 2575, 3465
tools.py:1687
```

These fire when the JWT didn't carry a `workspace_id` claim AND
the workspace resolver couldn't derive it from `project_id`. In
that edge case, the answer_run / cache key / lineage row gets
silently tagged with the default tenant. The right fix is to make
these paths fail loudly (raise a `WorkspaceResolutionError`) rather
than silently mis-tag — but flipping silently-broken to loud-broken
on production paths needs Kyle's call. Flagged for a dedicated
follow-up.

Theme: **"or default-tenant" is the agent-side equivalent of
`$user->workspace_id ?? <hardcoded>` in PHP.** Both shapes hide
multi-tenant configuration errors as silent cross-tenant
contamination. The cleanest systemic fix is a typed `WorkspaceContext`
that's required (not optional) on every call site, with construction
failing fast when the upstream auth/middleware didn't supply one.

### Theme K — Service-key bundle leak in EvidenceInspector + broken Evidence Inspector flow

`resources/js/Components/chat/EvidenceInspector.tsx` was reading
`import.meta.env.VITE_SERVICE_KEY` and sending it as
`x-service-key` to FastAPI's `/v1/evidence/{id}` endpoint
directly. Two failure modes depending on env config:

1. **`VITE_SERVICE_KEY` empty (the `.env.example` state):** No
   header sent. FastAPI returns 401. Evidence Inspector silently
   non-functional in every production deploy following the
   documented env contract.
2. **`VITE_SERVICE_KEY` set (operator "convenience"):** Vite
   inlines the value into the production JS bundle. Anyone who
   downloads the SPA can extract the FastAPI service key and
   call any X-Service-Key-gated internal endpoint as a
   fully-privileged service. **Textbook secret-in-bundle leak.**

**Fixed (three-layer):**

1. New Laravel controller
   `app/Http/Controllers/Api/V1/EvidenceController.php` proxies
   `GET /api/v1/evidence/{evidenceId}` to FastAPI. Resolves the
   evidence_item's project_id, gates on `hasProjectAccess`
   (Theme H pattern), mints a user-scoped FastAPI JWT, injects
   the service key server-side. Sanctum session cookie carries
   auth for the Laravel hop.
2. New route registered in `routes/api.php` under the
   auth:sanctum group.
3. `EvidenceInspector.tsx` updated:
   - Removed VITE_SERVICE_KEY usage entirely (closes the leak
     vector regardless of env config).
   - Calls `/api/v1/evidence/{id}` (Laravel proxy) instead of
     `/fastapi/v1/evidence/{id}` (direct FastAPI).

The service key never leaves the server. Evidence Inspector
works without the operator having to set any client-side env.

### Citation feedback poisoning (Theme H sibling)

`CitationFeedbackController` accepted `workspace_id` from the
request payload and forwarded to FastAPI which writes to
`silver.source_trust_features` tagged with that workspace_id.
**Cross-tenant feedback poisoning:** an attacker could POST
verdict=wrong with workspace_id=<other tenant>, planting trust
votes that the aggregator would later use to down-weight cited
documents in workspaces they don't own.

**Fixed** to resolve the answer_run's true project_id from
`silver.answer_runs`, gate on `hasProjectAccess`, AND verify
the payload's workspace_id matches the answer_run's actual
workspace (no cross-stitching).

### Theme J — `workspace-exports` bucket not provisioned

`WorkspaceExportInput.bucket` defaults to `"workspace-exports"`, but
that bucket isn't in `docker/minio/create-buckets.sh` OR in the
SeaweedFS init block in `docker-compose.yml` (which provisions
bronze, exports, bronze-raster, georag-backups, tier-hot/warm/cold).
The workflow calls `s3.put_object()` with no pre-flight check —
NoSuchBucket on every export attempt on a fresh cluster, no
diagnostic for the operator.

**Fixed:** added `workspace-exports` to both:
- `docker/minio/create-buckets.sh` (mc mb + mc anonymous set none)
- `docker-compose.yml` SeaweedFS init block (mc mb)

Sweep also found 7 SeaweedFS/MinIO buckets are created without
explicit `anonymous set none` (bronze-raster, georag-backups,
tier-hot, tier-warm, tier-cold from the docker-compose init block).
MinIO's default policy IS private, so this isn't a leak — just
slightly inconsistent with the bronze/exports buckets that DO
have explicit anonymous:none for clarity. Documented; not
changing without Kyle since it'd be cosmetic noise on what's
already private.

### DoS hardening — unthrottled expensive endpoints

Found in the rate-limit sweep but NOT fixed (parameter tuning
needs production telemetry):

- `POST /api/v1/projects/{project}/upload` — no throttle. The
  Cameco-recovery per-workspace Hatchet dispatch throttle serializes
  downstream processing but doesn't limit the upload itself; a
  spammer could fill S3 / `bronze.manifest` with thousands of
  small files.
- `POST /api/v1/projects/{slug}/drill-uploads` — same.
- `POST /api/v1/charts/render` — server-side Plotly chart render
  (CPU-heavy), no throttle.

Recommended: add per-user `throttle:uploads` + `throttle:charts`
named limiters in `AppServiceProvider::boot` with conservative
defaults (~60/hour for uploads, ~30/min for charts) and tune
from production telemetry.

### Theme G — SQL-injection shape in `/internal/v1/shadow/*/trigger`

`src/fastapi/app/routers/shadow_trigger.py` had three sites (one per
trigger handler) doing:

```python
await _conn.execute(
    f"SET LOCAL app.workspace_id = '{payload.workspace_id}'"
)
```

The ingest_pdf + tiff_normalize variants are protected by Pydantic
`workspace_id: UUID` typing — pre-validated, can't inject. **But the
ingest_zip_archive trigger** uses `IngestZipArchiveInput.workspace_id: str`
with no UUID validation. A malformed Laravel-side payload (or a
direct service-key call) would have interpolated arbitrary SQL
into the SET LOCAL statement. The X-Service-Key auth gates who can
call the endpoint, but the call-site gating doesn't prevent the
injection if the input is hostile.

**Fixed (defence in depth):**

1. All three trigger handlers now use parameter-bound
   `SELECT set_config('app.workspace_id', $1, true)` — same pattern
   the rest of the codebase already uses. F-string SET LOCAL is gone.

2. `IngestZipArchiveInput` got a `field_validator` for
   `workspace_id` / `project_id` / `run_id` that rejects any string
   that isn't a canonical UUID. The fields stay typed as `str` so
   downstream string-comparison call sites don't break, but the
   shape is now validated at the Pydantic boundary.

Verified: valid UUID accepted, `"'  OR 1=1 --"` rejected with
ValidationError.

### Theme F — Reverb workspace.activity channel was a cross-tenant leak

`routes/channels.php:60` registered the
`workspace.{workspaceId}.activity` private channel with the auth
callback:

```php
Broadcast::channel('workspace.{workspaceId}.activity', function ($user, string $workspaceId) {
    // TODO: Validate $workspaceId when the Workspace model is introduced.
    return $user->projects()->exists();
});
```

The `$workspaceId` parameter was thrown away — any authenticated user
with **any** project (in **any** workspace) could subscribe to **any
other workspace's** activity channel by typing the UUID. Two events
publish to it: `ActivityEventBroadcast` and `WorkspaceActivityBroadcast`
(fires on project mutations via `ProjectController`). Subscribers
would receive cross-tenant project-mutation events in real time.

The TODO comment is from before `silver.workspaces` existed; the
table is now load-bearing for tenancy isolation but the channel
auth never caught up.

**Fixed:** the callback now:
1. Rejects null users explicitly (no anonymous subscribes).
2. Validates the workspace_id is a well-formed UUID (avoids leaking
   existence on malformed input — mirrors the `query.{queryId}`
   pattern).
3. Joins through `silver.projects.workspace_id = $workspaceId` to
   confirm the user actually has a project IN that workspace.

**Regression test landed:** `tests/Feature/Tenancy/WorkspaceActivityChannelAuthTest.php`
— three string-level assertions against `routes/channels.php` so the
next refactor can't silently drop the workspace-scoping, the null
check, or the UUID guard. String-level rather than broadcaster-
invocation because invoking the registered closure needs the
broadcaster wired up + a live DB; the regression is "the idiom is
present in the file," which is exactly what a future delete would
have to remove. Tests pass (3/3).

### Theme E — Martin runs as read-write `georag_app`, not `martin_ro`

`docker-compose.yml:815` connects the Martin tile server with
`DATABASE_URL=postgresql://${GEORAG_APP_USER:-georag_app}…`. Martin's
job is rendering tiles — pure SELECT on the silver schema and
EXECUTE on the per-project tile functions. SAD §4.2 specifies a
dedicated `martin_ro` role with NOINHERIT and explicit SELECT-only
grants. The role NOW EXISTS (this audit landed
`00-create-app-roles.sql` + `zz-grant-app-role-memberships.sql`) and
inherits `georag_read`, which gives it SELECT on all silver tables.

**The gap that remains:** Martin's container still connects as
georag_app, which has georag_read + georag_write. A compromise of
Martin (SSRF via a tile-rendering function bug, malicious PostGIS
input) could write to silver. Switching to martin_ro is a
defense-in-depth tightening, not an exploit fix.

**Why this audit pass didn't flip Martin's connection string:**
Martin needs EXECUTE on the dozen `pg_*_by_project` functions
declared in `docker/martin/martin.yaml` (silver.pg_collars_by_project
etc.). The current `zz-grant-app-role-memberships.sql` only does
`GRANT georag_read TO martin_ro` — that confers SELECT on the
tables those functions read, but PostgreSQL function EXECUTE is a
separate grant. Without it, martin_ro would 403 on every tile
request. Adding the EXECUTE grants is straightforward but needs
Kyle to confirm the function list (the YAML evolves) and run the
swap in a controlled rotation.

**Action needed (Kyle):**

```sql
-- For each function referenced by docker/martin/martin.yaml:
GRANT USAGE ON SCHEMA silver TO martin_ro;
GRANT USAGE ON SCHEMA public_geo TO martin_ro;  -- once renamed to public_geoscience, update both this and martin.yaml in lockstep
GRANT EXECUTE ON FUNCTION silver.pg_collars_by_project(uuid, ...) TO martin_ro;
-- … repeat for every tile function …
```

Then flip docker-compose:
```yaml
DATABASE_URL: "postgresql://${MARTIN_RO_USER:-martin_ro}:${MARTIN_RO_PASSWORD}@postgresql:5432/${POSTGRES_DB:-georag}"
```

And add `MARTIN_RO_PASSWORD` to `.env.example` + SOPS.

### One more dead-setting note

The dead-settings sweep returned 26 settings — only the four
operator-facing ones (LLM_FALLBACK_*) were tagged this pass. The
remaining 22 (TEMPERATURE_BY_QUERY_TYPE, guard tolerances,
SUMMARIZER_ENABLED, single-tenant-mode, etc.) are mostly half-built
feature toggles. Bulk-tagging them risks signalling "delete me"
when some are awaiting wiring. Recommend a single sweep at the
next config-cleanup pass, distinguishing "never built" from "wired
but path-dependent on a setting nobody checks."

## Files touched across all four passes

- `docker/postgresql/init/00-create-app-roles.sql` — new (Pass 1)
- `docker/postgresql/init/zz-grant-app-role-memberships.sql` — new (Pass 3)
- `app/Events/QueryPersistFailure.php` — new (Pass 1), channel + transport
  corrected (Pass 2)
- `docker/prometheus/rules/watchdog.yml` — new (Pass 1)
- `docker/alertmanager/alertmanager.yml` — Watchdog dev-null route added
  (Pass 3)
- `database/migrations/2026_06_02_220000_drop_ollama_from_answer_runs_backend_check.php`
  — new (Pass 1), extended to also tighten
  `chk_assessment_summary_backend` (Pass 3)
- `docs/handover/REPORT_GAP_ANALYSIS.md` — C1 + C2 marked RESOLVED
  (Pass 1)
- `src/fastapi/app/config.py` — `RERANKER_MODEL_NAME` value + comment
  synced (Pass 2)
- `src/fastapi/app/main.py` — three lifespan-related comments synced
  (Passes 2 + 4)
- `src/fastapi/app/agent/deps.py` — class docstrings + GUC comment
  synced (Passes 2 + 4)
- `src/fastapi/app/agent/tools.py` — retrieval docstring synced (Pass 4)
- `src/fastapi/app/services/trace_writer.py` — legacy-GUC comment
  synced (Pass 2)
- `src/fastapi/app/services/ingest/cluster_runner.py` — legacy-GUC
  comment synced (Pass 4)
- `src/dagster/georag_dagster/resources.py` — Qdrant collection
  comment synced (Pass 4)
- `database/seeders/CgiVocabSeeder.php` — legacy-GUC `set_config`
  call switched to canonical `app.workspace_id` (Pass 4 late —
  functional bug, not just drift)
- `database/raw/phase0/README.md` — "set both keys, always" guidance
  rewritten; legacy GUC marked retired with explicit fail-closed
  warning (Pass 4 late)
- `tests/Feature/Tenancy/NoLegacyGucSetConfigInPhpTest.php` — new
  PHPUnit regression test covering the PHP surface that the existing
  Python `test_no_production_files_set_legacy_georag_gucs` doesn't
  reach (Pass 4 follow-up)
- `src/fastapi/app/config.py` — second pass on dead-setting tags:
  `LLM_FALLBACK_*` (Pass 5 extended sweep) + §2a retrieval-tunables
  comment correction
- `src/fastapi/app/hatchet_workflows/nightly_ingestion_integrity.py`
  — `_qdrant_count_misses` now respects RETRIEVAL_USE_DOCUMENT_PASSAGES
  instead of hardcoding `georag_reports` (legacy). Fixes
  near-100% false miss rate post-ADR-0010 cutover
- `src/fastapi/app/agents/phase0/store_reconciliation.py` — same
  fix: cross-store drift detector now reads canonical collection
- `src/fastapi/app/agent/figure_extractor.py` — docstring warning
  on the stale `collection="georag_reports"` default
- `routes/channels.php` — `workspace.{workspaceId}.activity` channel
  auth tightened: now requires the user to have a project IN the
  specific workspace, validates the UUID shape, and explicitly
  rejects unauthenticated subscribes. Closes a cross-tenant leak
  caught in Pass 5+ (Theme F)
- `tests/Feature/Tenancy/WorkspaceActivityChannelAuthTest.php` — new
  regression test pinning the channel auth idiom (3 string-level
  assertions; passes without needing live DB / broadcaster)
- `src/fastapi/app/routers/shadow_trigger.py` — three trigger
  handlers swapped from f-string `SET LOCAL` to parameter-bound
  `SELECT set_config('app.workspace_id', $1, true)` (Theme G)
- `src/fastapi/app/hatchet_workflows/ingest_zip_archive.py` —
  `IngestZipArchiveInput` got a Pydantic `field_validator` that
  rejects non-UUID `workspace_id` / `project_id` / `run_id`
  (defence in depth — pairs with the parameter binding above)
- `src/fastapi/app/config.py` — `LLM_BACKEND` got a
  `field_validator` that rejects `'ollama'` at config-load.
  Fail-fast guard so the matching DB CHECK doesn't have to deal
  with the silent-INSERT-failure shape. (Theme C extended.)
- `database/migrations/2026_06_03_010000_close_targeting_workflow_rls_gaps.php`
  — new migration: enables RLS + canonical workspace_isolation
  policies on 5 `targeting.*` + 2 `workflow.*` tables that the
  Phase 0 raw-SQL block missed. (Theme H — IDOR via
  `target_recommendations`.)
- `tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php` — schema
  filter extended to include `targeting`, `workflow`, `ops` so
  the same omission can't recur silently.
- `app/Http/Controllers/Api/V1/PublicApiController.php` — five
  endpoints (`mapLayers` / `targets` / `interpretations` /
  `audit` / `usage`) now gate on `hasProjectAccess` (or workspace
  membership). Defence in depth for the Theme-H IDOR fix. Also
  added: `::answer` (the answer-run lookup) now resolves the row's
  project_id and gates on `hasProjectAccess` — previously returned
  query text + class + model metadata for any answer_run by ID.
- `app/Http/Controllers/Api/V1/TrustController.php` — the trust-
  summary endpoint minted a Laravel→FastAPI JWT with a caller-
  supplied `project_id` query parameter. Now gates the mint behind
  `hasProjectAccess` so the JWT can't carry a project_id the
  caller doesn't legitimately own. (Theme H extension.)
- `app/Http/Controllers/InterpretationWorkspaceController.php` —
  `::proxy` had the same caller-supplied-project_id JWT mint bug as
  TrustController. **Fixed**. `::index` had a hardcoded default
  workspace_id passed to the Inertia page; now resolves the real
  workspace_id from the project (with hasProjectAccess gate).
- `app/Http/Controllers/Foundry/PortfolioController.php` +
  `app/Http/Controllers/Foundry/ProjectsIndexController.php` —
  both fell back to a hardcoded workspace_id because
  `$user->workspace_id` is null (User has no such column). After
  the Theme F channel-auth fix, that meant the Reverb activity
  subscription silently failed for any user whose projects weren't
  in the default tenant. **Fixed** to derive workspace_id from
  the user's first project.
- `resources/js/Components/chat/EvidenceInspector.tsx` —
  `getAuthHeaders()` was reading `localStorage.getItem('georag_workspace_id')`
  with a hardcoded default-tenant fallback. The localStorage key
  is never set anywhere in the app, so every Evidence Inspector
  request was tagged with the default workspace_id regardless of
  the user's actual tenant. **Fixed** to take workspaceId as a
  parameter (caller threads the prop already passed to the
  component) and omit `X-Workspace-Id` when null instead of
  silently mis-tagging.
- `resources/js/Pages/Chat.tsx` — same hardcoded fallback when
  reading the workspace_id for the Inspector state. **Fixed** to
  pass null when localStorage is empty (matching the EvidenceInspector
  fix above). Also: Chat.tsx itself may be dead code — no
  controller renders the `Chat` Inertia page anywhere
  (`grep "Inertia::render.*['\"]Chat['\"]"` returns zero hits).
  Foundry/Chat.tsx is the live chat page. Worth a Kyle
  conversation about deleting the orphaned Chat.tsx +
  Pages/Chat.test.tsx.
- `app/Http/Controllers/Foundry/Tier3Controller.php` — same
  pattern, more acute. `::request()` was writing
  `silver.tier3_unlock_requests` rows with `workspace_id = '00000000-...'`
  (nil UUID sentinel from `$user->workspace_id ?? <nil>`), which
  the admin review queue's workspace_id filter never matched.
  Every tier3 unlock request was effectively invisible — silent
  fail with a "request recorded" toast to the user. **Fixed** to
  resolve workspace_id from the user's first owned project and
  refuse the insert when no real workspace can be resolved (better
  to error than silent-success). `::show()` had the same bug
  reading the latest request — fixed.
- `app/Http/Controllers/Foundry/SettingsController.php` — showed
  "Default workspace" for every user because the same `$user->workspace_id`
  null read fell back to empty string. **Fixed** to derive from
  the user's first project.
- `app/Http/Controllers/Foundry/PortfolioController.php` (KPI
  panel) — the `WORKSPACE` KPI label was reading
  `$user->workspace_id ?? 'default'`. **Fixed** to use the first
  project's workspace_id; same idiom as the existing Reverb
  resolution above.
- `.env.example` — RAGFlow section retired (replaced by §04p in-process
  stack per ADR-0002; was telling operators to set `RAGFLOW_PORT` for
  a service that no longer exists). MinIO header note updated to
  remove the RAGFlow reference (Theme C follow-up sweep)
- `src/fastapi/app/hatchet_workflows/score_answer_quality.py` —
  Theme I: default workspace_id no longer hardcoded; cron now
  scans all workspaces (None = admin scope) instead of only the
  default tenant.
- `src/fastapi/app/hatchet_workflows/ingest_pdf.py` +
  `tiff_normalize.py` + `workspace_export.py` — Theme G follow-up:
  Pydantic `field_validator` rejects non-UUID `project_id` /
  `workspace_id`. Mirrors the ingest_zip_archive pattern.
- `app/Http/Controllers/CitationFeedbackController.php` — Theme H
  sibling: trusted payload-supplied workspace_id; could plant
  cross-tenant trust feedback. Now resolves the answer_run's true
  project_id, gates on `hasProjectAccess`, and verifies the
  claimed workspace_id matches the answer_run.
- `app/Http/Controllers/Api/V1/EvidenceController.php` — new
  (Theme K). Laravel proxy for `/v1/evidence/{id}` so the
  FastAPI service key never reaches the browser.
- `routes/api.php` — registered the new Evidence route under
  the auth:sanctum group.
- `resources/js/Components/chat/EvidenceInspector.tsx` —
  removed `VITE_SERVICE_KEY` usage entirely (Theme K leak vector
  closed), switched fetch URL from `/fastapi/v1/evidence/...` to
  the Laravel proxy `/api/v1/evidence/...`.
- `docs/handover/AUDIT_AND_FIX_REPORT.md` — this file

## Tests run

- `php artisan test --compact tests/Feature/Ingestion/` — 16 passed,
  31 assertions (after every pass).
- `docker exec georag-fastapi python -m pytest tests/test_acquire_scoped.py::test_no_production_files_set_legacy_georag_gucs`
  — 1 passed.
- `docker exec georag-fastapi python -c "from app.config import settings; print(settings.RERANKER_MODEL_NAME)"`
  — prints `BAAI/bge-reranker-base` (confirms Pass 2 config edit
  imports cleanly).
- `vendor/bin/pint --dirty --format agent` — `pass` on the final
  sweep.
- `php -l` on every new / modified PHP file — no syntax errors.

## Verifications NOT run (out of scope without prod access)

- Live point count of `georag_chunks` vs `silver.document_passages`
  (P1-B).
- Distribution of `silver.report_pages.char_count` (P3-D).
- Existence of any `silver.answer_runs` row with `backend_used='ollama'`
  (P4-C migration pre-flight).
- End-to-end smoke of the upload throttle + cancellation observability
  fixes landed earlier this session.

Kyle should run these against the live cluster before applying P4-C
and before flipping the P2-D flags.
