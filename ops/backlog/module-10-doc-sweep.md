# Module 10 Architecture Doc Drift — Sweep Backlog
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Initial seed: 2026-04-19 (Module 2 Phase B cleanup) -->
<!-- Source authority: georag-architecture.html, module specs, live container state -->

Each item is a known gap between what the architecture doc says and what the
code or live stack actually does. Module 10 owns the reconciliation pass.
Resolution approach indicates whether the doc should be updated to match
reality, the code should be changed to match the doc, or Kyle must decide
which direction is correct.

---

## Neo4j Version Pin

- **Source:** `ops/audit/2026-04-19-datastores-audit.md` A3; Module 1 Phase B infra audit IMG-01
- **Drift:** Architecture §12 references `neo4j:2026.02.3-community`. That image tag does not exist in Docker Hub. The effective live pin is `neo4j:2026-community@sha256:...` which resolved to `2026.03.1 Community`.
- **Resolution approach:** Update doc — update §12 to reference `2026.03.1` as the effective minimum version. Document that `2026.02.3-community` tag was never published and the digest-pinned 2026.03.1 is the correct reference.
- **Raised:** 2026-04-19 (Module 1 Phase B)

---

## Qdrant HNSW ef_construct Value

- **Source:** `ops/audit/2026-04-19-datastores-audit.md` QDR-03
- **Drift:** Architecture §06 specifies `ef_construct=128`. All five live Qdrant collections use `ef_construct=200`. The higher value was set intentionally (better recall, higher index build cost).
- **Resolution approach:** Kyle decides — if 200 is the desired production value, update §06 to `ef_construct=200` with a note on the recall/cost tradeoff. If 128 is the target, schedule a collection recreation in Phase C after recall baseline is established (QDR-03 says do not lower until Phase C).
- **Raised:** 2026-04-19 (Module 2 Phase A)

---

## Neo4j Node Label Case: DrillHole vs Drillhole

- **Source:** `ops/audit/2026-04-19-datastores-audit.md` N4J-04
- **Drift:** Architecture §04f uses `DrillHole` (capital H). The live graph has 33,510 nodes under `Drillhole` (lowercase h) and 0 nodes under `DrillHole`. Constraints and indexes exist for both spellings simultaneously. Queries using the spec label return 0 rows.
- **Resolution approach:** Kyle decides — canonicalize the label in both graph and doc. Options: (a) rename live nodes to `DrillHole` to match spec, or (b) update spec to `Drillhole` to match live data. Requires owner sign-off per Global Invariant 4 (§04f schema change). Graph-engineer agent owns the migration Cypher.
- **Raised:** 2026-04-19 (Module 2 Phase A)

---

## workspace_id Absent from All Data Layers

- **Source:** `ops/audit/2026-04-19-datastores-audit.md` QDR-02, N4J-03
- **Drift:** Architecture addendum §04h-i and §09 require `workspace_id` for tenant isolation across all data layers. Current state: absent from all PostgreSQL migrations, absent from all Neo4j node properties, absent from all Qdrant point payloads (payload index was added in Module 2 Phase B but no points carry the value yet).
- **Resolution approach:** Update code — Module 3 (ingestion) must populate `workspace_id` on all new points/nodes/rows. Module 9 (RBAC) must add the PostgreSQL migration column and application-layer enforcement. The Qdrant index is now in place (Module 2 Phase B). Module 10 should verify all three layers are populated and add a note to §04h-i clarifying the Module 2/3/9 boundary.
- **Raised:** 2026-04-19 (Module 2 Phase A)

---

## Qdrant sparse_vectors vs sparse_vectors_config in PATCH

- **Source:** `ops/audit/2026-04-19-datastores-audit.md` Phase B QDR-01 resolution; module spec `02-data-stores-hardening.md` B5
- **Drift:** Module spec B5 example uses `sparse_vectors` as the PATCH payload key. Qdrant 1.17.1 requires `sparse_vectors_config` for PATCH (using `sparse_vectors` returns `"Wrong input: Not existing vector name"`). The module spec example is wrong for Qdrant 1.17.1.
- **Resolution approach:** Update doc — update module spec `02-data-stores-hardening.md` B5 example to use `sparse_vectors_config`. Also note this in any Qdrant API reference in the architecture doc.
- **Raised:** 2026-04-19 (Module 2 Phase B)

---

## .env.example NEO4J_AUTH Still Shows =none — RESOLVED 2026-04-19

- **Source:** `ops/backlog/module-10-auth-bypass-sweep.md`; auth sweep 2026-04-19
- **Drift:** `.env.example` line 132 showed `NEO4J_AUTH=none`. The live docker-compose.yml requires `NEO4J_PASSWORD` and uses `NEO4J_AUTH: ${NEO4J_USERNAME}/${NEO4J_PASSWORD}`.
- **Resolution:** `.env.example` updated to show `NEO4J_USERNAME` + `NEO4J_PASSWORD` + constructed `NEO4J_AUTH`. `docker-compose.yml:811` stale comment updated to reflect authenticated state. Closed inline.
- **Raised:** 2026-04-19 (Module 2 Phase B cleanup) — **Closed 2026-04-19**

