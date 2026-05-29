# Phase 30 Implementation Diff Note (precise diffs ready to apply)

**Document version:** 1.0 — DRAFT (precondition: Phase 29 master sweep finishes green).
**Status:** Planned diffs; not yet applied.

Drafted during the Phase 29 master sweep window so it can be applied
in a single tight burst once the sweep clears.

---

## 1. Block A — downhole in the RRF cache pipeline

**File**: `src/fastapi/app/agent/orchestrator.py`
**Location**: after the existing PostGIS RRF block (around line 3910).

```python
# Phase 30 R-P29-DOWNHOLE-CACHE: query_downhole_logs -- each interval
# is a candidate so the cache pipeline carries it through.
# Previously downhole results were dropped on cache hit (Phase 29
# bypassed cache for downhole queries entirely as a stopgap). This
# block wires the 4th store properly.
for _tool_name, _result in tool_results:
    if _tool_name == "query_downhole_logs":
        _intervals = getattr(_result, "intervals", [])
        if _intervals:
            _rrf_lists.append([
                Candidate(
                    canonical_id=f"downhole:{getattr(iv, 'log_id', i)}",
                    store="downhole",
                    score=1.0,
                    payload=iv,
                )
                for i, iv in enumerate(_intervals)
            ])
```

## 2. Block B — downhole branch in cache-write candidate loop

**File**: `src/fastapi/app/agent/orchestrator.py`
**Location**: inside the loop that walks `_fused_candidates` (around line 3879),
after the existing `elif _cand.store in ("neo4j", "postgis"):` branch.

```python
elif _cand.store == "downhole":
    _payload_obj = _cand.payload
    _text = (
        f"Lithology interval "
        f"{getattr(_payload_obj, 'from_depth', '?')}-"
        f"{getattr(_payload_obj, 'to_depth', '?')}m: "
        f"{getattr(_payload_obj, 'lithology_code', '?')}"
    )
    _cand_ref_for_cache = {"store": "downhole", "canonical_id": _cand.canonical_id}
    try:
        import dataclasses as _dc  # noqa: PLC0415
        if _dc.is_dataclass(_payload_obj):
            _payload_for_cache = _dc.asdict(_payload_obj)
    except Exception:
        pass
```

## 3. Block C — downhole rehydration in cache-read branch

**File**: `src/fastapi/app/agent/orchestrator.py`
**Location**: inside the existing `elif _cache_hit and _cached_retrieval_ctx is not None:`
block, the rehydration loop (around line 4080-4100).

Add a new bucket alongside `_doc_chunks`, `_graph_entities`, `_collars`:

```python
_lithology_intervals: list[LithologyInterval] = []
# ...
elif _cc.source_store == "downhole":
    try:
        _lithology_intervals.append(LithologyInterval(**_cc.payload))
    except (TypeError, ValueError):
        _abort_rehydrate = True
        break
```

After the bucket-build, append the downhole tool result. Use the
first rehydrated collar (if any) as the `collar` field; intervals
all share one project so the collar choice is reasonable. Falls
back to None if no collars were rehydrated:

```python
if _lithology_intervals:
    _dh_collar = _collars[0] if _collars else None
    tool_results.append((
        "query_downhole_logs",
        DownholeLogsResult(
            collar=_dh_collar,
            intervals=_lithology_intervals,
        ),
    ))
```

## 4. Block D — imports

**File**: `src/fastapi/app/agent/orchestrator.py`
**Location**: existing `from app.agent.tools import (...)` block.

Add `LithologyInterval` to the import list (the others — `CollarRecord`,
`DocumentChunk`, `GraphEntity` — were added in Phase 24).

## 5. Block E — remove Phase 29 bypass (optional)

**File**: `src/fastapi/app/agent/orchestrator.py`
**Location**: the `if categories.get("downhole"):` guard added in
Phase 29 Step 2 (around line 3252).

Once Blocks A-D are in, the bypass is no longer needed. Two options:

- **Remove it.** Cleaner.
- **Keep it as defensive fallback.** If Blocks A-D have a bug, the
  bypass still produces correct (cache-miss) behaviour.

Recommendation: remove. The verifier asserts downhole cache hits
produce identical answers to cache misses; if the assertion holds,
the bypass is dead code.

## 6. Block F — `cache_skipped_reason` column

**File**: new `database/raw/phase30/10-cache-skipped-reason.sql`

```sql
ALTER TABLE silver.answer_runs
    ADD COLUMN IF NOT EXISTS cache_skipped_reason text
        CHECK (cache_skipped_reason IS NULL
            OR cache_skipped_reason IN (
                'zero_candidates',
                'partial_failures',
                'downhole_bypass_legacy',
                'schema_validation_failed'
            ));

CREATE INDEX IF NOT EXISTS idx_answer_runs_cache_skipped_reason
    ON silver.answer_runs (cache_skipped_reason)
    WHERE cache_skipped_reason IS NOT NULL;

COMMENT ON COLUMN silver.answer_runs.cache_skipped_reason IS
    'Why this run did not write a retrieval cache entry. NULL = cache write succeeded. Phase 30 R-P21-CACHE-SKIPPED-REASON.';
```

Wire orchestrator INSERT path to populate it where the existing
"skipping cache write" log lines fire.

---

## 6b. Block G (optional) — analytics parity at the second `elif _cand.store` site

**File**: `src/fastapi/app/agent/orchestrator.py`
**Location**: ~line 5125, inside the loop that builds
`AnswerRetrievalItemCreate` rows for the `silver.answer_retrieval_items`
batch insert. This is the analytics-telemetry path, separate from the
Redis cache write at line 3989.

The same downhole branch helps here so `silver.answer_retrieval_items`
analytics queries can distinguish downhole candidates from the others:

```python
elif _cand.store == "downhole":
    _candidate_ref = {
        "store": "downhole",
        "canonical_id": _cand.canonical_id,
    }
```

Verified during Phase 30 prep that both call sites carry the same
`elif _cand.store in ("neo4j", "postgis"):` pattern; updating only
line 3989 leaves analytics inconsistent on downhole rows. Low-priority
(analytics-only), but the diff is 5 LOC.

## 7. Verifier plan

`scripts/phase30_step1_verify.sh`:

1. orchestrator.py contains 4 RRF store branches (qdrant, neo4j,
   postgis, **downhole**)
2. orchestrator.py contains 4 cache-write branches
3. orchestrator.py contains 4 cache-read rehydration branches
4. `silver.answer_runs.cache_skipped_reason` column exists
5. gq-015 passes on cold + warm pair (the canary that triggered
   this work)
6. Cold-run golden ≥ 29 (matches Phase 29 baseline; the proper
   downhole fix shouldn't regress anything)

---

## 8. Rough LOC accounting

| Block | LOC |
|-------|----:|
| A — RRF downhole | 13 |
| B — cache-write branch | 16 |
| C — cache-read branch + result-wrap | 18 |
| D — imports | 1 |
| E — remove bypass | -12 |
| F — DDL migration | 15 |
| F — orchestrator wire | 8 |
| Total | ~60 |

Plus ~80 LOC for the new verifier. Total Phase 30 footprint
≈ 140 LOC.

End of diff note.
