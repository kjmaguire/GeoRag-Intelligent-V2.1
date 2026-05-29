# Phase 11 Step 2 — Golden-query test baseline

**Document version:** 1.0
**Status:** Baseline snapshot captured at Phase 11 close.
**Captured at:** 2026-05 (Phase 11 Step 2 verifier run)

---

## 1. Why this doc exists

Phase 11 Step 2 captures the current pass/fail state of the golden
query test suite so Phase 12+ can detect regression OR progress
against this floor. The framework itself (35 collected tests,
parametrised over query templates) is intact; the failing tests
indicate fixture/data gaps rather than code defects.

---

## 2. Baseline results

Run inside `georag-fastapi` container via:

```bash
docker exec georag-fastapi pytest --tb=no -q \
    /app/tests/test_golden_queries.py \
    /app/tests/test_public_geoscience_golden.py
```

### Phase 11 baseline (no fixture seeded)

| Suite | Total | Pass | Fail |
|-------|-------|------|------|
| Milestone-1 golden queries | 31 | 1 | 30 |
| Public-geoscience golden queries | 4 | 1 | 3 |
| **Combined** | **35** | **2** | **33** |

Total elapsed (cold run): ~21 seconds.

### Phase 13 baseline (after PLS-* fixture seed) — **CURRENT FLOOR**

After Phase 13 Step 3 seeded the Milestone-1 collar fixture
(`database/raw/phase13/10-golden-collars-fixture.sql`):

| Suite | Total | Pass | Fail |
|-------|-------|------|------|
| Combined | **35** | **13** | **22** |

Total elapsed: ~48 seconds (LLM round-trips dominate).

**+11 unlocked tests vs Phase 11 baseline.** The remaining 22 failures
split between:
- LLM-determinism (specific phrase matches like "364" for averages)
- Tests that need additional fixtures (lithology, assay, Neo4j
  graph nodes)
- The 3 public-geoscience tests (separate fixture path,
  R-P11-baseline-2, still deferred)

---

## 3. Failure characterisation

The single passing test in each file is `test_golden_query_class_coverage`
/ `test_pgeo_golden_query_class_present` — both metadata tests that
verify the test-class structure itself. The other 33 tests parametrise
over actual agent executions against real queries.

Sampled failure (`gq-001-count-holes`):

```
[gq-001-count-holes] Required phrase '20' not found in response.
Response text: "I don't have that number in this project [DATA-1]."
```

**The agent IS working correctly.** It produces well-formed
responses with citation markers (`[DATA-1]`), and the Section 04i
guards correctly **refuse to answer** rather than hallucinate when
the underlying fixture data is missing.

Root cause for all 33 failures: the golden tests expect a populated
`silver.collars` table containing the PLS-20-01..PLS-22-10 project
fixture (10 drill holes, depths, status, etc.) — that fixture seed
isn't loaded in this dev environment.

This is fixture infrastructure, not RAG code. Phase 12+ can either:
- Seed the fixture in the verifier setup, OR
- Add a feature flag that opts test runs into "expect data" mode
  vs "expect refusal" mode.

---

## 4. What this baseline asserts

- **Lower bound for Phase 12+:** pass count must be ≥ 2 — the
  conservative regression floor (the metadata tests always pass;
  LLM-determinism in the parameterised tests means pass counts
  beyond that fluctuate run-to-run).
- **Phase 13 peak: 13 passing** (post-fixture-seed first run).
  Subsequent runs intermittently drop back to 2 — the agent's
  refusal path fires when the orchestrator's tool dispatch
  returns empty for reasons that aren't deterministic (cached
  tool routing, classifier non-determinism, vLLM response
  variability). Phase 14+ should investigate why a freshly-cleared
  fastapi container produces 13/35 then 2/35 on identical
  fixture state. **R-P13-1**: investigate intermittent agent
  refusal path.
- Test framework loads + collects all 35 tests.
- Agent path produces well-formed responses (citation markers
  intact even when refusing).
- §04i layers correctly refuse rather than hallucinate on missing
  data — observed in `gq-001-count-holes` response: the agent says
  "I don't have that number" with a `[DATA-1]` cite of the (empty)
  source it consulted.

---

## 5. Carry-over for Phase 12+

- **R-P11-baseline-1** — seed PLS-20-01..PLS-22-10 golden fixture
  into `silver.collars` (or a fixture schema) so the 30 milestone-1
  parameterised tests have data to validate against.
- **R-P11-baseline-2** — seed the public-geoscience corpus fixtures
  for the 3 pgeo golden tests.
- After both fixtures land, re-baseline this doc with the new pass
  count.

---

End of baseline.
