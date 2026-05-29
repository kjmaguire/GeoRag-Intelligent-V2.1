# Structured answer format + anti-hallucination — spec

**Status:** Specification + draft prompt at `src/fastapi/app/agent/prompts/_drafts/structured_answer_format_v1.txt`. **NOT WIRED into production prompts.**
**Plan reference:** §4a (the spec), §0b (the budget this lives within), §4d (error catalog interacts with section 7)

## What this is

Plan §4a calls for:

1. An 8-section structured answer format for technical geological / mining questions.
2. Three answer modes (`short`, `detailed`, `evidence_only`) selectable by the caller.
3. Anti-hallucination rules (value-sourcing policy + core "no source = no confident answer" rule).

This doc specifies all three; the draft prompt is the compressed form (≤250 tokens) suitable for insertion into the system prompt's geology section. The draft sits under `_drafts/` so the `_version_registry.py` autoloader skips it — the underscore prefix marks it as proposed-not-wired.

## The 8 sections (plan §4a verbatim)

1. **Direct answer** — one or two sentences
2. **Key numbers** — values with units, source, and effective date
3. **Evidence** — direct quotes or data from retrieved sources with citation
4. **Source citation** — document title, section/page/table, date
5. **Assumptions** — any interpretations or inferences made explicit
6. **Confidence** — High / Medium / Low with brief reason
7. **What is missing or uncertain** — gaps in evidence
8. **Suggested follow-up questions** — 1–2 specific questions that would improve the answer

### Omission rule

Sections are omitted (NOT placeholder-filled) when they don't apply:

- A pure factual lookup ("hole ECK-22-001 lat/long") needs sections 1, 2, 4. Sections 3, 5–8 are noise.
- A synthesis with conflicting sources needs 1, 2, 3, 4, 5, 6, 7. Section 8 may or may not.
- An "exploration history summary" prose answer doesn't have "Key numbers" — section 2 omitted.

The LLM is told to omit, not pad. Padding inflates token spend; omission keeps the answer crisp.

### Interaction with the agentic_retrieval `answer_emphasis`

`retrieval_profile.py` already carries an `answer_emphasis` field per intent (`exact_citation`, `synthesis_with_conflicts`, `competing_hypotheses`, etc.). The structured-format prompt is **additive** to the emphasis hint:

- emphasis steers which sections matter MOST for this intent
- the structured format steers SHAPE

| Intent emphasis | Sections that matter most |
|---|---|
| `exact_citation` | 1, 2, 4 |
| `synthesis_with_conflicts` | 1, 3, 5, 7 |
| `competing_hypotheses` | 1, 3, 5, 6, 7 |
| `anomaly_table` | 2, 3 (table-shaped) |
| `uncertainty_drivers` | 1, 5, 6, 7 |
| `ranked_options` | 1, 5, 7, 8 |

The `assemble_node` can use this matrix when rendering — non-critical sections can be collapsed in the UI.

## Three answer modes (plan §4a)

Caller-selectable via query parameter `answer_mode` (default = `detailed`):

| Mode | What it produces |
|---|---|
| `short` | Section 1 + Section 4 only. ~50 tokens. For mobile / Field-mode (memory `project_phase3_geologist_question_plan.md`). |
| `detailed` | Full structured format. **Default.** |
| `evidence_only` | Section 3 + Section 4. No synthesis, no confidence, no interpretation. For SMEs who want to interpret themselves. |

Modes are not the same as intent. A `factual_lookup` intent in `short` mode still gives a short answer; the same intent in `detailed` mode gives sections 1+2+4 (the section-7 "what's missing" is appended only when something genuinely is missing).

### Where to plumb it

- **Laravel side:** `app/Http/Controllers/Api/V1/QueryController.php` accepts `answer_mode` as a query string param + validates against the enum.
- **FastAPI side:** `app/routers/queries.py` propagates the mode into the `ContextEnvelope` (plan §3a — already partially shipped per memory `project_phase3_geologist_question_plan.md`).
- **Prompt side:** the structured-format block is unconditional; the *renderer* (response_assembler) chooses which sections to keep based on mode.

## Anti-hallucination value-sourcing policy

