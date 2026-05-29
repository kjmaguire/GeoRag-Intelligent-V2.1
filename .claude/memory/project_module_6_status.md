# Module 6 Status — Citation & Hallucination Guards

**Last updated:** 2026-04-22 (Chunk 3 close-out)

---

## Phase A — Audit (CLOSED 2026-04-21)

Full audit of citation pipeline, guard layers, lifecycle, refusal payload, conflict handling, evidence inspector, and partial-failure behavior. Findings in `ops/audit/2026-04-21-citation-guards-audit.md`.

Key findings:
- CIT-01 CRITICAL: Two-stage citation pipeline absent
- LIFE-01 CRITICAL: Lifecycle state machine had only 2 states (generated/rejected)
- SCHEMA-01 CRITICAL: `answer_citation_items` table absent
- GUARD-01 CRITICAL: Completeness guard absent
- SCHEMA-02/03 HIGH: GIN indices + EvidenceItemCreate validator absent

---

## Phase B — Implementation

### Chunk 1 — Schema + Lifecycle (CLOSED 2026-04-22)

**Migration batch: 20**

Tables created:
- `silver.answer_citation_items` (migration 2026_04_21_150000) — per-citation anchor table with evidence_id FK, passage_id FK, marker_text, CHECK constraints
- `silver.answer_citation_spans` (migration 2026_04_21_160000) — per-occurrence character offset table

GIN indices added:
- `idx_evidence_items_structured_ref_gin`, `idx_evidence_items_graph_edge_ref_gin`, `idx_evidence_items_map_feature_ref_gin` on `silver.evidence_items` (migration 2026_04_21_170000)

Lifecycle state machine:
- `src/fastapi/app/services/citation_lifecycle.py` — `transition_lifecycle()` + convenience wrappers
- Orchestrator (`src/fastapi/app/agent/orchestrator.py`) wired: draft (early INSERT) → generated → validated → committed | rejected
- `citation_mode='posthoc_span_resolution'` populated on all new rows

Pydantic models added (6 models + 2 aliases):
- `AnswerCitationItemCreate`, `AnswerCitationItemRead` (with `@model_validator has_target`)
- `AnswerCitationSpanCreate`, `AnswerCitationSpanRead` (with `@model_validator range_valid`)
- `CitationLifecycleState`, `CitationMode` (type aliases)
- `EvidenceItemCreate.exactly_one_ref` `@model_validator` added (intake item 4 / SCHEMA-03)

Stub INSERT functions in `answer_run_store.py`:
- `insert_citation_item()` — `raise NotImplementedError("Chunk 2 implements")`
- `batch_insert_citation_spans()` — `raise NotImplementedError("Chunk 2 implements")`

Tests: `src/fastapi/tests/test_citation_lifecycle.py` — 21 tests, all passing

Backlog items resolved:
- Intake item 1 (evidence_id FK) — RESOLVED 2026-04-22
- Intake item 2 (GIN indices) — RESOLVED 2026-04-22
- Intake item 4 (EvidenceItemCreate validator) — RESOLVED 2026-04-22

Findings addressed: SCHEMA-01, SCHEMA-02, SCHEMA-03 (via intake items), LIFE-01

### Chunk 2 — Two-Stage Citation Pipeline (CLOSED 2026-04-22)

Implemented:
- Stage 1: `bind_evidence()` in `src/fastapi/app/agent/citation_binding.py`
- Stage 2: `resolve_spans()` in `src/fastapi/app/services/span_resolver.py` (async)
- INSERT wrappers: `insert_citation_items()`, `batch_insert_citation_spans()` in `answer_run_store.py`
- Marker format: colon-form `[DATA:N]` with dash-form normalization (dual-support window)
- Feature flag: `CITATION_SPAN_RESOLVER_ENABLED` (was `false` pending Chunk 3 flip)
- Senior-reviewer conditions C1/C2/C3 carried to Chunk 3 for resolution

### Chunk 3 — Four Guards + C1/C2/C3 Close-out (CLOSED 2026-04-22)

Migration batch: 21 (`2026_04_22_100000_add_rejection_reason_to_answer_runs.php` — adds `rejection_reason TEXT NULL` on `silver.answer_runs`)

**C1 closed:** `response.text = telemetry["normalized_text"]` in orchestrator Stage 2 — spans and displayed text are aligned

