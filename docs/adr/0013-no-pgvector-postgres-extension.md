# ADR 0013: No pgvector — vector retrieval stays in Qdrant

- **Date**: 2026-06-23
- **Status**: Accepted
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: implicit "Qdrant for vectors" decision in `georag-architecture.html` §07; surfaces it as an explicit ADR after the 2026-06 version audit flagged the gap.

## Context

The 2026-06 stack audit flagged that **`pgvector` is not installed** in our custom PostgreSQL 18 image, even though the build pulls in seven other extensions (`h3`, `hypopg`, `pg_stat_kcache`, `pg_partman`, `pg_repack`, `pg_ivm`, plus PostGIS et al.). All vector retrieval in GeoRAG goes through Qdrant (canonical collection `georag_chunks` per ADR-0010, plus seven public-geo collections per `app/agent/public_geoscience_tool.py`).

The audit's open question was *"deliberate or accidental?"* The answer is **deliberate but never formally documented**, so this ADR closes the loop.

## Options considered

| Option | What it enables | What it costs | Outcome |
|---|---|---|---|
| **A. Stay with Qdrant-only (current)** | Sharded HNSW, scalar/binary quantization, native filtered search, gRPC streaming for batch operations. Workspace tenancy enforced at the payload-filter layer (`FieldCondition(key="workspace_id", ...)`). | Cross-store joins ("rows near this embedding") require app-side stitching: app fetches IDs from Qdrant, then SQL-fetches rows by PK. Two round-trips per such query. | **Chosen.** |
| B. Add pgvector alongside Qdrant | SQL-side hybrid retrieval (`SELECT … ORDER BY embedding <-> $1 LIMIT k`). Joins to RLS-protected silver tables happen in a single query. | Maintenance: dual write paths (write to Qdrant AND pgvector on every embed), dual storage cost, dual operational surface. RLS-on-vector adds non-trivial planner cost vs Qdrant's payload index. | Rejected. |
| C. Replace Qdrant with pgvector | One vector store, one tenancy story. | pgvector at GeoRAG scale (millions of chunks, hybrid dense + sparse + multi-vector with the planned BGE-M3 migration) lacks Qdrant's HNSW tuning surface, doesn't support sparse vectors natively, and has weaker filtered-search performance under selective workspace filters. The §3.3.5 retrieval profile work + the 8-intent reranking pipeline would need substantial rework. | Rejected. |

## Decision

**Qdrant remains the sole vector store.** `pgvector` is intentionally not installed.

## Consequences

### Positive

- One vector store to operate, snapshot, restore, and tune. No dual-write consistency story.
- Qdrant's native sparse-vector slot (added 2026-06-01 per `project_qdrant_chunks_schema_2026_06_01`) is hot — the existing SPLADE++ path depends on it. pgvector doesn't have an equivalent.
- The workspace-isolation contract is `FieldCondition(key="workspace_id", match=MatchValue(value=ws))` at the Qdrant payload index — proven, with regression tests under `WorkspaceRlsCoverageTest`. Adding pgvector would create a second tenancy enforcement path to maintain in parallel.
- Image-build complexity stays lower. The custom `docker/postgresql/Dockerfile` already compiles six extensions from source; adding a seventh that we don't use is friction.

### Negative

- Cross-store hybrid queries ("get rows in silver.collars whose embedding is near this query") need app-side stitching. The current `cross_store_consistency.py` service handles this for the ingest pipeline; for retrieval, the §3.3.5 reranking pipeline already executes the embedding lookup as its first stage anyway.
- If a future feature genuinely needs SQL-side `ORDER BY embedding <-> $1` (e.g., a materialized view over near-duplicate chunks for de-duplication), the bar to re-open this ADR is *that specific feature*, not generic "vectors in SQL would be nice."

### Neutral

- Existing services (`qdrant_service.py`, `qdrant_fallback.py`, `index_document_passages.py` in Dagster) continue working unchanged.
- The audit's recommended "add pgvector for hybrid joins" is rejected; the same audit's call for an ADR is satisfied by this document.

## Revisit triggers

Re-open this decision if any of these become true:

1. **A specific feature is blocked.** A retrieval pattern emerges that genuinely cannot be served by "fetch IDs from Qdrant, then SQL by PK" — and the second round-trip materially hurts latency or correctness.
2. **Qdrant operational pain dominates.** Snapshot/restore, scaling, or the sparse-vector slot becomes problematic at scale in a way pgvector's simpler model would avoid.
3. **The vector workload shrinks dramatically.** Hypothetically, if the corpus shrinks to a size where a single Postgres node trivially handles vector ops, the cost/benefit tilts.

None of these are on the current trajectory.

## References

- ADR-0010: `silver.document_passages` is the canonical chunked-content corpus.
- `app/services/qdrant_service.py` — primary Qdrant client.
- `app/agent/public_geoscience_tool.py` — seven public-geo collections.
- `src/dagster/georag_dagster/assets/index_document_passages.py` — index builder + schema.
- Memory: `project_qdrant_chunks_schema_2026_06_01` — sparse-vector slot adoption.
- 2026-06 audit punch-list item 6 (the one this ADR resolves).
