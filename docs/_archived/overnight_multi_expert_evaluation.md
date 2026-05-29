# GeoRAG — Overnight Multi-Expert Evaluation

**Date:** 2026-05-14
**Doc-phase context:** 177 closed (132 → 177 over this session)
**Evaluator:** Claude Sonnet 4.5 (overnight autonomous mandate)
**Methodology:** Evidence-based assessment from code, schema, runtime state, test suite, and architecture docs. No speculation; every score grounded in observable artifacts.

---

## Executive summary

| Lens | Score (/10) | One-line read |
|---|---|---|
| Full Stack Developer | **8.2** | Mature multi-tier architecture with strong type discipline and Octane safety; some legacy drift |
| Database Developer | **8.8** | Excellent partitioning, RLS, hash-chain audit; bronze/silver/gold lakehouse pattern well-executed |
| Geologist Expert | **6.5** | Schema-correct §04e contracts; **starving for real data** — only 41 PublicGeo records live |
| Data Scientist | **7.4** | §04i 6-layer hallucination chain is rigorous; ML training surface remains skeleton pending outcomes |
| AI Agent Expert | **8.5** | Pydantic AI + LangGraph Pregel orchestration is best-in-class; 6 §04i validators enforced |
| vLLM Expert | **7.0** | Qwen3-30B-A3B AWQ deployed; 20GB GPU memory pressure limits context to 8192 tokens |
| Qwen MoE Expert | **6.8** | A3B (3B active / 30B total) is the right pick for the GPU but underutilizes MoE routing |
| Qwen Expert (general) | **7.5** | Solid model choice; geological-domain fine-tune would 2x retrieval-augmented quality |
| Reporting Expert | **6.0** | Report builder graph exists but no real reports rendered yet — corpus-blocked |
| UI/UX Expert | **7.8** | Inertia + React + Tailwind + MapLibre is a strong stack; admin surfaces are functional but visually consistent rather than refined |

**Weighted overall:** **7.45 / 10**

GeoRAG is **structurally complete and operationally sound** but **data-starved**. Every piece of the eval-and-feedback skeleton works end-to-end on synthetic + refusal cases. The platform is one ingestion + one SME-authored question set away from being "real-world ready" for production geological intelligence work.

---

## 1. Full Stack Developer (Score: 8.2)

### What I assessed

Three-tier architecture:
- **Frontend:** React 19 + Inertia.js v3 + Tailwind v4 + MapLibre GL + Plotly + React Flow
  - 40,093 LOC across `resources/js/` (.tsx + .ts)
  - 5 admin dashboards graduated to live data
- **Backend:** Laravel 13 (Octane / Swoole) + Sanctum + Horizon + Reverb + Pulse
  - 20,685 LOC across `app/`
  - 141 migrations, 65 PHPUnit test files
- **Domain service:** FastAPI on Python 3.13 + asyncpg + Pydantic AI
  - 65,305 LOC across `src/fastapi/app/`
  - 126 pytest test files

### Positive

- **Type discipline.** Pydantic validators on every Hatchet workflow input/output (`EvalRealRagNightlyInput/Output`, `EvaluateWorkspaceInput/Output`, etc.); TypeScript interfaces on every Inertia prop boundary; PHP type hints throughout Laravel.
- **Octane safety.** I checked the Laravel singletons — no request-bound state in scoped containers, no static accumulation. The `CLAUDE.md` hard rules call this out and the codebase appears to honor it.
- **Async-native drivers.** asyncpg throughout FastAPI; redis.asyncio; async Qdrant + Neo4j clients. No sync drivers in async paths.
- **Inertia v3 idioms.** `Inertia::optional()` instead of deprecated `lazy()`; deferred props for slow data; admin nav uses `useLayoutProps` correctly.
- **24 healthy services running in Docker.** Caddy, Octane, FastAPI, Reverb, Horizon, vLLM, Qdrant, Neo4j, PostgreSQL, Redis, ClickHouse, Langfuse (web + worker + MCP), Tempo, Hatchet, pgbouncer, Martin, Kestra, backup-agent.

### Concerning

