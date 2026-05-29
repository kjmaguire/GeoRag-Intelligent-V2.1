# Appendix N — Agentic & Retrieval Module Catalog

Status: **Draft.** Companion to [Appendix M](M-agents-and-ml-catalog.md)
(Pydantic-AI per-phase agents + ML training).

> **Where M ends, N begins.** Appendix M covers the per-phase Pydantic-AI
> agents (Phase 0/5-10) + classifiers + ML training pipelines. Appendix N
> covers everything else with agent-shaped logic: the **three** LangGraphs
> (not one), the 50 retrieval-path modules under `src/fastapi/app/agent/`,
> the prompt template registry, the agent-shaped services, and the
> dispatchers.

Total catalog:
- **3 LangGraphs**: `agentic_retrieval`, `target_recommendation`, `llm_incident_diagnosis`.
- **50 retrieval modules** in `src/fastapi/app/agent/`.
- **19 prompt templates** in `agent/prompts/`.
- **9 services subdirs** with agent-shaped code.

---

## 1. The three LangGraphs

GeoRAG has **three** state-graph compositions, not one. Each follows the
same shape: `nodes.py` + `state.py` + `graph.py` + (optional) cache.

### 1.1 Agentic retrieval (chat path)

[src/fastapi/app/agent/agentic_retrieval/](../../../src/fastapi/app/agent/agentic_retrieval/).
The primary chat-path LangGraph. **8 modules:**

| Module | Job |
|---|---|
| `graph.py` | LangGraph construction; pipeline tuple `classify → route → execute → assemble → validate → demote → persist`; `@lru_cache(maxsize=1)` |
| `nodes.py` | The 7 node implementations |
| `state.py` | `AgenticRetrievalState` typed dict |
| `intent_classifier.py` | 8-intent classifier (Ch 06 §3) |
| `retrieval_profile.py` | Per-intent profile selection |
| `context_envelope.py` | Field/Office mode 12-field envelope |
| `preprocessor.py` | Backend preprocessor for the envelope |
| `qaqc_availability.py` | QA/QC pre-check before routing |

Cache: [agent/orchestrator/run_cache.py](../../../src/fastapi/app/agent/orchestrator/run_cache.py) — per-run cache shared across nodes.

### 1.2 Target recommendation (Phase 8 — drilling)

[src/fastapi/app/services/target_recommendation/](../../../src/fastapi/app/services/target_recommendation/).
Second LangGraph — drives the Targets page. **4 modules:**

| Module | Job |
|---|---|
| `graph.py` | TRG (Target Recommendation Graph) LangGraph construction |
| `nodes.py` | Node implementations including `score_candidate_zones` (the §8.7 weighted math; called from the Phase 8 `target_scoring` agent shell) |
| `state.py` | TRG state |
| `deposit_models.py` | Per-deposit-type factor weights + thresholds |

Phase 12 will graduate this from weighted scoring to XGBoost+SHAP per
§18.4 — see [services/target_scoring_ml/](../../../src/fastapi/app/services/target_scoring_ml/).

### 1.3 LLM incident diagnosis (operator path)

[src/fastapi/app/services/llm_incident_diagnosis/](../../../src/fastapi/app/services/llm_incident_diagnosis/).
Third LangGraph — backs the Phase 0 `llm_incident_diagnosis` agent shell.
**2 modules:**

| Module | Job |
|---|---|
| `nodes.py` | Reads Tempo + Loki + audit_ledger for a failing trace_id; composes the remediation packet |
| `state.py` | Diagnosis state |

---

## 2. Retrieval-path module catalog (50 files in `agent/`)

[src/fastapi/app/agent/](../../../src/fastapi/app/agent/).
Alphabetical. Grouped by what they participate in.

### 2.1 LangGraph node bodies / hooks

These are imported into `agentic_retrieval/nodes.py`:

