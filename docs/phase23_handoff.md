# Phase 23 Handoff — Investigation only (no code shipped)

**Document version:** 1.0
**Status:** Investigation closed. Code change attempted + reverted.
**Predecessors:** `docs/phase22_handoff.md`.

---

## 1. Outcome

Phase 23 set out to unlock gq-013 (CGL + GPT formation
narration). Investigation found that gq-013 is gated by **two
interlocking infrastructure bugs**, both pre-existing this
session:

- **Cache rehydration is unimplemented.** Cache hits silently
  produce empty LLM context and a refusal-text response.
- **vLLM 400 on dense graph payloads.** Tool execution that
  surfaces ~29 graph entities crashes the LLM call, then an
  `UnboundLocalError` cascade masks the root error.

Detail in `docs/phase23_cache_rehydration_investigation.md`.

The minimal fix attempted (disabling the cache-hit shortcut so
tools rerun on every request) exposed Bug B and regressed
cold-run from 24 → 22. Reverted; Phase 22 baseline restored.

**No code shipped this phase.** The two bugs require paired
fixes (rehydrate + LLM resilience) carrying real risk —
deferred to Phase 24 with explicit scope.

---

## 2. Deliverables

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `docs/phase23_cache_rehydration_investigation.md` — full diagnosis of Bug A + Bug B + paired-fix sketches | `scripts/phase23_step1_verify.sh` |
| 2 | This handoff + master sweep | (same verifier) |

---

## 3. Carry-overs (priority-ranked)

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P23-CACHE-REHYDRATE** | Implement candidates_reranked → tool_results rehydration | `orchestrator.py:3979` + `retrieval_cache.py` | **Very high** |
| **R-P23-VLLM-400** | Cap per-entity payload + LLM-call try/except guard | `orchestrator.py:4491` | **High** |
| **R-P19-DOC** | NI 43-101 chunk seed (gq-026) | `gold.documents` | High |
| **R-P22-GRAPH-FORMATION** | gq-013 unlock — needs A + B + prompt | mixed | High |
| **R-P14-3.6** | Test assertion relaxations | tests | Medium |
| **R-P19-POPULATE** | populate_neo4j Report.title fix | populate script | Medium |
| **R-P15-1** | Bundled orchestrator prompts migration | orchestrator | Medium |

---

## 4. Files of record

```
docs/phase23_cache_rehydration_investigation.md
docs/phase23_handoff.md                              (this file)
scripts/phase23_master_sweep.sh
scripts/phase23_step1_verify.sh
```

---

## 5. Re-running

```bash
bash scripts/phase23_step1_verify.sh
bash scripts/phase23_master_sweep.sh
```

End of Phase 23 handoff.
