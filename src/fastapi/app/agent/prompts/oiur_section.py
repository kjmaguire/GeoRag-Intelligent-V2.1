"""OIUR output-rules block — Phase 1 / Step 1.2.

Appended to each colon-variant system prompt when
``settings.GEO_ANSWER_OIUR_ENABLED=True``. Defines the exact markdown shape
the LLM must emit so :func:`app.agent.oiur_parser.parse_oiur_markdown` can
deterministically reconstruct a :class:`GeoAnswer`.

Format contract (kept in lockstep with the parser — change both together):

    ## Observations
    (O1) <factual statement> [CIT:N]
    (O2) <factual statement> [CIT:N]

    ## Interpretations
    (I1) supports: O1, O2. <interpretation text> [CIT:N]
    (I2) supports: O2. competes-with: I1. <interpretation text>

    ## Uncertainty
    **Confidence: Medium**
    Reason: <one or two sentences>.
    Drivers:
    - <driver 1>
    - <driver 2>
    Data to reduce uncertainty: <one specific actionable item>.

    ## Recommended actions
    1. <action>. Rationale: <why> [CIT:N]. Expected gain: <what it resolves>. Risk: <key risk>.
    2. <action>. ...

When a section is not applicable to the query (e.g. factual lookup with no
decision context), the LLM emits:

    ## Recommended actions
    _Not applicable: <one-sentence reason>._

Refusal contract: if the Evidence Set does not support a single observation,
DO NOT emit the OIUR shape at all — emit the legacy refusal text ("I don't
have data on that in this project."). The assembler detects this and routes
to the refusal payload instead of attempting OIUR parse.
"""

from __future__ import annotations

OIUR_OUTPUT_RULES = """
OUTPUT STRUCTURE (OIUR):
Your answer MUST be a markdown document with exactly four H2 sections in this \
order: Observations, Interpretations, Uncertainty, Recommended actions. \
Do not omit a section — if a section does not apply, write \
"_Not applicable: <reason>._" on a single line under the H2 header.

11. ## Observations
    - One observation per line, tagged with a parenthesised id: (O1), (O2), ….
    - Each observation must carry at least one inline citation marker on the \
same line ([NI43:X], [DATA:X], [PGEO:X], or [PUB:X]).
    - Observations are statements of what the evidence directly shows — no \
interpretation, no inference.
    - If you cannot produce at least one observation from the Evidence Set, \
do NOT emit the OIUR structure. Emit the legacy refusal sentence instead \
("I don't have data on that in this project.").

12. ## Interpretations
    - One interpretation per line, tagged (I1), (I2), ….
    - Begin each line with "supports: O<a>, O<b>." naming the observations \
the interpretation rests on. At least one observation id is required.
    - If two interpretations explain the same observations differently, add \
"competes-with: I<other>." before the interpretation text.
    - Cite every factual claim that is not already covered by the supporting \
observation's citation.

13. ## Uncertainty
    - First line: "**Confidence: High**", "**Confidence: Medium**", or \
"**Confidence: Low**".
    - Second line: "Reason: <one or two sentences>." stating what constrains \
confidence (e.g. number of independent sources, data gaps, conflicts).
    - Then "Drivers:" with up to four "- " bullet items naming what most \
limits the interpretation.
    - Then "Data to reduce uncertainty: <one specific actionable item>." — \
must name a specific data type or location (e.g. "One infill hole between \
DDH-07 and DDH-12"). Generic phrases like "more data" or "additional \
information" are NOT acceptable.
    - "**Confidence: High**" is permitted only when the Evidence Set \
contains ≥2 independent sources that agree and no conflicting numeric \
claims on the same measurement. Otherwise use Medium or Low.

14. ## Recommended actions
    - Use a numbered list "1.", "2.", … starting at 1, with no gaps.
    - Each item: "<action>. Rationale: <why> [CIT:N]. Expected gain: <what \
it resolves>. Risk: <key risk>."
    - At least one item must carry an inline citation marker — no \
free-floating recommendations.
    - If the query is a factual lookup with no decision context, write \
"_Not applicable: <reason>._" under this header instead of a list.

OIUR CITATION DISCIPLINE: the OIUR structure does not relax the citation \
rules above — every factual sentence in any section still requires an inline \
[NI43:X] / [DATA:X] / [PGEO:X] / [PUB:X] marker. The structure organises the \
answer; it does not replace the citation contract.
"""


__all__ = ["OIUR_OUTPUT_RULES"]
