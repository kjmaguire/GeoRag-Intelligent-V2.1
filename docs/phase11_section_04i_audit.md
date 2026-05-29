# Phase 11 Step 1 — Section 04i hallucination layer audit

**Document version:** 1.0
**Status:** Snapshot at Phase 11 close. Update if layer code moves.
**Source of truth:** `georag-architecture.html` Section 04i (v1.49).

---

## 1. Why this doc exists

Phase 10's scoping inventory reported "10 files covering layers 1-6
+ completeness + validators + qualitative_detector" under
`src/fastapi/app/agent/hallucination/`. This audit reads each file
end-to-end and records what it actually enforces, so Phase 12+ has a
ground-truth reference against which to detect drift.

The architecture doc v1.49 consolidated the original 6-layer framing
into 4 guards (Numeric grounding / Entity grounding / Citation
completeness / Refusal path). The file names retain the 6-layer
numbering for git-history continuity; the audit below uses the
file-name vocabulary first and references the 4-guard mapping per
the package `__init__.py`.

---

## 2. Layer-by-layer audit

### Layer 1 — `layer1_retrieval.py` (125 lines)

**4-guard mapping:** Citation completeness (pre-filter component).

**Enforces:** Drops document chunks whose `relevance_score` falls
below the threshold *before* they reach the LLM's context window.
Applied inside tool functions (`search_documents` in `tools.py`),
NOT as a Pydantic AI `output_validator`.

**Status:** Implemented. Threshold = configurable via
`RETRIEVAL_QUALITY_THRESHOLD` env (Phase 0 set the default to 0.6).

**Coverage observation:** Filtering happens at the tool boundary so
low-signal chunks never enter the prompt. Downstream layers
(completeness, citation_binding) can still flag undercitation.

### Layer 2 — `layer2_typed_output.py` (128 lines)

**4-guard mapping:** Citation completeness (typed-output enforcement).

**Enforces:**
1. Every `[DATA-N]` / `[NI43-N]` marker in LLM text has a matching
   `Citation` object in the `citations` list.
2. No `Citation.source_chunk_id` is empty or a placeholder.
3. `text` is not empty or a pure refusal without grounding data.
4. `confidence` is in `[0.0, 1.0]`.

**Status:** Implemented as a post-assembly validator. Runs AFTER
response_assembler builds the `GeoRAGResponse`.

**Coverage observation:** The __init__.py docstring claims Layer 2
is "handled elsewhere" by Pydantic AI's typed-output mechanism. In
reality this file implements an additional post-assembly check on
top of Pydantic's schema validation. The __init__.py wording is
slightly stale.

### Layer 3 — `layer3_numerical.py` (301 lines)

**4-guard mapping:** Numeric grounding.

**Enforces:** Parses every integer and float from the LLM's
response text and traces each back to a tool-call result. Raises
`ModelRetry` when any number is ungrounded.

**Status:** Implemented. Decorated with `@geo_agent.output_validator`.

**Coverage observation:** Hardest layer to keep working as new
parsers + tool shapes land — every new numeric field that surfaces
in a response needs to be reachable from `ctx.messages` for the
trace-back. Phase 12+ should monitor `ModelRetry` rates as a drift
signal.

### Layer 4 — `layer4_entity.py` (362 lines)

**4-guard mapping:** Entity grounding.

**Enforces:** Extracts drill-hole IDs and quoted names from the
response, verifies them against `silver.collars` (PostGIS) and the
Neo4j knowledge graph. Raises `ModelRetry` for unknown entities.

**Status:** Implemented. Cross-DB pattern (Postgres + Neo4j).

**Coverage observation:** Depends on populated `silver.collars` +
Neo4j entity nodes. In a fresh-install / dev environment these
tables are sparse — the layer can pass spuriously when there are
no entities to check. Phase 12+ should add a CI fixture that seeds
known entities so the path is actually exercised.

### Layer 5 — `layer5_provenance.py` (157 lines)

**4-guard mapping:** Sub-component of Numeric / Entity grounding
(enrichment, not gating).

**Enforces:** Enriches each `Citation` with a chain:
`source_chunk_id → silver table → bronze.source_files
(file_path, sha256, bucket)`. Runs after assembly + after Layer 2.
Does NOT reject; only enriches.

**Status:** Implemented (157 lines of code). **NOTE:** the
`__init__.py` docstring claims Layer 5 is "Not implemented here.
Requires populated Qdrant documents (Milestone 2)." This is stale —
the file exists and runs. The Milestone-2 caveat applies to the
similarity-check variant, not to provenance enrichment.

**Coverage observation:** Once Qdrant chunk documents are populated
end-to-end, this layer should be extended with the similarity-check
variant. Phase 12+ ingestion completeness work will surface when.

### Layer 6 — `layer6_constraints.py` (337 lines)

**4-guard mapping:** Refusal path.