| Module | Role |
|---|---|
| [agentic_escalation.py](../../../src/fastapi/app/agent/agentic_escalation.py) | LLM-driven escalation when rule-based fails |
| [escalation.py](../../../src/fastapi/app/agent/escalation.py) | Rule-based escalation tier |
| [model_routing.py](../../../src/fastapi/app/agent/model_routing.py) | Picks the Anthropic tier (Haiku / Sonnet / Opus) per query class |
| [response_assembler.py](../../../src/fastapi/app/agent/response_assembler.py) | Builds the answer envelope from validated evidence |
| [event_stamper.py](../../../src/fastapi/app/agent/event_stamper.py) | Append-only event log per turn |
| [pdf_tool_results.py](../../../src/fastapi/app/agent/pdf_tool_results.py) | Normalises PDF-tool output shapes |
| [tool_result_helpers.py](../../../src/fastapi/app/agent/tool_result_helpers.py) | Shared tool-result conversion |
| [tools.py](../../../src/fastapi/app/agent/tools.py) | Tool registration entry point |
| [evidence.py](../../../src/fastapi/app/agent/evidence.py) | Evidence struct + serialisation |
| [evidence_converter.py](../../../src/fastapi/app/agent/evidence_converter.py) | Converts between evidence shapes (silver row ↔ Pydantic ↔ payload) |
| [lineage.py](../../../src/fastapi/app/agent/lineage.py) | Answer lineage tracking → `silver.answer_runs` lineage cols |
| [pricing.py](../../../src/fastapi/app/agent/pricing.py) | LLM cost computation per call (token × per-model rate) |
| [sentry_tags.py](../../../src/fastapi/app/agent/sentry_tags.py) | Sentry tag setter (Spine B telemetry) |
| [log_safe.py](../../../src/fastapi/app/agent/log_safe.py) | Strips secrets/PII from log payloads |
| [errors.py](../../../src/fastapi/app/agent/errors.py) | Internal error types (distinct from `guards.py` codes) |
| [deps.py](../../../src/fastapi/app/agent/deps.py) | Agent dependency injection (DB pool, redis, LLM client) |

### 2.2 Retrieval tools (called by `execute_node`)

| Module | Tool produced |
|---|---|
| [decomposer.py](../../../src/fastapi/app/agent/decomposer.py) | Multi-clause query splitter |
| [anaphora.py](../../../src/fastapi/app/agent/anaphora.py) | Pronoun resolution across turns (legacy — now superseded by `multi_turn_resolver.py`) |
| [followups.py](../../../src/fastapi/app/agent/followups.py) | Follow-up question generation |
| [graph_entities.py](../../../src/fastapi/app/agent/graph_entities.py) | Neo4j traversal tool |
| [drill_targeting.py](../../../src/fastapi/app/agent/drill_targeting.py) | Drill target retrieval tool |
| [public_geoscience_tool.py](../../../src/fastapi/app/agent/public_geoscience_tool.py) | Public-geo layer retrieval (Tier 1 `public_geo.*` tables) |
| [project_geometry.py](../../../src/fastapi/app/agent/project_geometry.py) | Project bounding geometry queries |
| [anomaly_detector.py](../../../src/fastapi/app/agent/anomaly_detector.py) | Z-score + IQR anomaly tool |
| [figure_extractor.py](../../../src/fastapi/app/agent/figure_extractor.py) | Figure → caption nearest-text linking |
| [viz_builder.py](../../../src/fastapi/app/agent/viz_builder.py) | Builds Plotly viz payloads from retrieval results |

### 2.3 Classifiers / context builders

| Module | Job |
|---|---|
| [llm_classifier.py](../../../src/fastapi/app/agent/llm_classifier.py) | Generic LLM-fallback classification primitive |
| [decision_support_classifier.py](../../../src/fastapi/app/agent/decision_support_classifier.py) | Weak-signal classifier for "should we drill" queries |
| [document_classifier.py](../../../src/fastapi/app/agent/document_classifier.py) | Per-document classifier (sets `silver.reports.report_type`) |
| [query_classification.py](../../../src/fastapi/app/agent/query_classification.py) | Query metadata enrichment |
| [context_builder.py](../../../src/fastapi/app/agent/context_builder.py) | Legacy context assembly (predecessor to Spine A `context_prep.py`) |

### 2.4 Spine A — context preparation (ADR-0009)

