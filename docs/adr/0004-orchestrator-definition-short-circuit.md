# ADR 0004: Orchestrator short-circuit for high-confidence definition queries

- **Date**: 2026-05-23
- **Status**: Proposed (implementation gated on SME sign-off + design review)
- **Deciders**: Kyle Maguire (SME)
- **Related**: [[CLAUDE.md hard rules 4 + 5]], `src/fastapi/app/agent/orchestrator/__init__.py`, [[adr-0003-defer-v2m3-gpu-reranker]], `src/fastapi/app/agent/hallucination/`

## Context

The agentic-retrieval graph (Phase 2, §04j) classifies an incoming question
into one of six intents: `factual`, `spatial`, `document`, `computation`,
`viz`, `unknown`. A non-trivial fraction of `factual` traffic is what we
call **definition queries** — surface-level "what is X?" / "define Y?" /
"explain term Z?" where:

- The answer is **literally present** in retrieved chunks (often verbatim
  in a glossary section of an NI 43-101 or a section heading like
  "Geological Setting").
- The reranker top-1 is highly confident (cosine score after rerank > 0.85
  on the per-query-class threshold tuned in `services/reranker.py`).
- No numerical claims are involved.
- A single chunk is sufficient (no multi-hop synthesis).

Today, every query — definition or not — runs primary synthesis through
the vLLM Qwen3-30B-A3B prompt with the full OIUR template, then the
six-layer hallucination gate runs over the output. This is the **right
default** because Hard Rule 4 (citations mandatory) and Hard Rule 5
(six-layer prevention) demand verified output for everything the platform
emits. But for definition queries it pays the LLM cost (~1-3 s on the
A4500 at warm vLLM, $0 monetary at local inference but real latency)
*and* the validator round-trip for an answer that is already in the
chunk text.

Short-circuiting these would:
- Reduce p95 latency on definition traffic from ~3 s to <400 ms.
- Free vLLM TTFT budget for genuinely synthetic queries.
- Reduce risk of LLM paraphrase drift on terms whose precise wording
  matters (NI 43-101 has legally-defined terms; verbatim extraction is
  safer than LLM paraphrase).

The hazard: if we short-circuit by **bypassing** Layers 1–6, we violate
both hard rules. The hazard mitigation is that we still build a
**typed, citation-bearing GeoAnswer** from the chunk's text + provenance
and **rerun all six validators** against that synthetic payload — same
gate, no model call. The decision under consideration is whether that
synthetic build is safe to introduce.

## The six layers and what each requires from a short-circuit payload

| Layer | What it gates | Short-circuit payload must provide |
|---|---|---|
| 1. Retrieval quality | Top-k score threshold | The top-1 chunk's rerank score (already computed) |
| 2. Typed output validation | Pydantic `GeoAnswer` shape | Build OIUR shape (observation = chunk excerpt; interpretation/uncertainty/recommendation either filled from the same chunk or marked `null` with `confidence=low`) |
| 3. Numerical claim verification | All numbers cite a chunk | If chunk contains numbers, they're cited by chunk_id; if any number lacks provenance, abort short-circuit |
| 4. Entity resolution | Named entities resolve | Optional — definition queries usually have no novel entities; if any unknown entity is in the chunk excerpt, abort short-circuit |
| 5. Chunk provenance | Every claim → chunk_id | Trivially satisfied: there is exactly one claim and exactly one chunk |
| 6. Geological constraint rules | Domain invariants | Run unchanged against the assembled payload |

If any layer fails, the orchestrator **falls back to full synthesis** —
short-circuit is opportunistic, never load-bearing.

## Trigger conditions (all must hold)

1. `query_class == "factual"` AND the agentic-retrieval intent
   classifier scored `definition` ≥ 0.85 (new sub-intent — extends the
   6-intent classifier in `agent/agentic_retrieval/intent_classifier.py`).
2. Top-1 rerank score ≥ 0.85.
3. Top-1 to top-2 score gap ≥ 0.15 (no close runner-up).
4. Chunk length is between 80 and 1,500 chars (long enough to be a
   real definition, short enough to be a definition not a synthesis).
5. No numbers in the chunk excerpt that aren't surrounded by
   provenance-locked context (passes a regex pre-check that `\d` in
   the excerpt is accompanied by units / dates / chunk-internal context).
6. `GEO_ANSWER_OIUR_ENABLED=true` and `ORCHESTRATOR_SHORT_CIRCUIT_ENABLED=true`
   (new flag, default `false` until SME sign-off).

