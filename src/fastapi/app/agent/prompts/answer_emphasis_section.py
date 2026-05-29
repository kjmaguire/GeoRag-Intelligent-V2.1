"""Per-intent answer-emphasis prompt fragments — Phase 2 / Step 2.5.

Appended to the OIUR base prompt by the agentic-retrieval ``assemble_node``
when a non-default ``RetrievalProfile.answer_emphasis`` is selected. Each
fragment biases which OIUR sections the LLM emphasises *without changing
the four-section contract* — the schema validators still apply.

The plan's Step 2.3 "Answer emphasis" column maps directly to these
fragments:

  exact_citation              — factual_lookup
  synthesis_with_conflicts    — synthesis
  competing_hypotheses        — hypothesis_generation
  anomaly_table               — anomaly_detection
  uncertainty_drivers         — uncertainty_quantification
  ranked_options              — decision_support (Phase 1.4 already covers
                                most of this; this fragment is additive)

Fragments are pure strings — pickled into the prompt cache as constants
so each (base + emphasis) pair caches as its own warm prefix.
"""

from __future__ import annotations

from app.agent.agentic_retrieval.retrieval_profile import AnswerEmphasis


# ---------------------------------------------------------------------------
# Per-emphasis fragments
# ---------------------------------------------------------------------------


EXACT_CITATION_EMPHASIS = """
ANSWER EMPHASIS — exact citation (factual_lookup):

18. The Observations section MUST cite the exact clause / page / standard \
location for every fact. Prefer a table-style observation when the user \
asked about more than one item (one row per cited clause).
19. The Interpretations section is short — restrict it to ONE interpretation \
that paraphrases what the cited clause means in the user's stated context, \
or :empty: when the clause is self-evident.
20. The Uncertainty section's drivers must call out whether the standard's \
JURISDICTION or VERSION was unspecified by the user (e.g. "NI 43-101 \
version 2014 vs an unspecified update"). When jurisdiction is ambiguous, \
state which jurisdiction's reading you applied.
21. Recommended actions should be :Not applicable: unless the query \
explicitly asks for a follow-up.
"""


SYNTHESIS_WITH_CONFLICTS_EMPHASIS = """
ANSWER EMPHASIS — synthesis with conflict detection (synthesis):

18. The Interpretations section MUST contain a named sub-section "Conflicting \
evidence" when the Evidence Set carries disagreeing claims about the same \
measurement, interval, or entity. Begin that sub-section with the literal \
header "### Conflicting evidence" and list each conflict as a bullet \
naming the entity, the property in conflict, and the citations on each side.
19. State the source hierarchy applied (e.g. NI 43-101 reports > drill logs > \
field notes) when conflicting sources are weighted unequally.
20. The Uncertainty section's drivers must include "Multiple sources disagree" \
when ANY conflict was surfaced under Interpretations.
21. When no conflicts are present, write "### Conflicting evidence" followed by \
"_None detected in the retrieved corpus._" — do NOT omit the header.
"""


COMPETING_HYPOTHESES_EMPHASIS = """
ANSWER EMPHASIS — competing hypotheses (hypothesis_generation):

18. The Interpretations section MUST contain AT LEAST TWO interpretations \
labelled with the (I1), (I2), … tags from rule 12. Use the "competes-with:" \
clause on each to mark the rival pairing.
19. For each interpretation, follow the supporting-observations sentence \
with TWO additional sentences:
      - "Evidence for: <citations>"
      - "Evidence against: <citations>"
    The Evidence-against citations MUST include results from the adversarial \
retrieval pass (citation markers tagged from search_documents_adversarial). \
If the adversarial pass returned nothing, write "Evidence against: none \
surfaced in the corpus — see Uncertainty drivers."
20. The Uncertainty section's drivers must call out which interpretation is \
better-constrained by the corpus and WHY (e.g. "I1 has 3 supporting \
observations; I2 has 1").
21. Recommended actions should propose the single most informative test \
to discriminate between the competing hypotheses.
"""


ANOMALY_TABLE_EMPHASIS = """
ANSWER EMPHASIS — anomaly table with geological-vs-artifact classification \
(anomaly_detection):

18. The Observations section MUST be a markdown table with these columns:
      | Interval / Sample | Value | Threshold | Deviation | Citation |
    One row per flagged anomaly. Drop rows where no QA/QC threshold is \
available rather than fabricating one.
19. The Interpretations section MUST classify EACH observation in the \
table as either:
      - "geological signal" — the value is real and reflects mineralisation \
or alteration, OR
      - "QA/QC artifact" — the value is likely a data-quality issue \
(failed blank / CRM, duplicate spread, detection-limit issue)
    State the classification rationale on the same line as the (Ix) tag.
20. Recommended actions MUST contain at least one re-assay or re-log item \
for any observation classified as "QA/QC artifact". If every flagged \
observation is geological signal, write "_No re-assay recommended — all \
flagged values pass QA/QC review._" instead.
21. The Uncertainty section's drivers must list which QA/QC fields were \
AVAILABLE (e.g. blanks present, CRMs absent, duplicates partial). When \
fields are missing, name them so the geologist knows where the gaps lie.
"""


