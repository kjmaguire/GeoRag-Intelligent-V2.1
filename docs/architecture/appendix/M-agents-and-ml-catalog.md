# Appendix M — Agents & ML Catalog

Status: **Draft.** The companion to [Ch 06](../manual/06-retrieval-and-agents.md)
(retrieval-path agents) and [Ch 08](../manual/08-llm-and-ml.md) (LLM + core
ML models). This appendix enumerates **every** Pydantic-AI agent, every
classifier, every trained model, and the LoRA fine-tune pipeline.

Counts at Pass 4 close: **~41 Pydantic-AI agents**, **6 classifiers**,
**5 ML training pipelines**.

## 1. Agent runtime infrastructure

The `@georag_agent` decorator wraps every agent function with a uniform
operational contract.

- **Source of truth**: [src/fastapi/app/agents/wrapper.py](../../../src/fastapi/app/agents/wrapper.py).
- **Runtime registration**: [src/fastapi/app/agents/runtime.py](../../../src/fastapi/app/agents/runtime.py) —
  `register_runtime(pg_pool, redis)` wires the global state from FastAPI's
  lifespan. Tests inject their own pool.
- **Context**: [src/fastapi/app/agents/context.py](../../../src/fastapi/app/agents/context.py)
  — `AgentContext` holds workspace_id, user_id, request_id, trace_id, plus
  the `usage` accumulator (LLM tokens, tool calls) and the `outcome`
  enum.
- **Exceptions**: [src/fastapi/app/agents/exceptions.py](../../../src/fastapi/app/agents/exceptions.py)
  — `AgentError` hierarchy; `AgentRefusalError` is **not** counted as a
  circuit-breaker failure.

### What `@georag_agent` does per call

From [wrapper.py](../../../src/fastapi/app/agents/wrapper.py):

1. Build `AgentContext` from kwargs + decorator metadata.
2. Read `workspace.agent_timeouts` row (in-process cache, TTL 60 s).
3. Check Redis-backed circuit breaker — per-workspace + global.
4. Compute idempotency key for **R2+** tier agents (skip R0/R1).
5. Look up `workspace.idempotency_keys` — if hit, return stored result.
6. Run agent under `asyncio.wait_for(hard_timeout)`.
7. Persist idempotency record on success (R2+).
8. Write `usage.usage_events` if `ctx.usage` is non-empty.
9. Update circuit breaker (success → reset; failure → increment).
10. Emit `audit.audit_ledger` entry.
11. Return `AgentResult(value, outcome, ctx)`.

### Risk-tier convention

Declared at decoration time. **R0** = read-only, no side effects.
**R1** = read with cache write. **R2** = single-write side effect.
**R3+** = multi-write or cross-store. Idempotency mandatory R2+;
break-glass approval mandatory R3+.

## 2. Phase 0 — Infrastructure agents (10)

[src/fastapi/app/agents/phase0/](../../../src/fastapi/app/agents/phase0/).
All read-only / operator-facing. Run via Hatchet `phase0_agents` workflow.

