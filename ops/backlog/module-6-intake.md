# Module 6 (Citation & Answer Pipeline) — pre-approved intake items

Items flagged during Module 3 Phase B3 that are explicitly out of Module 3 scope
and must be picked up by Module 6 at intake. Raised here as the canonical handoff
so Module 6 Phase A picks them up first.

Authority: Module 3 Phase B3 close-out 2026-04-20; evidence-model migration plan
`ops/migrations/2026-04-20-evidence-model-migration-plan.md` § Coordination Notes.

---

## 1. Add `evidence_id` FK to `answer_citation_items`

- **Raised:** 2026-04-20 (Module 3 Phase B3 close-out)
- **RESOLVED:** 2026-04-22 (Module 6 Phase B Chunk 1)
  - `silver.answer_citation_items` created in migration batch 20.
  - `evidence_id UUID NULL REFERENCES silver.evidence_items(evidence_id) ON DELETE SET NULL` included.
  - NOT NULL remains DB-nullable per plan; application-layer enforcement deferred to B8.7.
- **Action:** When Module 6 creates `answer_citation_items`, add:
  ```sql
  evidence_id UUID NULL REFERENCES silver.evidence_items(evidence_id)
  ```
  as a nullable FK column. NOT NULL enforcement is a later step (B8.7) that
  requires the behavioral write path to be stable in production first.
- **Pre-condition:** `silver.evidence_items` table exists (created in Module 3
  Phase B3, migration batch 14, 2026-04-20).
- **Reversibility:** The nullable FK can be dropped without data loss up until
  the B8.7 enable. Do NOT make it NOT NULL at the DB level until Module 6 is
  confirmed stable in production.
- **Owner:** Module 6 schema migration (boilerplate-writer can scaffold DDL;
  backend-fastapi agent owns the citation-writer code).

---

## 2. Three GIN indices on `silver.evidence_items` JSONB columns

- **Raised:** 2026-04-20 (Module 3 Phase B3 tightening T3, per senior-reviewer)
- **RESOLVED:** 2026-04-22 (Module 6 Phase B Chunk 1, migration batch 20)
  - `idx_evidence_items_structured_ref_gin`, `idx_evidence_items_graph_edge_ref_gin`, `idx_evidence_items_map_feature_ref_gin` all created as partial GIN indices.
  - Added while table has zero rows (zero lock cost, correct timing per audit SCHEMA-02).
- **Action:** Once Module 6 enables the behavioral write path (B8.5 equivalent)
  and non-passage evidence types (`structured_record`, `graph_edge`,
  `map_feature`) start landing in `evidence_items`, add per-column GIN indices
  with NULL-pruning partials:
  ```sql
  CREATE INDEX idx_evidence_items_structured_ref
    ON silver.evidence_items USING GIN (structured_ref)
    WHERE structured_ref IS NOT NULL;

  CREATE INDEX idx_evidence_items_graph_edge_ref
    ON silver.evidence_items USING GIN (graph_edge_ref)
    WHERE graph_edge_ref IS NOT NULL;

  CREATE INDEX idx_evidence_items_map_feature_ref
    ON silver.evidence_items USING GIN (map_feature_ref)
    WHERE map_feature_ref IS NOT NULL;
  ```
  Without these, any lookup of the form `WHERE structured_ref @> '{...}'` or
  `WHERE graph_edge_ref->>'start_node_id' = '...'` will table-scan.
- **Deferral rationale:** Zero rows exist today. Cost of adding indices now is nil
  operationally, but the actual hot-paths in Module 6's query code are not known
  yet. Estimated post-ingestion volume and access patterns should drive the
  decision on whether to also add functional indices on specific JSONB paths.