---

## docker-compose.yml APP_DEBUG Default is true

- **Source:** `ops/backlog/module-10-auth-bypass-sweep.md`; auth sweep 2026-04-19
- **Drift:** `docker-compose.yml` lines 441, 518, 585 use `APP_DEBUG: ${APP_DEBUG:-true}` for the three Laravel service definitions (octane, horizon, reverb). This means any deployment that does not explicitly set `APP_DEBUG=false` will expose stack traces. Architecture §07 / deployment section does not document the expected default per profile.
- **Resolution approach:** Update code + doc — for any prod/staging compose profile, either set `APP_DEBUG: "false"` explicitly or document that `APP_DEBUG=false` is required in the production `.env`. Module 9 (Security) should add this to the deployment checklist.
- **Raised:** 2026-04-19 (Module 2 Phase B cleanup)

---

## SeaweedFS Bucket Naming: georag-bronze vs bronze — RESOLVED 2026-04-20

- **Source:** `ops/audit/2026-04-19-datastores-audit.md` SFS-01
- **Drift:** Architecture addendum §02b specifies bucket names `bronze` and `bronze-raster`. Live buckets were named `georag-bronze` and `georag-exports`. No `bronze-raster` bucket existed.
- **Resolution:** Kyle approved option (a) — renamed live buckets to match addendum §02b. Executed 2026-04-20 (Module 2 Phase C close-out).
  - `georag-bronze` → `bronze` (71 objects / 290 MiB copied and verified)
  - `georag-exports` → `exports` (2 objects / 3.5 KiB copied and verified)
  - `bronze-raster` — new empty bucket created per addendum §02b (Module 3 populates)
  - `georag-backups` — unchanged (correctly named)
  - All code references updated: `.env`, `.env.example`, `docker-compose.yml`, 17 Dagster and FastAPI source files
  - `ops/tests/s3-abstraction-check.sh` default bucket updated to `bronze`
  - S3 round-trip integrity test passed post-rename
  - Old buckets `georag-bronze` and `georag-exports` deleted after verification
  - Final bucket list: `bronze`, `bronze-raster`, `exports`, `georag-backups`
- **Raised:** 2026-04-19 (Module 2 Phase A) — **Closed 2026-04-20**

---

## PostgreSQL Version: 18.3 vs Arch Doc Reference — PARTIALLY RESOLVED 2026-04-19

- **Source:** `ops/audit/2026-04-19-datastores-audit.md` A1
- **Drift:** Live PostgreSQL is 18.3 (image `postgis/postgis:18-3.6-alpine`). CLAUDE.md and arch §12 referenced `PostgreSQL 17.9 + PostGIS 3.6`.
- **Resolution:** CLAUDE.md tech snapshot updated to `PostgreSQL 18.3 + PostGIS 3.6.3 + PgBouncer edoburu 1.25 + Neo4j Community 2026.03 + Redis 8.6 + SeaweedFS`. Architecture §12 in `georag-architecture.html` is still Module 10's responsibility to sweep.
- **Raised:** 2026-04-19 (Module 2 Phase A) — **CLAUDE.md closed 2026-04-19; arch §12 remains open for Module 10**

---

## PostgreSQL Tuning Values in §06 and §12 — Need Update

- **Source:** `ops/baselines/2026-04-19-pg-tuning-results.md` (Module 2 Phase B)
- **Drift (§06):** Architecture §06 "Database Performance Configuration" references PG memory settings. Live values are now `shared_buffers=8GB` (moderate path, Kyle-approved 2026-04-19), `effective_cache_size=24GB`, `maintenance_work_mem=1GB`. The §06 table references older values and may reference the "aggressive" 16GB/48GB path as the target. Update §06 to document the two-tier approach: moderate path (dev workstation) vs aggressive path (dedicated server), with the moderate path as the workstation default.
- **Drift (§12):** §12 deployment reference likely still shows `shared_buffers=4GB` from the original spec. Update to reflect live compose values.
- **Drift (§06 io_method):** `io_method=io_uring` was tested and rejected on Docker/WSL2 (seccomp blocks io_uring syscalls). §06 should note that io_uring requires either a custom Docker seccomp profile or bare-metal deployment. Default for containerized deployments: `io_method=worker`.
- **Resolution approach:** Update §06 and §12 to show live values and the Docker seccomp caveat on io_uring.
- **Raised:** 2026-04-19 (Module 2 Phase B)

---

## Neo4j Heap Initial Size Requires Restart