**Enforces:** SME-defined hard limits (max depth, grade, recovery,
etc.). Applies the rule to any numerical value near a geological
keyword. Raises `ModelRetry` for implausible values.

**Status:** Implemented. Limits are baked in code today; per CLAUDE.md
hard rule 6 ("Schemas in Section 04e are contracts") they should be
loaded from config so Kyle SME can adjust without code deploy.

**Coverage observation:** **Gap.** Constraints are hard-coded. Phase
12+ should externalise to a config file the geologist can edit (the
Phase 0 placeholder for SME-provided feature-engineering rules
applies here too).

### Layer completeness — `layer_completeness.py` (434 lines)

**4-guard mapping:** Citation completeness (positive coverage).

**Enforces:** Asserts every claim made in the response has a
corresponding citation. Counterpart to Layer 2's "every citation
marker resolves" check — this one ensures no UN-cited claim
sneaks through.

**Status:** Implemented. Biggest file in the package.

**Coverage observation:** Hard test case to write — requires a
fixture corpus where the agent's answer should reference X distinct
sources. The existing `test_golden_queries.py` is the natural home.

### Orchestrator validators — `orchestrator_validators.py` (480 lines)

**Enforces:** Deterministic-orchestrator-compatible adaptations of
Layers 3, 4, 6 that read from `tool_results: list` instead of
Pydantic AI's `ctx.messages`. Used when the agent path bypasses
the Pydantic-AI `output_validator` decorator.

**Status:** Implemented.

**Coverage observation:** Maintains parallel implementations of
the same logic for two execution paths (Pydantic AI vs deterministic
orchestrator). Phase 12+ could consolidate if the agent eventually
unifies on one execution model.

### Qualitative detector — `qualitative_detector.py` (101 lines)

**4-guard mapping:** Entity grounding (keyword-driven
disambiguation).

**Enforces:** Keyword-pattern matching to flag qualitative claims
("major", "significant", "high-grade") that lack quantitative
backing.

**Status:** Implemented.

**Coverage observation:** Smallest file in the package. Useful as a
pre-LLM signal — it catches the class of hallucination where the
agent invents qualitative descriptors not in the source corpus.

---

## 3. Notable gaps

1. **`__init__.py` docstring drift.** The package init claims Layer
   2 is "handled elsewhere by Pydantic AI" and Layer 5 is "Not
   implemented here. Requires populated Qdrant documents (Milestone
   2)." Both files exist with real implementations. The init
   should be updated to reflect the actual layer status — Phase
   12+ docs-cleanup item.

2. **Layer 6 constraints are hard-coded.** SME-editable config is
   the right end state per CLAUDE.md hard rule 6. Currently inline
   in Python — any constraint change requires a code deploy.

3. **Layer 4 entity grounding can pass on sparse fixtures.** A
   fresh-install or low-corpus dev env has no `silver.collars`
   rows; the layer's "unknown entity" check finds nothing to
   complain about. Needs CI fixtures.

4. **Layer 5 Qdrant-similarity variant is the named gap.** Once
   ingestion completeness lands and Qdrant documents are populated,
   add the similarity-check path noted in the v1.49 4-guard mapping.

5. **No golden test exercises the `ModelRetry` path.** The existing
   `test_golden_queries.py` covers happy-path retrieval + citation
   shape. There's no test that asserts an ungrounded number triggers
   Layer 3's `ModelRetry`. Phase 12+ should add one.

---

## 4. Implementation summary

| Layer | File | Lines | 4-guard mapping | Status |
|-------|------|------:|------------------|--------|
| 1 | `layer1_retrieval.py` | 125 | Citation completeness (pre-filter) | ✓ Implemented |
| 2 | `layer2_typed_output.py` | 128 | Citation completeness (typed output) | ✓ Implemented (init doc stale) |
| 3 | `layer3_numerical.py` | 301 | Numeric grounding | ✓ Implemented |
| 4 | `layer4_entity.py` | 362 | Entity grounding | ✓ Implemented (fixture coverage gap) |
| 5 | `layer5_provenance.py` | 157 | Numeric/Entity grounding (enrichment) | ✓ Implemented (init doc stale) |
| 6 | `layer6_constraints.py` | 337 | Refusal path | ✓ Implemented (hard-coded constraints — gap) |
| completeness | `layer_completeness.py` | 434 | Citation completeness (positive coverage) | ✓ Implemented |
| orchestrator validators | `orchestrator_validators.py` | 480 | All 4 guards (alt exec path) | ✓ Implemented |
| qualitative | `qualitative_detector.py` | 101 | Entity grounding (qualitative keyword check) | ✓ Implemented |

Total: **2425 lines** of hallucination-prevention code across 9
files (+ the 96-line `__init__.py`).

All four §04i v1.49 guards have at least one implementation file.
CLAUDE.md hard rules 4 (citations mandatory) and 5 (all six layers
apply) are satisfied at the architecture level — the gaps above
are about coverage robustness, not about missing infrastructure.

---

End of audit.
