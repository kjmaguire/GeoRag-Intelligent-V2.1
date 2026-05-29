"""Decision-support output rules — Phase 1 / Step 1.4.

Appended to the system prompt AFTER the OIUR rules block when the
:mod:`app.agent.decision_support_classifier` flags the query as a
decision-support intent. Defines the markdown shape inside the
``## Recommended actions`` section so the parser
(:func:`app.agent.oiur_parser.parse_oiur_markdown`) can reconstruct the
:class:`app.agent.schemas.DecisionSupport` extras.

Format contract (kept in lockstep with the parser — change both together):

    ## Recommended actions
    1. <action>. Rationale: <why> [CIT:N]. Expected gain: <what it resolves>. Risk: <key risk>.
    2. <action>. ...

    ### Unresolved prerequisites
    - <item that must be known before the decision can be made confidently>
    - <item>

    ### Reporting / regulatory constraints
    - <NI 43-101 / CRIRSCO / applicable code implication for the recommendation>

When the corpus does not support a defensible ranking, the LLM emits an
explicit deferral sentence inside the section header instead of a list:

    ## Recommended actions
    _Ranking deferred: <reason>._

    ### Unresolved prerequisites
    - <data needed before ranking can be defended>

The ``regulatory_constraints`` requirement: when the query touches resource
classification, drilling, sampling, or QA/QC, the LLM MUST emit at least
one bullet under "Reporting / regulatory constraints". This is the plan's
"≥1 NI 43-101 implication must be surfaced" criterion.
"""

from __future__ import annotations

# Generic decision-support rules — appended for ALL decision-support queries.
DECISION_SUPPORT_OUTPUT_RULES = """
DECISION-SUPPORT OUTPUT (applies to this query — the user is asking for a \
recommendation, ranking, or next-steps decision):

15. ## Recommended actions
    - Render the ranked options as a numbered list per rule 14.
    - If the Evidence Set does not support a defensible ranking (e.g. \
insufficient data to differentiate options), DO NOT fabricate an order. \
Replace the numbered list with a single line: \
"_Ranking deferred: <one-sentence reason>._" — and populate Unresolved \
prerequisites with the specific data needed before ranking can proceed.

16. ### Unresolved prerequisites
    - Required H3 subsection inside ## Recommended actions.
    - Use "- " bullet items naming concrete items that must be known \
BEFORE the decision can be made confidently (e.g. "Confirmed CRM pass-rate \
for batch B-2024-17", "Updated topography along section 5+50N").
    - When the corpus supports the ranking outright, write "- None — the \
retrieved evidence supports the ranking as stated."

17. ### Reporting / regulatory constraints
    - Required H3 subsection inside ## Recommended actions.
    - Use "- " bullet items naming any NI 43-101, CIM Definition Standards, \
CRIRSCO, JORC, or applicable provincial-code implications for the \
recommended action.
    - Include the clause / section reference when the Evidence Set carries \
it (e.g. "NI 43-101 §1.3 requires QP sign-off for any Measured Resource \
classification").
"""


# Stronger variant — appended when the query also touches resource
# classification, drilling, sampling, or QA/QC. The plan's "≥1 NI 43-101
# implication must be surfaced" criterion is enforced via the prompt;
# schema validation cannot prove the LLM emitted a relevant constraint.
DECISION_SUPPORT_REGULATORY_REQUIRED = """
REGULATORY REQUIREMENT: This query touches resource classification, drilling, \
sampling, or QA/QC. The Reporting / regulatory constraints subsection MUST \
contain at least ONE non-trivial entry citing a specific NI 43-101 / CIM / \
CRIRSCO / provincial-code clause. "- None applicable." is NOT acceptable for \
this query shape — if you cannot identify an applicable clause from the \
Evidence Set, name the clause that WOULD apply once the missing data lands \
(e.g. "NI 43-101 §1.3 will apply once a Measured classification is sought").
"""


__all__ = [
    "DECISION_SUPPORT_OUTPUT_RULES",
    "DECISION_SUPPORT_REGULATORY_REQUIRED",
]
