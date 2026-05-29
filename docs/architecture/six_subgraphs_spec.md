# Six "subgraphs" specification

**Status:** Documentary — reconciles plan §0d with shipped implementation.
**Authored:** 2026-05-26 (overnight autonomous run)
**Supersedes for the codebase:** plan §0d's expectation of 6 data-store-specific subgraphs.

---

## 1. The mismatch with plan §0d

Plan §0d expects **six subgraphs partitioned by data store / answer type**:

1. Document/report retrieval (Qdrant)
2. Structured data (PostGIS + structured tables)
3. Spatial (PostGIS geometry)
4. Graph relationship (Neo4j)
5. Multi-document synthesis (cross-document)
6. Clarification / refusal

The shipped agentic retrieval architecture (Phase 2 of `project_phase2_geologist_question_plan.md`, 2026-05-20) instead uses **one LangGraph with six question-intent classes**:

1. `factual_lookup`
2. `synthesis`
3. `hypothesis_generation`
4. `anomaly_detection`
5. `uncertainty_quantification`
6. `decision_support`

The architectural decision to use a single graph (rather than per-intent subgraphs) is captured in ADR-0006 (`agentic-retrieval-single-graph`) — note that ADR-0006/0007 are untracked WIP on `main` at the time of writing, so this doc cross-references the in-repo code rather than the ADR text. The rationale is summarised in §4 below.

The data-store-by-data-store retrieval the plan describes still happens — but it's a *retrieval profile* per intent, not a *subgraph*. The `retrieval_profile.py` table assigns each intent its own mix of tools (`search_documents`, `query_spatial_collars`, `query_downhole_logs`, `query_assay_data`, `traverse_knowledge_graph`, `query_project_overview`), BM25 weight, and answer emphasis. The execute node dispatches to all primary tools in parallel.

**Practical consequence:** plan §0d's "subgraphs" are realised as **`(intent × tool)` retrieval matrix cells**, not as separately-compiled subgraphs.

## 2. Shipped pipeline (the actual single graph)

From `src/fastapi/app/agent/agentic_retrieval/graph.py`:

```
START → classify → route → execute → assemble → validate → demote → persist → END
```

Linear, no branches, no subgraph compilation. The branching the plan envisioned is collapsed into the *route* node's choice of retrieval profile.

| Node | What it does | Source of truth |
|---|---|---|
| `classify` | 6-intent classifier (keyword-first + optional LLM fallback) | `agentic_retrieval/intent_classifier.py` |
| `route` | Pick retrieval profile for the effective intent + apply envelope overrides | `agentic_retrieval/nodes.py:route_node` |
| `execute` | Fan-out to the tool layer per profile (parallel where safe) | `agentic_retrieval/nodes.py:execute_node` + `agent/tools.py` |
| `assemble` | Build the answer envelope from tool results | `agent/response_assembler.py` |
| `validate` | Hallucination / numeric grounding / citation checks | `agent/hallucination.py` |
| `demote` | Lower confidence on conflicts / missing evidence | `agent/confidence_computer.py` |
| `persist` | Best-effort write to `answer_runs` for lineage | `agent/agentic_retrieval/nodes.py:persist_node` |

## 3. Per-intent specifications (the answer to plan §0d, mapped onto reality)

For each of the six intents, the table below provides the §0d-required shape: trigger condition, input/output state fields, retrieval stores, retrieval pattern, token budget, known failure modes.

State field references resolve to `agentic_retrieval/state.py::AgenticRetrievalState`.

### 3.1 `factual_lookup`

| Field | Value |
|---|---|
| Trigger | "what is", "define", "definition of", "standard", "NI 43-101 §X", "classification of/for/under" |
| Input state consumed | `query`, `deps`, `context_envelope` (low usage) |
| Output state produced | `intent="factual_lookup"`, `retrieval_profile`, `tool_results.search_documents`, `response` |
| Retrieval stores queried | Qdrant (BM25-heavy, weight 0.75) |
| Retrieval pattern | Single-store, sparse-biased (standards documents respond to clause language) |
| Token budget (`max_chunks`) | 6 |
| Answer emphasis | `exact_citation` |
| Known failure modes | (a) Ambiguous standard reference (NI 43-101 vs CIM); (b) BM25 false-positives on synonyms not in the standards corpus; (c) clauses split across chunk boundaries |

### 3.2 `synthesis`

| Field | Value |
|---|---|
| Trigger | "integrate", "across wells/holes/sites", "summarize", "compare holes/wells/targets", "overall picture" |
| Input state consumed | `query`, `deps`, `context_envelope` (high usage — drives filters) |
| Output state produced | `intent="synthesis"`, `retrieval_profile`, `tool_results.*` (all 5 primary tools), `conflicting_evidence`, `response` |
| Retrieval stores queried | Qdrant + PostGIS (collars) + PostgreSQL (logs, assays) + Neo4j |
| Retrieval pattern | Parallel fan-out across all 4 primary stores |
| Token budget (`max_chunks`) | 16 |
| Answer emphasis | `synthesis_with_conflicts` (conflict_detection_enabled=true) |
| Known failure modes | (a) Token budget overrun when many sources contribute; (b) silent over-merge of conflicting values (Phase 1.3 demoter is the safety net); (c) `query_project_overview` secondary tool can re-fetch what other primaries already returned |