- **Source:** `ops/audit/2026-04-19-datastores-audit.md` N4J-02; Module 2 Phase B
- **Drift:** `NEO4J_server_memory_heap_initial__size=4G` is set in `docker-compose.yml` but a
  container restart is required for the JVM to pick up the new initial heap size. The live JVM
  was previously started with initial=2G (N4J-02 finding). The compose env value is now correct;
  the running process has not consumed it yet.
- **Resolution approach:** Execute during the next authorized maintenance window (or let the next
  weekly Neo4j backup window do it — that window already stops Neo4j for the dump). After restart,
  verify with: `docker exec georag-neo4j bash -c "ps aux | grep java" | grep -oE '\-Xms[0-9]+[gGmM]'`
- **Raised:** 2026-04-20 (Module 2 Phase C open item 3) — env correct, restart pending

---

## KML Parser Removed — Architecture Doc Reference Cleanup

- **Source:** `ops/audit/2026-04-20-ingestion-audit.md` PARSE-04; Module 3 Phase B Decision B
- **Drift:** `spatial_parser.py` previously handled `.kml` / `.kmz`. Spec §04d lists KML/KMZ as
  V1-roadmap deferred ("do not implement"). Parser removed 2026-04-20.
- **Resolution approach:** Module 10 should remove any KML references from the architecture doc
  §04d that suggest KML is supported, and update to "deferred — V1 roadmap". Also check §04d
  for the formal list of supported input formats and ensure KML/KMZ appears in the "deferred"
  section only.
- **Raised:** 2026-04-20 (Module 3 Phase B Decision B)

---

## §04j FK Cascade Semantics — `evidence_items.passage_id` Not Specified

- **Source:** `ops/reviews/2026-04-20-evidence-model-migration-review.md` (senior-reviewer advisory); Phase B3 apply 2026-04-20
- **Drift:** Addendum §04j does not specify the FK cascade semantics for `evidence_items.passage_id → silver.document_passages.passage_id`. The drafter initially used SET NULL, which would have violated the `evidence_items_exactly_one_ref` and `evidence_items_type_ref_consistent` CHECK constraints. RESTRICT was identified as the correct semantics by the senior-reviewer and is now implemented. The arch doc has no text mandating RESTRICT for this FK.
- **Resolution approach:** Update §04j to add one sentence: "The FK from `evidence_items.passage_id` to `document_passages.passage_id` must use `ON DELETE RESTRICT` — SET NULL and CASCADE are both self-contradictory with the CHECK constraints or silently destroy citations."
- **Raised:** 2026-04-20 (Module 3 Phase B3)

---

## §04j `bronze.provenance` / `document_revisions` Coexistence Not Documented

- **Source:** `ops/reviews/2026-04-20-evidence-model-migration-review.md` (senior-reviewer advisory)
- **Drift:** §04j has no text explaining the relationship between `bronze.provenance` (row-level parser audit) and `document_revisions` (document-level revision chain). They serve different purposes and are not redundant, but the arch doc does not make this distinction. Dual-write is required on future ingestion assets.
- **Resolution approach:** Add a paragraph to §04j clarifying: `bronze.provenance` = row-level parser audit (one row per Bronze object parse event); `document_revisions` = document-level revision chain for citation provenance. Ingestion assets must write to both on each parse run.
- **Raised:** 2026-04-20 (Module 3 Phase B3)

---

## §04j / Module 3 §6 B8.5 Enable-Order Not Cross-Referenced

- **Source:** `ops/reviews/2026-04-20-evidence-model-migration-review.md` (senior-reviewer advisory)
- **Drift:** No cross-reference in §04j pointing to the Module 3 §6 B8.5 enable-order sequence. The dependency (Module 6 consumer readiness gates B8.5 enable) is only documented in the Module 3 plan doc, not in the architecture doc.
- **Resolution approach:** Add a sequence diagram or ordered list in §04j showing: B8.1–B8.4 (Module 3) → B8.5 enable (requires Module 6 consumer ready) → B8.7 NOT NULL enforcement (Module 6 production stable).
- **Raised:** 2026-04-20 (Module 3 Phase B3)

---

---

## Schema placement: silver.workspaces vs public.workspaces — RESOLVED-AS-LIVE 2026-04-20

- **Source:** Module 3 Phase B1 migration `2026_04_20_100000_create_workspaces_and_data_version.php`; surfaced in Chunk 1 close-out
- **Drift:** Architecture §04e / addendum §05d prose implies `workspaces` and `projects` live in `public` schema (default Laravel convention). Live schema places both in `silver` alongside domain tables.
- **Resolution:** KEEP LIVE. Moving the tables now would cascade through 4 FK-bearing tables (`document_passages`, `document_revisions`, `evidence_items`, and `projects` itself via workspace_id) for zero functional benefit. `silver.*` is internally consistent: all user-generated state lives together.
- **Action for Module 10:** update §04e / §05d prose to state "`silver.workspaces` / `silver.projects`" (with schema prefix) rather than bare table names.
- **Raised:** 2026-04-20 (Module 3 Chunk 1) — **Closed-as-live 2026-04-20**