If any condition fails, fall through to full synthesis. **No retry,
no warning** — fall-through is the normal case, short-circuit is the
optimization.

## Options considered

| Option | Cost | Decision |
|---|---|---|
| A. **Short-circuit per the trigger conditions above; run all six validators against the synthetic payload; fall through on any validator failure.** | Medium (one new sub-intent classifier + an assembly helper + a flag) | **Chosen.** Preserves Hard Rules 4 + 5; speedup is real for the targeted slice. |
| B. Short-circuit without re-running validators ("the chunk is already trusted"). | Low engineering, **high safety risk** | Rejected. Violates Hard Rule 5 by definition. The validators exist precisely because chunks aren't unconditionally trustworthy — OCR errors, table-vs-prose context, formation-name ambiguity. |
| C. Pre-cache definition answers in a static lookup table. | High maintenance burden | Rejected. Domain vocabulary drifts (deposit type names, mineralogy reclassifications); a static cache would rot. |
| D. Keep full synthesis everywhere; tune vLLM for definition throughput instead. | Already partially done (see vllm-migration memory tuning rounds) | Insufficient. Even at 160 tok/s warm single-stream, the cold-call gap (28→154 tok/s) burns latency on traffic that doesn't need any LLM at all. |

## Decision

**Adopt option A** with the trigger conditions above, gated by
`ORCHESTRATOR_SHORT_CIRCUIT_ENABLED=false` until SME design-review sign-off
covers:

1. The synthetic GeoAnswer construction code path (review surface:
   ~50 LOC in a new `agent/short_circuit.py` module).
2. The new `definition` sub-intent classifier addition (review surface:
   ~10 lines of prompt + a Pydantic field on the intent classifier).
3. A regression test set: 50 known definition queries with their
   expected verbatim answers, run nightly through the eval harness with
   the flag on vs off — assert no quality regression on `Recall@1`,
   `NDCG@10`, or citation-precision.
4. A canary plan: flip the flag on for 1 % of definition traffic for a
   week, monitor `answer_runs.short_circuit_used` (new lineage column,
   already covered by the 5/21 lineage migration), inspect a sample
   nightly.

## Consequences

### What stays the same
- Hard Rules 4 + 5 — citations + six-layer prevention are still applied
  to every emitted answer.
- Fallback to full synthesis is the default for **any** condition not
  satisfied.
- `answer_runs.reranker_version` and the existing audit trail are
  unchanged.

### What this ADR enables
- A latency win on the highest-frequency `factual` sub-slice without
  giving up safety guarantees.
- A clear extension point: if the canary passes, we can expand the
  short-circuit to other query classes (e.g., `document` queries that
  ask "what's in this report?" and can be answered from the
  `silver.reports.title + abstract` text).
- An audit-trail story: `answer_runs.short_circuit_used` makes the
  short-circuit rate observable per-workspace, per-query-class.

### What this ADR closes off
- Caching definition answers in a static table (option C).
- Bypassing the validators (option B). This is non-negotiable per
  Hard Rule 5.

### Open questions to answer at revisit
- What's the actual measured share of `factual.definition` traffic?
  Need 2 weeks of `agentic_retrieval` log analysis before flipping the
  canary. Today (2026-05-23) we have ~1 day of agentic-retrieval
  production traffic.
- Does the short-circuit interact poorly with the OIUR shape's
  `uncertainty` field? A definition answer with `uncertainty=null` and
  `confidence=high` might mislead a downstream agent that's used to
  every answer having a populated uncertainty paragraph. Need a
  product-side decision on whether short-circuited answers should
  *always* populate uncertainty with a generic "This is a literal
  quote from <source>; precision is verbatim, interpretation is the
  caller's responsibility." stub.

## Implementation note (not for this ADR)

The implementation surface is small but touches the orchestrator's
hottest path. When the SME sign-off happens, the implementation goes
into a new `app/agent/short_circuit.py` module rather than inlined into
`agent/orchestrator/__init__.py:resolve_answer` — the orchestrator is
already 3,800+ LOC and any new branch needs to be isolatable.

## References

- [CLAUDE.md](../../CLAUDE.md) — Hard Rules 4 + 5
- `src/fastapi/app/agent/hallucination/` — six-layer implementation
- `src/fastapi/app/agent/agentic_retrieval/intent_classifier.py` —
  intent classifier surface to extend
- `src/fastapi/app/agent/schemas/geo_answer.py` — OIUR shape
- [[adr-0003-defer-v2m3-gpu-reranker]] — for the GPU-contention precedent
  that influenced the "no extra model call" constraint here
