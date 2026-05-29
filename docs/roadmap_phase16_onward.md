# GeoRAG Roadmap — Phase 16 onward

**Document version:** 1.0
**Status:** Recommendations from the autonomous Phase 0-15 run.
**Predecessors:** all phase handoffs + `docs/retrospective_0_15.md`.

This doc ranks Phase 16+ candidate scopes by leverage and lists
their pre-conditions. Phases 7-15 covered six categories: ops
discipline (7-10), RAG validation (11-12), fixture seeding (13),
intermittent-refusal investigation (14), and operational
maintenance crons (15). What's next depends on whether the
priority is **deeper RAG quality** (path A), **first user-facing
surface** (path B), or **continued discipline** (path C).

---

## 1. Candidates ranked by leverage

### Path A — RAG quality investigation (recommended next)

**Phase 16-A.1: R-P14-3 — golden-test pass-rate investigation**

Peak observed: 13/35. Floor: 2/35. Even with `silver.mv_collar_summary`
populated, pass count fluctuates run-to-run. Two sub-hypotheses
from Phase 14 Step 3 scoping doc:
- LLM-determinism (specific phrase matches fragile)
- Second-factor missing fixture (lithology / assay / Neo4j
  entities the broader tests retrieve)

Effort: **5-7 steps**. High value: gets golden-test pass count
reliable, which unlocks real RAG-quality regression testing.
Pre-condition: none.

**Phase 16-A.2: R-P11-baseline-2 — public-geoscience fixture**

Three pgeo golden tests fail today. Need
`public_geoscience.sources`, `.jurisdictions`, plus per-jurisdiction
tables (BC MINFILE, SK Drillhole, etc.) seeded with at least one
record per assertion.

Effort: **4-5 steps**. Medium value. Pre-condition: light SME
input on what realistic test data looks like (jurisdiction
codes, license summaries).

**Phase 16-A.3: R-P11-l4-fixture — Layer 4 entity fixture**

Layer 4 entity grounding currently passes spuriously on sparse
data (no entities to flag as unknown). Seed Neo4j with the
PLS-* collars as nodes + a few cross-references so the layer
actually exercises its grounding path.

Effort: **3-4 steps**. Medium value. Depends on Neo4j ingestion
patterns from Module 4 (Phase B Chunk 2).

### Path B — First user-facing RAG surface

**Phase 16-B: R-P11-B — frontend Search/Query page**

Create `resources/js/Pages/Search.tsx`:
- Question input + submit button
- SSE stream from `/v1/queries/{queryId}/start`
- Citation chips linked to chunk store
- Snapshot test fixtures

Effort: **6-8 steps**. High value: turns the backend RAG into
something a pilot customer can actually use. Pairs best with
Path A.1 first so the underlying agent produces reliable
answers.

### Path C — Continued discipline

**Phase 16-C.1: R-P15-1 — bundled orchestrator prompt migration**

Scoped in Phase 15 Step 2. Single-file refactor moving 10 inline
`_SYSTEM_PROMPT_*` constants to
`app/agent/prompts/orchestrator_system.py`. Risky in that the
agent's `select_system_prompt()` resolution must keep working.

Effort: **3-4 steps** including verification. Low-medium value
— the existing inline structure works; this is purely about
applying the prompt-discipline pattern to the last set of
inline prompts.

**Phase 16-C.2: R-P10 follow-ons that landed**

Both rotation history + rotate-hmac are live. No more obvious
operator-UI items pending.

### Deferred indefinitely

| ID | Item | Blocker |
|----|------|---------|
| R-P3-5 | Dual-write harness | Waiting for second migration target |
| R-P3-6 | Hatchet HA | Path B per phase8_hatchet_ha_design.md only if forcing function lands |
| R-P3-9 | Vendor-profile column-mapping | SME-gated (Kyle) |
| R-P9-2 | Production ACME | Deploy-time, not code-time |
| R-P12-l6-sme-review | Layer 6 SME review | SME-gated (Kyle) |

---

## 2. Recommended Phase 16 + Phase 17 pairing

**Phase 16: Path A.1 (R-P14-3) — golden-test pass-rate investigation.**

This is the unique unblocker for everything else. Until the
golden tests pass reliably, neither a frontend Search page
(can't demo against unreliable answers) nor a public-geoscience
fixture (can't tell if its tests pass for the right reasons) is
fully useful.

Specific candidate steps:
1. Trace the second-factor refusal hypothesis — what other
   prompt-context fields besides `mv_collar_summary` does the
   agent's tool-result-extraction need? Are any of them
   populated by Dagster ingestion that's currently paused?
2. Add lithology + assay sample fixtures so tests retrieving
   those tables get real data.
3. Relax the most brittle test assertions (specific-phrase
   matches → regex or substring tolerance).
4. Add a `pytest --tb=no -q --count=5` invocation that runs the
   golden suite 5 times and asserts the WORST run still passes
   the floor — catches the flakiness directly.
5. Re-baseline `phase11_golden_baseline.md`.

**Phase 17: Path B (R-P11-B) — frontend Search page.**

Once golden tests are reliable, build the UX that exposes the
RAG capability to humans. Pair-debugging between the frontend
and the agent will surface edge cases the test suite missed.

---

## 3. Notes for the next operator

- `scripts/phase15_master_sweep.sh` is the canonical "is
  everything green" check. Run it after pulling any branch.
- `scripts/phase4_step7_verify.sh` is the rollup verifier. It
  occasionally flakes (4/5 instead of 5/5) when run in the
  master sweep; passes reliably standalone. Not a real
  regression — the rollup's REFRESH MATERIALIZED VIEW
  serialises against other ops on the live DB. Re-run the
  sweep if it lands at 402/403.
- All 4 prompt migrations (rephrase_system, classifier_system,
  agent_system, example_system) live under
  `src/fastapi/app/agent/prompts/` with version bookkeeping in
  `_version_registry.py`.

End of roadmap.