---

*Append new items to this file as drift is discovered. Do not close items here — they are closed in Module 10 when the doc or code is reconciled. Reference the finding ID or audit file for traceability.*

## rio-cogeo version range stale in Module 3 spec

- **Source:** Module 3 Chunk 2 (2026-04-20)
- **Drift:** Module spec `03-ingestion-pipeline.md` B5 references `rio-cogeo` without a pin; Chunk 2 agent interpreted spec intent as `<6.0.0` and installed 5.4.2. Latest stable is 7.x.
- **Resolution approach:** Update spec to reference `rio-cogeo>=7.0.0,<8.0.0` as the target, OR document why 5.x is preferred (breaking changes between 5.x and 7.x may matter — verify before bumping).
- **Raised:** 2026-04-20 (Module 3 Chunk 2)

## Query-class precedence: viz outranks spatial

- **Source:** Module 4 Chunk 1 (2026-04-21)
- **Drift:** Module 4 spec §6 B13 suggested precedence `spatial > computation > viz > document > factual`. Implemented precedence is `viz > spatial > computation > document > factual > unknown` — calibrated against a 54-test corpus where explicit rendering verbs (plot/render/draw/stereonet) were being incorrectly captured by spatial tokens.
- **Resolution approach:** Update spec B13 to match implementation. "Plot a map of collars within 50 km" is canonically a viz request (the rendering intent dominates), even though spatial tokens are present.
- **Raised:** 2026-04-21 (Module 4 Chunk 1)

---

## Dagster daemon GPU access for SPLADE inference

- **Source:** Module 4 Chunk 2 cleanup (2026-04-21)
- **Drift:** SPLADE++ (`naver/splade-cocondenser-ensembledistil`) runs on CPU inside `georag-dagster-daemon`. At CPU-only rate (~32 texts/7s batch), embedding 22,229 rows takes ~85 minutes and the full `index_public_geoscience_qdrant` run (56,767+ rows across 6 collections) is estimated at 4–5 hours wall time. This is Kyle-accepted for dev. Production scale (full SK + BC + AB public geoscience) will require either GPU access or offline pre-compute.
- **Resolution approach:** Kyle to decide at production deployment time. Options: (a) Add NVIDIA device reservation to dagster-daemon in docker-compose.yml for GPU-enabled hosts (requires nvidia-container-toolkit). (b) Pre-compute sparse embeddings offline and ingest as a Bronze→Silver step so the Qdrant index asset only does upserts, not inference. Option (b) fits the medallion architecture better and avoids coupling the index asset to GPU availability.
- **Raised:** 2026-04-21 (Module 4 Chunk 2 cleanup) — Kyle-accepted for dev

## Neo4j retrieval timeout: 2.0s code vs 3.0s arch §06

- **Source:** Module 4 Phase D runbook cross-check 2026-04-21
- **Drift:** `orchestrator.py` sets `TIMEOUT_NEO4J_S = 2.0`. Arch §06 specifies 3.0s.
- **Resolution approach:** Update arch §06 to match code (2.0s is empirically fine on the indexed graph) OR tune code up to 3.0s if field evidence warrants.
- **Raised:** 2026-04-21 (Module 4 Phase D)

## Retrieval cache key includes routing categories hash

- **Source:** Module 4 Phase D runbook cross-check 2026-04-21
- **Drift:** Cache key v4 (`src/fastapi/app/agent/orchestrator.py`) includes the full routing-bucket dict in the hash input. Arch addendum §05d describes a simpler composition without routing categories.
- **Resolution approach:** Update addendum §05d to reflect the more-conservative cache-key contract. Code is correct — no false hits when routing decisions change.
- **Raised:** 2026-04-21 (Module 4 Phase D)

## answer_runs.retrieval_strategy_version VARCHAR(32) too narrow — RESOLVED 2026-04-21

- **Source:** TOOL-CALL-01 fix 2026-04-21
- **Drift:** Column width 32 chars. `v3.1-thinking-off-synthesis-2026-04-21` (38 chars) rejected at INSERT. Had to shorten to `v3.1-think-off-2026-04-21` (25 chars).
- **Resolution:** Migration `2026_04_21_140000_widen_retrieval_strategy_version.php` widens column to VARCHAR(64). Applied and verified — `\d silver.answer_runs` shows `character varying(64)`. Online/non-locking ALTER on PostgreSQL 18 (metadata-only widening).
- **Raised:** 2026-04-21 (Module 5 TOOL-CALL-01 fix) — **Closed 2026-04-21** (cross-module cleanup sweep Item 7)
