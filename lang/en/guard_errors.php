<?php

declare(strict_types=1);

/**
 * Plan §4d user-facing GuardErrorCode templates.
 *
 * Resolved via `__('guard_errors.<CODE>', $placeholders)`. Placeholder
 * syntax uses Laravel's `:placeholder` form — `App\Services\Guards\
 * GuardErrorRenderer` handles dispatch + degradation variants.
 *
 * Source of truth for the catalog: `docs/architecture/
 * user_facing_error_catalog.md`. Keep templates in sync there.
 */

return [
    // ── Retrieval-failure codes (9) ─────────────────────────────────────
    'NO_EVIDENCE_FOUND' => "I couldn't find anything in the current project's documents that "
        ."addresses this. Try rephrasing or check that the documents you're "
        .'thinking of have been ingested.',

    'ENTITY_NOT_FOUND' => "I couldn't find :entity in the current project. Did you mean: "
        .':suggested_aliases? Or this entity may not appear in the '
        .'ingested documents.',

    'ENTITY_NOT_FOUND_NO_ALIASES' => "I couldn't find :entity in the current project. This entity may "
        .'not appear in the ingested documents.',

    'AMBIGUOUS_HOLE_ID' => "The hole ID ':term' could refer to multiple drillholes: "
        .':candidates. Which did you mean?',

    'AMBIGUOUS_FORMATION_NAME' => "The formation ':term' matches more than one entry in the "
        ."project's stratigraphy. Did you mean: :candidates?",

    'AMBIGUOUS_PROPERTY_NAME' => "The property ':term' matches more than one project in this "
        .'workspace: :candidates. Pick one to narrow the search.',

    'OVER_FILTERED_QUERY' => "Your question's filters returned no results. I relaxed the most "
        .'restrictive one and got these: :result_summary. If you want the '
        .'original filters back, edit the query.',

    'SPATIAL_QUERY_EMPTY' => 'The spatial query you asked for returned no matches. '
        .':spatial_explanation. Try a larger search radius or a different '
        .'reference geometry.',

    'SPATIAL_CRS_MISMATCH' => 'I found spatial data for this query but the coordinate systems '
        ."don't match and I can't safely compare them without risking an "
        .'incorrect result. The data uses :crs_a and :crs_b. This may '
        .'need to be reviewed.',

    'GRAPH_PATH_NOT_FOUND' => 'I traced the entity relationships but found no path between '
        .':entity_a and :entity_b in the knowledge graph. '
        .':path_explanation.',

    // ── Evidence-quality codes (6) ──────────────────────────────────────
    'NUMERIC_GROUNDING_FAILED' => 'I found references to :entity but could not confirm the specific '
        ."value you're asking about from the retrieved documents. The "
        .'available evidence shows: :what_was_found. For the exact figure, '
        .'check :document, :section.',

    'CITATION_INCOMPLETE' => "I don't have enough citations to back up this answer confidently. "
        .'I found :n documents related to :topic but none contained '
        .':specific_data. Try asking about :suggested_reformulation.',

    'CONFLICTING_SOURCES' => 'Two documents disagree on this value. :document_a states '
        .':value_a. :document_b states :value_b. This may reflect '
        ."(:interpretation_or_rounding). I've cited both above.",

    'CONFLICTING_SOURCES_WITH_AUTHORITY' => 'Two documents disagree on this value. :document_a states '
        .':value_a. :document_b states :value_b. The current source is '
        .':authoritative_doc; the other has been superseded. This may '
        ."reflect (:interpretation_or_rounding). I've cited both above.",

    'MISSING_DEPTH_INTERVAL' => 'The question asks about a depth interval (:from–:to m) but the '
        ."retrieved evidence doesn't specify the depth range for the "
        .'values it contains. Showing the evidence anyway; depth '
        .'interpretation is yours.',

    'MISSING_ASSAY_UNITS' => "The assay values I found don't specify their units. Cannot "
        .'safely compare or aggregate them without unit information.',

    'SOURCE_SCOPE_VIOLATION' => 'Internal error: a source from outside your workspace was almost '
        .'included in the results. This was blocked and reported. Your '
        .'data is safe. No information leaked.',

    // ── Query-failure code (1) ──────────────────────────────────────────
    'UNSUPPORTED_QUERY_TYPE' => 'This question falls outside what I can answer from the ingested '
        .'documents. :reason. You may want to :specific_alternative_action.',

    // ── Egress / policy code (1) ────────────────────────────────────────
    // Z.1 / Appendix C §5 — external-LLM egress gate refused the call
    // because the active workspace has not opted into external LLM use
    // (profile.allow_external_llm is false or absent). The refusal is a
    // hard stop — no Anthropic call is made.
    'EGRESS_BLOCKED' => 'External LLM access is disabled for this workspace. '
        .'Contact your admin to enable.',

    // ── Out-of-band (plan §4c death-loop response) ──────────────────────
    'DEATH_LOOP' => 'I was unable to find evidence to answer this question. The '
        .'following filters returned no results: :filters. You may want '
        .'to verify :entity exists in the current project.',

    // ── Partial-answer format scaffolding ───────────────────────────────
    'PARTIAL_ANSWER_HEADER' => 'Confidence: Low — :reason',
    'PARTIAL_ANSWER_EVIDENCE_LABEL' => 'Evidence found:',
    'PARTIAL_ANSWER_MISSING_LABEL' => "What's missing:",
    'PARTIAL_ANSWER_SUGGESTION_LABEL' => 'Suggestion:',

    // ── Plan §4b terminal-strategy templates (Stage 2 surfaces) ──────────
    'REQUEST_UNIT_CLARIFICATION' => "I found :commodity assay values but I'm not sure which unit "
        .'family applies. Which would you like me to use: :candidates?',

    'REQUEST_DEPTH_CLARIFICATION' => "The depth value :value appeared without a unit. Mining "
        .'reports vary between metric and imperial — which would you like: '
        .':candidates?',
];
