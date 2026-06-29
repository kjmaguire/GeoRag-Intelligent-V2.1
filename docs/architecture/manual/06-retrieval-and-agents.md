# Chapter 06 — Retrieval and Agents

Every component on the chat path classified by *what kind of thing it is* —
LLM agent (multi-call reasoning loop), ML model (one forward pass), or
rule-based code (deterministic, no probabilistic step).

## 1. The end-to-end path

```
POST /api/chat ──▶ laravel-octane
                       │  (Sanctum cookie)
                       ▼
              ChatController dispatches Laravel queue job (Horizon),
              streams back via Reverb channel query.streaming.{run_id}
                       │
                       │ POST http://fastapi:8000/v1/query (X-Service-Key)
                       ▼
              FastAPI handler — app/routers/queries.py
                       │
                       ▼
              app/agent/orchestrator/ — `Orchestrator.handle_query()`
                       │
            ┌──────────┴──────────┐
            ▼                     ▼
  AGENTIC_RETRIEVAL_V2_ENABLED=true     false
            │                              │
            ▼                              ▼
   run_agentic_retrieval                run_deterministic_rag
   (LangGraph, §04j)                    (legacy linear chain)
```

The flag default is `False` in `config.py`; `.env` flips it on for dev
([docker-compose.yml:993-999](../../../docker-compose.yml)).

## 2. The §04j LangGraph pipeline

[src/fastapi/app/agent/agentic_retrieval/graph.py:40-51](../../../src/fastapi/app/agent/agentic_retrieval/graph.py).
Compiled once, cached via `@lru_cache(maxsize=1)`.

> **ADR-0006** ([docs/adr/0006-agentic-retrieval-single-graph.md](../../adr/0006-agentic-retrieval-single-graph.md))
> codifies the choice: **one** LangGraph + six routed intents rather than
> six data-store-specific subgraphs. See
> [six_subgraphs_spec.md](../six_subgraphs_spec.md) for the
> reconciliation against plan §0d. **ADR-0004** ([0004-orchestrator-definition-short-circuit.md](../../adr/0004-orchestrator-definition-short-circuit.md))
> proposes a high-confidence definition short-circuit that bypasses the
> graph for cheap `factual_lookup` queries — still in proposal status.

```
START → classify → route → execute → assemble → validate → demote → persist → END
```

State is `AgenticRetrievalState`
([src/fastapi/app/agent/agentic_retrieval/state.py](../../../src/fastapi/app/agent/agentic_retrieval/state.py))
— a typed dict carrying the query, intent, retrieval profile, hits, draft
answer, validation results, lineage row.

| Node | Classification | What it does | File |
|------|---------------|--------------|------|
| `classify_node` | LLM agent + rules | Calls `classify_intent()` to pick one of 8 intents | [agentic_retrieval/nodes.py](../../../src/fastapi/app/agent/agentic_retrieval/nodes.py) |
| `route_node` | Rule-based | Resolves the intent to a `RetrievalProfile` ([retrieval_profile.py](../../../src/fastapi/app/agent/agentic_retrieval/retrieval_profile.py)) | nodes.py |
| `execute_node` | Mix (calls tools) | Dispatches to tools (hybrid_search, structured_query, graph_traversal) via the dispatcher (`_call_tool_safely`) | nodes.py + [services/dispatchers/](../../../src/fastapi/app/services/dispatchers/) |
| `assemble_node` | LLM agent (Pydantic AI) | Builds the OIUR answer envelope. Citations mandatory at the type level. | nodes.py + [agent/oiur_parser.py](../../../src/fastapi/app/agent/oiur_parser.py) |
| `validate_node` | Mix (ML + rules) | Applies the six hallucination layers — see §6 below | [agent/hallucination/](../../../src/fastapi/app/agent/hallucination/) |
| `demote_node` | Rule-based | If validation failed, demotes/strips claims rather than blocking the whole answer; logs a `refusal_reason` | nodes.py |
| `persist_node` | Rule-based | Writes `silver.answer_runs` + child rows. **Currently best-effort — this is a known gap (see §2.1).** | nodes.py |

### 2.1 Persistence is currently best-effort — fix required

**Problem.** `persist_node` is the last node in the LangGraph
([graph.py:50](../../../src/fastapi/app/agent/agentic_retrieval/graph.py)).
By the time it runs, the answer has already streamed to the user via
Reverb. A failure inside `persist_node` (DB hiccup, RLS misconfiguration,
constraint violation) is logged but does **not** propagate to the client.

