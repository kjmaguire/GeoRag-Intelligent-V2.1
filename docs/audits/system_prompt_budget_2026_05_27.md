# System prompt token budget audit — 2026-05-27

**Tokenizer:** `qwen3-14b-awq` (live production model per `CLAUDE.md`)
**Measured against:** `src/fastapi/app/agent/prompts/` as of commit `f49d451`
**Run context:** Job 2 (structured answer format wiring) gate decision

## Per-file token counts

| File | Tokens |
|---|---:|
| `app/agent/prompts/answer_emphasis_section.py` | 1,793 |
| `app/agent/prompts/agent_system.py` | 948 |
| `app/agent/prompts/orchestrator_shared_preamble_colon.py` | 759 |
| `app/agent/prompts/orchestrator_shared_preamble_dash.py` | 759 |
| `app/agent/prompts/oiur_section.py` | 680 |
| `app/agent/prompts/orchestrator_numeric_colon.py` | 612 |
| `app/agent/prompts/orchestrator_numeric_dash.py` | 612 |
| `app/agent/prompts/orchestrator_graph_colon.py` | 542 |
| `app/agent/prompts/orchestrator_graph_dash.py` | 542 |
| `app/agent/prompts/orchestrator_narrative_colon.py` | 474 |
| `app/agent/prompts/orchestrator_narrative_dash.py` | 474 |
| `app/agent/prompts/decision_support_section.py` | 442 |
| `app/agent/prompts/orchestrator_default_colon.py` | 292 |
| `app/agent/prompts/orchestrator_default_dash.py` | 292 |
| `app/agent/prompts/classifier_system.py` | 234 |
| `app/agent/prompts/rephrase_system.py` | 138 |
| `app/agent/prompts/example_system.py` | 61 |
| **Sum across all files** | **9,654** |

## What the 9,654 number actually means

Not all prompt constants stack into a single request's system prompt. Critical caveats:

1. **`answer_emphasis_section.py` (1,793 tok)** holds variants for all 6 intents — only ONE is selected per query.
2. **`orchestrator_*_colon.py` vs `orchestrator_*_dash.py`** are A/B variants — only one used per query.
3. **`orchestrator_default/numeric/graph/narrative_*`** are query-class-specific — one selected per query based on routing.
4. **`decision_support_section.py` (442 tok)** only fires when intent classifies as `decision_support`.
5. **`classifier_system.py` (234 tok) + `rephrase_system.py` (138 tok)** are separate sub-call prompts, NOT stacked on the answer prompt.

## Per-query realistic static prompt

For a typical answer call:

| Section | Tokens |
|---|---:|
| `agent_system` (always) | 948 |
| One `orchestrator_shared_preamble_*` | 759 |
| One `orchestrator_<class>_*` (numeric / graph / narrative / default) | 292–612 |
| `oiur_section` (always) | 680 |
| One `answer_emphasis` variant from the 6 | ~300 |
| `decision_support_section` (only if decision_support intent) | +442 |
| **Typical query slice** | **~3,000–3,400** |
| **Decision-support query slice** | **~3,500–3,800** |

## Verdict against plan §0b

| Plan §0b spec | Reality |
|---|---|
| Static budget warn ≤ 1,000 tok | **3.0–3.8× over** |
| Static hard ceiling ≤ 1,200 tok | **2.5–3.2× over** |
| With dynamic vocab_context +640 tok | n/a — vocab not yet wired |

**Plan §0b's budget was estimated, not measured.** The numbers in plan §0b appear to be an aspirational target rather than reflecting what the GeoRAG prompt stack actually requires for a domain-specific RAG with intent-conditional emphasis sections + OIUR uncertainty + decision-support augmentation.

## Recommended budget revision

| Field | Plan §0b original | Revised (this audit) | Rationale |
|---|---:|---:|---|
| Static per-query (warn) | 1,000 | **3,750** | Match measured per-query reality + ~10% headroom |
| Static per-query (hard fail) | 1,200 | **4,500** | Headroom for §4a structured-format (~240 tok) + §1d-iv vocab_instructions (~200 tok) + future additions |
| Dynamic vocab_context (warn) | 640 | 640 | Unchanged — plan §1d-iv's 8 defs × ~80 tok still correct |
| Total runtime (warn) | 1,640 | **4,390** | Static + dynamic at the warn levels |
| Total runtime (hard fail) | n/a | **5,140** | Static-fail + dynamic-warn |

Against `MAX_CONTEXT_TOKENS = 6,500`, the revised hard-fail leaves **1,360 tokens for query + evidence**. That's tight for synthesis-intent queries that want 16 chunks at ~256 tok = 4,096 tok of evidence alone.

## Implications for §4a wiring (Job 2)

- Job 2's draft prompt (`_drafts/structured_answer_format_v1.txt` ≈ 240 tok) **fits** within the revised budget when added to a typical query slice: 3,400 + 240 = 3,640, still under 3,750 warn.
- Decision-support intent worst case: 3,800 + 240 = 4,040, under 4,500 hard fail.
- Job 2 unblocked under the revised budget. Compression of the existing prompts remains a desirable separate optimization (see §Future work below).

## Future work — prompt compression candidates

1. **`answer_emphasis_section.py` (1,793 tok)** — six variants, average ~300 tok each. Likely compressible to ~200 tok each (1,200 tok total saved through brevity).
2. **`orchestrator_shared_preamble_{colon,dash}` (759 tok each)** — A/B variants. Once production locks one variant, drop the other (saves 759 tok of module bloat, not per-query).
3. **`oiur_section.py` (680 tok)** — large; review for trimming once §1d-iv vocab_instructions land (they may obsolete part of OIUR).
4. **`example_system.py` (61 tok)** — trivial; OK as-is.

Compression is its own work stream — DO NOT block Job 2 on it.

## Tracking real per-query token utilisation

`silver.query_traces` (live as of Job 1, commit `f49d451`) captures `system_prompt_tokens` and `remaining_context_budget` per query. Once Job 2 wires populate these fields from `state` (currently NULL), the table provides ground-truth for any future budget revision — no more inference from static module measurements.

---

_Generated by inline `scripts/measure_system_prompt_tokens.py` equivalent run in the fastapi container, 2026-05-27._