| Agent | File | Job | Writes |
|---|---|---|---|
| Index Health | [index_health.py](../../../src/fastapi/app/agents/phase0/index_health.py) | PG slow queries (pg_stat_statements), bloat, zero-hit indices, hypopg cost-delta suggestions, Qdrant HNSW reachability self-check, Neo4j page-cache hit ratio via JMX | `silver.corpus_health_findings` |
| Lineage Reporter | [lineage_reporter.py](../../../src/fastapi/app/agents/phase0/lineage_reporter.py) | Cross-store lineage map for a given silver row → which bronze, parser_run, embed run, Qdrant point, Neo4j node | (read-only — returns JSON) |
| LLM Incident Diagnosis | [llm_incident_diagnosis.py](../../../src/fastapi/app/agents/phase0/llm_incident_diagnosis.py) | Reads Tempo + Loki + audit_ledger for a failing trace_id → composes a remediation packet | `ops.support_replay_runs` |
| Model Cost Summary | [model_cost_summary.py](../../../src/fastapi/app/agents/phase0/model_cost_summary.py) | Per-workspace per-model token cost rollup over a window | (read-only) |
| Model Upgrade Watch | [model_upgrade_watch.py](../../../src/fastapi/app/agents/phase0/model_upgrade_watch.py) | Watches HuggingFace + Anthropic for new model versions vs the configured `LLM_PRIMARY_MODEL` | Posts notifications via the notifications queue |
| Storage Tiering | [storage_tiering.py](../../../src/fastapi/app/agents/phase0/storage_tiering.py) | Decides which bronze objects move `tier-hot` → `tier-warm` → `tier-cold` based on `silver.storage_tier_policy` | `silver.storage_tier_policy.last_decision_at`; SeaweedFS lifecycle actions |
| Store Reconciliation | [store_reconciliation.py](../../../src/fastapi/app/agents/phase0/store_reconciliation.py) | Cross-store consistency check (PG silver ↔ Qdrant ↔ Neo4j) | `silver.store_reconciliation_findings` |
| Support Packet | [support_packet.py](../../../src/fastapi/app/agents/phase0/support_packet.py) | Bundles trace + audit + repro envelope for a support ticket | `silver.support_packets` |
| Tenant Isolation Auditor | [tenant_isolation_auditor.py](../../../src/fastapi/app/agents/phase0/tenant_isolation_auditor.py) | Walks every workspace-scoped table; asserts RLS + FORCE; flags cross-workspace FK leaks | `audit.audit_ledger` (action_type=`tenant_isolation.audit`) |
| vLLM Security Check | [vllm_security_check.py](../../../src/fastapi/app/agents/phase0/vllm_security_check.py) | Verifies the vLLM endpoint is on the internal network only + the served model name matches `VLLM_MODEL` env | `audit.integration_credentials_audit` |

## 3. Phase 5 — Visual QA (2)

[src/fastapi/app/agents/phase5/](../../../src/fastapi/app/agents/phase5/).

| Agent | Job |
|---|---|
| `drillhole_visual_qa` | Audit the §B6 cross-section panels + strip-log renderings vs source silver rows |
| `visual_readiness` | Decides whether a project has enough data to render the Workspace 3D view |

## 4. Phase 6 — Public/Private boundary (1)

[src/fastapi/app/agents/phase6/public_private_boundary.py](../../../src/fastapi/app/agents/phase6/public_private_boundary.py).
Decides per-document whether content can be quoted in public-corpus
chat vs only private-workspace chat. Backs ADR-0007 `public_geoscience`
chat surface gating.

## 5. Phase 7 — Report production (8)

[src/fastapi/app/agents/phase7/](../../../src/fastapi/app/agents/phase7/).

| Agent | Job |
|---|---|
| `report_planner` | NI 43-101 outline assembly from a workspace evidence set |
| `appendix_builder` | Builds NI 43-101 Appendix A/B/C (data tables) |
| `claim_validator` | Pre-export validation of every numeric/qualitative claim against `silver.evidence_items` |
| `conflict_resolver` | Resolves intra-report conflicting statements |
| `evidence_curator` | Promotes/demotes evidence inclusion per section |
| `export_compliance` | NI 43-101 / CIM-equivalent compliance checks before export |
| `map_chart_planner` | Decides which Plotly + MapLibre exports a report needs |
| `presentation_coach` | Pass over the assembled draft for tone / completeness |

## 6. Phase 8 — Targeting (11)

[src/fastapi/app/agents/phase8/](../../../src/fastapi/app/agents/phase8/).
The drill-target generation surface. All agents write to `targeting.*`
or `silver.target_rationales`.

| Agent | Job |
|---|---|
| `candidate_generation` | Generate candidate target zones from PostGIS + Neo4j context |
| `target_scoring` | §8.5 / §18.4 — weighted scoring per-zone; delegates to TRG `score_candidate_zones` for the §8.7 math |
| `recommendation_explainer` | Per-target rationale with SHAP-equivalent breakdown (mandatory per §18.3 "no black-box targeting") |
| `evidence_layer` | Per-target evidence aggregation |
| `uncertainty` | Per-target uncertainty breakdown ([Targets.tsx](../../../resources/js/Pages/Foundry/Targets.tsx) consumer) |
| `constraint` | Hard / soft constraint application |
| `deposit_model` | Per-deposit-type model parameters |
| `backtesting` | Backtest a model against historical labelled targets |
| `field_outcome` | Closes the loop on a drilled target's actual outcome |
| `geologist_signoff` | Human-in-the-loop approval gate |
| `scenario_planning` | "What if commodity price = X" scenario rollouts |

