# Phase C — Knowledge Graph population from silver (doc-phase 180)

**Status:** Live + eval pipeline pass rate **doubled (2/10 → 4/10)** + 112/112 substrate verifier preserved.

## What landed

### `app/services/ingest/kg_sync.py` (~340 LOC)

`sync_silver_project_to_neo4j(pg_conn, project_id)` walks one project's
silver state and pushes to Neo4j with idempotent `MERGE`:

| Source (silver) | Neo4j label | name property | Relationship from Project |
|---|---|---|---|
| `silver.projects.project_name` | `:Project` | project_name | (anchor node) |
| `silver.projects.company` | `:Formation{formation_type='company'}` | company | `HAS_FORMATION` |
| Derived from project_name (Shirley/Powder River/etc.) | `:Formation{formation_type='basin'}` | basin | `HAS_FORMATION` |
| Derived from region (county part) | `:Formation{formation_type='county'}` | county | `HAS_FORMATION` |
| Derived from basin → deposit type | `:Deposit` | "sandstone-hosted roll-front uranium" | `TARGETS` |
| `silver.collars[N]` | `:DrillHole` | hole_id | `HAS_HOLE` (×N) |
| `silver.reports[N]` | `:Report` | title | `HAS_REPORT` (×N) |

**Why Formation for aliases instead of multiple Project nodes:**
`:Project.project_id` has a Neo4j uniqueness constraint, so creating a
second Project node for "CAMECO RESOURCES" fails. `:Formation` doesn't
have that constraint and the orchestrator's entity matcher (substring
on `n.name` across any label) picks it up either way.

### Live verification

Direct execution of the orchestrator's entity query (`MATCH (n) WHERE
n.project_id = ... AND n.name IS NOT NULL ... WHERE degree >= 1`):

```
Project    Cameco Shirley Basin Uranium                   degree=70
Report     2012 Shirley Basin Drill Hole Coordinates      degree=1
Deposit    sandstone-hosted roll-front uranium            degree=1
Report     2011 Exploration SN 36 T28N R79W               degree=1
Report     2012 Shirley Basin DH Locates                  degree=1
Formation  CAMECO RESOURCES                               degree=1
Formation  SHIRLEY BASIN                                  degree=1
Formation  CARBON                                         degree=1
DrillHole  36-1042, 36-1043, ... 36-1111                  degree=1 each
                                                          (63 holes)
```

All entities pass the `degree >= 1` filter that the orchestrator's
`fetch_project_graph_entities` enforces. Substring matching against
LLM output will now resolve any of "CAMECO", "SHIRLEY BASIN",
"CARBON", "roll-front", "36-1042" etc.

### Sister fix — eval project selection

The eval evaluator's `_build_agent_deps` was hard-pinned to the
**first-created project** (`Phantom Lake Silver`, created 2026-05-11),
which has 20 synthetic collars but 0 reports. After the Cameco ingest,
the eval was still querying the empty Phantom Lake instead of the
populated Cameco data.

**Fix:** order project selection by data weight:
```sql
ORDER BY COALESCE(collar_count, 0) + COALESCE(report_count, 0) DESC,
         created_at ASC
