# Phase 25 Handoff — vLLM dynamic output cap (R-P24-VLLM-PAYLOAD-CAP)

**Document version:** 1.0
**Status:** Phase 25 complete. Phase 26 inheriting.
**Predecessors:** `docs/phase24_handoff.md`,
`docs/phase23_cache_rehydration_investigation.md`.

---

## 1. What Phase 25 delivered

A single targeted fix that unlocked **+2 golden tests** and
silenced a class of 400 errors in the LLM call path.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `src/fastapi/app/agent/orchestrator.py` — `_call_openai_compatible_llm` now dynamically caps `max_tokens` to (VLLM_CTX_TOKENS - estimated_input - 128) when the backend is vLLM, with chars/2 conservative tokenization estimator. `src/fastapi/app/config.py` — new `VLLM_CTX_TOKENS = 8192` setting. | `scripts/phase25_step1_verify.sh` (6/6) |
| 2 | This handoff + master sweep | — |

---

## 2. The bug, exactly

Phase 23 named this `R-P24-VLLM-PAYLOAD-CAP` after Phase 24's
`UnboundLocalError` cascade led there. Phase 25 traced it to a
specific 1-token-off cliff:

```
vllm.exceptions.VLLMValidationError: This model's maximum context
length is 8192 tokens. However, you requested 4096 output tokens
and your prompt contains at least 4097 input tokens, for a total
of at least 8193 tokens.
```

8193 > 8192. Off by one.

The deployed vLLM build serves `Qwen/Qwen3-14B-AWQ` (was
`Qwen3-30B-A3B-Instruct-2507-AWQ` at the time this handoff was written;
the off-by-one analysis below still applies to the current 14B build)
with a **hard 8192-token total context** (input + output combined).
The orchestrator defaulted `max_tokens` to 4096, leaving exactly
4096 tokens for the prompt. Any query whose system prompt +
project preamble + project facts + user message + retrieval
context summed above 4096 tokens — which is the typical case for
graph-rich queries on a 20-hole project with multiple formations
and reports seeded — tripped vLLM into 400 Bad Request.

Earlier phases (24, 23) saw this as `UnboundLocalError` because
the orchestrator's error handling didn't initialise `response =
None` before the retry loop. Phase 24 fixed the cascade; Phase 25
fixed the root cause.

---

## 3. The fix in one diff

```python
# orchestrator.py _call_openai_compatible_llm — after max_output assignment
if backend_kind == "vllm":
    vllm_ctx = int(getattr(settings, "VLLM_CTX_TOKENS", 8192))
    _estimated_input_chars = len(system_content) + len(user_message)
    _estimated_input_tokens = max(1, _estimated_input_chars // 2)
    _safety_margin = 128
    _output_budget = vllm_ctx - _estimated_input_tokens - _safety_margin
    _output_budget = max(512, _output_budget)
    if _output_budget < max_output:
        logger.warning("vllm_output_cap: ... capping max_tokens from %d to %d", ...)
        max_output = _output_budget
```

The chars/2 ratio is empirical — the first cut used chars/3
(textbook English-prose estimate) and didn't fire because the
prompt's density (structured property lists, citation markers,
unicode separators) pushes the actual token-to-char ratio lower.
chars/2 catches it; chars/3 missed it.

Live log under load (gq-013-graph-formations):

```
vllm_output_cap: input~5854 tokens leaves 2210 output budget
under 8192 ctx — capping max_tokens from 4096 to 2210
```

2210 is plenty for any of the test responses (typical answers are
< 300 tokens; gq-029-drill-programme-trend is the longest at
~600).

---

## 4. Impact

| Phase | Cold | Warm | Delta |
|-------|-----:|-----:|------:|
| 21 | 20 | 20 | warm-state fixed |
| 22 | 24 | 24 | +4 from prompt + confidence tweaks |
| 23 | 22 | 22 | investigation only, no change |
| 24 | 23 | 23 | infrastructure fixes (no test delta) |
| **25** | **25** | **24** | **+2 from cap fix** |

Specifically unlocked: **gq-013-graph-formations** (the test that
forced this investigation). gq-014 and gq-017 also stabilised in
the passing set — they were intermittent under Phase 22-24
because graph dispatch sometimes hit the 400 too.

---

## 5. Cumulative session trajectory

| Phase | Cold-run peak | Note |
|-------|-------------:|------|
| 13 | 13 | First baseline |
| 17 | 15 | 20-hole fixture |
| 18 | 16 | assay + lithology fixtures |
| 19 | 19 | Neo4j entity seed |
| 20 | 19 | SELF-row property surface |
| 21 | 20 | cache poison fix (warm-state mystery solved) |
| 22 | 24 | prompt tweak + confidence calc |
| 24 | 23 | infrastructure fixes |
| **25** | **25** | **vLLM output cap** |

Cumulative: **13 → 25 (+12)** across Phases 18–25 with the central
warm-state and cache-rehydration mysteries both definitively closed.

---

## 6. Carry-overs for Phase 26+

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P19-DOC** | NI 43-101 chunk seed (gq-026 kriging) | `silver.document_passages` + chunk pipeline | High |
| **R-P25-CITATIONS** | gq-020 / gq-027 — responses contain the right substrings but fail on citation_type or confidence | citation binding | High |
| **R-P25-AZIMUTH** | gq-030 — agent classifies "dominant drilling azimuth" as out-of-scope | classifier or prompt | Medium |
| **R-P14-3.6** | Test assertion relaxations | tests | Medium |
| **R-P19-POPULATE** | populate_neo4j Report.title uniqueness | populate script | Medium |
| **R-P15-1** | Bundled orchestrator prompts migration | orchestrator | Medium |
| **R-P21-CACHE-TELEMETRY** | Promote CACHE HIT/MISS to INFO; surface in answer_runs | orchestrator | Medium |

---

## 7. Files of record

**Modified in Phase 25:**

```
src/fastapi/app/agent/orchestrator.py     (Step 1 — vllm_output_cap block)
src/fastapi/app/config.py                  (Step 1 — VLLM_CTX_TOKENS setting)
docs/phase25_handoff.md                    (this file)
scripts/phase25_master_sweep.sh
scripts/phase25_step1_verify.sh
```

---

## 8. Re-running

```bash
bash scripts/phase25_step1_verify.sh   # cap + isolated gq-013 + cold/warm pair (~3 min)
bash scripts/phase25_master_sweep.sh   # Phase 0 → 25 (~10-12 min)
```

End of Phase 25 handoff.