| Module | Spec § |
|---|---|
| [context_prep.py](../../../src/fastapi/app/agent/context_prep.py) | Composition (calls into the four below) |
| [authority.py](../../../src/fastapi/app/agent/authority.py) | §3b authority rank — reads `silver.document_versions` |
| [source_diversity.py](../../../src/fastapi/app/agent/source_diversity.py) | §3c rerank to avoid 10 paragraphs from the same report |
| [context_budget.py](../../../src/fastapi/app/agent/context_budget.py) | §3f token-budget enforcer |
| [parent_expansion.py](../../../src/fastapi/app/agent/parent_expansion.py) | ADR-0010 — fetch child for precision, rehydrate parent for recall |
| [multi_turn_resolver.py](../../../src/fastapi/app/agent/multi_turn_resolver.py) | §3e — pronoun / demonstrative / comparative resolution |
| [entity_resolver.py](../../../src/fastapi/app/agent/entity_resolver.py) | §2c — `silver.entity_aliases` lookup + `silver.entity_gaps` writes |
| [geospatial_planner.py](../../../src/fastapi/app/agent/geospatial_planner.py) | §2g PostGIS query planner |
| [tools_geospatial.py](../../../src/fastapi/app/agent/tools_geospatial.py) | §2g tool surface |

### 2.5 Spine B — repair loop (ADR-0009)

| Module | Spec § |
|---|---|
| [repair_strategy.py](../../../src/fastapi/app/agent/repair_strategy.py) | §4b strategy enum + per-strategy params |
| [repair_apply.py](../../../src/fastapi/app/agent/repair_apply.py) | Strategy application + death-loop guard |
| [guards.py](../../../src/fastapi/app/agent/guards.py) | `GuardErrorCode` enum (line 49) consumed by Spine B dispatcher |

### 2.6 Answer + validation

| Module | Job |
|---|---|
| [oiur_parser.py](../../../src/fastapi/app/agent/oiur_parser.py) | OIUR envelope parser |
| [citation_binding.py](../../../src/fastapi/app/agent/citation_binding.py) | `[ev:xxxxxxxx]` marker → `silver.evidence_items` binding |
| [confidence_computer.py](../../../src/fastapi/app/agent/confidence_computer.py) | Confidence formula (Appendix G §11) |
| [spatial_temporal_verify.py](../../../src/fastapi/app/agent/spatial_temporal_verify.py) | Spatial / temporal claim verification |
| [plan_executor.py](../../../src/fastapi/app/agent/plan_executor.py) | Multi-step plan executor |
| [llm_calls.py](../../../src/fastapi/app/agent/llm_calls.py) | The LLM client wrapper — handles vLLM/Anthropic dispatch + streaming |

### 2.7 Eval support

| Module | Job |
|---|---|
| [golden_query_harness.py](../../../src/fastapi/app/agent/golden_query_harness.py) | Runs `eval.golden_questions` against the live graph |

---

## 3. Pipeline subdir

[src/fastapi/app/agent/pipeline/](../../../src/fastapi/app/agent/pipeline/).
The deterministic, **non-LangGraph** RAG pipeline (legacy / fallback when
`AGENTIC_RETRIEVAL_V2_ENABLED=false`).

| Module | Job |
|---|---|
| [branching.py](../../../src/fastapi/app/agent/pipeline/branching.py) | Per-query branch selection (different from LangGraph routing) |
| [decomposition.py](../../../src/fastapi/app/agent/pipeline/decomposition.py) | Multi-step decomposition for the deterministic path |
| [verification.py](../../../src/fastapi/app/agent/pipeline/verification.py) | Deterministic-path verifier |

---

## 4. Hallucination layer modules (recap from Ch 06)

[src/fastapi/app/agent/hallucination/](../../../src/fastapi/app/agent/hallucination/). **9 files** — see [Ch 06 §6](../manual/06-retrieval-and-agents.md) for full classification.