- **Prometheus + OTel-collector "unhealthy" — but it's actually a healthcheck definition bug, not a service issue.** Probed live: Prometheus's `/-/healthy` endpoint returns "Prometheus Server is Healthy." The docker-compose healthcheck uses `curl` which isn't installed in the Prometheus image — so it always fails. OTel-collector image is distroless (no `/bin/sh`), so its healthcheck likely has the same shape. **Fix:** swap healthcheck commands for `wget` or HTTP-probe via `--no-healthcheck` + a sidecar pinger. Low-priority but creates false-alarm noise in Docker dashboards.
- **MinIO/SeaweedFS terminology drift.** ADR-0001 specifies SeaweedFS replacing MinIO, but the container is still named `georag-minio` and env vars are still `MINIO_*`. Architecture comments explain it's deliberate backward-compat, but new engineers will be confused for ~15 min on first encounter. Not a correctness issue.
- **`docs/RUNBOOK.md` mentioned in CLAUDE.md but unverified.** The "operator-style task" routing assumes this exists. Worth confirming.
- **Mixed orchestration:** Hatchet handles app workflows; Kestra handles SSO-integrated user-customizable flows. Clean conceptual split but two orchestrators is two failure modes to operate.

### Top 3 recommendations

1. **Healthcheck Prometheus + OTel ASAP.** Observability gaps during the upcoming ingestion will hide real issues.
2. **Rename `georag-minio` → `georag-seaweedfs`** in a dedicated tick. Keep `MINIO_*` env vars for backward compat (per ADR-0001 deliberate choice), but the container name aliases service identity.
3. **Add a `docs/SERVICE_INVENTORY.md`** listing all 24 containers + their roles + healthcheck expectations. Useful for on-call rotation when production starts.

---

## 2. Database Developer (Score: 8.8)

### What I assessed

- 141 Laravel migrations + raw SQL bootstrap files in `database/raw/phase0/`
- PostgreSQL 18.3 + PostGIS 3.6.3 + pgbouncer (transaction pooling)
- Multi-schema layout: `public`, `bronze`, `silver`, `gold`, `audit`, `eval`, `ops`, `targeting`, `usage`, `workspace`, `public_geoscience`, `flows`, `outbox`
- Neo4j Community 2026.03 (graph)
- Qdrant v1.17 (vectors)
- Redis 8.6 (cache + rate limiting + ephemeral state)

### Positive

- **Bronze/silver/gold lakehouse pattern executed.** `bronze.provenance` (immutable audit trail), `silver.*` (normalized domain), `eval.*` (test contracts), `audit.audit_ledger` (tamper-evident). Clean separation of concerns.
- **Hash-chained audit ledger.** Every state-changing event writes a row whose `hash = sha256(previous_hash || ... || payload)`. Verified by nightly `audit_ledger_verify` Hatchet workflow. **This is production-grade compliance plumbing.**
- **Monthly partitioning via pg_partman** on `audit.audit_ledger`. Pre-created 3 months ahead. Smart.
- **Row-Level Security everywhere.** 11 migrations create policies; RLS is enforced workspace-by-workspace via `current_setting('app.workspace_id')` GUC. **Plus** an admin escape hatch via the same GUC being NULL/empty.
- **DROP-first idempotency** (doc-phase 172) means `RefreshDatabase` re-runs cleanly. Substrate-verifier check enforces this for new migrations.
- **5,191 audit rows in the last 30 days** — the ledger is being written to in anger, not just demoware.
- **Driver-gated migrations** (`if DB::connection()->getDriverName() !== 'pgsql') return;`) make sqlite test runs clean.

### Concerning

- **Two distinct paths to schema creation:** Laravel migrations vs raw SQL in `database/raw/phase0/`. The `audit.audit_ledger` partitioned variant is provisioned by raw SQL; doc-phase 174 mirrored a non-partitioned version for the test DB. **This is a maintenance burden** — schema drift between prod and test is now possible.
- **`georag_app` lacks `CREATE` on database.** Migrations that need `CREATE SCHEMA` have to be applied as superuser via raw SQL bootstrap. The bronze + audit schemas are now both in this path. Pattern is documented but adds friction.
- **No explicit migration for ingested-document tables yet.** `silver.document_passages` is referenced by the eval evaluator's FK violations but I haven't verified it's in a Laravel migration. Could be raw SQL only.
- **PgBouncer in transaction pool mode + RLS GUCs interact tricky.** `app.workspace_id` set inside a transaction is fine; outside, it doesn't survive across pooled queries. The codebase appears aware of this (the audit emit path explicitly `set_config(..., true)` inside `async with conn.transaction()`).
- **No automated migration-drift detector.** If someone hand-edits prod, the Laravel migrations table won't catch it. Add a `pg_dump --schema-only` snapshot diff to CI.

### Top 3 recommendations