- **Owner:** data-engineer agent during Module 6 Phase B (coordinate with
  backend-fastapi agent on which JSONB paths Module 6's citation queries hot-path).

---

## 3. B8.5 Behavioral Enable — ingestion emits non-passage `evidence_items` rows

- **Raised:** 2026-04-20 (Module 3 Phase B3 close-out; plan doc § "NOT in this migration")
- **Action:** Module 6 owns gating the B8.5 behavioral enable. Until Module 6's
  citation consumer is ready to read from `evidence_items`, the Dagster ingestion
  assets must NOT emit `structured_record`, `graph_edge`, or `map_feature` rows
  into `evidence_items`. The schema is in place; the write path is off by default.
- **Enable sequence:**
  1. Module 6 citation writer implemented and reviewed.
  2. Module 6 signals readiness to Module 3 (coordinate via Kyle).
  3. Module 3 data-engineer enables B8.5 in Dagster assets (`index_collars` et al.
     emit `evidence_items` + `structured_record_lineage` rows per ingestion run).
  4. B8.6 verification: confirm `structured_record_lineage` rows accumulate
     correctly with correct `ingestion_run_id` from Dagster.
  5. B8.7 enable: `answer_citation_items.evidence_id` made NOT NULL at application
     layer (not DB level) once B8.5+B8.6 confirmed stable.
- **Pre-condition:** `silver.evidence_items`, `silver.structured_record_lineage`
  tables exist (Module 3 Phase B3 batch 14). `answer_citation_items.evidence_id`
  nullable FK column added (item 1 above, Module 6 Phase B).
- **Owner:** data-engineer agent (B8.5 Dagster wiring) + backend-fastapi agent
  (Module 6 citation consumer). Kyle gates the enable decision.

---

## 4. Pydantic `EvidenceItemCreate` mutual-exclusion validator

- **Raised:** 2026-04-20 (senior-reviewer advisory in review doc)
- **RESOLVED:** 2026-04-22 (Module 6 Phase B Chunk 1)
  - `@model_validator(mode='after')` `exactly_one_ref` added to `EvidenceItemCreate` in `src/fastapi/app/models/evidence.py`.
  - Also validates `evidence_type` matches the populated ref field.
  - 5 unit tests in `tests/test_citation_lifecycle.py` cover all cases.
- **Action:** The `EvidenceItemCreate` model in `src/fastapi/app/models/evidence.py`
  does not enforce the `exactly_one_ref` mutual-exclusion rule at the Pydantic
  layer — only at the DB CHECK layer. Add a `@model_validator(mode='after')` that
  mirrors the DB CHECK logic, so malformed evidence items are rejected before they
  hit the database.
- **Owner:** backend-fastapi agent during Module 6 Phase B.

---

*Append new items as Module 3/4/5 work surfaces additional Module 6 dependencies.*
*Do not close items here — close them in Module 6 Phase A intake verification.*

---

## 5. `answer_citation_items.evidence_id` FK flip SET NULL → RESTRICT (post-B8.5)

- **Raised:** 2026-04-22 (Module 6 Chunk 2 senior-reviewer review, OFR-1)
- **Status:** OPEN — scheduled post-B8.5
- **Context:** Chunk 1 landed `evidence_id UUID NULL REFERENCES silver.evidence_items(evidence_id) ON DELETE SET NULL`. Module 3 B3 precedent was RESTRICT on `evidence_items.passage_id` to protect citation integrity. Today SET NULL is acceptable because `evidence_items` has 0 rows — RESTRICT would needlessly block evidence pruning when no citation actually depends on it.
- **Action:** After B8.5 enables non-passage `evidence_items` writes AND Module 6 is producing `[ev:*]`-bound citations regularly, file an additive migration flipping `evidence_id` FK to `ON DELETE RESTRICT`. This forces ingestion code to explicitly nullify citation references before pruning evidence, matching the Invariant-1 integrity guarantee the Module 3 reviewer established for passage_id.
- **Owner:** backend-fastapi agent, paired with whoever owns the B8.5 dispatch.
- **Reference:** `ops/reviews/2026-04-22-module-6-chunk-2-review.md` OFR-1.

---

## 5b. OFR-3 — `partial_resolution_rate NUMERIC(5,4) NULL` on `answer_runs`

- **Raised:** 2026-04-22 (Module 6 Chunk 2 senior-reviewer review, OFR-3)
- **RESOLVED:** 2026-04-22 (Module 6 Phase B Chunk 4b)
  - Column added via `database/migrations/2026_04_22_110000_add_partial_resolution_rate_to_answer_runs.php`
  - Applied to live DB (column confirmed `numeric(5,4)` in `\d silver.answer_runs`)
  - Orchestrator writes value alongside `citation_mode` in combined UPDATE after Stage 2 span resolution

---

## 6. Chunk 3.5 — perf work to let `CITATION_SPAN_RESOLVER_ENABLED=true` fit dev-GPU budget

- **Raised:** 2026-04-22 (Chunk 3 smoke testing)
- **Status:** CLOSED — Chunk 3.6 trim resolved the synthesis regression; flag is live (true)
- **Applied 2026-04-22:**
  - TIMEOUT_GATHER_S raised 120→180 (`.env`, `.env.example`, `docker-compose.yml`)
  - Guards parallelized via `asyncio.gather` in `evaluate_guards()` (`layer_completeness.py`)
  - Formation cache added to entity guard (`orchestrator_validators.py`, `_FORMATION_CACHE`, 300s TTL)
  - Stage 1 `render_evidence_block` trimmed: 160-char → 80-char preview, verbose dual-label removed
  - Timing log lines added around Stage 1/2 + guard eval in orchestrator
  - 128 tests passing (1.66s)
- **Smoke result:** S1 timed out at 181s. Root cause is LLM synthesis itself consuming the full 180s budget — the colon-form system prompt v9 + classifier LLM call + tool fan-out exhausts the deadline before Stage 1/2/guards have a chance to run. Flag reverted to false.
- **Residual bottleneck:** LLM synthesis wall time, not guard overhead. All Chunk 3.5 perf work is on disk and will apply once synthesis time is addressed.
- **Reference:** `ops/audit/2026-04-21-citation-guards-audit.md` "Chunk 3.5" section.

## 6b. Chunk 3.6 — close the synthesis time gap

- **Raised:** 2026-04-22 (Chunk 3.5 smoke failure)
- **Status:** CLOSED 2026-04-22 — Outcome A (flag stays on)
- **Root cause:** LLM synthesis with qwen3:30b-a3b at 14.6 tok/s + 16K context + colon-form prompt v9 (larger than dash-form v8) takes ~140-160s warm. Tool fan-out + classifier + prompt assembly adds ~20-30s. Total: 160-190s, consistently over 180s budget.
- **Options for Kyle to decide:**
  1. **Push TIMEOUT_GATHER_S to 240s** — accept ~3-4 min UX latency on dev. Prod GPU faster. Scope constraint says don't do this without Kyle approval. Simplest.
  2. **Async guard evaluation** (invasive) — guards run in background post-stream; `validated`/`rejected` lifecycle transitions arrive out-of-band. Synthesis completes within ~150s; guards add 30s async. Proper long-term architecture.
  3. **Dev model downgrade to qwen3:14b** — 44 tok/s dense, fits synthesis in ~60s. Prod stays on 30B-A3B. Conflicts with MoE-as-default call from Module 5; needs explicit Kyle override.
- **Owner:** Kyle to decide path; backend-fastapi to implement.
- **Reference:** `ops/audit/2026-04-21-citation-guards-audit.md` "Chunk 3.5" section.

## 7. `num_ctx` must be passed per-request (applied inline 2026-04-22)

- **Raised:** 2026-04-22 (Chunk 3 smoke)
- **Status:** RESOLVED inline
- **Context:** `OLLAMA_NUM_CTX=16384` in compose env is only a server-level *default*. Ollama reloads `qwen3:30b-a3b` at its model-built-in default (4096) on every cold request unless the chat payload explicitly includes `options.num_ctx`. Discovered when Chunk 3 smoke queries stalled.
- **Fix**: added `"num_ctx": settings.OLLAMA_NUM_CTX` to the options dict in `_call_openai_compatible_llm` (`orchestrator.py` ~line 1489), and added `OLLAMA_NUM_CTX: int = 16_384` as a real Pydantic Settings field in `config.py`. FastAPI recreated.
- **Verification**: post-fix warm queries complete in 78s (baseline pre-Chunk-3). Lifecycle `committed` observable.
- **Cross-module note**: update `memory/feedback_datastore_gotchas.md` Gotcha #1 ("Ollama num_ctx sticky") to extend: not just on model reload — on every cold request via the chat API. Module 5 status memo already captured the reload-time aspect; this adds the per-request aspect.