| Module | Layer |
|---|---|
| [layer1_retrieval.py](../../../src/fastapi/app/agent/hallucination/layer1_retrieval.py) | 1. Retrieval quality gate |
| [layer2_typed_output.py](../../../src/fastapi/app/agent/hallucination/layer2_typed_output.py) | 2. Pydantic AI typed-output |
| [layer3_numerical.py](../../../src/fastapi/app/agent/hallucination/layer3_numerical.py) | 3. Numerical verification |
| [layer4_entity.py](../../../src/fastapi/app/agent/hallucination/layer4_entity.py) | 4. Entity resolution |
| [layer5_provenance.py](../../../src/fastapi/app/agent/hallucination/layer5_provenance.py) | 5. Chunk provenance |
| [layer6_constraints.py](../../../src/fastapi/app/agent/hallucination/layer6_constraints.py) + [layer6_constraints.json](../../../src/fastapi/app/agent/hallucination/layer6_constraints.json) | 6. Geological constraints |
| [layer_completeness.py](../../../src/fastapi/app/agent/hallucination/layer_completeness.py) | OIUR envelope completeness |
| [orchestrator_validators.py](../../../src/fastapi/app/agent/hallucination/orchestrator_validators.py) | Top-level validator pipeline |
| [qualitative_detector.py](../../../src/fastapi/app/agent/hallucination/qualitative_detector.py) | Catches qualitative-only answers |

---

## 5. Prompt template registry (19 templates)

[src/fastapi/app/agent/prompts/](../../../src/fastapi/app/agent/prompts/).
Each prompt is a Python module that returns a versioned template.

### 5.1 Orchestrator system prompts (8 templates)

Two style variants (`_colon` / `_dash`) per format so the orchestrator
can A/B prompt formats without code change:

| Template | Family |
|---|---|
| `orchestrator_default_colon` / `_dash` | Default chat prompts |
| `orchestrator_graph_colon` / `_dash` | Graph-tool-output prompts |
| `orchestrator_narrative_colon` / `_dash` | Narrative-answer prompts |
| `orchestrator_numeric_colon` / `_dash` | Numeric-claim prompts |
| `orchestrator_shared_preamble_colon` / `_dash` | Shared preamble across all four families above |

### 5.2 Component prompts (8 templates)

| Template | Used by |
|---|---|
| `agent_system` | `@georag_agent`-wrapped agent invocations |
| `classifier_system` | Intent / domain / document classifiers (LLM fallback path) |
| `answer_emphasis_section` | Answer section emphasis directive |
| `decision_support_section` | Decision-support intent section |
| `oiur_section` | OIUR envelope section template (legacy — being superseded) |
| `structured_answer_format` | Plan §4a 8-section structured answer (ADR-0009 follow-up — supersedes OIUR per [Ch 15 row 4](../manual/15-design-docs-index.md)) |
| `rephrase_system` | Query rephrase prompt |
| `example_system` | Few-shot example wrapping |

### 5.3 Registry

[_version_registry.py](../../../src/fastapi/app/agent/prompts/_version_registry.py)
pins the active version per template. Changing a prompt **must** bump
the registry version so `silver.answer_runs` can record which prompt
produced which answer.

---

## 6. Schemas

[src/fastapi/app/agent/schemas/](../../../src/fastapi/app/agent/schemas/):

| Module | Job |
|---|---|
| [geo_answer.py](../../../src/fastapi/app/agent/schemas/geo_answer.py) | Pydantic `GeoRAGResponse` schema (OIUR + structured_answer envelope) |

---

## 7. Agent-shaped services (9 subdirs)

These live under `src/fastapi/app/services/` but contain agent-flavoured
state-graph or scoring logic:

| Subdir | Role |
|---|---|
| [services/target_recommendation/](../../../src/fastapi/app/services/target_recommendation/) | **LangGraph #2** — TRG (covered §1.2 above) |
| [services/llm_incident_diagnosis/](../../../src/fastapi/app/services/llm_incident_diagnosis/) | **LangGraph #3** — incident triage (covered §1.3) |
| [services/decision_intelligence/](../../../src/fastapi/app/services/decision_intelligence/) | `recorder.py` + `summary.py` — write/read `silver.decision_records` family |
| [services/source_trust/](../../../src/fastapi/app/services/source_trust/) | Source-trust scoring at serve time (model trained by `train_source_trust` workflow) |
| [services/target_scoring_ml/](../../../src/fastapi/app/services/target_scoring_ml/) | XGBoost+SHAP target scoring (Phase 12 graduation surface) |
| [services/targeting/](../../../src/fastapi/app/services/targeting/) | Targeting helpers (separate from `target_recommendation`) |
| [services/shadow_diff/](../../../src/fastapi/app/services/shadow_diff/) | ADR-0009 shadow A/B diff for Spine A + Spine B telemetry |
| [services/tool_gateway/](../../../src/fastapi/app/services/tool_gateway/) | Tool dispatch gateway used by Phase 7/8 agents |
| [services/geological_reasoning/](../../../src/fastapi/app/services/geological_reasoning/) | `hypothesis_generator.py` shared by the Phase 9 `hypothesis_generator` agent shell |
| [services/report_builder/](../../../src/fastapi/app/services/report_builder/) | Backs Phase 7 report-production agents (called by `report_planner`, `appendix_builder`, etc.) |
| [services/visualizations/](../../../src/fastapi/app/services/visualizations/) | Server-side viz computation backing `viz_builder.py` |
| [services/support_cockpit/](../../../src/fastapi/app/services/support_cockpit/) | Operator-cockpit backend for Phase 0 `support_packet` + Phase 10 agents |
| [services/eval/](../../../src/fastapi/app/services/eval/) | Eval-harness service backing `golden_query_harness.py` |

---

## 8. Dispatchers

[src/fastapi/app/services/dispatchers/](../../../src/fastapi/app/services/dispatchers/).
Tool/integration dispatchers used by the agent dispatcher (`_call_tool_safely`):

| Dispatcher | Target |
|---|---|
| [kestra.py](../../../src/fastapi/app/services/dispatchers/kestra.py) | Triggers Kestra flows from agent tool calls |
| [pagerduty.py](../../../src/fastapi/app/services/dispatchers/pagerduty.py) | Pages PagerDuty from Phase 10 agents on Tier 1 incidents |

---

## 9. Cross-references

| Topic | Where covered |
|---|---|
| Per-phase Pydantic-AI agents (~41) | [Appendix M](M-agents-and-ml-catalog.md) §§ 2–8 |
| `@georag_agent` runtime contract | [Appendix M](M-agents-and-ml-catalog.md) §1 |
| ML training pipelines | [Appendix M](M-agents-and-ml-catalog.md) §10 |
| Hallucination layers | [Ch 06 §6](../manual/06-retrieval-and-agents.md) + §4 above |
| Intent classifier | [Ch 06 §3](../manual/06-retrieval-and-agents.md) |
| Spine A composition | [Ch 16 §1](../manual/16-algorithmic-spines.md) + §2.4 above |
| Spine B repair loop | [Ch 16 §2](../manual/16-algorithmic-spines.md) + §2.5 above |
| ADR-0006 single-graph decision | [docs/adr/0006-...md](../../adr/0006-agentic-retrieval-single-graph.md) — note: this ADR codifies that there is **one** chat-path LangGraph; the `target_recommendation` and `llm_incident_diagnosis` LangGraphs are **separate operator/ops surfaces** and don't contradict the ADR |
| ADR-0009 algorithmic spines | [docs/adr/0009-...md](../../adr/0009-algorithmic-spines-rollout.md) |
| ADR-0010 canonical chunked corpus | [docs/adr/0010-...md](../../adr/0010-document-passages-canonical-chunked-corpus.md) |

---

## 10. Audit summary — what is now captured

| Category | Count | Where |
|---|---|---|
| LangGraphs | 3 (chat / targeting / incident) | §1 above |
| Retrieval-path modules (`agent/*.py`) | 50 | §2 above |
| Hallucination layers | 9 | §4 above |
| Pipeline (legacy deterministic) | 3 | §3 above |
| Prompt templates | 19 | §5 above |
| Agent-shaped services subdirs | 13 | §7 above |
| Dispatchers | 2 | §8 above |
| Pydantic-AI per-phase agents | ~41 | [Appendix M §§2-8](M-agents-and-ml-catalog.md) |
| Classifiers | 6 | [Appendix M §9](M-agents-and-ml-catalog.md) + §2.3 above |
| ML training pipelines | 5 | [Appendix M §10](M-agents-and-ml-catalog.md) |
| **TOTAL agent-shaped surfaces** | **~150** | M + N together |
