# Phase B Tier 1 — Cameco Shirley Basin ingestion complete (doc-phase 179)

**Cluster:** `028N079W36` (Cameco 2011-2013 Shirley Basin uranium operation)
**Status:** Live + real data flowing through the §04i validator chain for the FIRST TIME

## End-to-end results

| Surface | Count | Notes |
|---|---|---|
| `silver.projects` | **1** | "Cameco Shirley Basin Uranium", EPSG:32613, uranium commodity |
| `silver.collars` | **63** | drillholes at PLSS T28N R79W S36 (42.06°N -105.35°W) |
| `silver.well_log_curves` | **753** | 12 curves per hole avg |
| `silver.well_log_curves` (GAMMA) | **63** | gamma-ray uranium proxy in CPS |
| `silver.well_log_curves` (GRADE) | **63** | uranium grade in percent |
| `silver.reports` | **3** | 2 native PDFs + 1 XLSX |
| `silver.document_passages` | **3** | chunked text for RAG |
| Cameco `.log` files processed | 146 | header regex didn't match cleanly — Phase C tuning |
| TIFF files (deferred to Tier 2) | 1,230 | OCR pipeline |

## Phase B Tier 1 ingesters (4 new modules)

1. **`app/services/ingest/las_ingester.py`** — `lasio` reader for LAS 2.0
   - Extracts well metadata: WELL, FLD, LOC (PLSS section), CNTY, STAT, COMP, DATE
   - Derives UTM Z13N coords from PLSS section key + per-hole deterministic offset
   - Inserts curves as `(depths[], values[])` arrays
   - Provenance per collar

2. **`app/services/ingest/pdf_ingester.py`** — `pdfminer.six` text extraction
   - Per-page extraction; paragraph chunking (100-1200 chars, page-bounded)
   - Detects scanned vs native PDFs; flags scanned for §04p OCR
   - `silver.reports` row + N `silver.document_passages`

3. **`app/services/ingest/cameco_log_ingester.py`** — Binary header parser
   - Reads first 4KB, regex on embedded text header
   - Extracts state-plane WY East coords for collar updates
   - Currently 0/146 matches — Phase C regex tuning needed

4. **`app/services/ingest/xlsx_ingester.py`** — `openpyxl` reader
   - One passage per sheet (tabular content kept whole)
   - Hash-based dedup

5. **`app/services/ingest/cluster_runner.py`** — End-to-end orchestrator
   - Pre-creates project (Pass 0) so RLS GUC is always valid
   - 4-pass walk: LAS → log → PDF → XLSX
   - Per-file txn isolation; collected `ClusterIngestSummary`

## Dependency change

`src/fastapi/pyproject.toml` — added `lasio>=0.31`. Installed in running
`georag-fastapi` container via `pip install`. Container rebuild
pending for persistence; sidecar runs re-install lasio inline.

## First real §04i eval run — the system bites

Seeded **10 Wyoming uranium core_chat questions** (`core_chat_wyoming_uranium.py`)
covering Layer 1-6 validators. Ran `real_rag_v1` against them:

```
run_id: 97976ea7-fc0b-45b9-b7ef-d5f4b4abd928
question_count: 10
pass_count: 2
fail_count: 8
regression_count: 0
```

**This is the FIRST real-data eval run in the project's history.** Until now,
every eval against `refusal_correctness` vacuous-passed Layers 1-5 (no chunks
to retrieve → refusal). Now the orchestrator actually exercises retrieval,
the validators actually score, and the system catches REAL hallucinations.

### Per-question outcomes

| Question | Pass | Failure layer | Key insight |
|---|---|---|---|
| Max drilled depth across all holes | ✅ | — | SQL-direct answer path works |
| Production rate of Shirley Basin mill | ✅ | — | Correctly refused (data not in corpus) |
| Company that drilled section 28N 79W | ❌ | 6_refusal | Over-refused — KG missing CAMECO entity |
| Drill hole count | ❌ | 6_refusal | Over-refused |
| County and state | ❌ | 6_refusal | Over-refused |
| Total depth of hole 36-1042 | ❌ | 6_refusal | Over-refused |
| When was 36-1042 logged? | ❌ | 6_refusal | Over-refused |
| Geophysical measurements collected | ❌ | 6_refusal | Over-refused |
| Does dataset include grade measurements? | ❌ | 6_refusal | Over-refused |
| Type of uranium deposit | ❌ | 6_refusal | Over-refused (LLM mentioned Athabasca — Layer 4 caught it!) |

### What the failures actually tell us

The orchestrator's per-question logs show the cascade:

```
Layer 4: Formation/entity name 'CAMECO' could not be resolved in the Neo4j KG
Layer 4: Formation/entity name 'SHIRLEY BASIN' could not be resolved in the Neo4j KG
Layer 4: Formation/entity name 'Athabasca Basin' could not be resolved in the Neo4j KG
hybrid_delayed_attachment: fallback also failed for all N unresolved markers
run_deterministic_rag: guard failure — transitioning to 'rejected'
   reason=numeric_guard: 3 ungrounded number(s) [79.0, 79.0, 43.0];
          entity_guard: 6 unresolved entity(ies);
          completeness_guard: 3 uncited sentence(s)
```

