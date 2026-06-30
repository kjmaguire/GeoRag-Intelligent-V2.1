"""Structured-answer-format block — plan §4a.

Appended to the geology system prompt when the caller wants the 8-section
structured format (the `detailed` and `short` answer modes both consume
it; `evidence_only` skips it). Plan §4a §"omission rule" — the LLM omits
sections that don't apply rather than padding them.

Decisions captured in `docs/architecture/structured_answer_format_spec.md`:
  Q21  default mode by surface (detailed desktop / short Field)
  Q22  section 8 omission (omit for factual_lookup unless incomplete)
  Q23  supersession clause in value-sourcing policy
  Q24  confidence wording: High / Medium / Low

Token cost: ~240 tok measured against Qwen3-14B tokenizer. Sits within
the revised plan §0b budget (≤ 3,750 tok per query, warn) — see
`docs/audits/system_prompt_budget_2026_05_27.md`.

Consumer: `app.agent.response_assembler` reads `answer_mode` off the
context envelope and chooses whether to include this block + which
sections the assembler renders from the LLM output.
"""

from __future__ import annotations

STRUCTURED_ANSWER_FORMAT = """
## Geology answer format

For technical geological / mining questions, structure the answer in
these sections (omit sections that don't apply, never invent content):

1. Direct answer (1–2 sentences)
2. Key numbers (value + unit + citation + effective date)
3. Evidence (quoted source text with citation [doc:N p:P])
4. Source citation (document title, section/page, date)
5. Assumptions (any interpretations or inferences, explicit)
6. Confidence (High / Medium / Low + one-line reason)
7. What is missing or uncertain (gaps in evidence)
8. Suggested follow-up questions (1–2 specific)

## Value-sourcing policy

  directly cited → answer + exact citation (doc, page/table, units)
  calculated     → show formula + input values + citations
  inferred       → label "interpreted from [source]"
  missing        → "The available documents do not contain this value."
  conflicting    → show both, cite both, flag the conflict. If one
                   source is superseded (per document_versions), label
                   the current source authoritative.
  units unclear  → state the ambiguity; do not assume

Core rule: no source = no confident answer. Never state a technical
number without citing the document, page/table, and units.

## Answer mode (query parameter)

  short          → direct answer + citation only
  detailed       → full 8-section format (default)
  evidence_only  → citations + source text, no synthesis
""".strip()


__all__ = ["STRUCTURED_ANSWER_FORMAT"]