Why that violates the citation-first contract:
- Hard Rule #4 says citations are mandatory. If we never wrote the
  `answer_runs` row + `answer_citation_items` + `answer_citation_spans`,
  the citation never lands in the audit trail — even though the user saw
  it.
- The reproducibility / replay path (`support_replay`, `silver.answer_runs`
  lookups) silently misses these turns.
- A workspace cost ceiling that depends on `usage.usage_events` linked to
  `answer_runs` under-counts.

**Required posture.** `persist_node` must be **transactional, retry-bounded,
and refusal-on-final-failure**:

1. Write `answer_runs` + child rows in a **single transaction** through
   the direct PG path (already the case — `POSTGRES_DIRECT_HOST` is
   available to the FastAPI process).
2. On failure, retry with exponential backoff (`tenacity` is already a
   dependency) — `tries=3, backoff=[0.2s, 1s, 5s]`.
3. On final failure, **emit a refusal event** on
   `query.streaming.{run_id}` (`QueryPersistFailure` event with
   `recoverable: false`) and downgrade the answer status to
   `citation_lifecycle_state='persistence_failed'` on a fallback minimal
   row write — failing **that** triggers an Alertmanager page.
4. The frontend treats `QueryPersistFailure` as a turn-final state and
   surfaces a "This answer was not recorded — refresh to retry" banner.

**Status:** not yet implemented. Tracked as part of the citation
lifecycle hardening, alongside the layer-5 provenance binding (Ch 06 §6).

## 3. The intent classifier

[src/fastapi/app/agent/agentic_retrieval/intent_classifier.py](../../../src/fastapi/app/agent/agentic_retrieval/intent_classifier.py).

**Classification:** Rule-based regex + ML fallback. Hybrid.

- Eight intent labels
  ([intent_classifier.py:47-57](../../../src/fastapi/app/agent/agentic_retrieval/intent_classifier.py)):
  - `factual_lookup`, `synthesis`, `hypothesis_generation`,
    `anomaly_detection`, `uncertainty_quantification`, `decision_support`,
  - ADR-0007 additions: `project_summary`, `coverage_gap`.
- Regex triggers per intent (`_TRIGGERS`, line 115).
- Tiebreak: scores within 0.1 → broken by `_BREADTH_RANK` (broader retrieval
  preferred — the plan’s "retrieve more, not less" on ambiguity).
- Confidence < 0.6 → LLM fallback (single Qwen call) when an HTTP client is
  available.
- `classify_intent_sync()` (line 576) — the legacy synchronous shim.

Returns `IntentResult` with `intent`, `confidence`, `second_choice`, and a
canonical `tool_target` (`query_collar_details`, `query_project_summary`,
`query_coverage_gap`, etc.).

## 4. The dispatcher (`_call_tool_safely`)

