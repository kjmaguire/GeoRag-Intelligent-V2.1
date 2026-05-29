# Module 4 Parallel Dispatch Timing Baseline — 2026-04-21

Test query: "List all uranium showings near Patterson Lake with their grades"
Workspace: default (workspace_id lookup returned None — no workspace row for this project yet)
Project: 019d74a7-cbf3-7331-b62f-e103e4b07017 (Patterson Lake South)

## Method

Temporary `print(..., file=sys.stderr, flush=True)` instrumentation added to the
`asyncio.gather()` fan-out block in `orchestrator.py` lines ~2878–2909.
Each branch wrapped in a `_timed()` coroutine that measures `perf_counter` before
and after `asyncio.wait_for(store_coro, timeout=...)`.
Instrumentation removed after capture — no debug logs remain in production code.

## Per-store timings

| Store | Wall time (ms) | Timeout cap | Under gate? |
|---|---|---|---|
| PostGIS (spatial collars) | 202 | 5000 | Y |
| Qdrant (documents) | 0 | 2000 | Y |
| Qdrant (Public Geoscience) | 987 | 2000 | Y |

Note: Qdrant documents returned 0 ms because the document collection has no
matching chunks for this project yet (no NI 43-101 ingested). The call still
executes and returns an empty result set.

## Parallel dispatch summary

**Total retrieval phase wall time**: max(202, 0, 987) = **988 ms** (measured)
**Sum of store times (hypothetical serial)**: 202 + 0 + 987 = **1189 ms**
**Speedup factor**: 1189 / 988 = **1.20x** (modest because PgeoQdrant dominates)

Synthesis + LLM: 120s (hit the overall 120s deadline — 30B model on this query shape)
Total end-to-end: ~121s (synthesis-timeout limited, not retrieval-limited)

## Interpretation

Retrieval phase is healthy and parallel. All three stores complete well within
their individual timeout caps. The 988 ms retrieval wall time confirms parallel
dispatch is working as designed — the total is max(stores), not sum.

The end-to-end timeout is dominated by LLM synthesis (qwen3 30B model, ~120s
for complex multi-source queries). This is Kyle-accepted for dev per the
"120s gather timeout is a known constraint on the 30B model" note in the
cleanup sweep brief. Production will use vLLM which should bring synthesis
below 15s.

## Context

Validates Module 4 Chunk 3 deferred item: "parallel dispatch timing validation —
skipped because pg_* Qdrant collections were mid-materialize".
Collections now populated per Dagster run `778c604c`.
Deferred item marked RESOLVED 2026-04-21.