```

Now the evaluator picks the project with the most ingested data
automatically. Cameco wins with 66 (63 collars + 3 reports) vs
Phantom Lake's 20.

## Eval results — before vs after

| Stage | Pass | Fail | Notes |
|---|---|---|---|
| **Pre-Phase C** (empty KG, Phantom Lake project) | 2/10 | 8/10 | 8 over-refusals; orchestrator can't resolve CAMECO/SHIRLEY in KG |
| **Post-Phase C** (KG populated, Cameco project) | **4/10** | 6/10 | 2 new passes: depth + drill date for hole 36-1042 |

### Pre/post change detail

| Question | Pre | Post | Δ |
|---|---|---|---|
| Total depth of hole 36-1042 | ❌ | ✅ | **+** |
| When was 36-1042 logged | ❌ | ✅ | **+** |
| Max drilled depth | ✅ | ✅ | — |
| Production rate refusal | ✅ | ✅ | — |
| 6 others | ❌ | ❌ | — |

The 2 new passes are the questions that target a specific drillhole by
hole_id. With the DrillHole nodes in Neo4j, the orchestrator can now
ground the LLM's response against real entity records.

## Remaining failures (6 of 10) — root-cause breakdown

| Failure layer | Count | Underlying cause | Next phase |
|---|---|---|---|
| 6_refusal (over-refused) | 3 | Other guards (numeric/completeness) still triggering | guard tuning + KG enrichment |
| 4_entity_resolution | 1 | LLM didn't mention "CAMECO RESOURCES" in response despite KG containing it (prompt issue) | prompt steering |
| 5_chunk_provenance | 2 | The 3 ingested PDFs have document_passages rows but **NO Qdrant embeddings** — retrieval returns empty | Phase D: embed silver passages → Qdrant |

The 2 chunk_provenance failures aren't a KG problem; they're a
"PDF passages don't have embeddings yet" problem. The PDF ingester
(doc-phase 179) wrote passages to PostgreSQL but didn't push them to
the `georag_reports` Qdrant collection. Phase D fixes this.

## Cumulative state

- **Doc-phase ticks this run:** **48** (132 → 180)
- **Substrate verifier:** 112/112 PASS
- **Neo4j nodes (Cameco):** 71 (1 Project + 63 DrillHole + 3 Formation + 1 Deposit + 3 Report)
- **Neo4j relationships:** 70
- **Eval pass rate on real data:** 2/10 → **4/10** (KG-bound questions all pass)
- **Track3 dashboard tests:** 14/14 PASS

## Files added

- `src/fastapi/app/services/ingest/kg_sync.py` (KG sync service)
- `src/fastapi/tmp/kg_sync_smoke.py` (one-shot runner)

## Files modified

- `src/fastapi/app/services/eval/real_rag_evaluator.py` — project
  selection by data weight (10 LOC change)

## Phase D (recommended next step)

**Embed silver.document_passages → Qdrant `georag_reports`** —
unlocks the remaining 2 chunk_provenance failures + lights up real
RAG retrieval against ingested PDFs.

Outline:
1. Walk `silver.document_passages WHERE embedding_id IS NULL`
2. For each passage:
   - Encode text via BGE-small (dense, 384-dim)
   - Encode via SPLADE++ (sparse)
   - Upsert to Qdrant with payload `{report_id, project_id, workspace_id, text, section_title=title}`
   - Update `silver.document_passages.embedding_id` with the Qdrant point ID

After Phase D, the eval should jump to ~6-7/10 (the 2 chunk_provenance
fails resolve + possibly 1-2 of the over-refusals).

## Phase E (subsequent)

After Phase D, the remaining bottlenecks are:
- **Numeric/completeness guard tuning** — these guards are too aggressive
  on legitimate answers (3 over-refusals)
- **Prompt steering** to encourage the LLM to mention canonical entity
  names (CAMECO RESOURCES vs "Cameco")
- **TIFF OCR** for the 1,230 scanned pages in the cluster

## Open issues

- The orchestrator caches entity lists in Redis with a 15-min TTL.
  When KG is updated, that cache becomes stale. The eval works around
  this with a manual FLUSHDB; production should add a cache-bust hook
  to `kg_sync`.
- The KG sync is per-project; running across all silver.projects is a
  loop the caller does. A Hatchet workflow `sync_silver_to_kg` is the
  natural wrapper (Phase D-2).
- The `:DrillHole` nodes carry `total_depth` + `easting/northing` as
  properties but not the curve summaries (max GAMMA, max GRADE). Adding
  those would let the orchestrator answer grade-magnitude questions
  without needing a SQL probe into `silver.well_log_curves`.
