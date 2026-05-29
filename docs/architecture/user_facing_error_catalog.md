# User-facing error message catalog

**Status:** Specification. Not wired into FastAPI / Laravel yet.
**Plan reference:** §4b (16-code GuardErrorCode taxonomy), §4d (user-facing mapping), §4c (death loop response)

## Why this exists

Plan §4b defines a clean internal error taxonomy. Plan §4d notes the gap: *"The internal error code system is well-designed. What the geologist sees when errors fire is not."*

This catalog is the bridge — every internal `GuardErrorCode` gets a user-facing message template, a UI surface, and a follow-up action.

## Catalog

Each row: `internal code → user template → UI surface → suggested user action → optional follow-up retrieval`.

### Retrieval-failure codes

#### `NO_EVIDENCE_FOUND`

> "I couldn't find anything in the current project's documents that addresses this. Try rephrasing or check that the documents you're thinking of have been ingested."

- **UI surface:** full message, neutral tone (no red badge — this is informational).
- **Follow-up:** offer a "Search document inventory" button that runs a broader inventory search.

#### `ENTITY_NOT_FOUND`

> "I couldn't find **{entity}** in the current project. Did you mean: {suggested_aliases}? Or this entity may not appear in the ingested documents."

- **UI surface:** message + clickable alias chips.
- **Follow-up:** on alias click, re-issue the original query with the canonical name substituted.

#### `AMBIGUOUS_HOLE_ID`

> "The hole ID **'{term}'** could refer to multiple drillholes: {candidate_list_with_programs}. Which did you mean?"

- **UI surface:** list of candidate hole IDs with program labels (drill year + program name when known).
- **Follow-up:** on selection, re-issue with `hole_id` fixed.

#### `AMBIGUOUS_FORMATION_NAME`

> "The formation **'{term}'** matches more than one entry in the project's stratigraphy. Did you mean: {candidates}?"

- **UI surface:** candidate list with parent stratigraphic context.
- **Follow-up:** on selection, re-issue with `formation_uri` fixed.

#### `AMBIGUOUS_PROPERTY_NAME`

> "The property **'{term}'** matches more than one project in this workspace: {candidates}. Pick one to narrow the search."

- **UI surface:** candidate list with project ID + last-active date.

#### `OVER_FILTERED_QUERY`

> "Your question's filters returned no results. I relaxed the most restrictive one and got these: {result_summary}. If you want the original filters back, edit the query."

- **UI surface:** info banner, results shown anyway.
- **Follow-up:** banner has a "Restore strict filters" link that re-issues with the original `unsafe_to_filter=false`.

#### `SPATIAL_QUERY_EMPTY`

> "The spatial query you asked for returned no matches. {spatial_explanation}. Try a larger search radius or a different reference geometry."

- **UI surface:** message + the spatial-query summary (e.g. "within 500 m of {ref geometry}").

#### `SPATIAL_CRS_MISMATCH`

> "I found spatial data for this query but the coordinate systems don't match and I can't safely compare them without risking an incorrect result. The data uses {crs_a} and {crs_b}. This may need to be reviewed."

- **UI surface:** warning banner (not refusal — there IS data, just unsafe to compare).
- **Follow-up:** "Show me the data anyway, without the spatial comparison" link runs a degraded retrieval.

#### `GRAPH_PATH_NOT_FOUND`

> "I traced the entity relationships but found no path between {entity_a} and {entity_b} in the knowledge graph. {path_explanation}."

- **UI surface:** message; render a small empty-graph SVG to make the negative result obvious.

### Evidence-quality codes

#### `NUMERIC_GROUNDING_FAILED`

> "I found references to **{entity}** but couldn't confirm the specific value you're asking about from the retrieved documents. The available evidence shows: {what_was_found}. For the exact figure, check {document}, {section}."

- **UI surface:** partial-answer format (see §Partial Answer below). NOT a refusal.
- **Follow-up:** auto-trigger the plan §4b repair strategy ("retry structured assay retrieval with tighter hole_id + commodity filters"); only show this user message if the repair also fails.

#### `CITATION_INCOMPLETE`

> "I don't have enough citations to back up this answer confidently. I found {n} documents related to {topic} but none contained {specific_data}. Try asking about {suggested_reformulation}."

- **UI surface:** refusal path. The answer is NOT shown — we don't ship an uncited answer.
- **Follow-up:** repair plan §4b "expand to parent section OR trigger ReadDocument for full section read" runs first; this message only shown if that fails too.

#### `CONFLICTING_SOURCES`

> "Two documents disagree on this value. **{document_a}** states **{value_a}**. **{document_b}** states **{value_b}**. This may reflect {interpretation_or_rounding}. I've cited both above."

- **UI surface:** NOT a refusal — the answer IS shown with both values and citations. This message is the contextual note. Plan §1h supersession may flag one as authoritative; if so, include "The current source is **{authoritative_doc}**."
- **Follow-up:** none — surfacing the conflict IS the action.

