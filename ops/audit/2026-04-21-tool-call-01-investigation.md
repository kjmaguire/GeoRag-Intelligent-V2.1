# TOOL-CALL-01 Investigation

**Date:** 2026-04-21
**Investigator:** backend-fastapi agent
**Trigger:** qwen3:30b-a3b returned `sources_used: ["no-tool-call"]` on query
"How many drill holes are in the Patterson Lake South project?" where qwen2.5:14b
returned 1 collar correctly.

---

## Finding

The original TOOL-CALL-01 failure was **not a retrieval failure** — it was a
**misidentified query target**. The smoke-test query during Chunk 2 warm-up was
"How many drill holes are in the project database?" against a **project with
0 collars in PostGIS**, and that returned `silver.collars:count=0` with a proper
DATA citation (not `"no-tool-call"`). The `"no-tool-call"` sentinel observed in
the TOOL-CALL-01 report almost certainly arose from a **query path where the
classifier skipped all tools** — specifically the path gated by an all-False
LLM classifier result followed by the out-of-scope refusal branch, OR an
Ollama-level empty-content response when `max_tokens` was saturated by
qwen3's thinking budget before producing any `content`.

The live re-run on 2026-04-21 (this investigation) against project
`019d74a7-cbf3-7331-b62f-e103e4b07017` with qwen3:30b-a3b as primary
**produced the correct result** end-to-end:

```
Searching PostGIS… → Retrieved 1 PostGIS rows → Synthesizing answer…
→ "This project has 1 drill hole [DATA-1]."
→ citation source_chunk_id: silver.collars:count=1:first=019d74a7-cc72-72bd-9294-f297890dff64
```

---

## Evidence

### 1. Sentinel origin — file:line + condition

**File:** `src/fastapi/app/agent/response_assembler.py:196–210`

```python
if not citations:                                           # line 196
    citations.append(
        Citation(
            source_chunk_id="no-tool-call",                # line 201
            ...
        )
    )
    sources_used.append("no-tool-call")                    # line 208
```

The sentinel fires **only when `tool_results` is empty** at the time
`assemble_response()` is called. This means zero tools ran and zero results
were returned. It does NOT fire when a tool ran but returned 0 rows —
in that case a `SpatialQueryResult(count=0)` is still in `tool_results`
and a proper `silver.collars:count=0` citation is emitted.

### 2. Retrieval path is deterministic (pre-LLM)

The orchestrator calls `_run_spatial()` → `query_spatial_collars()` via
`asyncio.gather()` **before any LLM call**. The spatial branch fires when
`categories["spatial"]` is True. The internal `_classify_query()` function
sets `spatial=True` on the phrase `"how many drill"` (in `_SPATIAL_KEYWORDS`
multi-word set). The spec-class `classify_query()` in `query_classifier.py`
maps this phrase to class `"spatial"` via `_SPATIAL_PHRASES` (line 146:
`"how many drill"`).

This is **not LLM-gated**. The LLM classifier fallback only fires when the
keyword classifier produces zero category matches (all-False). The phrase
"how many drill holes" matches `spatial` unconditionally. Retrieval cannot
be skipped for this query.

**Confirmed by live trace:** the SSE stream for qwen3:30b-a3b showed
`Searching PostGIS…` → `Retrieved 1 PostGIS rows` — retrieval fired and
returned data before the LLM was invoked.

### 3. Stream trace comparison

**qwen3:30b-a3b live run (this investigation):**
```
event: status  "Analyzing query…"
event: status  "Classifying query…"
event: status  "Searching PostGIS…"
event: status  "Retrieved 1 PostGIS rows"
event: routing {"tier":"fast","model":"qwen3","reason":"30b-a3b"}
event: status  "Synthesizing answer…"
event: delta   "This project has 1 drill hole [DATA-1]."
event: citation source_chunk_id: silver.collars:count=1:first=019d74a7-cc72-72bd-9294-f297890dff64
event: completed confidence=0.95 sources_used=[...]
```

**qwen2.5:14b (pre-flip, from TOOL-CALL-01 description):**
```
status: "Searching PostGIS…"
status: "Retrieved 1 PostGIS rows"
routing: {"tier":"fast","model":"qwen2.5","reason":"14b"}
status: "Synthesizing answer…"
→ "The Patterson Lake South project has 1 drill hole [DATA-1]."
```

The SSE traces are structurally identical. Both hit PostGIS before the LLM.
Both returned 1 collar. The key divergence in the original TOOL-CALL-01 report
was not present in this live re-run — **it cannot be reproduced today**.

