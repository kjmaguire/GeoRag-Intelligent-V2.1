# Retrieval trace logging — design

**Status:** Draft. Schema migration drafted (not applied); write-path not implemented.
**Authored:** 2026-05-26 (overnight autonomous run)
**Plan reference:** §0e (mandatory instrumentation before all other tickets), §5c (dashboard SLA targets that consume this), §0b (system prompt budget that this surfaces)

---

## 1. The plan §0e trace object

Plan §0e specifies a verbatim 30+ field JSON shape per query — see `georag-complete-implementation-plan (2).md` lines 174-224. The shape covers identification, context budgeting, routing, raw results per source, reranker scores, evidence types, guard results, repair attempts, death-loop detection, and per-stage latency.

## 2. Why a new table instead of extending `silver.answer_runs`

`silver.answer_runs` (migration `2026_04_21_100000_create_answer_runs.php`) already captures answer-side audit fields:
- query_text, query_class (≈ router_decision)
- embedding_model, sparse_model, fusion_method, reranker_version
- workspace_data_version, project_data_version (cache invalidation keys)
- backend_used, model_name, input/output/cache tokens
- citation_lifecycle_state, citation_mode
- trace_id, root_span_id (OTel link)
- created_at, updated_at

Plan §0e's trace object adds *retrieval-pipeline* fields that `answer_runs` does NOT carry:
- normalized_query, conversation_turn
- system_prompt_tokens, remaining_context_budget
- router_confidence
- tool_plan, tool_calls (detail)
- generated_filters
- vocab_terms_matched, entities_resolved
- raw_results_per_source counts (qdrant_dense, qdrant_sparse, postgis, neo4j)
- candidate_count_pre_rerank, reranker_scores, dropped_candidates_with_score
- selected_context_groups, evidence_types_in_context
- guard_results, guard_failure_codes
- repair_attempts, repair_strategies_used, death_loop_triggered
- per-stage latency_ms breakdown
- cache_hit, cache_type

Extending `answer_runs` with these fields would (a) inflate a hot table touched on every answer, (b) couple retrieval-pipeline schema evolution to the answer-audit schema. So: new `silver.query_traces` table, 1:1 by `answer_run_id`, indexed for dashboard reads.

## 3. Storage shape

- **Verbatim plan-§0e JSON** → `trace_payload` JSONB column.
- **Denormalised columns** for the fields plan §5c dashboards filter on (latency, guard pass, death loop, per-source candidate counts, cache hit). These are computed on insert from the JSONB payload — see §5 below.
- **RLS:** workspace-scoped, same canonical pattern as 2026_05_25 RLS migrations.
- **GIN index** on `trace_payload jsonb_path_ops` for ad-hoc queries on the non-denormalised fields (e.g. `WHERE trace_payload->'vocab_terms_matched' ? 'argillic_alteration'`).

## 4. Write path

Not implemented in this overnight run. Suggested integration:

1. **`app/services/trace_writer.py`** (new) — Pydantic model `RetrievalTrace` with the plan §0e shape. `async def write_trace(trace: RetrievalTrace, db: asyncpg.Pool) -> None` issues a single INSERT, computing the denormalised columns from the payload fields.
2. **Hatchet workflow** for batched writes — traces aren't latency-critical; buffer them and flush every 5 s or 50 traces to avoid hot-path INSERT contention with `answer_runs`. Same pattern as `app/hatchet_workflows/reliability_metrics_publisher.py` (already in WIP on main).
3. **Trace assembly** — done by the agentic_retrieval graph. Each node already logs structured info; the persist_node (graph.py:50) is the natural place to assemble the final trace dict and enqueue the write. State extension: add `trace_payload_builder: dict` to `AgenticRetrievalState`; each node appends its own fields.
4. **Sampling** — plan §0e says "every query." For early operation that is fine; once volume grows, add a Redis-side sample-rate gate to drop low-value traces (e.g. cache hits don't need a trace beyond `(query_id, cache_hit=true)`).

## 5. Denormalisation contract

On insert, `trace_writer.py` is responsible for:

| Column | Source in trace_payload |
|---|---|
| `query_id` | `query_id` |
| `query_text` | `user_query` |
| `normalized_query` | `normalized_query` |
| `conversation_turn` | `conversation_turn` |
| `system_prompt_tokens` | `system_prompt_tokens` |
| `remaining_context_budget` | `remaining_context_budget` |
| `final_token_count` | `final_token_count` |
| `router_decision` | `router_decision` |
| `router_confidence` | `router_confidence` |
| `effective_intent` | `tool_plan` (parsed first segment) |
| `qdrant_dense_count` | `raw_results_per_source.qdrant_dense` |
| `qdrant_sparse_count` | `raw_results_per_source.qdrant_sparse` |
| `postgis_count` | `raw_results_per_source.postgis` |
| `neo4j_count` | `raw_results_per_source.neo4j` |
| `candidate_count_pre_rerank` | `candidate_count_pre_rerank` |
| `selected_context_groups` | `selected_context_groups` |
| `guard_pass` | `all(guard_results.values())` |
| `guard_failure_codes` | `guard_failure_codes` |
| `repair_attempts` | `repair_attempts` |
| `death_loop_triggered` | `death_loop_triggered` |
| `cache_hit` | `cache_hit` (default false) |
| `cache_type` | `cache_type` |
| `latency_total_ms` | `latency_ms.total` |
| `latency_routing_ms` | `latency_ms.routing` |
| `latency_retrieval_ms` | `latency_ms.retrieval_fan_out` |
| `latency_reranking_ms` | `latency_ms.reranking` |
| `latency_generation_ms` | `latency_ms.generation` |
| `latency_guards_ms` | `latency_ms.guard_evaluation` |

If any denormalised column is NULL because the trace_payload doesn't contain it (early integration, missing instrumentation), the INSERT does not fail — that's the explicit signal "this node didn't run yet."

## 6. Dashboard queries this enables (plan §5c)

```sql
-- p95 end-to-end latency, rolling 24h
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_total_ms)
FROM silver.query_traces
WHERE created_at > now() - interval '24 hours';

-- Guard pass rate, rolling 24h
SELECT
  100.0 * count(*) FILTER (WHERE guard_pass = true) / count(*) AS guard_pass_rate
FROM silver.query_traces
WHERE created_at > now() - interval '24 hours' AND guard_pass IS NOT NULL;

-- Death loop trigger rate, rolling 30 minutes
SELECT
  100.0 * count(*) FILTER (WHERE death_loop_triggered = true) / count(*) AS death_loop_rate
FROM silver.query_traces
WHERE created_at > now() - interval '30 minutes';

-- Cache hit rate by type
SELECT cache_type, count(*) FROM silver.query_traces
WHERE created_at > now() - interval '24 hours'
GROUP BY cache_type;

-- Slowest queries needing investigation
SELECT trace_id, query_text, latency_total_ms, trace_payload->'tool_calls'
FROM silver.query_traces
WHERE latency_total_ms > 8000
ORDER BY latency_total_ms DESC
LIMIT 50;
```

## 7. Acceptance criteria (plan §0e, applied to this schema)

- [x] Schema designed to capture every query's trace object — DONE in migration
- [ ] Trace object queryable (write to PostgreSQL `query_traces` table) — schema READY; write path NOT IMPLEMENTED
- [ ] Grafana or equivalent reads from `query_traces` for latency and guard pass rate dashboards — query examples in §6; Grafana panel JSON to be added under `ops/grafana/`

## 8. Decisions captured — 2026-05-27 morning

Kyle reviewed and accepted all four recommendations:

| Q | Decision | Implication |
|---|---|---|
| Q1 | **Buffered writes** via Hatchet (accept ≤5 s loss on worker crash) | `trace_writer.py` enqueues; flush every 5 s or 50 traces. Same pattern as `reliability_metrics_publisher.py`. |
| Q2 | **Log everything** for now; revisit sampling at >100 QPS sustained | No sample-rate gate at write time. Cache-hit queries get full traces. |
| Q3 | **90 days online + cold-tier archive** | Cron job (Hatchet) moves rows > 90 days to cold tier via the existing `cold_tier_archive.py` pattern. |
| Q4 | **Denormalise `trace_id`** onto `silver.query_traces` | Add `otel_trace_id VARCHAR(64) NULL` column when migration applies; populated from `answer_runs.trace_id` on insert. Saves dashboard joins. |

### Schema follow-up

The migration drafted overnight (`2026_05_26_220000_create_silver_query_traces.php`) does not yet have the `otel_trace_id` column from Q4. Add via a follow-up migration when wiring `trace_writer.py`, OR amend the original migration before applying it. Suggested: amend before apply since nothing has run it yet.