## 7. Phase 9 — Discovery (4)

[src/fastapi/app/agents/phase9/](../../../src/fastapi/app/agents/phase9/).

| Agent | Job |
|---|---|
| `analogue_finder` | Find analogue deposits across the corpus |
| `hypothesis_generator` | Generate testable hypotheses; writes `silver.hypotheses` |
| `next_best_data` | Recommends what dataset to acquire next |
| `spatial_relationship` | Cross-feature spatial reasoning (proximity, alignment, density) |

## 8. Phase 10 — Support ops (5)

[src/fastapi/app/agents/phase10/](../../../src/fastapi/app/agents/phase10/).

| Agent | Job |
|---|---|
| `ticket_triage` | Inbound ticket → priority + topic + suggested owner |
| `root_cause_investigation` | Traces a ticket to the failing trace_id / commit |
| `customer_response_drafting` | Drafts the reply |
| `escalation_routing` | Routes per Tier 1/2/3 |
| `support_packet` | Phase-10 variant that adds product-team-facing summary |

## 9. Classifiers (6)

All rule-based or LLM-fallback. Each returns a typed classification +
confidence.

| Classifier | File | Output |
|---|---|---|
| Intent (8 labels) | [agent/agentic_retrieval/intent_classifier.py](../../../src/fastapi/app/agent/agentic_retrieval/intent_classifier.py) | `factual_lookup` / `synthesis` / `hypothesis_generation` / `anomaly_detection` / `uncertainty_quantification` / `decision_support` / `project_summary` / `coverage_gap` |
| Decision-support | [agent/decision_support_classifier.py](../../../src/fastapi/app/agent/decision_support_classifier.py) | Weak-signal classifier for queries like "should we drill here" |
| Domain | [services/domain_classifier.py](../../../src/fastapi/app/services/domain_classifier.py) | Routes to the right ontology subgraph (gold/Cu/U₃O₈/etc.) |
| Document | [services/document_classifier.py](../../../src/fastapi/app/services/document_classifier.py) | Sets `silver.reports.report_type` (NI 43-101 / internal / regulatory / etc.) |
| Query | [services/query_classifier.py](../../../src/fastapi/app/services/query_classifier.py) | Per-query metadata enrichment before retrieval |
| LLM (generic) | [agent/llm_classifier.py](../../../src/fastapi/app/agent/llm_classifier.py) | Generic LLM-fallback classification primitive used by the above when rule confidence < 0.6 |

## 10. ML training pipelines (5)

### 10.1 Reranker LoRA