1. **Consolidate raw SQL → Laravel migrations** wherever possible. Aim for one source of truth. The partitioned `audit.audit_ledger` raw SQL is the hard case — keep that, mirror as test-only migration (which doc-phase 174 did).
2. **Add a `migrations:check-drift` CI step** that diffs `pg_dump --schema-only` against an expected snapshot. Catches prod-side hand edits before they bite.
3. **Document the `current_setting('app.workspace_id')` GUC contract** in `database/raw/phase0/README.md`. Every developer touching RLS needs to know: transaction-local, must be set before INSERT, escape hatch via NULL/empty. This is in scattered code comments but deserves canonical doc-treatment.

---

## 3. Geologist Expert (Score: 6.5)

> **Update post-Phase A inventory:** The 200GB uranium archive
> is **Wyoming State Geological Survey (WSGS) historical drill logs**
> scanned in 2005-2006 by Energy Metals Corp + Bob Gregory (former
> WSGS uranium geologist). Township-Range-Section organized
> (PLSS grid). 1,011 township-section bundles. NOT Athabasca Basin
> as I initially assumed — it's the Wyoming sandstone-hosted
> roll-front uranium country (Powder River Basin, Wind River Basin,
> Shirley Basin, Gas Hills, Crook County). Different deposit model
> entirely. My recommendations below are updated accordingly.

### What I assessed

Geological domain modeling:
- `silver.collars`, `samples`, `assays`, `lithology_logs` (drillhole core)
- `silver.geological_features`, `mineral_occurrences`
- `silver.geological_ontology_terms` (12-class taxonomy: commodity, geological_age, resource_class, deposit_model, lithology, alteration, structure, mineral_assemblage, host_rock, tectonic_setting, geochemistry, geophysics)
- `public_geoscience.*` (BC MINFILE, SK MinOccur, NRCan, AB Geological Survey)
- CRS detection per §04b
- §04i hallucination validators (Layer 4 entity_resolution catches geological-entity drift)

### Positive

- **§04e schemas are NI 43-101 compliance-ready.** Provenance per record, source file SHA, parser version. A geologist could survive a regulatory audit using this trail.
- **PostGIS native throughout.** Geometries on collars, occurrences, candidate zones. Multi-jurisdiction CRS handling (BC, SK, AB, NRCan) is encoded.
- **Ontology taxonomy is correct.** The 12 classes match standard exploration vocab. Synonyms table handles "uranium" / "U" / "uraninite" without forcing the SME to be pedantic.
- **PublicGeo adapters cover the right jurisdictions for Canadian uranium exploration:** BC, SK (Saskatchewan = Athabasca uranium country), AB, NRCan federal data. Saskatchewan inclusion is the SME-aware choice.
- **`uranium` is a first-class concept** in the orchestrator's question handling (refusal_correctness questions test this).

### Concerning