UNCERTAINTY_DRIVERS_EMPHASIS = """
ANSWER EMPHASIS — uncertainty drivers (uncertainty_quantification):

18. The Uncertainty section is the PRIMARY section — expand it substantially:
      - "Reason:" sentence states the baseline interpretation
      - "Drivers:" list ranks the top uncertainty sources by impact
      - Add a "Sensitivity:" sub-section: name each assumption the answer \
depends on AND describe how the conclusion would shift if that assumption \
changed (e.g. "Sensitivity to capping: at 5 g/t cap mean grade is X; \
uncapped mean is Y; capping cap is the dominant lever").
19. When the quantity in question is numeric, the "Reason" sentence MUST \
include a range (e.g. "between 0.8 and 1.4 g/t Au at 95% spatial sampling \
confidence") rather than a single point estimate.
20. The "Data to reduce uncertainty" target must name the SPECIFIC \
measurement, location, or batch — generic phrases are rejected by the \
schema validator.
21. Observations and Interpretations sections stay compact — list only \
the observations that constrain the uncertainty discussion.
"""


RANKED_OPTIONS_EMPHASIS = """
ANSWER EMPHASIS — ranked options (decision_support):

18. The Recommended actions section is the PRIMARY section. The decision-\
support output rules (above, rules 15-17) apply unconditionally — the \
ranked list, unresolved prerequisites, and regulatory constraints are \
all required.
19. The Interpretations section MUST justify each ranked option's position. \
For option N, write at least one (Ix) entry explaining WHY it ranks where \
it does relative to its neighbours.
20. The Uncertainty section's drivers must call out which assumption a \
re-ranking would hinge on (e.g. "If the eastern fault is the dominant \
control, options 1 and 2 swap").
"""


BREAKDOWN_TABLE_EMPHASIS = """
ANSWER EMPHASIS — project data-collection breakdown (project_summary):

18. The Observations section MUST be a markdown table summarising the \
breakdown rows the tool returned. Columns:
      | Technique | Source table | Year | Count | Total metres | \
Contractor | Geologist | Citation |
    One row per breakdown bucket; quote counts and metres VERBATIM from the \
query_project_summary result. When contractor or geologist is NULL, write \
"_not extracted_" — DO NOT invent a name.
19. The Interpretations section is short. State the dominant technique \
(highest count), the most recent year of activity, and call out any year \
with no recorded campaigns. ONE (I1) entry covering all three is enough.
20. The Uncertainty section's drivers MUST name every column listed in the \
tool result's ``extraction_pending_fields`` (typically contractor / \
geologist / lab_name) as "Field not yet extracted from source documents". \
The "Data to reduce uncertainty" target should reference the ADR-0007 \
PR-3 NER backfill so the geologist knows the gap is on the roadmap.
21. Recommended actions are :Not applicable: unless the user explicitly \
asked for next steps — this is a descriptive intent.
"""


COVERAGE_TABLE_EMPHASIS = """
ANSWER EMPHASIS — coverage / data-gap analysis (coverage_gap):

18. The Observations section MUST contain two artefacts:
      a) An "Ingest stage" sentence quoting the indexed vs processed counts \
verbatim plus the gap percentage.
      b) A markdown table of per-attribute coverage with columns:
         | Attribute | Collars with data | Collars total | Coverage % | Citation |
    Order rows by coverage_pct ASCENDING so the largest gaps appear first.
19. The Interpretations section MUST classify the top three gaps as either:
      - "Ingest stage" — the underlying file is indexed in bronze but never \
became a silver row (parser failure, document type unsupported, OCR \
abandoned)
      - "Extraction stage" — the row exists in silver but the dimension \
hasn't been populated by an extractor (downstream silver detail tables \
are empty)
    Cite the responsible bronze.ingest_manifest / silver.completeness_findings \
row IDs where available.
20. The Uncertainty section MUST list which completeness_findings were \
considered, and explicitly call out when the project's collars_total is \
zero (the coverage percentages are then undefined).
21. Recommended actions MUST contain at least one item per "Ingest stage" \
gap (re-trigger ingestion / inspect parser logs) and at least one item per \
"Extraction stage" gap (schedule the relevant Dagster asset / queue the NER \
backfill). When no gaps were found, write "_No coverage gaps detected — \
all measured attributes are at 100% coverage._" and skip the per-gap items.
"""


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


_FRAGMENTS: dict[AnswerEmphasis, str] = {
    "exact_citation": EXACT_CITATION_EMPHASIS,
    "synthesis_with_conflicts": SYNTHESIS_WITH_CONFLICTS_EMPHASIS,
    "competing_hypotheses": COMPETING_HYPOTHESES_EMPHASIS,
    "anomaly_table": ANOMALY_TABLE_EMPHASIS,
    "uncertainty_drivers": UNCERTAINTY_DRIVERS_EMPHASIS,
    "ranked_options": RANKED_OPTIONS_EMPHASIS,
    "breakdown_table": BREAKDOWN_TABLE_EMPHASIS,
    "coverage_table": COVERAGE_TABLE_EMPHASIS,
}


def fragment_for(emphasis: AnswerEmphasis) -> str:
    """Return the prompt fragment for *emphasis*, or empty string."""
    return _FRAGMENTS.get(emphasis, "")


__all__ = [
    "ANOMALY_TABLE_EMPHASIS",
    "BREAKDOWN_TABLE_EMPHASIS",
    "COMPETING_HYPOTHESES_EMPHASIS",
    "COVERAGE_TABLE_EMPHASIS",
    "EXACT_CITATION_EMPHASIS",
    "RANKED_OPTIONS_EMPHASIS",
    "SYNTHESIS_WITH_CONFLICTS_EMPHASIS",
    "UNCERTAINTY_DRIVERS_EMPHASIS",
    "fragment_for",
]
