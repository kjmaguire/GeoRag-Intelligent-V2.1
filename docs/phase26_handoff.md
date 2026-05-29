# Phase 26 Handoff — Factoid insights gate + two stale-test fixes

**Document version:** 1.0
**Status:** Phase 26 complete. Phase 27 inheriting.
**Predecessors:** `docs/phase25_handoff.md`.

---

## 1. What Phase 26 delivered

Three small changes that compound into **+2 net unlocks**
(gq-005 + gq-020 + gq-027 all green; no regressions). Cumulative
session trajectory now **13 → 27**.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `src/fastapi/app/agent/orchestrator.py` — Step 4b's `detect_anomalies` block now skips when `[PRE-COMPUTED SUMMARY]` is in `llm_text`. Factoid responses (count/min/max/avg queries that quote the summary block verbatim) no longer get the "Proactive Insights" trailer that was contaminating them with depth strings + extra hole IDs. | `scripts/phase26_step1_verify.sh` checks 1+2 |
| 2 | `src/fastapi/tests/test_golden_queries.py` — gq-005 expects `"20"` instead of stale `"10"` (Phase 17 raised the fixture from 10 → 20 holes; the test was only passing on incidental "510 m" matches in the insights block); gq-020 `must_not_contain` switched from the digit `"0"` to the word `"zero"` (the original assertion was a substring trap — every hole ID like PLS-22-10 contains the character "0"). | `scripts/phase26_step1_verify.sh` checks 3+4 |
| 3 | Cold-run peak ≥ 26, cold/warm parity within ±2 | `scripts/phase26_step1_verify.sh` checks 5+6 |
| 4 | This handoff + master sweep | — |

---

## 2. The three changes, one-paragraph each

**R-P26-FACTOID-INSIGHTS** — When the LLM answers a count or stat
query, the orchestrator's `_build_retrieval_summary` emits a
`[PRE-COMPUTED SUMMARY]` marker and the LLM quotes the SUMMARY
block verbatim per the NUMERIC prompt variant. After synthesis,
the orchestrator's anomaly detector unconditionally appended
"Proactive Insights" — depth anomaly notes like "PLS-22-08 is
510 m TD". For a factoid answer, those notes are noise: they
contradict the lead with extra hole IDs (gq-027: "shallowest is
PLS-21-06" → trailer mentions PLS-22-08) and they pollute the
allowed-substring space (gq-020: "1 hole in progress" must_not
mention "0" but the trailer says "510 m"). Gating insights off
when the SUMMARY marker is present is the architecturally clean
fix.

**gq-005** — The test expected `"10"` diamond drill holes. That
count was correct before Phase 17 added 10 XLS-24-* collars,
raising the fixture total to 20 (all diamond drill). The test
was incidentally passing because the proactive-insights trailer
mentioned hole depths like "510 m" — substring "10". Once Phase
26's gate dropped the trailer, the test failed honestly. Updating
to `"20"` matches Phase 17 fixture ground truth — this is a
stale-spec correction, not a relaxation.

**gq-020** — `must_not_contain: ["0", "none", "no holes"]`. The
intent was "must not say *zero* holes". The substring `"0"` is
a false-positive trap: any hole ID with a 0 digit (PLS-22-10) and
any depth value containing 0 (e.g. "20 m", "510 m") trips it.
Switching to the word `"zero"` matches intent and stops the trap.

---

## 3. Cold/warm pass count

| Phase | Cold | Warm | Delta |
|-------|-----:|-----:|------:|
| 24 | 23 | 23 | infra fixes |
| 25 | 24-25 | 24-25 | vLLM cap unlocks gq-013 |
| **26** | **27** | **26** | **+2 net (gq-005 + gq-020 + gq-027 all green)** |

The gain breakdown:
- Insights gate alone: unlocked gq-027 (was tripping on PLS-22-08
  in trailer), no regressions on existing passes that didn't
  carry the marker.
- gq-005 test correction: unlocked gq-005 directly.
- gq-020 substring-trap fix: unlocked gq-020 directly.

---

## 4. Remaining failures (cold-run)

| Test | Reason | Phase carry-over |
|------|--------|------------------|
| gq-021-orientation-reference | Agent doesn't surface drilling orientation specifically | R-P26-DOC or prompt |
| gq-023-fault-count | Fixture has no fault data; agent refuses | R-P19-DOC |
| gq-026-estimation-method | Needs NI 43-101 chunk containing "kriging" | R-P19-DOC |
| gq-030-dominant-azimuth | Agent classifies "dominant drilling azimuth" as out-of-scope | R-P25-AZIMUTH |

---

## 5. Carry-overs for Phase 27+

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P19-DOC** | NI 43-101 chunk seed for gq-026 (kriging) + gq-023 (fault) | `silver.document_passages` + chunk pipeline | High — 2 unlocks |
| **R-P25-AZIMUTH** | gq-030 — agent mis-classifies azimuth question | classifier / prompt | High |
| **R-P26-ORIENTATION** | gq-021 — orientation_reference field surface | data + agent | Medium |
| **R-P14-3.6** | Other test relaxations | tests | Medium |
| **R-P19-POPULATE** | populate_neo4j Report.title uniqueness | populate script | Medium |
| **R-P15-1** | Bundled orchestrator prompts migration | orchestrator | Medium |
| **R-P21-CACHE-TELEMETRY** | Promote CACHE HIT/MISS to INFO | orchestrator | Medium |

---

## 6. Files of record

```
src/fastapi/app/agent/orchestrator.py     (Step 1 — factoid insights gate)
src/fastapi/tests/test_golden_queries.py  (Step 2 — gq-005 + gq-020 corrections)
docs/phase26_handoff.md                    (this file)
scripts/phase26_master_sweep.sh
scripts/phase26_step1_verify.sh
```

End of Phase 26 handoff.