| Situation | Rule |
|---|---|
| Directly sourced value | Answer with exact citation (doc, page/table, units). |
| Calculated value | Show formula + input values + citations. |
| Inferred value | Label clearly as "interpreted from [source]". |
| Missing evidence | "The available documents do not contain this value." |
| Conflicting evidence | Show both values, cite both sources, flag the conflict. |
| Table value not found | "This specific value does not appear in the retrieved data." |
| Units unclear | State the ambiguity explicitly, do not assume. |

### Core policy (non-negotiable)

> "No source = no confident answer. Never state a technical number without citing the document, page/table, and units."

This is the rule that powers plan §4b's `NUMERIC_GROUNDING_FAILED` and `CITATION_INCOMPLETE` guards. If the model emits a number without `[doc:N p:P]` proximate to it, the validator flags it and the repair loop fires (plan §4b strategy: "expand to parent section OR trigger ReadDocument").

## Draft prompt + budget

`src/fastapi/app/agent/prompts/_drafts/structured_answer_format_v1.txt` is the compressed block (~240 tokens, estimated; will be measured precisely by `scripts/measure_system_prompt_tokens.py` once wired).

Plan §0b budget envelope: **revised** 2026-05-27 per `docs/audits/system_prompt_budget_2026_05_27.md`. Plan §0b's original budget of 1,000 tok was aspirational and far below measured reality (~3,000–3,400 tok per typical query). Revised budget: static per-query ≤ 3,750 tok (warn) / ≤ 4,500 tok (fail). This draft prompt's ~240 tok addition lands comfortably within that envelope.

## Plumbing checklist when Kyle wires this

1. [ ] Run `scripts/measure_system_prompt_tokens.py` against current prompts. PASS / WARN / FAIL on §0b budget.
2. [ ] Move the draft into a real prompts module (e.g. `prompts/structured_answer_format.py`) — convention is one constant per file, exported via `_version_registry.py`.
3. [ ] Register in `_version_registry.py` (see how `oiur_section.py` is registered as the pattern).
4. [ ] Add `answer_mode` enum to `app/models/rag.py` query request model.
5. [ ] Plumb through `ContextEnvelope` so `route_node` can pass it to the prompt builder.
6. [ ] Update `app/agent/response_assembler.py` to read `answer_mode` and decide which sections to render.
7. [ ] Re-run measurement script. Re-baseline.
8. [ ] Re-run Qwen3 citation compliance benchmark (overnight task 8) under the new prompt — Test 6 specifically tests citation placement inside the structured format.

## Acceptance criteria (plan §4a)

- [x] System prompt addition compressed to ≤ 250 tokens — DRAFT MEETS (estimated ~240 tokens; precise measurement when wired)
- [ ] Query "what is the current resource at Rowan?" produces a response with: direct answer, Au oz value with source and date, NI 43-101 citation with section, confidence level — INTEGRATION TEST PENDING WIRING
- [ ] A query with no supporting evidence produces a refusal, not a guess — DEPENDS ON PLAN §4b CITATION GUARDS LANDING

## Decisions captured — 2026-05-27 morning

Kyle reviewed and accepted all four recommendations:

| Q | Decision | Implication |
|---|---|---|
| Q21 | **Default `answer_mode`: `detailed` desktop, `short` Field** | The `ContextEnvelope` already carries Field/Office mode (per `project_phase3_geologist_question_plan.md`). The mode dispatcher in `response_assembler.py` picks the default from the envelope, override-able by an explicit `answer_mode` query param. |
| Q22 | **Omit section 8** (suggested follow-ups) for `factual_lookup` unless answer was incomplete; **always include** for synthesis / hypothesis_generation / decision_support | Renderer logic: `if intent == 'factual_lookup' and confidence == 'High' and no_missing_evidence: omit_section(8)`. Default to include if any condition fails. |
| Q23 | **Yes, add one-line clause** mentioning supersession label to the value-sourcing policy | Done — see [_drafts/structured_answer_format_v1.txt](../../../src/fastapi/app/agent/prompts/_drafts/structured_answer_format_v1.txt). Added: *"conflicting → show both, cite both, flag the conflict. If one source is marked superseded per `silver.document_versions`, label the current source authoritative."* |
| Q24 | **High / Medium / Low words** (not floats) | The draft prompt already uses words. `response_assembler.py` maps Pydantic AI's float confidence → bucket: ≥0.75 = High, 0.5–0.75 = Medium, <0.5 = Low. |