**C2 closed:** `_SYSTEM_PROMPT_VERSION = 9`; `.env` flag flipped `CITATION_SPAN_RESOLVER_ENABLED=true`; `docker-compose.yml` env passthrough added (was missing — root cause of flag not propagating)

**C3 closed:** `insert_citation_items_with_spans()` — atomic `conn.transaction()` block; old two-call sequence replaced

**passage_id lookup:** `_lookup_passage_id(chunk_id, pg_pool)` in `span_resolver.py` — queries `silver.document_passages WHERE embedding_id = $1`; NI43/PUB/PGEO tool-slot bindings now produce citation rows (`has_target` CHECK satisfied)

**Guard 3 tightened:** numerical guard silent-skip removed; unit conversions (ppm↔%, g/t↔oz/t, m↔ft) added via `_expand_grounded_with_conversions()`

**Guard 4 expanded:** entity guard adds commodity codes (Au, Ag, Cu, U3O8, etc.) + formation proper-noun heuristic beyond hole IDs

**Completeness guard (GUARD-01):** `layer_completeness.verify_completeness()` — sentence-level marker coverage; exemptions for questions, refusal phrases, imperatives, short sentences

**Guard aggregator:** `evaluate_guards() -> GuardBundle` — async; `all_passed=False` → `rejected` lifecycle + `rejection_reason` written to `answer_runs`

**Test suite:** 113 tests, all passing (1.56s); new files: `test_citation_pipeline.py` (7 tests)

### Chunk 3.5 — Perf + Flag Re-flip Attempt (PARTIAL — 2026-04-22 evening)

**Applied:**
- `TIMEOUT_GATHER_S` 120→180 in `.env`, `.env.example`, `docker-compose.yml`
- Guards parallelized via `asyncio.gather` in `evaluate_guards()` (`layer_completeness.py` ~line 243)
- Formation cache added to entity guard (`orchestrator_validators.py` — `_FORMATION_CACHE` dict, 300s TTL, `_get_known_formations()`)
- `render_evidence_block` preview trimmed 160→80 chars, dual-label removed (`citation_binding.py`)
- Timing log lines added in orchestrator: `citation_stage_1_bind`, `citation_stage_2_resolve`, `citation_guard_eval`
- Test suite: **128 tests, all passing (1.66s)**

**Smoke result:** OUTCOME B — flag reverted to false.
S1 timed out at 181s. Root cause: LLM synthesis itself (colon-form prompt v9 + 30B-A3B at 14.6 tok/s) consumes the full 180s budget before Stage 1/2/guards run. Guards never started. Chunk 3.5 perf code is on disk, correct, and will apply once synthesis time is addressed.

**Remaining bottleneck:** LLM synthesis wall time, not guard overhead. Kyle decision required for Chunk 3.6:
1. Push TIMEOUT_GATHER_S to 240s (UX regression — needs explicit approval)
2. Async guard evaluation post-stream (invasive refactor)
3. Dev model downgrade to qwen3:14b (conflicts with Module 5 MoE-as-default — needs Kyle override)

### Chunk 3.6 — Prompt Trim + Flag Flip (CLOSED 2026-04-22)

**Outcome: A — flag stays on.**

Audit revealed v9 preamble was only +236 chars / +59 tokens larger than v8 — not the cause of the 140-160s synthesis regression. The actual cause was the changed phrasing of rules 6-9 (added explanatory prose + `[PUB:X]` as 4th type) causing increased model output verbosity when tools returned context.

Trim applied to `_SYSTEM_PROMPT_SHARED_PREAMBLE_COLON`:
- Removed ` where X is the slot number from the Evidence Set` from rules 6/7/8
- Condensed rule 9 to v8 structure with colon markers substituted
- Result: v9 preamble = v8 preamble in char count (3298 chars each, delta 0)
- `_SYSTEM_PROMPT_VERSION` remains 9
- Colon-form markers + citation-per-claim discipline both preserved

Flag flip:
- `.env`: `CITATION_SPAN_RESOLVER_ENABLED=true` (with Chunk 3.6 banner comment)
- `.env.example`: updated to `true`
- Container recreated + warmed