The bge-reranker-base fine-tune pipeline (path C — fine-tune in place,
[notes/INDEX.md#project_reranker_v1](../notes/INDEX.md#project_reranker_v1)).

| Stage | File | Notes |
|---|---|---|
| Label synthesis | [src/dagster/georag_dagster/assets/reranker_labels.py](../../../src/dagster/georag_dagster/assets/reranker_labels.py) + [reranker_labels_helpers.py](../../../src/dagster/georag_dagster/assets/reranker_labels_helpers.py) | Generates (query, chunk_id, label, hardneg_ids) triples from the canonical `silver.document_passages` corpus (ADR-0010 re-target). Generator model: `ELVISIO/Qwen3-30B-A3B-Instruct-2507-AWQ`. |
| Training dataset | [src/fastapi/data/reranker_dataset_v1/train.jsonl](../../../src/fastapi/data/reranker_dataset_v1/train.jsonl) | JSON Lines: `{query, chunk_id, pdf_id, page, bbox, label, hardneg_ids[], gen_model, gen_prompt_hash, fact_span}` |
| Trainer | [scripts/train_reranker_lora.py](../../../scripts/train_reranker_lora.py) | LoRA fine-tune of `BAAI/bge-reranker-base`. Runs in the `fastapi` container with GPU passthrough ([docker-compose.yml:1086-1094](../../../docker-compose.yml)) — stop vLLM before training (they share the A4500). |
| Eval | [scripts/eval_reranker_lora.py](../../../scripts/eval_reranker_lora.py) | NDCG@k / MRR over the golden eval set |
| Production wire | [src/fastapi/app/services/reranker.py](../../../src/fastapi/app/services/reranker.py) | Loads bge-reranker-base + LoRA adapter; 8 s `wait_for` budget |
| Locked-decision regression | [src/dagster/tests/test_reranker_locked_decisions.py](../../../src/dagster/tests/test_reranker_locked_decisions.py) | Pins a frozen SME-labelled set; any LoRA bake that flips one of these labels fails CI |
| Canonical corpus contract | [src/dagster/tests/test_reranker_uses_document_passages_canonical.py](../../../src/dagster/tests/test_reranker_uses_document_passages_canonical.py) | ADR-0010 contract: the reranker label chain must read `silver.document_passages`, not `silver.ingest_extractions` |

### 10.2 Source-trust model

Hatchet workflow [train_source_trust.py](../../../src/fastapi/app/hatchet_workflows/train_source_trust.py).

- Reads citation feedback + `silver.message_feedback` + cross-store agreement signals.
- Writes `silver.source_trust_scores` + `silver.source_trust_features`.
- Triggered manually via `POST /api/v1/admin/ml/train-source-trust` ([routers/ml_training.py](../../../src/fastapi/app/routers/ml_training.py)).
- **Body**: `{workspace_id, min_citations_per_source, model_version}`.
- Run history: `audit.audit_ledger` with `action_type='ml.train_source_trust'`.

### 10.3 Target scoring model

Hatchet workflow [train_target_model.py](../../../src/fastapi/app/hatchet_workflows/train_target_model.py).

- Trains the §18.4 target-scoring model + per-factor weights.
- Reads `silver.collars` + `silver.assays_v2` + `silver.geophysics_surveys`
  + `targeting.target_backtests` history.
- Writes `targeting.target_score_factors` (the factor weights).
- Phase 8 weighted scoring today; Phase 12 will augment with XGBoost+SHAP.
- Triggered via `POST /api/v1/admin/ml/train-target-model` ([routers/ml_training.py](../../../src/fastapi/app/routers/ml_training.py)).
- **Body**: `{target_model_id, activate_on_success}`.

### 10.4 SPLADE++ sparse encoder

Dagster asset [sparse_encoder.py](../../../src/dagster/georag_dagster/assets/sparse_encoder.py)
— batch-encodes `silver.document_passages` rows to sparse vectors;
writes the `text` sparse slot of the Qdrant `georag_chunks` collection
(ADR-0010).

### 10.5 Continuous learning loop

Hatchet workflow [continuous_learning_loop.py](../../../src/fastapi/app/hatchet_workflows/continuous_learning_loop.py)
+ [field_outcome_learning.py](../../../src/fastapi/app/hatchet_workflows/field_outcome_learning.py).
Closes the loop on **drilled-target outcomes** → feeds back into
target-scoring weights. Experimental.

## 11. ML training admin surface

[src/fastapi/app/routers/ml_training.py](../../../src/fastapi/app/routers/ml_training.py)
backs the `/admin/ml/training-runs` Inertia surface.

| Method | Path | Body |
|---|---|---|
| GET | `/api/v1/admin/ml/training-runs` | (none) — lists recent training runs from `audit.audit_ledger` |
| POST | `/api/v1/admin/ml/train-target-model` | `{target_model_id, activate_on_success}` |
| POST | `/api/v1/admin/ml/train-source-trust` | `{workspace_id, min_citations_per_source, model_version}` |

The workflows are Hatchet-decorated; the router invokes the underlying
task body via `aio_mock_run` so it runs inline for synchronous operator
flows. For long jobs the operator switches to Hatchet's
client-side `.aio_run()` to enqueue a real workflow run.

## 12. LoRA serving path

At inference time:

1. `services/reranker.py` boots once per worker → loads
   `BAAI/bge-reranker-base` from HF cache.
2. If `RERANKER_LORA_ADAPTER_PATH` env points at a baked adapter directory,
   loads the LoRA adapter via PEFT and merges weights at boot (no per-
   request merge cost).
3. CrossEncoder scoring runs at 8 s `wait_for` budget; 2000-char
   pre-truncate per (query, passage) pair.
4. New LoRA bake → `activate_on_success=true` flips the env atomically
   on the next worker reload.

## 13. Hallucination-prevention "soft ML"

[src/fastapi/app/agent/hallucination/](../../../src/fastapi/app/agent/hallucination/)
holds **6 layers** documented in [Ch 06 §6](../manual/06-retrieval-and-agents.md):

| Layer | File | Kind |
|---|---|---|
| 1. Retrieval quality gate | [layer1_retrieval.py](../../../src/fastapi/app/agent/hallucination/layer1_retrieval.py) | ML score threshold |
| 2. Typed output validation | [layer2_typed_output.py](../../../src/fastapi/app/agent/hallucination/layer2_typed_output.py) | Pydantic AI |
| 3. Numerical claim verification | [layer3_numerical.py](../../../src/fastapi/app/agent/hallucination/layer3_numerical.py) | Rule + unit normalisation |
| 4. Entity resolution | [layer4_entity.py](../../../src/fastapi/app/agent/hallucination/layer4_entity.py) | `silver.entity_aliases` lookup (Spine A) |
| 5. Chunk provenance | [layer5_provenance.py](../../../src/fastapi/app/agent/hallucination/layer5_provenance.py) | Rule |
| 6. Geological constraints | [layer6_constraints.py](../../../src/fastapi/app/agent/hallucination/layer6_constraints.py) + [layer6_constraints.json](../../../src/fastapi/app/agent/hallucination/layer6_constraints.json) | Domain rule pack |

Plus `qualitative_detector.py` (catches qualitative-only answers) and
`layer_completeness.py` (OIUR envelope completeness).

## 14. Spine A library (ADR-0009 — covered in Ch 16)

5 modules live at `src/fastapi/app/agent/`:
- `context_prep.py` (§3a-c-f composition)
- `multi_turn_resolver.py` (§3e — pronoun/demonstrative/comparative)
- `entity_resolver.py` (§2c — `silver.entity_aliases` lookup + `silver.entity_gaps` writes)
- `geospatial_planner.py` (§2g — PostGIS query planner)
- `tools_geospatial.py` (§2g — tool wire)

See [Ch 16 §1](../manual/16-algorithmic-spines.md).

## 15. Model registry summary

| Model | Loaded by | Where it runs | LoRA-tunable? |
|---|---|---|---|
| Qwen/Qwen3-14B-AWQ | vllm container | GPU | No (full bake) |
| Qwen/Qwen2.5-VL-7B (opt-in) | vllm container | GPU | No |
| BAAI/bge-small-en-v1.5 | hatchet-worker-ai | GPU | **Yes** — ADR-0008 Option D path |
| BAAI/bge-reranker-base | fastapi container | CPU + (optional GPU for LoRA bake) | **Yes (live)** — §10.1 |
| naver/splade-cocondenser-ensembledistil | hatchet-worker-ai | GPU | No |
| Anthropic Claude (Haiku/Sonnet/Opus) | Anthropic API | — | No |
| Generator model (label synthesis) | `ELVISIO/Qwen3-30B-A3B-Instruct-2507-AWQ` on vLLM | GPU | No |
| Source-trust model | Custom; written by `train_source_trust` workflow | CPU at serve | n/a (sklearn-style) |
| Target scoring model | Custom; written by `train_target_model` workflow | CPU at serve | n/a (weighted scoring; Phase 12 = XGBoost+SHAP) |

## 16. Open work tracked

Items moving into Z roadmap:

- **bge-small fine-tune in place** (ADR-0008 Option D) — Owed. Pipeline parallel to §10.1 but smaller dataset.
- **XGBoost+SHAP target scoring** (Phase 12) — Owed.
- **Agent circuit-breaker dashboards** in Grafana — Owed.
- **Agent invocation audit table** — currently `workspace.tool_invocations` exists but isn't surfaced in a dashboard.
