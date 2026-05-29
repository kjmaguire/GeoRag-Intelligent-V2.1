# Phase F.4 — Empty tool_result filter (structured-data tool wiring)

**Status:** Complete
**Date:** 2026-05-14
**Question fixed:** *"What type of uranium deposit is targeted by drilling in
Shirley Basin, Wyoming?"* — Layer 1 retrieval_quality gate

## Symptom

The Shirley Basin deposit-type question failed §04i Layer 1 with:

```
Layer 1 violation: 1 citation(s) below relevance_score gate 0.5.
First: [DATA-2] (score=0.000)
```

Two-to-three citations per run carried `relevance_score=0.000`, e.g.:

```
[NI43-2] type=NI43  score=0.000 title=Qdrant document search (no results)
[DATA-3] type=DATA  score=0.000 title=Assay data — U3O8_ppm (0 samples)
```

## Root cause

`run_deterministic_rag` collected every dispatched tool into `tool_results`,
including tools that returned **zero rows** (empty `chunks=[]`, `count=0`,
`records=[]`). `response_assembler.assemble_response()` then emitted one
`Citation` per tuple, and `_extract_relevance()` correctly returned `0.0` for
the empty entries. Those zero-score Citations went straight into
`GeoRAGResponse.citations` where Layer 1's `min_relevance_score=0.5` gate
caught them.

The LLM's Evidence Set block likewise carried empty `[DATA:N]` / `[NI43:N]`
slots, so the model would dutifully cite a "source" that had zero rows.

## Fix

Drop empty tool results from `tool_results` **before** `assign_citation_ids`
and `bind_evidence` run, so the prompt + Citation list contain only real
sources and slot numbering stays contiguous (no gaps).

### Changes — `src/fastapi/app/agent/orchestrator.py`

1. **New helper `_is_empty_tool_result(result)`** (near `_build_retrieval_summary`)
   — returns True for any of:
   - `AssayDataResult.count == 0`
   - `DownholeLogsResult.count == 0`
   - `SpatialQueryResult.count == 0`
   - `DocumentSearchResult.chunks == []`
   - `GraphTraversalResult.count == 0`
   - `PublicGeoscienceSearchResult.records == []`

   Unknown / synthetic result types (e.g. the `drill_targeting` text blob)
   return False — we never silently drop a tool the response assembler doesn't
   yet special-case.

2. **Filter immediately before `assign_citation_ids`** (~line 4000):

   ```python
   _all_tool_results = list(tool_results)
   tool_results = [
       (n, r) for (n, r) in tool_results if not _is_empty_tool_result(r)
   ]
   _empty_dropped = len(_all_tool_results) - len(tool_results)
   if _empty_dropped > 0:
       logger.info("run_deterministic_rag: dropped %d empty tool_result(s)...")
   ```

   Telemetry preserved in `_all_tool_results` for any future partial-failure
   bookkeeping; book-keeping loops earlier in the function (RRF, retrieval
   summary, cache write) already iterate the *unfiltered* `tool_results`
   because they run before this line.

## Verification

`src/fastapi/tmp/f4_verify_deposit.py` runs the failing question through
`run_deterministic_rag` end-to-end.

**Pre-fix:**

```
citations (5):
  [DATA-1] score=1.000 title=Drill collars from PostGIS (63 records)
  [NI43-2] score=0.000 title=Qdrant document search (no results)
  [DATA-3] score=0.000 title=Assay data — U3O8_ppm (0 samples)
  [DATA-4] score=0.500 title=Result from drill_targeting
  [DATA-5] score=1.000 title=Neo4j knowledge graph (1 entities)
FAIL — 2 citation(s) still below 0.5
```

**Post-fix:**

```
citations (2):
  [DATA-1] score=1.000 title=Neo4j knowledge graph (1 entities)
  [DATA-2] score=1.000 title=Drill collars from PostGIS (63 records)
PASS — all citations >= 0.5 (Layer 1 retrieval_quality gate clear).
```

## Out-of-scope

The same question still trips Layers 3/4/6 (numeric_guard on `374.0`,
entity_guard on `Wyoming` etc., completeness_guard on uncited proactive-insight
sentences). Those are pre-existing issues not in scope for F.4.

## Gotcha — Python module path

The container has two copies of `app/agent/orchestrator.py`:

* `/app/app/agent/orchestrator.py` — host bind mount (live edits).
* `/usr/local/lib/python3.13/site-packages/app/agent/orchestrator.py` — baked
  in at Docker build time from `uv pip install --system --no-deps .`.

Production FastAPI runs with `WORKDIR=/app`, so `sys.path[0]=/app` and the
live mount wins. Ad-hoc scripts run from `/app/tmp` get `sys.path[0]=/app/tmp`
and silently fall through to the **stale site-packages copy**. Always run
verification scripts with `cd /app && python tmp/<script>.py`. (See also the
"WSL drift bidirectional" recurring pattern in
`feedback_autonomous_run_cadence.md`.)
