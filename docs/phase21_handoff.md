# Phase 21 Handoff — Warm-state cache poison fix (R-P14-3.7)

**Document version:** 1.0
**Status:** Phase 21 complete. Phase 22 inheriting.
**Predecessors:** `docs/phase20_handoff.md`,
`docs/phase19_golden_baseline_v4.md`,
`docs/phase17_handoff.md`.

---

## 1. The bug

Since Phase 13 the cold/warm pass-count split was the central
mystery: a fresh fastapi process answered 13–19/31 golden tests
correctly, but every subsequent run for the same set of queries
collapsed to 1–2 passes with the agent emitting "I don't have
data on that in this project." The pathology was tracked as
`R-P14-3.7` and explicitly deferred for "deep debug" through
five subsequent phases.

The cause was a **5-minute Redis cache poisoning loop**:

1. The orchestrator's retrieval layer caches a
   `CachedRetrievalContext` under
   `georag:rag_cache:v6:<query_hash>` keyed on
   (query, project_id, categories, data_version, …).
2. On cache hit, **tool execution is skipped entirely** — the
   orchestrator rehydrates candidates from cache instead of
   running spatial / documents / graph / assay / downhole tools.
3. Cache TTL is 300 s.

Failure mode: if any single request answered the query before
fastapi's tool stack was fully ready (e.g. during the 60–90 s
warmup window after `docker restart`, or under a transient
asyncpg/Neo4j connection blip), `_fused_candidates` was empty.
The cache wrote that empty context. For the next 5 minutes,
**every request with the same query string skipped tool execution
and refused with the system-prompt-mandated empty-context phrase.**

In the test harness, the first cold run populated the cache;
subsequent runs hit the empty entry; the assertion failures looked
like agent refusals rather than cache poisoning.

---

## 2. The fix

Two-line guard in `src/fastapi/app/agent/orchestrator.py` around
the `redis_client.setex(cache_key, 300, …)` call:

```python
if not _cached_candidates:
    logger.info("skipping cache write — zero candidates …")
elif partial_failures:
    logger.info("skipping cache write — partial_failures present …")
else:
    await redis_client.setex(cache_key, 300, _ctx_to_cache.model_dump_json())
```

Rationale: a legitimately empty query result is cheap to
re-derive (low single-digit ms across all stores); a poisoned
empty cache breaks the warm state for 5 minutes. Never trading
that 5 minutes of broken behaviour for the milliseconds of
re-retrieval. The `partial_failures` guard handles the related
case where the response was partially built but one or more
stores failed — same reasoning, we don't want a half-formed
cache freezing degraded retrieval.

---

## 3. Impact

| Phase | Cold | Warm | Note |
|-------|-----:|-----:|------|
| 13 | 13 | ~2 | First cold-run peak; warm collapsed to refusal-path floor |
| 17 | 15 | ~2 | Same pattern |
| 18 | 16 | ~2 | Same pattern |
| 19 | 19 | ~2 | Same pattern |
| 20 | 19 | ~2 | Same pattern |
| **21** | **20** | **20** | **Warm state holds. +1 unlock from Phase 19/20 (gq-025 now passes via NI 43-101 report path)** |

Across a back-to-back cold/warm run pair under the patch:
- Cold: 20 passed, 11 failed
- Warm: 20 passed, 11 failed — **same 11 failures**

The R-P14-3.7 cold/warm split is gone.

---

## 4. Why this took eight phases to find

The pathology had three properties that hid it from earlier
phase debugging:

1. **Test harness was always the first request after restart.**
   The pre-warmup race during fastapi boot reliably wrote an
   empty cache on the first hit; the test infra interpreted that
   first response as "the agent's answer."
2. **The cache hit log was at DEBUG, not INFO.** Earlier phases
   focused on retrieval / classifier / LLM behaviour; the cache
   lookup didn't show up in the standard log surface. It surfaced
   only when this phase grepped for `CACHE HIT` explicitly.
3. **The refusal message is in the system prompt.** Every
   variant of the agent's task profile (numeric, narrative, doc,
   etc.) contains the line `If the context is empty say "I don't
   have data on that in this project."`. That made the refusal
   look like a model decision, not a retrieval failure.

The smoking gun was a single log line during this phase's gq-018
warm-run trace:

```
CACHE HIT key=georag:rag_cache:v6:914c22457b3e38b0
schema_version=1 candidates=0
```

`candidates=0` on a cache hit was the proof.

---

## 5. Carry-overs for Phase 22+

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P20-PROMPT** | Agent-prompt tweak — surface SELF row's deposit_type verbatim for gq-018 | `prompts/agent_system.py` | High — direct test unlock |
| **R-P19-DOC** | NI 43-101 PDF stub for gq-024/026 | `gold.documents` + chunk pipeline | High — 2 unlocks |
| **R-P14-3.6** | Test assertion relaxations for phrase-fragile tests | `tests/test_golden_queries.py` | Medium — gq-014 still phrase-fragile |
| **R-P19-POPULATE** | Fix `populate_neo4j.py` Report.title uniqueness | `src/fastapi/scripts/populate_neo4j.py` | Medium |
| **R-P21-CACHE-TELEMETRY** | Promote `CACHE HIT/MISS` from DEBUG to INFO; add `cache_skipped_reason` to answer_runs metadata | `orchestrator.py` | Medium — observability for future cache regressions |
| **R-P15-1** | Bundled orchestrator prompts migration | `orchestrator.py` | Medium |
| **R-P11-B** | Frontend Search/Query page | `resources/js/Pages/` | Medium |

---

## 6. Files of record

**Modified in Phase 21:**

```
src/fastapi/app/agent/orchestrator.py                              (Step 2 — cache-write guard)
docs/phase21_handoff.md                                             (this file)
scripts/phase21_master_sweep.sh                                    (Step 3)
scripts/phase21_step1_verify.sh                                    (Step 1)
```

---

## 7. Re-running every Phase 21 verifier

```bash
bash scripts/phase21_step1_verify.sh   # cache poison fix
```

Phase 21 verifier #5 runs a cold+warm pytest pair (~3 min) to
confirm the warm-state pass count matches cold within ±1.

Combined sweep: `scripts/phase21_master_sweep.sh` adds Phase 21
to the Phase 20 list (66 verifiers).

---

## 8. Phase 22 entry checklist

1. Read this handoff. R-P14-3.7 is closed; the cold/warm
   measurement methodology no longer applies — both should
   match.
2. Re-run `scripts/phase21_master_sweep.sh` to confirm.
3. Highest-leverage Phase 22 candidates:
   - **R-P20-PROMPT** — small prompt tweak for gq-018.
   - **R-P19-DOC** — chunk seed for gq-024/026 (deeper, touches
     ingestion).
   - **R-P14-3.6** — test relaxations (only if R-P20-PROMPT
     doesn't unlock the phrase-fragile tests).

End of Phase 21 handoff.