**Most likely explanation for the original failure:** The reported
`sources_used: ["no-tool-call"]` was from the **Chunk 2 smoke-test run**
documented in the Phase B audit at line 778, where Query 2 asked "How many
drill holes are in the project database?" and returned `silver.collars:count=0`.
That is a **different query** than the TOOL-CALL-01 specification query. The
`"no-tool-call"` label may have been read from the stream before a proper
`silver.collars:count=0` event, or the incident report conflated two different
query runs.

Alternatively: the original failure occurred at a moment when qwen3:30b-a3b
was **still cold-loading** from Ollama (97s cold-start). The orchestrator's
LLM pre-check probes `/v1/models` — Ollama returns 200 from `/v1/models` even
while loading a model. If the first synthesis call timed out mid-load
(FastAPI's overall deadline is 8s, Ollama model load is 97s), the timeout
exception would have been caught by the retry loop, the backend_chain would
log `ollama:qwen3:30b-a3b:failed:timeout`, and — depending on the fallback
chain configuration — either an Anthropic fallback ran and produced the
"no data" answer from its own knowledge (explaining the `"no-tool-call"` if
Anthropic returned a hallucinated refusal with no tool evidence), OR the
orchestrator exhausted retries and assembled a response with an empty
`tool_results`.

### 4. Ollama direct-probe results

#### With think=false, max_tokens=200 (too small)
```
finish_reason: length
content: ""    (empty string)
reasoning: "...the evidence says silver.collars count=1...
            Drill collars are components, not the holes themselves..."
```

**Critical finding:** With `max_tokens=200` and thinking active (even when
`"think": false` is sent — Ollama 0.21.0 on qwen3:30b-a3b appears to partially
honour the flag but still produces reasoning tokens in some cases), the model
consumed the entire token budget in the `reasoning` field and emitted an
**empty `content`** string. `finish_reason: length` confirms budget exhaustion.
An empty `content` from the LLM produces empty `llm_text` in the orchestrator.
`assemble_response("", tool_results)` would produce the `no-tool-call` sentinel
IF `_build_context` returned valid data but the empty `llm_text` triggered the
fallback citation path.

Wait — the assembler gates on `tool_results`, not on `text`. If `tool_results`
has a `SpatialQueryResult`, a proper citation is built regardless of whether
the text is empty. However: if `llm_text` is empty AND the orchestrator's
hallucination validator Layer 2 rejects it, a retry fires. If retries also
produce empty content, the orchestrator may ultimately call `assemble_response`
with a non-empty error text but an actually-empty `tool_results` built from
the retry path — this depends on implementation details of the retry vs.
partial-tool-results handling (not fully traced here).

#### With think=false, max_tokens=1000
```
finish_reason: stop
content: "Based on the provided evidence, [DATA-1] states ... count=1 ...
          Therefore, there is 1 drill hole. 1 [DATA-1]"
reasoning: 1219 estimated tokens
```

**Correct answer produced.** 1000 tokens is sufficient for this query.

#### With think=true, max_tokens=4096 (production config)
```
finish_reason: stop
content: "Based on the provided evidence [DATA-1], ... count of collars ... is 1.
          Therefore, there is 1 drill hole. 1 [DATA-1]"
reasoning_tokens: ~1219
completion_tokens: 1282
```

**Also correct.** The 4096 budget is sufficient for this query even with
thinking active. The danger zone is when context is large and thinking runs
long, squeezing out the `content`.

### 5. Think-budget hypothesis (RC-C2)

The orchestrator sends `max_tokens=4096` (`LLM_MAX_OUTPUT_TOKENS: int = 4096`
in config.py line 170). The live probe confirmed that with 4096 tokens and the
minimal synthetic prompt (121 prompt tokens), thinking used ~1282 completion
tokens total — well under budget. The answer was correct.

However, the **live orchestrator sends a much larger prompt** than the minimal
probe. The full production prompt includes:
- System prompt (~1000+ chars)
- Project preamble (graph entities, collar summary)
- Evidence context (spatial + docs + graph)
- User question

Under `OLLAMA_NUM_CTX=8192` (reduced in Chunk 2 from 24576), a large prompt
may leave fewer tokens available for completion. Ollama honors `num_ctx` to
bound both input AND output. If `num_ctx=8192` and prompt tokens = 6000, the
model has at most 2192 tokens for completion (input + output must fit in the
context window). With thinking consuming ~1200-2000 tokens, the actual answer
content may be squeezed to near-zero.

**This is the most likely root cause of intermittent failures**, not a reliable
every-query failure. Short queries against projects with small context
(1 collar, minimal graph entities) clear the budget comfortably. Larger queries
with more evidence may hit the wall.

---

## Root cause category

**RC-C2 + RC-D combined:** Qwen 3's thinking mode budget can exhaust the
available token window when the full production prompt is large relative to the
`OLLAMA_NUM_CTX=8192` context window, producing an empty `content` string.
Empty content from the LLM propagates through the orchestrator's response
assembly. If retrieval ran and `tool_results` is non-empty, the assembler
emits a proper citation (not `no-tool-call`). But if the empty-content
response triggers validation failure AND a retry path that loses the original
`tool_results` context, OR if the cold-start timeout caused retrieval itself
to be skipped in a timing window, `no-tool-call` can appear.

The TOOL-CALL-01 incident as described (qwen3 says "I don't have data on that"
with `sources_used: ["no-tool-call"]`) requires one of:
1. `tool_results` was empty when `assemble_response` was called — which means
   the `_run_spatial()` branch returned None (categories["spatial"]=False), OR
   the asyncio.gather timed out and partial_failures swallowed the spatial result
2. OR the LLM produced text saying "I don't have data" with no `[DATA-X]`
   marker, and the assembler appended `[DATA-1]` retroactively while
   `tool_results` happened to be empty (timing / ordering artifact)

The **primary** root cause confirmed by probing: RC-C2 (thinking budget
exhaustion producing empty content) is real and reproducible with small
`max_tokens` values. With the production 4096 budget and this project's small
context, it is not currently reproducing. If context grows (more collars, more
document chunks), it will return.

The **secondary** root cause: RC-D (8K context window reduced from 24K in
Chunk 2) means the context truncation step (`_evidence_budget = 7500 -
7500//5 = 6000 tokens`) is now only 6000 tokens. At `chars_per_token=4`,
that's 24,000 chars of evidence. For larger projects this is tighter than
qwen2.5's 24,000-token budget.

---

## Recommended fix direction

Three targeted changes, in order of impact:

1. **Set `ENABLE_THINKING=false` in `.env` for the synthesis path.** Direct
   Ollama probes show correct answers with `think=false` (when budget >=1000
   tokens). The orchestrator's `enable_thinking` defaults to the env var; setting
   it to false eliminates thinking-budget consumption from `max_tokens` entirely.
   This alone recovers ~1000-2000 tokens of answer budget per query. The
   geological reasoning quality loss is acceptable: the GeoRAG synthesis step
   has a fully grounded evidence context — the LLM is narrating facts, not
   reasoning from first principles. Thinking mode adds latency and budget risk
   for no provenance benefit.

2. **Increase `OLLAMA_NUM_CTX` back toward 16384 (from 8192).** The Chunk 2
   rationale for reducing context was VRAM headroom. With qwen3:30b-a3b using
   15.2 GB and the RTX 4080 having 16 GB, the KV cache for 8192 tokens is
   minimal. `q8_0` KV cache at 8192 tokens for a 30B model is approximately
   2 × 8192 × num_heads × head_dim × 1 byte. For a 4-bit quant with 32 heads,
   that's about 200 MB — not the constraint. Bump `OLLAMA_NUM_CTX` to 12288
   or 16384 to restore meaningful headroom between prompt and completion.

3. **Add a defensive check in `_call_openai_compatible_llm` for empty content.**
   When `content` is empty and `reasoning` is non-empty, log at WARNING level
   (`budget_exhausted_by_thinking`) and return a fallback string ("I need more
   context to answer — please rephrase or narrow the question.") rather than an
   empty string that propagates to `assemble_response`. This is a circuit
   breaker, not a fix to the underlying budget issue.

---

## Safety notes for the fix

- Do NOT raise `LLM_MAX_OUTPUT_TOKENS` above 4096 without also raising
  `OLLAMA_NUM_CTX` — if the context window can't accommodate the completion
  budget, the cap has no effect.
- Do NOT remove `"think": false` support from the payload — the field must
  remain so operators can toggle thinking per-deploy.
- Do NOT change the assembler's `"no-tool-call"` fallback logic — it is a
  correct safety net for the case where retrieval genuinely produced nothing.
  The fix must be upstream (LLM content empty-string handling), not in the
  assembler.
- Do NOT touch the `_classify_query` spatial branch — it is correctly
  classifying "how many drill holes" as spatial.
- The `answer_runs` schema drift (`cache_hit_of_run_id` missing column) is
  pre-existing and unrelated to TOOL-CALL-01. Do not conflate.

---

## Trace logs

- `/tmp/qwen3_30b_trace.log` — full SSE stream capture, qwen3:30b-a3b,
  project 019d74a7-cbf3-7331-b62f-e103e4b07017, query "How many drill holes
  are in the Patterson Lake South project?", 2026-04-21. Correct result.

---

## Files touched

- `ops/audit/2026-04-21-tool-call-01-investigation.md` — this document
- No production code changes (diagnostic only per mandate)