[src/fastapi/app/services/dispatchers/](../../../src/fastapi/app/services/dispatchers/).
The orchestrator-facing wrapper that invokes a Pydantic AI tool by name,
passing the `RunContext` arg correctly. The 2026-05-25 bug fix in
[project_agentic_dispatcher_ctx_fix_2026_05_25](../notes/INDEX.md#project_agentic_dispatcher_ctx_fix_2026_05_25)
landed an introspection regression test that pins each tool’s signature so
future "missing RunContext" mistakes can’t silently drop results.

## 5. Retrieval tools (one per intent class)

[src/fastapi/app/agent/](../../../src/fastapi/app/agent/). Each tool returns
typed `EvidenceItem` lists. All workspace-scoped.

| Tool | Backing store | Classification | File |
|---|---|---|---|
| `hybrid_search` | Qdrant + SPLADE + BM25 fusion | ML (embed) + ML (sparse) + rule (RRF/DBSF) | services/fusion.py |
| `query_collar_details` | Postgres `silver.collars` + Neo4j neighbours | Rule-based SQL | services/dispatchers/structured_query.py |
| `query_project_summary` | Postgres aggregate query + Neo4j entity rollup | Rule-based | dispatchers (ADR-0007 PR-1) |
| `query_coverage_gap` | Postgres set-difference vs golden-corpus catalogue | Rule-based | dispatchers (ADR-0007 PR-1) |
| `vector_search` | Qdrant only | ML (Qwen3-Embedding-0.6B encoder, 1024-dim — swapped from bge-small 2026-06-03) | services/fusion.py |
| `splade_search` | Qdrant sparse vectors | ML (SPLADE++ encoder) | services/fusion.py |
| `bm25_search` | Postgres tsvector `silver.document_passages` | Rule-based | services/fusion.py |
| `graph_traversal` | Neo4j Cypher | Rule-based | agent/graph_entities.py |
| `geological_query_expansion` | Geological ontology (`silver.geological_ontology_*`) | Rule-based | agent/geological_query_expansion.py |
| `figure_extractor` | PostGIS `silver.report_figures` + SeaweedFS PNG fetch | Rule-based | agent/figure_extractor.py |
| `anomaly_detector` | Postgres aggregate query (z-score, IQR) | ML (statistical) | agent/anomaly_detector.py |
| `drill_targeting` | `targeting.target_score_factors` | Mix (LLM ranking + rules) | agent/drill_targeting.py |
| `decomposer` | LLM (Qwen) to split multi-clause queries | LLM agent | agent/decomposer.py |
| `anaphora` | LLM-based pronoun resolution across turns | LLM agent | agent/anaphora.py |
| `followups` | LLM-based follow-up generation | LLM agent | agent/followups.py |
| `escalation` + `agentic_escalation` | Rule-based first; LLM if needed | Mix | agent/escalation.py |
| `model_routing` | Rule-based size → model picker | Rule-based | agent/model_routing.py |
| `event_stamper` | Append-only event log of every tool call | Rule-based | agent/event_stamper.py |
| `confidence_computer` | Statistical confidence model | ML (calibration) | agent/confidence_computer.py |
| `citation_binding` | Binds `[ev:xxxxxxxx]` markers to evidence rows | Rule-based | agent/citation_binding.py |

## 6. Hallucination prevention — the §04i six layers

Hard Rule #5. Every code path that touches RAG output must apply all six.
[src/fastapi/app/agent/hallucination/](../../../src/fastapi/app/agent/hallucination/):

| Layer | File | Kind |
|------:|------|------|
| 1. Retrieval quality gate | [layer1_retrieval.py](../../../src/fastapi/app/agent/hallucination/layer1_retrieval.py) | ML score threshold (default `RETRIEVAL_QUALITY_THRESHOLD=0.6` from [docker-compose.yml:992](../../../docker-compose.yml)) |
| 2. Typed output validation | [layer2_typed_output.py](../../../src/fastapi/app/agent/hallucination/layer2_typed_output.py) | Pydantic AI typed-output — refuses unstructured / un-cited claims |
| 3. Numerical claim verification | [layer3_numerical.py](../../../src/fastapi/app/agent/hallucination/layer3_numerical.py) | Re-runs every numeric claim against the cited evidence row; flags mismatch |
| 4. Entity resolution | [layer4_entity.py](../../../src/fastapi/app/agent/hallucination/layer4_entity.py) | Resolves named entities (deposits, holes, formations) against `workspace.entities` + ontology |
| 5. Chunk provenance | [layer5_provenance.py](../../../src/fastapi/app/agent/hallucination/layer5_provenance.py) | Every `[ev:xxxxxxxx]` marker must resolve to a real `silver.evidence_items` row |
| 6. Geological constraints | [layer6_constraints.py](../../../src/fastapi/app/agent/hallucination/layer6_constraints.py) + [layer6_constraints.json](../../../src/fastapi/app/agent/hallucination/layer6_constraints.json) | Domain rule pack (e.g., "azimuth ∈ [0, 360)", "Au grade < 50 g/t in vein deposits unless flagged") |

Additional:
- `qualitative_detector.py` — catches qualitative-only answers ("rich",
  "promising") and forces them to be either retracted or evidence-anchored.
- `layer_completeness.py` — verifies the OIUR envelope has all four sections.
- `orchestrator_validators.py` — top-level validator pipeline.

## 7. The OIUR answer architecture

OIUR = Observation / Interpretation / Uncertainty / Recommendation.

- Pydantic model defined in [src/fastapi/app/models/rag.py](../../../src/fastapi/app/models/rag.py) (`GeoRAGResponse`).
- Parser/composer at [src/fastapi/app/agent/oiur_parser.py](../../../src/fastapi/app/agent/oiur_parser.py).
- Flag: `GEO_ANSWER_OIUR_ENABLED` (default off; flipped on in dev).
- Every observation/interpretation/recommendation MUST carry one or more
  citation markers — rejected by `layer2_typed_output` otherwise.
- The frontend renders the four sections as discrete cards
  ([resources/js/Pages/Chat.tsx](../../../resources/js/Pages/Chat.tsx)).

## 8. The context envelope (Field/Office mode)

[src/fastapi/app/agent/agentic_retrieval/context_envelope.py](../../../src/fastapi/app/agent/agentic_retrieval/context_envelope.py)
defines a 12-field envelope passed from the frontend pre-processor:

- `mode` — `field` | `office`
- Geographic context: lat/lon, project_id, hole_id (if active),
  `current_depth_m`, `azimuth_facing`, `nearby_radius_m`
- Temporal: `as_of_date`
- Permissions tier
- Frontend trace context

Used by `route_node` to skew retrieval (`field` mode favours nearest-by-geom +
recent data; `office` mode opens the full hybrid surface).

Backend pre-processor at
[src/fastapi/app/agent/agentic_retrieval/preprocessor.py](../../../src/fastapi/app/agent/agentic_retrieval/preprocessor.py).
React form lives in [resources/js/Pages/Chat.tsx](../../../resources/js/Pages/Chat.tsx).

## 9. Hole-ID extractor (rule-based)

[src/fastapi/app/agent/](../../../src/fastapi/app/agent/) — `extract_hole_ids`
function. **Pure rule-based.** Per memory
([project_hole_id_extraction_2026_05_21](../notes/INDEX.md#project_hole_id_extraction_2026_05_21)):

- Original matched only letter-prefixed IDs (`PLS-22-08`).
- Now also matches Cameco numeric format (`36-1085`) when the surrounding
  text contains an explicit "hole" / "drillhole" / "DDH" cue.
- The `downhole` gate (which required lithology/log keywords) was relaxed:
  if the query explicitly names a hole, the gate is bypassed.

## 10. Citation lifecycle + claim ledger

- `silver.citation_lifecycle_state` on `answer_runs` tracks
  `pending|resolved|broken|refused`.
- `services/citation_lifecycle.py` walks the answer’s `[ev:xxxxxxxx]`
  markers, joins to `silver.answer_citation_items` and
  `silver.answer_citation_spans`.
- `services/claim_ledger.py` posts every numeric claim into the in-memory
  claim ledger so Layer 3 can re-verify.

## 11. Cross-store reasoning helpers

- `services/cross_store_consistency.py` — checks the same fact against
  Postgres + Neo4j + Qdrant; raises on disagreement.
- `services/cross_workspace_audit.py` — ensures no row references another
  workspace’s data.
- `services/fusion.py` — RRF (Reciprocal Rank Fusion) and DBSF (Distribution
  Based Score Fusion) implementations; selectable per intent via the
  retrieval profile.

## 12. Pydantic AI agents (multi-LLM-call)

Real LLM-driven agents live under:
- [src/fastapi/app/agents/phase0/](../../../src/fastapi/app/agents/phase0/)
- [src/fastapi/app/agents/phase5/](../../../src/fastapi/app/agents/phase5/)
- through [phase10/](../../../src/fastapi/app/agents/phase10/)

These are run **only** by Hatchet workflows (e.g., the Index Health Agent,
Storage Tiering Agent, Support Packet Agent, LLM Incident Diagnosis Agent),
not by the chat path. Each gets a focused tool set and is timeout-budgeted
via `workspace.agent_timeouts`.

## 13. LLM Incident Diagnosis (`services/llm_incident_diagnosis/`)

A multi-agent debugging assistant for ops:
- Reads recent traces from Tempo (via `OTEL_EXPORTER_OTLP_ENDPOINT`).
- Reads Loki logs (LogQL).
- Reads `audit.audit_ledger` for the failing trace_id.
- Composes a remediation packet → posted to `ops.support_replay_runs`.
- Pydantic AI agent; called from the Support Cockpit.

## 14. Refusal path

When validation fails irrecoverably:
- `demote_node` strips the failing claim.
- If nothing remains, `assemble_node` returns a `RefusalResponse` with
  `refusal_reason` and any partial evidence.
- The frontend renders a "No defensible answer" card with the reason.
- `silver.answer_runs.citation_lifecycle_state = 'refused'`.

## 15. Agentic chat-cards (ADR-0007)

[project_chat_cards_initiative_2026_05_25](../notes/INDEX.md#project_chat_cards_initiative_2026_05_25):
4-PR plan + ADR-0007 (Proposed) introduce 5 inline card types
(`evidence_list`, `metric_box`, `coverage_gap_chart`, `project_summary_card`,
`spatial_quick_map`) plus the two new intents. Schema landed; extractors
partially shipped.
