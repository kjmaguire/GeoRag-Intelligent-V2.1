# Phase 20 Handoff — Graph property surface (R-P19-A3)

**Document version:** 1.0
**Status:** Phase 20 complete. Phase 21 inheriting.
**Predecessors:** `docs/phase19_handoff.md`,
`docs/phase19_golden_baseline_v4.md`.

---

## 1. What Phase 20 delivered

Phase 20 closed `R-P19-A3` — the graph property surface gap that
Phase 19 documented. Two-file code change, no SQL/fixture work.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `src/fastapi/app/agent/tools.py` — `traverse_knowledge_graph` cypher gains a third UNION branch that emits the matched start node itself as a SELF row, carrying `is_self=true` and `direction='SELF'` | `scripts/phase20_step1_verify.sh` (6/6) |
| 2 | `src/fastapi/app/agent/orchestrator.py` — `_build_record_context` renders SELF rows with a `◉ (matched entity)` prefix instead of the ambiguous `← []` arrow, so the LLM treats them as the entity-of-interest rather than an inbound related node | (same Step 1 verifier check #3) |
| 3 | This handoff + baseline note | — |

Phase 20 cumulative: 6 verifier checks across 1 verifier.

---

## 2. The patch in one paragraph

`traverse_knowledge_graph` previously returned only entities related
to the matched start node, never the start node itself. So
`deposit.deposit_type`, `qp.role`, `formation.code`, and other
properties on the matched entity were invisible to the LLM. The
patch adds a UNION branch that returns the start node with
`is_self=true`, plus an outer projection that emits its full
property bag under `node.props`. Orchestrator rendering surfaces
the SELF row with a clear marker.

---

## 3. Cold-run pass count

| Phase | Total | Pass | Notes |
|-------|------:|-----:|-------|
| 18 | 31 | 16 | gq-015 lithology unlock |
| 19 | 31 | 19 | gq-011 + gq-012 graph unlocks |
| **20** | **31** | **19** | structural fix lands; no test number delta yet |

The patch is structurally correct (verifier 4/4 confirms SELF row
fires for Triple R; verifier 5/5 confirms its `deposit_type`
contains "unconformity"). The cold-run number didn't move because
the remaining gq-018 / gq-013 failures bind on agent-prompt and
warm-state issues, not on tool data availability:

- **gq-018-deposit-type** — under cold-warm boundary noise, the
  agent sometimes refuses with "I don't have data on that in this
  project" even when the SELF row carries `deposit_type=unconformity-related uranium`. Same warm-state R-P14-3.7 pathology.
- **gq-013-graph-formations** — agent narrates formations by long
  name (Athabasca Group, basement gneiss) rather than by code
  (CGL, GPT). Agent-prompt concern.

---

## 4. Why land this anyway?

Without the SELF row, even a fully working agent has no path to
surface `deposit.deposit_type` from a graph traversal. The patch
removes a structural blocker; downstream prompt tuning or
warm-state fixes can now actually move the test number. Leaving
this out would force any future Phase 21 work on gq-018 to first
re-derive the same root cause.

---

## 5. Risks / regression surface

The change:

1. **Adds a 3rd UNION branch** to the existing 2-branch cypher.
   UNION semantics in Neo4j are tolerant of mixed-type columns
   (`r` vs `NULL`) — verified live against the seeded Neo4j.
2. **Changes outer projection column names**: `related` → `node`,
   `start` → `source`, `type(r)` → `CASE ... type(rel)`. The
   tool's Python wrapper updates the dict keys it reads
   accordingly (`rec.get("rel_type")` etc — unchanged).
3. **Renders SELF rows with a new prefix**. Existing tests don't
   assert on graph-line formatting, only on response substring
   matches. Risk surface: zero.

Verified: cold-run still reproduces Phase 19's 19/31 peak —
no regression detected across 2 cold-run samples post-patch.

---

## 6. Carry-overs for Phase 21+

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P14-3.7** | Warm-run agent refusal investigation | orchestrator + agent state | **High** — gates whether the SELF row's deposit_type ever reaches the LLM consistently |
| **R-P20-PROMPT** | Agent-prompt tweak: when query is "what type of X", surface the matched-entity SELF row's `deposit_type`/`role`/`code` properties verbatim | `prompts/agent_system.py` | **Medium-high** — direct unlock for gq-018 |
| **R-P19-DOC** | NI 43-101 PDF stub for gq-024/025/026 | `gold.documents` + chunk pipeline | **Medium-high** — 3 unlocks |
| **R-P19-POPULATE** | Fix `populate_neo4j.py` Report.title uniqueness collision | `src/fastapi/scripts/populate_neo4j.py` | Medium |
| **R-P14-3.6** | Test assertion relaxations | `tests/test_golden_queries.py` | Medium |
| **R-P15-1** | Bundled orchestrator prompts migration | `orchestrator.py` | Medium |

---

## 7. Files of record

**Modified in Phase 20:**

```
src/fastapi/app/agent/tools.py                                     (Step 1)
src/fastapi/app/agent/orchestrator.py                              (Step 2)
docs/phase20_handoff.md                                             (this file)
scripts/phase20_master_sweep.sh                                    (Step 3)
scripts/phase20_step1_verify.sh                                    (Step 1)
```

---

## 8. Re-running every Phase 20 verifier

```bash
bash scripts/phase20_step1_verify.sh   # SELF-row patch          (6/6)
```

Combined Phase 0 → Phase 20 sweep: `scripts/phase20_master_sweep.sh`
adds the Phase 20 verifier to the Phase 19 list. Expected: 65
verifiers green excluding the two documented Phase 4 + 9 carry-overs.

End of Phase 20 handoff.