**Two distinct things are happening:**

1. **True positives (Layer 4 catches Athabasca hallucination):** The LLM, asked about Wyoming uranium deposit models, pulled "Athabasca Basin" from its training data. Layer 4 entity resolution flagged it as not in our KG. **This is exactly what §04i is designed to catch.**

2. **False positives (Layer 4 misses Wyoming entities):** "CAMECO RESOURCES", "SHIRLEY BASIN", "CARBON County" — these EXIST in our `silver.*` data but aren't yet in the Neo4j knowledge graph. Layer 4 flags them as unresolved → orchestrator over-refuses.

The fix is **Phase C: Knowledge Graph population from silver entities.** A Hatchet workflow that walks `silver.projects`, `silver.collars`, `silver.well_log_curves` and emits Neo4j nodes + relationships.

## Cumulative session state — 46 ticks closed

- **Doc-phase ticks this run:** **46** (132 → 179)
- **Substrate verifier:** **112/112** PASS (unchanged)
- **Live pytest cases:** 286
- **Track3 dashboard tests:** 14/14 PASS
- **§04i validators:** 6 of 6 graduated + **biting on real data**
- **silver.collars:** 0 → **63** (Cameco Shirley Basin)
- **silver.well_log_curves:** 0 → **753** (with real GAMMA + GRADE)
- **silver.reports:** 0 → 3
- **silver.document_passages:** 0 → 3
- **Active golden questions:** 53 → **63** (+10 core_chat Wyoming uranium)
- **Eval runs with non-vacuous Layers 1-5:** 0 → **1**

## Files added (Phase B-1 through B-9)

- `src/fastapi/app/services/ingest/__init__.py`
- `src/fastapi/app/services/ingest/las_ingester.py` (438 LOC)
- `src/fastapi/app/services/ingest/pdf_ingester.py` (293 LOC)
- `src/fastapi/app/services/ingest/cameco_log_ingester.py` (203 LOC)
- `src/fastapi/app/services/ingest/xlsx_ingester.py` (190 LOC)
- `src/fastapi/app/services/ingest/cluster_runner.py` (276 LOC)
- `src/fastapi/app/services/eval/mechanical_questions/core_chat_wyoming_uranium.py` (10 SME questions)

Total Phase B Tier 1 production code: **~1,400 LOC** (excluding tmp/ smoke scripts).

## Phase C — recommended next steps

1. **Knowledge Graph population from silver** — the single biggest unlock
   - Hatchet workflow: walk `silver.projects` → Neo4j `:Project` nodes
   - Walk `silver.collars` → `:Drillhole` nodes with `IN_PROJECT` rels
   - Walk `silver.well_log_curves` → `:LogCurve` nodes with `MEASURED_IN` rels
   - Once done, the 8 over-refusing questions should turn green

2. **Cameco .log header regex tuning** — 146 binary files have surveyed
   coordinates that would replace our derived UTM. Currently regex matches 0.

3. **§04p OCR pipeline against the 1,230 TIFFs** — Tier 2 of the cluster.
   ~5 sec/page × 1,230 = ~1.7 hours of CPU per the existing pipeline.

4. **Embedding the 3 document_passages into Qdrant** — RAG retrieval
   currently can find chunks via PostgreSQL but the embedding model
   hasn't been run against them. Wire to `_get_embedding_model()`.

5. **Container image rebuild with `lasio`** so the dep persists across
   container restart.

## Open issues

- **3 of 66 LAS files failed** with negative `T_DEPTH` values (-0.1ft, -0.2ft —
  legitimate above-ground measurements from the start of logging). The
  `chk_well_log_curves_min_depth_non_negative` constraint rejects them.
  Fix: either skip the T_DEPTH curve when negative, or relax the constraint
  to allow small negative values (-1.0 to 0).

- **Cameco .log header parser is regex-naive** — works on the sample header
  I inspected manually but didn't match any of the 146 files in production.
  Likely the binary delimiter pattern needs adjustment.

- **The 10 core_chat questions are SME-pending Kyle's review.** They were
  drafted by Claude based on the ingested data; some entity names may need
  refinement. The `expected_entities` lists are best-effort and should be
  validated.

## What this changes

The platform now has REAL silver data flowing through the eval pipeline.
The §04i validator chain is no longer a structural exercise — it's
actively scoring against retrieval quality + entity resolution + numeric
grounding + chunk provenance on a real Wyoming uranium dataset.

The 2 of 10 pass rate is honest: 1 pass is via SQL-direct (skipping the
RAG path), and 1 pass is a correct refusal. The 8 over-refusals all
trace to the **Neo4j knowledge graph being empty** — a Phase C fix.

Once Phase C populates the KG with the 63 ingested entities, the eval
run should flip to ~9/10 passing (the deposit-model question may still
fail because the LLM keeps mentioning Athabasca, which is exactly the
behavior §04i is designed to catch).
