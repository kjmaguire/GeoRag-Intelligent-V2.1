# ADR 0006: Agentic retrieval — one LangGraph + six routed intents (not six subgraphs)

- **Date**: 2026-05-23
- **Status**: Accepted (codifies the as-built architecture from Phase 2 / 2026-05-20)
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: §04j wording in `georag-architecture.html` v1.49 that describes "six distinct LangGraph subgraphs" for the agentic retrieval surface.
- **Related**: `src/fastapi/app/agent/agentic_retrieval/graph.py`, `src/fastapi/app/agent/agentic_retrieval/intent_classifier.py`, `src/fastapi/app/agent/decision_support_classifier.py`, [[memory/project_phase2_geologist_question_plan.md]] (Phase 2 landing 2026-05-20)

## Context

CC-01 Item 4 verification (2026-05-23) flagged a discrepancy between the
architecture doc's §04j and the shipped code:

- **Doc says:** "Six LangGraph subgraphs — factual lookup, synthesis,
  hypothesis generation, anomaly detection, uncertainty quantification,
  decision support. Each with its own retrieval strategy and OIUR
  answer template."
- **Code is:** One `StateGraph` (`agentic_retrieval/graph.py:54`)
  implementing `classify → route → execute → assemble → validate →
  demote → persist`. The six intents are first-class names in
  `intent_classifier.py:48-62` but they're routed *inside*
  `execute_node`, not compiled as separate subgraphs.

Verification details:

```
$ grep -rn "StateGraph(" src/
src/fastapi/app/agent/agentic_retrieval/graph.py:56:    g: StateGraph = StateGraph(AgenticRetrievalState)
src/fastapi/app/services/target_recommendation/graph.py:62:    g: StateGraph = StateGraph(TargetRecommendationState)
src/fastapi/app/services/report_builder/graph.py:98:    g: StateGraph = StateGraph(ReportBuilderState)
```

Of those three `StateGraph` instances, only the first one is the
agentic-retrieval surface. The other two are unrelated features
(Target Recommendation Cockpit, Report Builder).

OIUR answer schema *is* shipped — `app/agent/schemas/geo_answer.py`,
`oiur_parser.py`, `prompts/oiur_section.py` — and is the
per-intent answer template the doc describes. So that half of the
spec is met.

## Decision

**Keep the as-built architecture (1 graph + 6 routed intents). Update
the architecture doc to match.** No code refactor.

## Rationale

1. **Functional equivalence.** The doc's intent (`each with its own
   retrieval strategy and OIUR answer template`) is met in the as-built
   code:
   - `intent_classifier.py` resolves the intent (6 named scoring paths
     + `decision_support_classifier.py` for the regulatory-touch path).
   - `retrieval_profile.py` carries per-intent retrieval knobs
     (top-k, filter shape, rerank weight, etc.) consumed by
     `execute_node`.
   - `prompts/oiur_section.py` + `prompts/decision_support_section.py`
     carry per-intent answer-template overrides applied by
     `assemble_node`.

   The 6 intents are first-class identities in code (not strings
   buried inside execute_node), they each affect retrieval shape and
   prompt assembly, and the OIUR answer schema is uniform across them.
   What "subgraph" would buy that the routed-intent shape doesn't is:
   per-intent node graphs (e.g. anomaly_detection might fan out a sub-tree
   that doesn't exist for factual_lookup). The current intents *don't*
   need divergent node graphs — they all need `classify → retrieve →
   answer`. The doc's "6 subgraphs" framing was a forecast of complexity
   that the implementation didn't need.

2. **Lower complexity cost.** Six compiled `StateGraph` objects means
   six `lru_cache`s, six startup compile passes, six places to keep in
   sync when the `classify → route → execute → assemble → validate →
   demote → persist` pipeline shape changes. Today, when we add
   `persist_node` (which we did in Phase 4 follow-up — see graph.py:47
   comment), we change one file. Under the six-subgraph shape we'd
   change six.

3. **Debuggability.** A single graph means one trace per query in
   Langfuse / Sentry. Six subgraphs would mean classify-then-dispatch
   spans every query, with the active subgraph hidden behind the
   dispatcher — harder to scan in the trace UI, and harder to reason
   about under load.

4. **Reversibility.** If a future intent genuinely needs a divergent
   node graph (e.g. `hypothesis_generation` wants its own retrieval +
   evidence-fanout loop), it can be lifted out into a sibling
   `StateGraph` at that time. The intent → routing decision in
   `execute_node` is the natural seam. No information is lost by
   shipping one graph today.

## Consequences

### Positive

- No code refactor — the agentic retrieval path stays stable through
  the cc-01 verification and the autonomous run downstream of it.
- The architecture doc gets corrected to describe what shipped.
- Adding a 7th intent (e.g. a hypothetical `comparative_jurisdiction`
  intent for cross-region cross-corpus queries) is one new entry in
  `intent_classifier.py`'s vocabulary + one new
  `retrieval_profile` profile + one new prompt section — no new
  graph compilation.

### Negative

- The doc → code drift is now formally accepted, not a bug. Future
  readers who skim §04j without reading this ADR will think the
  architecture is more complex than it is.
- Future architectural reviews need to consciously evaluate
  "should this intent be its own subgraph?" rather than answering by
  default. The decision is now an active engineering choice, not a
  fixed shape.

## Options considered

| Option | Shape | Why rejected |
|---|---|---|
| **Adopted: keep current 1-graph + 6-intents, doc edit only** | Code stays; §04j updated to describe one StateGraph with intent-routed execute_node. | Cheapest. Functionally equivalent. Reversible if a future intent needs divergence. |
| Refactor to 6 distinct `StateGraph` objects | Six `agentic_retrieval/<intent>/graph.py` files, each compiled separately, dispatched by a thin router. | Multi-day refactor with regression risk on a working path. Buys nothing measurable today — no intent currently needs a divergent node graph. |
| Defer the decision; flag the discrepancy in the doc with a TODO | §04j gets a note "the shipped implementation differs — see TODO Phase X". | Leaves the doc/code drift open. Forces every reader to re-derive the question. Worst of both worlds. |

## Doc edit applied alongside this ADR

`georag-architecture.html` §04j updated to read:

> The agentic retrieval surface is one compiled LangGraph
> (`classify → route → execute → assemble → validate → demote → persist`)
> that routes between six intents inside `execute_node`:
> `factual_lookup`, `synthesis`, `hypothesis_generation`,
> `anomaly_detection`, `uncertainty_quantification`, `decision_support`.
> Each intent has its own retrieval profile
> (`agentic_retrieval/retrieval_profile.py`) and answer-template
> overrides (`prompts/oiur_section.py`,
> `prompts/decision_support_section.py`). The OIUR answer schema
> (`schemas/geo_answer.py`) is uniform across intents.
>
> The "six subgraphs" framing in earlier drafts is superseded by
> ADR-0006 (2026-05-23) — see that ADR for the rationale.

## Verification

- `intent_classifier.py:48-62` lists all six intent names.
- `decision_support_classifier.py` exists for the regulatory-touch
  branch; the other 5 score via regex patterns in `intent_classifier.py`.
- `app/agent/schemas/geo_answer.py` carries the OIUR shape.
- Flag: `AGENTIC_RETRIEVAL_V2_ENABLED` (default off in production until
  Phase X gate-flip — see `memory/project_phase2_geologist_question_plan.md`).