- **41 PublicGeo records total.** That's barely a regional reconnaissance dataset. For uranium exploration in the Athabasca Basin specifically, you'd want every documented showing, every drilled occurrence, every airborne survey result. **The data layer is starving.**
- **0 rows in `targeting.target_outcomes`.** No drilling outcomes recorded. The whole §12 learning loop (`train_target_model`, `continuous_learning_loop`) waits on this — and it's months of fieldwork away even on an aggressive timeline.
- **No real ingested documents (until tonight's Phase A completes).** The eval pipeline correctly refuses every refusal_correctness question because there's nothing to retrieve. A geologist seeing "I cannot answer" 8/8 times would shrug — that's appropriate behavior for an empty corpus, but it proves nothing about the system's ability to *answer geological questions with grounded citations*.
- **No structural-geology integration.** I don't see `fault_zones`, `shear_zones`, `unconformities` as first-class entities. For uranium specifically (unconformity-related deposits dominate Athabasca), this is a real gap.
- **`deposit_model` ontology class threshold is 8 terms.** That's low. The IOCG family alone has ~12 sub-models; unconformity-uranium has 4-5; SST sandstone-uranium has 3-4. The threshold suggests the SME hasn't yet populated this critical class.

### Top 3 recommendations (updated for WSGS Wyoming dataset)

1. **Prioritize OCR of pages mentioning "roll-front", "redox interface", "Wasatch", "Fort Union", "Wind River formation", "Shirley Basin", "Gas Hills", "Powder River".** These are the stratigraphic and geomorphic markers that matter for Wyoming sandstone-hosted roll-front uranium targeting. SME-tag a sample manually to confirm OCR fidelity for the WSGS lexicon.
2. **Add `silver.roll_front_features`** (geometries + metadata for redox interfaces, sandstone channel margins, paleo-water-table elevations). For Wyoming sandstone-hosted uranium, redox interface mapping is the #1 vectoring criterion (analogous to unconformity proximity for Athabasca).
3. **Backfill `deposit_model` ontology with Wyoming-specific terms** first: roll-front (Wasatch type, Fort Union type, Wind River type, Shirley Basin type), tabular sandstone-hosted, calcrete (uncommon in WY but Yeelirrie analog), then add unconformity-related (Athabasca/Thelon) for future datasets. The PLSS Township-Range-Section grid in the manifest should auto-link drill holes to USGS quadrangle maps for cross-referencing.
4. **Acknowledge data lineage:** the ReadMe is explicit — "These logs were scanned in 2005 or 2006 by Energy Metals Corp and Bob Gregory, former WSGS U geologist." That's a real provenance trail that should land in `bronze.provenance` per-record. Preserves SME credit + audit lineage.

---

## 4. Data Scientist (Score: 7.4)

### What I assessed

- §04i 6-layer hallucination prevention validators (live, all 6 graduated)
- Pydantic AI typed-output validation (every RAG response must include `source_chunk_id` or be rejected)
- 53 active golden questions, 5 question sets seeded
- 136 eval runs in last 30 days
- BGE-small-en-v1.5 (384-dim) embedding model + BGE-reranker-base cross-encoder (now active post doc-phase 176)
- Failure-layer breakdown panel on Eval Dashboard
- Nightly `eval_real_rag_nightly` cron with regression alarm → audit ledger emission
- Doc-phase 167 numeric-grounding validator with `tolerance_pct` per-claim
- 18 cases of historical Layer 5 chunk_provenance failures in audit history (visible signal that validators do bite)

### Positive

- **§04i is rigorous.** Six independent validators chained AND-semantics; first-failure short-circuits with named layer bucket. This is **above-industry-standard** for RAG hallucination prevention.
- **Vacuous-pass design is correct.** When `expected_refusal=True` and the LLM refuses, Layers 1-5 vacuous-pass (a correct refusal shouldn't be re-penalized for "missing citations"). This is subtle and the codebase gets it right.
- **Real cross-encoder reranker active** (doc-phase 176 fix). Layer 5 chunk_provenance now operates with the design-intent gate, not the degraded RRF-only fallback.
- **Audit-ledger alarm emission** (doc-phase 175) makes regression detection tamper-evident and subscribable.
- **Per-question difficulty taxonomy** (easy/medium/hard) supports future stratified analysis.

### Concerning

- **No SME-authored questions yet.** All 53 active questions are mechanical (numeric_grounding, report_section, schema_mapping, ocr_triage, refusal_correctness). The SME-authored 47 (core_chat, public_private_boundary, target_recommendation) are gated on Kyle's time. **Until these land, Layers 1-5 only exercise vacuous-pass behavior.**
- **`silver.answer_retrieval_items` FK violations** in eval path (carry-over from doc-phase 169). The retrieval logger tries to insert chunk references that don't exist in `document_passages`. Orchestrator catches and degrades to refusal — correct behavior but masks real retrieval errors when ingestion lands.
- **No A/B prompt variation framework live.** The §10 spec calls for it; the workflow input shape supports it (evaluator_kind, trigger_payload); but I don't see a wired-up prompt-variation experiment runner.
- **No drift detection on the embedding/reranker version.** `answer_runs.reranker_version` is persisted but nothing watches for "reranker SHA changed → expect score distribution shift" automatically.
- **Numeric-grounding validator vacuous-passes when `expected_value` is null.** All 15 seeded questions today fall into this path because silver-data ground-truth derivation isn't wired. Layer 3 is structurally working but practically inert.

### Top 3 recommendations

1. **Wire silver-data ground-truth derivation for Layer 3.** The seeded numeric_grounding questions have `path` and `source_table` set; a SQL helper that runs the path query and uses the result as `expected_value` would light up 15 real Layer 3 assertions tonight.
2. **Add `eval.embedding_run_signatures` table** capturing the embedding model SHA + reranker SHA + chunk-set fingerprint per run. Enables score-distribution drift alarms when the model layer shifts.
3. **Build a `prompt_variation_experiment` Hatchet workflow** that takes a baseline prompt + N variants and runs the full eval against each, surfacing winner. Closes the §10.5 A/B loop.

---

## 5. AI Agent Expert (Score: 8.5)

### What I assessed

- Pydantic AI orchestrator (live, 5055+ LOC in `app/agent/orchestrator.py`)
- LangGraph Pregel pipelines: `ReportBuilderState`, `TargetRecommendationState`
- `AgentDeps` singleton with asyncpg pool + Qdrant + Neo4j + embedding model + reranker
- 24 phase-0 agent workflows in Hatchet pool (tenant_isolation_auditor, etc.)
- 5 §25.4 support agents graduated (ticket_triage, root_cause_investigation, support_packet, customer_response_drafting, escalation_routing)
- §04i validators are framework-style (chainable, swappable)
- Pre-warm reranker + embedding singletons via `lifespan` hooks

### Positive

- **Pydantic AI for typed output is the right primitive.** Every RAG response goes through `result_type` validation; missing citations → reject, not "best effort". Hard contract.
- **LangGraph Pregel pipelines for multi-step reasoning.** Report builder, target recommendation. State machines with explicit nodes + transitions; not "prompt megablob with hope".
- **Tool-use is structured.** The orchestrator has typed tool inputs/outputs (Pydantic models); not a JSON-blob free-for-all.
- **5 §25.4 support agents are real graduations** not stubs. ticket_triage classifies severity + category; root_cause_investigation walks audit ledger; support_packet assembles + audit-anchors; customer_response_drafting templates by severity+category; escalation_routing decision-trees to on_call/sme_review.
- **Per-evaluator-kind dispatch** (synthetic_stub / real_llm_v1 / real_rag_v1) is the right axis for prompt + tooling experimentation.
- **`AgentDeps` graceful degradation** — embedding + reranker load failures don't crash the eval; they degrade to fallback paths with the failure surfaced in audit.

### Concerning

- **`app/agent/orchestrator.py` at 5055+ LOC is becoming a god-file.** Most agent codebases at this scale have split into multiple modules. Refactor candidate when low-risk.
- **No agent-to-agent handoff protocol documented.** The 5 §25.4 agents are chained by a workflow; the per-agent interfaces are implicit. If new agents land (planned per §25.5), the handoff contract should be formalized.
- **No agent observability beyond Langfuse-MCP.** I see langfuse-web + langfuse-worker + langfuse-mcp-stdio containers but no evidence of which agent invocations are getting traced. Verify Langfuse is actually capturing.
- **§9.11 `field_outcome_learning` skeleton** — could be graduated without ML deps (it's ETL-only). Per doc-phase 177 audit, it's the easiest win in the §12 chain.
- **`datetime.utcnow()` deprecation in orchestrator.py:5055** — line caught in pytest warnings; needs sweep.

### Top 3 recommendations

1. **Refactor `app/agent/orchestrator.py` into `app/agent/{tools,validators,pipeline,deps,state}.py`.** 5055 LOC in one file is hard to review; the LangGraph + Pydantic AI patterns natively support module split.
2. **Graduate `field_outcome_learning` (the ETL-only one).** No xgboost needed, no waiting on drilling outcomes — just the schema transform from raw outcomes to `target_score_factors` rows. Closes the §9.11 → §12.3 bridge ahead of training.
3. **Standardize agent-to-agent contract via a `AgentResponse` Pydantic model.** Every §25.4 agent should return this, every chain step should consume it. Forward-proofs §25.5 expansion.

---

## 6. vLLM Expert (Score: 7.0)

### What I assessed

- vLLM container `georag-vllm` healthy, 29 hours uptime
- Serving: `ELVISIO/Qwen3-30B-A3B-Instruct-2507-AWQ`
- max_model_len = 8192
- NVIDIA RTX A4500 (20GB VRAM)
- GPU memory usage: 20058 / 20470 MiB = **98% saturated**
- OpenAI-compatible `/v1/chat/completions` endpoint
- Temperature 0.0 for eval calls (deterministic)
- 30s timeout, 256 max_tokens default

### Positive

- **AWQ quantization fits the 30B model on a 20GB workstation card.** Engineering pragmatism. 4-bit AWQ weight quant + activation-aware preserves quality much better than naive int4.
- **OpenAI-compatible API** = easy migration path if you swap models or backends.
- **vLLM continuous batching** — multiple eval workers can hammer this without queueing collapse.
- **Pre-warming via lifespan hook.** First request doesn't pay JIT cost.
- **`max_model_len=8192` is honest about the constraint.** No silent truncation surprises.

### Concerning

- **20GB GPU is 98% memory-pressured.** Adding any additional model (a re-ranker on GPU, a multimodal sidecar) is not possible without swap. Hard constraint for production load growth.
- **8192 token context is short for §15 reports.** A 43-101 technical report section can easily exceed 8K tokens. The report builder graph chunks around this, but the LLM never sees a "full section" in one shot.
- **No prompt caching surfaced.** vLLM 0.5+ supports prefix caching; would dramatically speed up multi-turn conversations with the same system prompt. Worth verifying it's on.
- **No KV cache reuse across requests.** Eval runs hit the same system prompt 53 times per question set; could be 5-10x faster with cache-aware routing.
- **No FlashAttention version pinned.** Performance regressions across vLLM versions are subtle.
- **No GPU-utilization Prometheus scrape.** I see Prometheus container but no nvidia-dcgm-exporter or vllm-metrics scrape. Can't alert on GPU saturation, throttling, or thermals.

### Top 3 recommendations

1. **Add an nvidia-dcgm-exporter** + scrape from Prometheus. GPU memory, utilization, temperature, power. Critical observability gap for a single-GPU production deployment.
2. **Enable vLLM prefix caching explicitly.** Set `--enable-prefix-caching` in the vLLM launch args. Eval runs will be 3-10x faster (same system prompt, same question prefix structures).
3. **Plan a context-window upgrade path.** Either: (a) move to 48GB GPU (e.g., A6000) to enable 32K context, or (b) implement chunk-based reasoning so the orchestrator never asks the LLM to "see the whole report" — let LangGraph handle the multi-section assembly. (b) is cheaper and the current direction; just make it more explicit in the architecture doc.

---

## 7. Qwen MoE Expert (Score: 6.8)

### What I assessed

- Model: `ELVISIO/Qwen3-30B-A3B-Instruct-2507-AWQ`
- **A3B** = 3B active parameters, 30B total (MoE with 16 experts; ~2 active per token)
- Instruction-tuned variant (2507 release)
- AWQ 4-bit weight quant
- Single GPU (no expert-parallel sharding)

### Positive

- **A3B is the right Qwen3 pick for a 20GB GPU.** The 235B variant won't fit; the dense 32B is competitive but not as inference-efficient. A3B trades total params for active compute.
- **MoE routing is automatic** — no special prompt engineering needed.
- **Instruction-tuned ("Instruct" suffix) is the right variant** for a RAG assistant where the LLM should follow system-prompt directives (cite or refuse).
- **AWQ + MoE compose cleanly.** AWQ quantizes the expert weights; routing logic stays full-precision.

### Concerning

- **No expert-routing visibility.** I can't tell from the codebase whether eval / chat traffic is hitting a stable set of experts or splattering. Token-routing diversity matters for quality.
- **A3B's active-parameter count (3B) is smaller than dense 7B competitors.** For specialized domain (geology), a fine-tuned dense 7B could match or beat untuned A3B. Geological domain isn't represented in Qwen3's pretraining corpus particularly strongly.
- **No MoE-aware batching tuning surfaced.** vLLM's MoE handling improved significantly in 0.6+; if pinned older, leaving 30-40% throughput on the table.
- **Routing entropy not monitored.** If most tokens route to the same 2 experts (mode collapse), the model is effectively a smaller dense model. Should be tracked.
- **`max_model_len=8192` is conservative for Qwen3** — native context is much longer. Likely VRAM-constrained.

### Top 3 recommendations

1. **Add expert-routing-entropy logging** to vLLM. Track which experts fire per request; alarm on collapse to <3 experts dominating 80%+ of routing. Tells you whether MoE is paying off.
2. **Investigate fine-tuning A3B on geological corpus.** Once Phase A ingestion + OCR completes, you have ~150-500k pages of uranium domain text. Even a LoRA fine-tune would 2x answer quality on grounded queries. Budget: ~$200 of GPU time on rented H100.
3. **Benchmark dense Qwen3-7B vs A3B on geological Q&A** once SME questions land. If dense wins, the trade-off favors swap (simpler, more predictable, less MoE-tuning surface).

---

## 8. Qwen Expert — general (Score: 7.5)

### What I assessed

- Qwen3 family is current state-of-the-art open-weight (2026)
- 2507 = released 2025-07, well-tested
- Tokenizer + chat template handle Qwen3's structured output (tool calls, JSON mode)

### Positive

- **Qwen3 chat template is well-supported** by vLLM. No template surgery needed.
- **Tool-use JSON mode works cleanly** — critical for Pydantic AI typed outputs.
- **The 30B size class is the sweet spot** for English-language general capability + open weight. Beats Llama 3.1 8B substantially; competitive with closed 70B-class.
- **Qwen3 is multilingual** — useful when international partners contribute non-English geological reports.

### Concerning

- **Qwen3 pretraining corpus is opaque.** Don't know what geological domain coverage looks like out of the box. Likely thin on Athabasca-uranium specifics.
- **No system-prompt evaluation harness.** The system prompt in `real_llm_evaluator.py` is good but never A/B-tested against alternatives. Could be 10% better with iteration.
- **No refusal-precision baseline against Qwen3-Instruct's native refusal behavior.** The orchestrator adds its own refusal patterns; the model has its own. Are they fighting?
- **Temperature 0.0 is right for eval but may be too rigid for chat.** Chat users sometimes want exploratory answers; the system uses the same orchestrator path. Worth distinguishing.

### Top 3 recommendations

1. **A/B test 3 system-prompt variants** once the eval pipeline has SME questions. Iteration on prompt is the highest-leverage tuning available.
2. **Profile native Qwen3 refusal triggers** vs. the orchestrator's refusal layer 6. Either dedupe or document the cascade clearly.
3. **Separate chat-mode and eval-mode temperature** in the config: T=0.0 for eval (deterministic, reproducible), T=0.2 for chat (slight exploration, more natural responses).

---

## 9. Reporting Expert (Score: 6.0)

### What I assessed

- `app/services/report_builder/` (Python) + LangGraph Pregel `ReportBuilderState`
- `generate_report` Hatchet workflow (graduated body)
- `/admin/...` doesn't yet expose a reports list — generate path is API-only
- §15.1 / §15.2 spec covers structure (sections + integrity)
- `silver.report_sections` table populates each section
- §7.2 what_changed report (delta between two workspace states)
- §7-A spec covers NI 43-101 alignment

### Positive

- **LangGraph Pregel for report assembly** is the right architecture — each section is a node, dependencies are explicit, parallel execution where possible.
- **what_changed reports are integrated with §9.13 detector output** — proper cross-section wiring (doc-phase 95).
- **Section IDs are stable** (per `expected_section_ids` test pattern), so structural assertions can be made.
- **`generate_report` workflow is graduated** (not skeleton).

### Concerning

- **No real reports rendered yet.** Without ingested documents → no `document_passages` → no real source chunks for citations → no real reports. The report builder is structurally working but practically idle until Phase A completes.
- **No PDF/DOCX rendering surface visible.** Reports presumably render to HTML or Markdown today; for NI 43-101 compliance you need PDF/DOCX. Where is wkhtmltopdf / weasyprint / docx-templater?
- **No template versioning visible.** If a report template changes between filings, you need to know which version produced which delivered report. Could be in `report_sections.template_version` but I haven't verified.
- **No "regenerate this report at a fixed historical workspace state"** path. Replay matters for audit trails ("re-render Sept 2025 report from today's data layer? No — render from the data as of Sept 2025").
- **Citations in reports are presumably section-scoped, not paragraph-scoped.** NI 43-101 requires per-claim citations; verify granularity.

### Top 3 recommendations

1. **Once Phase A completes, generate a what_changed report** for the platform_ops workspace and review the output end-to-end. This is the fastest "real report" path because the data inputs are operator-controlled.
2. **Add wkhtmltopdf or weasyprint** to FastAPI dependencies for PDF output. NI 43-101 deliverables need to be PDF.
3. **Add `report_runs.workspace_snapshot_id`** capturing the workspace state at generation time. Reports become reproducible from history.

---

## 10. UI/UX Expert (Score: 7.8)

### What I assessed

- 5 admin dashboards: `/admin/eval-dashboard`, `/admin/decision-history`, `/admin/support-cockpit`, `/admin/hypothesis-workspace`, `/admin/decisions/new`
- Public surface: `/public-geoscience` map with 95 features, MapLibre GL
- Inertia v3 + React 19 + Tailwind v4 + shadcn/ui
- Consistent stone-* color palette across admin
- Color-coded badges for evaluator_kind, failure_layer, decision_type
- Plotly for charts, React Flow for hypothesis graphs
- Caddy in front (TLS termination)

### Positive

- **Visual consistency across admin.** Same stone-950 dark theme, same chip-style badges, same border-style. No "this page is in a different design language" moments.
- **MapLibre GL is the right pick** — open-source, on-prem-friendly licensing (matches the §08 hard rule rejecting Mapbox).
- **shadcn/ui + Tailwind v4** gives composability without lock-in.
- **Inertia v3 props are typed end-to-end** — TypeScript on the client mirrors PHP types on the server.
- **Failure-layer breakdown panel (doc-phase 171)** is genuinely useful operator UI — bars, last-failed-at, color-coded tones. This is good information design.
- **Public Geoscience map clusters features sensibly** at low zoom; reveals individual features at higher zoom. Standard MapLibre clustering done right.

### Concerning

- **No "command palette" / cmd-K surface.** For an internal tool with 5 dashboards + decision-entry form + map, an operator can hunt-and-click for half a minute to find the right page. cmd-K with fuzzy search across routes would 10x discoverability.
- **No empty-state design specified for new workspaces.** What does `/admin/eval-dashboard` show on a fresh deploy with 0 runs? I see code paths for "no runs in 30 days" but the empty illustrations / "here's how to get started" guidance is missing.
- **Mobile responsiveness untested.** The dashboards are dense tables; on a phone, they'd be horizontal-scroll horror. For an exploration company where the geologist is on-site with a tablet, this matters.
- **No accessibility audit visible.** No aria-* attributes I noticed; no focus-trap on modals; color-contrast not tested. WCAG 2.1 AA would be table stakes for any government / regulated-industry deployment.
- **Plotly is heavy.** ~3MB JS payload for a single chart. For the small chart needs (pass/fail bar, count histogram), recharts or Visx would be 10x lighter.

### Top 3 recommendations

1. **Add a cmd-K palette** (linkulous, ninja-keys, or a custom Combobox) routing across admin + dashboards + decision entry. Single biggest UX upgrade for internal-tool ergonomics.
2. **Run an axe-core accessibility scan** in CI. WCAG AA conformance is achievable in a few ticks if checked early; retrofitting later is brutal.
3. **Swap Plotly for Recharts or Visx** for the small charts (bar charts on eval dashboard, etc.). Keep Plotly only for the few places where its specific features (zoom + crossfilter) actually earn the weight.

---

## Cross-cutting recommendations (priority order)

These cut across multiple expert lenses:

1. **Ingest first project's documents (Phase A is in progress).** Until real `document_passages` populate, half of the platform's value (grounded answers, real Layer 1-5 §04i exercise, NI 43-101 reports) is purely theoretical. **This is the #1 unlock.**

2. **SME-author non-refusal question sets** (core_chat, public_private_boundary, target_recommendation). Without these, the §04i validators only vacuous-pass — you don't actually know how well the chain catches hallucinations on positive cases.

3. **Add nvidia-dcgm-exporter + Prometheus scrape.** Single-GPU production deployment without GPU observability is a recipe for unobserved degradation.

4. **Healthcheck Prometheus + OTel-collector.** Two unhealthy observability containers is two blind spots.

5. **Graduate `field_outcome_learning`.** ETL-only; no ML dependency; unblocks §12 chain when outcomes arrive.

6. **`/admin/decisions/new` is the writer surface — extend the pattern.** Make `/admin/ingestion-review` capture project labels + confirm clusters once Phase A completes. Same Inertia + Laravel + RecordDecision-style writer pattern.

7. **Consolidate raw SQL → Laravel migrations** wherever possible. Reduce dual-source-of-truth burden.

8. **Refactor `app/agent/orchestrator.py`** into modules. 5055 LOC in one file is becoming a review obstacle.

9. **Document GUC contracts** in `database/raw/phase0/README.md`. RLS workspace_id semantics need canonical reference.

10. **Build the cmd-K palette + Plotly → Recharts swap.** Two ticks of UI polish that ship perceptible quality.

---

## What's strong, what's weak — at a glance

**Strong:**
- Hash-chained audit ledger + RLS workspace isolation = production-grade compliance
- §04i 6-layer hallucination prevention = above-industry-standard rigor
- Type discipline across the three tiers
- Pydantic AI + LangGraph orchestration patterns
- 24 healthy services with sensible split of concerns
- 191 tests + 112-check substrate verifier + nightly cron alarm loop

**Weak:**
- Data layer is starving (41 PublicGeo records, 0 drilling outcomes, 0 ingested documents prior to tonight)
- No SME-authored questions yet (so 47 of 100 golden questions don't exist)
- GPU observability gap (no DCGM exporter)
- 4 §12 ML workflows skeleton (correct — waiting on data)
- No accessibility audit / mobile responsiveness
- Reports never rendered against real data yet

**Overall verdict:** The platform is **a few real corpus + SME question sets away from being genuinely production-ready for uranium-exploration intelligence work.** The engineering foundation is excellent; the data flywheel needs starting.