### 3.3 `hypothesis_generation`

| Field | Value |
|---|---|
| Trigger | "could explain", "what if", "alternative", "possible causes", "hypothesis", "more consistent with" |
| Input state consumed | `query`, `deps`, `context_envelope` |
| Output state produced | `intent="hypothesis_generation"`, `retrieval_profile`, `tool_results.*`, `adversarial_results`, `response` |
| Retrieval stores queried | Qdrant + Neo4j + PostgreSQL (assays); PostGIS as secondary |
| Retrieval pattern | Two-pass: (1) supporting-evidence retrieval, (2) adversarial pass with "find disconfirming evidence" prompt framing against the same corpus |
| Token budget (`max_chunks`) | 16 |
| Answer emphasis | `competing_hypotheses` |
| Known failure modes | (a) Adversarial pass returns near-duplicates of pass 1 because the corpus and tools are the same; (b) hypothesis count not capped — LLM can emit too many; (c) Neo4j queries may be slow when the relationship graph is dense around the target entity |

### 3.4 `anomaly_detection`

| Field | Value |
|---|---|
| Trigger | "outliers", "anomalies", "QA/QC", "blanks", "CRMs", "duplicates", "detection limits", "re-assay", "rerun" |
| Input state consumed | `query`, `deps`, `context_envelope` |
| Output state produced | `intent="anomaly_detection"`, `retrieval_profile`, `tool_results.{query_assay_data,query_downhole_logs}`, `response.anomaly_observations[]` |
| Retrieval stores queried | PostgreSQL (assays + logs); Qdrant as secondary |
| Retrieval pattern | Schema-first with QA/QC field surfacing (`surface_qa_qc_fields=true`); degrades gracefully if Phase-4 QA/QC columns are missing |
| Token budget (`max_chunks`) | 20 (highest of the six — assay rows are small) |
| Answer emphasis | `anomaly_table` |
| Known failure modes | (a) Pre-Phase-4 schemas miss `qaqc_flag` columns → degraded answer; (b) outlier detection currently relies on LLM judgement rather than statistical thresholds (plan §1g's QA/QC schema closes this gap); (c) assay-only bias — alteration or lithology anomalies are weakly served |

### 3.5 `uncertainty_quantification`

| Field | Value |
|---|---|
| Trigger | "how certain", "confidence", "sensitivity", "range", "assumptions", "how reliable/robust", "uncertainty", "capping", "direct/indirect measurements", "constraints" |
| Input state consumed | `query`, `deps`, `context_envelope` |
| Output state produced | `intent="uncertainty_quantification"`, `retrieval_profile`, `tool_results.*`, `conflicting_evidence`, `uncertainty_drivers[]` |
| Retrieval stores queried | Qdrant + PostgreSQL (assays) + PostGIS; logs as secondary |
| Retrieval pattern | Deliberately retrieves conflicting chunks (conflict_detection_enabled=true) so the answer can surface the disagreement |
| Token budget (`max_chunks`) | 14 |
| Answer emphasis | `uncertainty_drivers` |
| Known failure modes | (a) "Confidence" is asked for the answer, not the data, and the answer's confidence is computed post-hoc by the demoter — there's no upfront sensitivity analysis; (b) capping / capping-grade questions need numeric grounding that the assay tool returns as raw rows, not summary statistics |

### 3.6 `decision_support`

| Field | Value |
|---|---|
| Trigger | Phase 1.4 classifier (7 canonical phrases) + augmentations: "documentation gaps would", "which drill targets would/should", "would (most) reduce/prevent/block" |
| Input state consumed | `query`, `deps`, `context_envelope` (decision-context fields critical — envelope can demote to synthesis if missing) |
| Output state produced | `intent="decision_support"` (or `effective_intent="synthesis"` after envelope override), `retrieval_profile.require_regulatory_constraints` (when `regulatory_touch=true`), `tool_results.*`, `decision_support.ranked_options[]`, `decision_support.regulatory_constraints[]` |
| Retrieval stores queried | Qdrant + PostGIS + PostgreSQL (assays) + Neo4j; logs + overview as secondary |
| Retrieval pattern | Parallel fan-out; same breadth as synthesis but answer assembly ranks options |
| Token budget (`max_chunks`) | 18 |
| Answer emphasis | `ranked_options` |
| Known failure modes | (a) Without decision-context in the envelope, the route node demotes to synthesis (logged but silent to the user); (b) `regulatory_touch` is keyword-based and may miss queries that imply regulation without using the trigger vocabulary; (c) "next action" ranking has no project-state awareness — the LLM does it from text alone |

## 4. Why one graph, six intents (not six subgraphs)

Three reasons (collated from the Phase 2 implementation memory and `retrieval_profile.py` comments):

1. **Tool composition is more reusable than node composition.** Every intent uses the same 5-6 tools in different proportions; encoding the proportions in declarative profiles is shorter and easier to test than maintaining six bespoke subgraphs.
2. **Cross-intent envelope overrides need a single state object.** The route node's envelope-override logic (`apply_envelope_overrides`) can demote decision_support → synthesis when context is missing. That kind of cross-intent flow is awkward across subgraph boundaries.
3. **The lineage write (`persist_node`) wants one state shape.** `answer_runs` rows are uniform regardless of which intent fired; one graph keeps the persist contract trivial.

This means plan §0d's "Clarification / refusal subgraph" (#6) **does not exist as a subgraph**. Refusal is handled inline by the `validate` and `demote` nodes when grounding fails, and clarification is handled before agentic retrieval runs (the envelope pre-processor + Field/Office form). See §5.

## 5. Plan §0d ↔ codebase mapping

| Plan §0d subgraph | Codebase realisation |
|---|---|
| 1. Document/report retrieval | `factual_lookup` intent + `search_documents` tool (BM25-biased) |
| 2. Structured data | `query_assay_data`, `query_downhole_logs`, `query_spatial_collars` tools — invoked by `synthesis`, `anomaly_detection`, `uncertainty_quantification`, `decision_support` |
| 3. Spatial | `query_spatial_collars` tool — no dedicated intent; geospatial answer-path is plan §2g territory (gap) |
| 4. Graph relationship | `traverse_knowledge_graph` tool — invoked by `synthesis`, `hypothesis_generation`, `decision_support` |
| 5. Multi-document synthesis | `synthesis` intent (the broadest profile) + `conflict_detection_enabled` |
| 6. Clarification / refusal | NOT a subgraph. Pre-agentic envelope checks (Field/Office form) handle clarification; in-graph `validate` + `demote` handle refusal |

## 6. Gaps the plan calls for that this graph does not yet cover

These are gaps relative to the broader plan (Phases 0-6), not against the six-subgraphs §0d alone. Tracked here because they're the "what should the next subgraph wiring be?" questions implicit in §0d.

1. **No dedicated spatial-query node.** Plan §2g (geospatial query answer path) calls for a node that detects spatial intent (distance/containment/overlap), extracts spatial parameters, validates CRS, and emits `SpatialEvidence`. Today this is implicit in the `query_spatial_collars` tool which only handles collar proximity. ST_DWithin/ST_Contains/ST_Intersects answer flows are not wired.
2. **No VocabEnrichmentNode.** Plan §1d-iv calls for a CGI vocab enrichment node between classify and route. Today the only vocab-aware code is the CGI loader script (not yet run per project memory — `project_vllm_migration.md` does not mention CGI vocab tables existing).
3. **No EntityResolutionNode.** Plan §2c calls for entity/alias resolution before retrieval fan-out. Today entity extraction happens implicitly in the tool layer (`extract_hole_ids`, etc.) without a typed `entity_aliases` table backing it.
4. **No DocumentInventoryInjectionNode.** Plan §3b multi-document authority ranking has no shipped equivalent — document authority is not a state field.
5. **No structured query parameter extraction with geological validation.** Plan §2e calls for an LLM-generated structured filter object alongside the query text; currently the envelope captures *some* of this (mode, CRS, allowed_data_sources) but not commodity/hole-id/depth/formation extraction with the 10 geological failure-mode guards.

## 7. Acceptance criteria from plan §0d, applied to this doc

Plan §0d says: "All six subgraphs named and fully specified before any Phase 2 LangGraph work begins. Wiring diagram showing node insertion points for vocabulary enrichment, entity resolution, and inventory injection."

This doc satisfies the first acceptance criterion (six named + fully specified, as intents). The second criterion — node insertion points for vocab/entity/inventory — is shown in §6 above as gaps. Suggested insertion points for the gap nodes:

```
START
  → preprocess_envelope (existing)
  → vocab_enrich  (NEW — plan §1d-iv) ─┐
  → entity_resolve (NEW — plan §2c)    ├── all three insert BEFORE classify
  → inventory_inject (NEW — plan §3b) ─┘     so retrieval has the enriched query/filters
  → classify (existing)
  → route (existing)
  → structured_param_extract (NEW — plan §2e — could also live inside route's filter prep)
  → execute (existing)
  → assemble (existing)
  → validate (existing)
  → demote (existing)
  → persist (existing)
  → END
```

The new nodes are additive: existing nodes consume what they previously consumed, plus optional new fields when present. State extensions required are listed alongside the plan-section references in §6.