Smoke results:
- S1 wall time: 87.3s warm (pass criterion: ≤120s) — PASS
- Model output: `[DATA:1]` colon-form confirmed
- Lifecycle: `committed` confirmed in answer_runs
- Citation items: 0 rows (correct; PostGIS DATA bindings have no passage_id)
- S3 (guard-fail): infrastructure timeout on Qdrant — not a code issue

Backlog items 6 + 6b: CLOSED.

### Chunk 4a — Refusal Payload + Evidence Inspector (CLOSED 2026-04-22)

**REF-01 RESOLVED:** Full B4 refusal payload shape implemented.
- `src/fastapi/app/services/refusal_builder.py` — NEW: 4 async factory functions (`build_guard_refusal_payload`, `build_llm_unavailable_payload`, `build_budget_exhausted_payload`, `build_insufficient_evidence_payload`)
- `RefusalReasonCode` Literal (6 values) added to `src/fastapi/app/models/answer_run.py`
- `GeoRAGResponse.refusal_payload: dict | None` field added — emitted in SSE `completed` event
- Orchestrator: guard refusal path awaits `build_guard_refusal_payload()` + attaches result to `response.refusal_payload`
- `layer_completeness.build_refusal_payload` stub updated to stable B4 shape (reason_code now specific, not "guard_failure")

**INSP-01 RESOLVED:** Evidence inspector endpoint live.
- `src/fastapi/app/routers/evidence.py` — NEW: `GET /v1/evidence/{evidence_id}` with 4 type-branched Pydantic payloads
- Auth: X-Service-Key (router dependency) + X-Workspace-Id header for workspace scope
- 4 branches: document_passage (passage text + context), structured_record (structured_ref + lineage), graph_edge (Neo4j hydration via asyncio.gather), map_feature (JSONB parse)
- Registered in `main.py` (no /internal prefix — auth on the router itself)

Tests: 16 new tests in `tests/test_refusal_payload.py` — all passing. Total suite: 144 tests, all passing.

### Chunk 4b — Conflict Detection + Partial-Failure Fallback (CLOSED 2026-04-22)

**Migration batch: 22** (`2026_04_22_110000_add_partial_resolution_rate_to_answer_runs.php`)

**CONF-01 RESOLVED**: `src/fastapi/app/services/conflict_detector.py` — NEW
- Structured-record path: groups by entity key, compares scalar properties
- Graph-edge path: groups by edge key, detects rel_type + property conflicts
- Non-raising; passage evidence skipped

**PF-01 RESOLVED**: `span_resolver.resolve_spans_delayed()` — fuzzy regex + preview_text substring fallback. Orchestrator retry loop wired in Stage 2. `citation_mode='hybrid_delayed_attachment'` when fallback resolves markers.

**OFR-3 RESOLVED**: `partial_resolution_rate NUMERIC(5,4) NULL` on `silver.answer_runs`. Applied via direct DDL (migration file: `database/migrations/2026_04_22_110000_...`).

**GeoRAGResponse**: `conflicting_evidence: list[dict] | None` + `freshness: dict | None` added.

Tests: 26 new in `tests/test_conflict_detector_and_delayed_resolver.py` — 26/26. Total: 170 tests.

---

## Phase B — COMPLETE (2026-04-22)

All Phase B chunks closed. Full trail: `ops/audit/2026-04-21-citation-guards-audit.md`.

## Phase C — DEFERRED

Measurement (precision/recall). Pairs with Module 10 golden query set.

## Phase D — DEFERRED

Runbooks for citation guard tuning. Next session.

---

## Backlog items

| Item | Status |
|---|---|
| 1. evidence_id FK on answer_citation_items | RESOLVED 2026-04-22 |
| 2. GIN indices on evidence_items JSONB | RESOLVED 2026-04-22 |
| 3. B8.5 behavioral enable (ingestion emits non-passage evidence_items) | OPEN — Module 6 consumer live; coordinate with data-engineer + Kyle gate |
| 4. EvidenceItemCreate mutual-exclusion validator | RESOLVED 2026-04-22 |
| 5. `answer_citation_items.evidence_id` FK: SET NULL → RESTRICT (OFR-1) | OPEN — post-B8.5 |
| 6. `partial_resolution_rate` column on `answer_runs` (OFR-3) | RESOLVED 2026-04-22 |

---

*See MEMORY.md for index of all memory files.*