#### `MISSING_DEPTH_INTERVAL`

> "The question asks about a depth interval ({from}–{to} m) but the retrieved evidence doesn't specify the depth range for the values it contains. Showing the evidence anyway; depth interpretation is yours."

- **UI surface:** warning banner above the answer.

#### `MISSING_ASSAY_UNITS`

> "The assay values I found don't specify their units. Cannot safely compare or aggregate them without unit information."

- **UI surface:** warning banner; the values are shown as raw numbers; aggregation widgets are disabled.

#### `SOURCE_SCOPE_VIOLATION`

> "Internal error: a source from outside your workspace was almost included in the results. This was blocked and reported. **Your data is safe.** No information leaked."

- **UI surface:** critical banner with a "Report incident" button. Sentry event already fired automatically.
- **Follow-up:** the answer is suppressed; user re-issues the query.
- **Note:** this code firing in production is a P0 incident — plan §5c lists it as CRITICAL.

### Query-failure codes

#### `UNSUPPORTED_QUERY_TYPE`

> "This question falls outside what I can answer from the ingested documents. {reason}. You may want to {specific_alternative_action}."

- **UI surface:** info message; no answer shown.
- **Follow-up:** if `specific_alternative_action` is "search the public-geo layer", offer a button.

### Out-of-band: death loop

(Not a GuardErrorCode but lives in this catalog because the user sees the message.)

> "I was unable to find evidence to answer this question. The following filters returned no results: {filters}. You may want to verify **{entity}** exists in the current project."

- **UI surface:** info banner; sentry alert already fired.
- **Follow-up:** "Search workspace inventory" + "Reword question" actions.

## Partial answer format

When a guard fires but **some** evidence exists, prefer the partial-answer format over a refusal:

```
Answer: {partial_answer_clearly_labeled_as_partial}

⚠ Confidence: Low — {reason: missing evidence / conflicting sources / no citations}

Evidence found: {what_was_retrieved}
What's missing: {specific_data_not_found}
Suggestion: {what_additional_information_would_help}
```

The "⚠" badge renders amber, NOT red. Red is reserved for refusals.

## Why not just throw HTTP 5xx codes?

Three reasons:

1. **The guard system fires on the success path** — answers exist, they just failed quality checks. Surfacing them as 5xx misleads the client about what happened.
2. **The user always needs an actionable next step** — error pages don't give one; this catalog requires one per code.
3. **The UI needs to differentiate** — `AMBIGUOUS_HOLE_ID` renders a clickable picker, `CONFLICTING_SOURCES` shows both sources side-by-side, `SOURCE_SCOPE_VIOLATION` triggers an incident report flow. A single 5xx code can't carry that.

The HTTP response is always 200 (or 207 when a partial answer streams alongside guard metadata); the `GuardErrorCode` rides in the response body as a typed field.

## i18n hint

Templates above use `{placeholder}` syntax. A future locale pack will:

```json
// resources/lang/en/guard_errors.json
{
  "NO_EVIDENCE_FOUND": "I couldn't find anything in the current project's documents that addresses this. Try rephrasing or check that the documents you're thinking of have been ingested.",
  "ENTITY_NOT_FOUND": "I couldn't find {entity} in the current project. Did you mean: {suggested_aliases}? Or this entity may not appear in the ingested documents.",
  // ...
}
```

Laravel's `__()` helper composes the placeholders; the React side reads via `usePage().props.guardErrorMessages` populated from the same source. NOT WIRED — when Kyle picks up §4d implementation, this is the staging point.

## Acceptance criteria (plan §4d)

- [x] Every GuardErrorCode has a corresponding user-facing message — DONE for 16 codes + death-loop
- [x] Partial answer format — SPEC DONE; renderer NOT IMPLEMENTED
- [ ] No raw error codes or stack traces shown to users — NOT VERIFIED end-to-end (FastAPI middleware needs an audit)

## Decisions captured — 2026-05-27 morning

Kyle reviewed and accepted all four recommendations:

| Q | Decision | Implication |
|---|---|---|
| Q17 | **Keep first-person tone** ("I couldn't find …") | Future Field-mode terser variant possible but not now. `lang/en/guard_errors.json` ships with the first-person templates. |
| Q18 | **Acceptable** that `ENTITY_NOT_FOUND` degrades when `silver.entity_aliases` is empty | Renderer falls back to the second clause of the template ("Or this entity may not appear …") when the alias suggestion query returns empty. Transitional state OK until the table is populated. |
| Q19 | **Acceptable** that `CONFLICTING_SOURCES` cites both equally before §1h supersession lands | When `silver.document_versions` doesn't yet flag one of the sources as superseded, the message renders without the "The current source is …" annotation. Transitional state. |
| Q20 | **Button only** for `SOURCE_SCOPE_VIOLATION`; support form linked but not forced | The "Report incident" button POSTs to the support endpoint; user can dismiss without sending if they want. Sentry event still fires automatically regardless of the user action. |
