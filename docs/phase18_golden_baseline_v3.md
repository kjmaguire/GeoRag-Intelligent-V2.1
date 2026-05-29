# Phase 18 Golden Baseline v3 — assay + lithology unlocks

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase17_golden_baseline_v2.md`,
`docs/phase17_golden_failure_audit.md`,
`docs/phase18_implementation_kickoff.md`.

---

## 1. What changed at Phase 18

Phase 18 seeded the `R-P14-3.5` assay + lithology fixtures:

- 4 core samples on `PLS-22-08` with `U3O8_ppm` peaking at 52000
  and `Au_ppb` keys on the top two samples (peak Au = 410 ppb).
- 4 lithology intervals on `PLS-20-01` covering
  `OVB → SST → PGN → GNT` from 0–320m.
- `silver.projects.workspace_id` linked to the default workspace so
  `silver.samples` FK satisfies.
- `silver.mv_collar_summary` refresh appended.

Migration: `database/raw/phase18/10-assay-litho-fixture.sql`.

---

## 2. Cold-run pass count

| Phase | Total | Cold-run peak | Delta |
|-------|------:|--------------:|------:|
| 13 | 35 | 13 | baseline |
| 14 | 35 | 12 | -1 (MV refresh added; one phrase-fragile test slipped) |
| 17 | 31 | 15 | +2 vs Phase 13 peak |
| **18** | **31** | **16** | **+1 vs Phase 17** |

Reproduced across the runs surveyed in Step 4:
- run a: 16 / 31 (warm — first sweep after restart-and-wait)
- run b: 15 / 31 (cold immediately after fastapi restart)
- run c: 16 / 31 (cold after 100s warmup)

Conservative reliable floor unchanged at **≥2 metadata passes** —
the warm-state agent-refusal phenomenon (R-P14-3.7) is still
unresolved.

---

## 3. The Phase 18 unlock: gq-015

`gq-015-lithology-narration` now passes cold. It expected the
substrings `PLS-20-01`, `SST`, and `PGN` in the agent's response.
With the Phase 18 lithology rows present, the `query_downhole_logs`
tool now returns codes that the agent narrates verbatim.

---

## 4. The two non-unlocks: gq-014 + gq-017

| Test | Expects | Data present? | Why still failing |
|------|---------|--------------:|-------------------|
| gq-014-assay-u3o8 | "U3O8" + "52" | yes — 4 rows, peak 52000 ppm | Agent's response narrates the assay range but the phrasing of "52" alongside "U3O8" doesn't match the substring assertion. Tool returns data; LLM doesn't render the exact fragment. |
| gq-017-assay-gold | "Au" | yes — 2 rows carry Au_ppb | Same pattern. Agent acknowledges gold presence but the response copy doesn't contain a bare "Au" substring. |

This is now a Class A2 (phrase-rendering) failure class — distinct
from Class E (data-missing). The fixtures are seeded; the gap is
agent prompt or test-assertion phrasing. Both belong in:

- **Phase 19 candidate scope:** tighten agent rendering of numerical
  facts and chemical symbols when fact-block data is available, OR
- **Test-side fix (R-P14-3.6 carry-over):** relax assertions to
  case-insensitive / synonym-aware matching.

Neither is appropriate for a fixture-only phase. Carry-over into
Phase 19.

---

## 5. Class breakdown after Phase 18

Reusing `phase17_golden_failure_audit.md` class IDs:

| Class | Description | Phase 17 count | Phase 18 count |
|-------|-------------|---------------:|---------------:|
| A | Number-mismatch (rendering) | 6 | 6 |
| A2 | Phrase-rendering (new at P18) | — | 2 |
| B | Hole-ID not in response | 4 | 4 |
| C | Confidence below threshold | 3 | 3 |
| D | Neo4j-missing (R-P14-3.4) | 5 | 5 |
| E | Assay/litho missing | 3 | 0 ← Phase 18 closed |
| F | Trend / aggregate logic | 4 | 4 |
| (other / phrase-fragile) | — | 1 | 1 |
| **Sum failing** | — | **16** | **15** |

---

## 6. Bonus fix — MV cartesian-join (Step 5)

The first-ever non-empty `silver.samples` + `silver.lithology_logs`
seeds in this phase surfaced a pre-existing bug in the
`silver.mv_collar_summary` definition:

```sql
-- old (buggy when downhole tables are populated):
SELECT count(c.collar_id)             AS total_collars,
       avg(c.total_depth)             AS avg_depth,
       ...
  FROM collars c
  LEFT JOIN samples s ON s.collar_id = c.collar_id
  LEFT JOIN lithology_logs l ON l.collar_id = c.collar_id
 GROUP BY c.project_id;
```

LEFT JOIN multiplies collar rows by `samples × lithology` matched
rows before the GROUP BY. With Phase 18's 4 samples on PLS-22-08
and 4 lithology intervals on PLS-20-01, `total_collars` jumped from
20 → 26 and `avg_depth` skewed from 360.8 → 373.3.

Fix in `database/raw/phase18/15-fix-mv-collar-summary.sql`:
`count(DISTINCT c.collar_id)` for the collar count, scalar
subqueries for samples + lithology counts. After fix:
total_collars=20, avg_depth=360.8, samples=4, litho=4 — all
correct.

This was a latent bug; pre-Phase-18, downhole tables were empty so
nothing exercised the cartesian path. Phase 18's fixtures + the
fix close the loop together.

---

## 7. What didn't change

- Warm-run agent-refusal phenomenon: still unresolved
  (`R-P14-3.7`). Cold-run peak stays the source-of-truth metric.
- Neo4j entity fixtures: not in scope (R-P14-3.4 deferred).
- Public geoscience fixture: not in scope (R-P11-baseline-2 deferred).

End of Phase 18 baseline v3.
