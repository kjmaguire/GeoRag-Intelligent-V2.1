"""Prometheus metrics for the GeoRAG signal-harvesting dashboard.

Central registry of the structured signals the orchestrator/escalation
code emits. Each metric is a Counter/Histogram defined once here and
incremented from the relevant call sites — keeps the metric schema
in one place so the Grafana dashboard JSON matches what the code emits.

Design notes
------------
- Uses prometheus_client's default registry. The /metrics endpoint wired
  in main.py serves that registry via `prometheus_fastapi_instrumentator`
  or a direct `generate_latest` call; both paths end up on the same
  registry.
- Counters use `.inc()` only — Prometheus histograms handle distribution
  shape; counters handle event rates.
- Histograms for latency use exponential buckets roughly tuned for the
  expected p50/p95: ~100ms fast path, ~3s normal path, ~10s deep-reasoning.
- Labels are kept low-cardinality — no user_id, no query_id, no project_id.
  (Project-scoped debugging goes through structured logs + Loki, not metrics.)
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# B1 model routing — tier + model choices per query.
# ---------------------------------------------------------------------------

QUERIES_TOTAL = Counter(
    "georag_queries_total",
    "Total RAG queries processed by the orchestrator, labelled by tier+outcome.",
    labelnames=("tier", "outcome", "backend"),
)

ROUTING_DECISIONS = Counter(
    "georag_routing_decisions_total",
    "Counts of classifier-driven routing decisions per tier + reason.",
    labelnames=("tier", "reason"),
)

# ---------------------------------------------------------------------------
# B1 failover — Anthropic → downshift OR cross-backend (vLLM).
# ---------------------------------------------------------------------------

FAILOVERS = Counter(
    "georag_llm_failovers_total",
    "LLM failover events, e.g. Opus 429 → Sonnet downshift or cross-backend.",
    labelnames=("from_tier", "to", "exception_class"),
)

# ---------------------------------------------------------------------------
# C5/C6 + R15 prompt caching (Anthropic ephemeral cache AND vLLM prefix cache).
# ---------------------------------------------------------------------------

PROMPT_CACHE_TOKENS = Counter(
    "georag_prompt_cache_input_tokens_total",
    "Total input tokens the backend reported as cache hits.",
    labelnames=("backend",),
)

PROMPT_TOTAL_TOKENS = Counter(
    "georag_prompt_input_tokens_total",
    "Total input tokens (cached + uncached) sent to the backend.",
    labelnames=("backend",),
)

# ---------------------------------------------------------------------------
# R9 classifier escalation — the signal the whole dashboard was built for.
# ---------------------------------------------------------------------------

ESCALATION_TRIGGERED = Counter(
    "georag_escalation_triggered_total",
    "Queries where classifier_fallback + all_tools_empty fired.",
    labelnames=("reason",),
)

ESCALATION_REPHRASED = Counter(
    "georag_escalation_rephrasings_total",
    "LLM-generated rephrasings produced for a single query.",
)

ESCALATION_OUTCOME = Counter(
    "georag_escalation_outcome_total",
    "Did the rephrasing retry rescue the query? success = non-empty chunks.",
    labelnames=("outcome",),   # "success" | "empty" | "error"
)

# ---------------------------------------------------------------------------
# B4/B5 retrieval quality — chunks returned per query after gating.
# ---------------------------------------------------------------------------

CHUNKS_RETURNED = Histogram(
    "georag_retrieval_chunks_returned",
    "Distribution of chunks returned to the LLM after all retrieval gates.",
    buckets=(0, 1, 2, 3, 5, 8, 12, 20, 50),
)

# ---------------------------------------------------------------------------
# Context-budget pressure signal (P1 #16).
# Counts every time the orchestrator's context-packing path hits the
# MAX_CONTEXT_TOKENS ceiling and truncates. A sustained rate > 0 is a
# quality regression — likely dropping MMR-selected chunks that would
# have improved the answer.
# ---------------------------------------------------------------------------

CONTEXT_TRUNCATIONS = Counter(
    "georag_context_truncations_total",
    "Count of context-budget truncation events, labelled by backend.",
    labelnames=("backend",),
)

# ---------------------------------------------------------------------------
# Cost accountability — USD per query, per model + user bucket.
# Closes the C+ gap from the post-Phase-4 score card.
# ---------------------------------------------------------------------------

LLM_COST_USD = Counter(
    "georag_llm_cost_usd_total",
    "Total estimated USD spent on LLM calls, by model + low-cardinality user bucket.",
    labelnames=("model", "user_bucket"),
)

LLM_TOKENS_OUTPUT = Counter(
    "georag_llm_output_tokens_total",
    "Total output tokens generated by the LLM, by model.",
    labelnames=("model",),
)

# ---------------------------------------------------------------------------
# Overall query latency.
# ---------------------------------------------------------------------------

QUERY_DURATION = Histogram(
    "georag_query_duration_seconds",
    "Wall-clock seconds from orchestrator entry to response assembly.",
    labelnames=("outcome",),   # "completed" | "timeout" | "error"
    buckets=(0.1, 0.3, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 60.0),
)

# ---------------------------------------------------------------------------
# Pool saturation (P1 #36b — DB review follow-on).
# Sampled once per RAG run from `pg_pool.get_size()` / `pg_pool.get_max_size()`.
# When this gauge sits near 1.0 you are queueing on connection acquire and the
# asyncpg max_size needs another bump (or PgBouncer default_pool_size needs
# raising, depending on which layer is the actual ceiling).
# Gauges are scalar — we don't want labels here because there's only one
# pool. Adding a backend label later is safe (additive) if we run a second
# pool against an analytics DB.
# ---------------------------------------------------------------------------

PG_POOL_SATURATION = Gauge(
    "georag_pg_pool_saturation",
    "Fraction of asyncpg pool slots currently in use (0.0–1.0). "
    "Sampled at the end of every RAG run.",
)

PG_POOL_SIZE = Gauge(
    "georag_pg_pool_size_total",
    "Configured asyncpg pool max_size — for sanity-checking config bumps.",
)

# ---------------------------------------------------------------------------
# Partial-result rescue (P1 #7).
# Counts every parallel-fan-out tool branch that raised. Rate > 0 means
# we're salvaging peer branches from a failure — the request still
# completes with whatever succeeded, but the failing tool may need a
# bug fix or a connection-pool/timeout bump.
# ---------------------------------------------------------------------------

PARTIAL_TOOL_FAILURES = Counter(
    "georag_partial_tool_failures_total",
    "Tool calls that raised inside the parallel gather, by tool + exception class.",
    labelnames=("tool", "exception_class"),
)

# ---------------------------------------------------------------------------
# Global LLM-call cap (P1 #14).
# A single user query can rack up multiple LLM calls (classifier escalation,
# rephrasings, primary synthesis, retry-on-validation-fail, failover).
# When the per-run counter hits MAX_LLM_CALLS_PER_QUERY we abort further
# calls and surface a graceful error. This counter records every cap hit;
# a sustained > 0 rate means MAX_LLM_CALLS_PER_QUERY is too low OR a
# specific code path is looping unintentionally.
# ---------------------------------------------------------------------------

LLM_CALL_BUDGET_EXCEEDED = Counter(
    "georag_llm_call_budget_exceeded_total",
    "Queries that hit MAX_LLM_CALLS_PER_QUERY and were aborted.",
)

# Z.1 / Appendix C §5 — external-LLM egress gate firings. A non-zero rate
# means workspaces are configured for LLM_BACKEND=anthropic but their
# profile policy refuses egress; either the operator forgot to enable
# allow_external_llm or a misconfiguration is routing protected workspaces
# to the external backend. The `reason` label distinguishes the cases.
EXTERNAL_LLM_EGRESS_BLOCKED = Counter(
    "georag_external_llm_egress_blocked_total",
    "External-LLM calls refused by the workspace profile gate (Appendix C §5).",
    labelnames=("reason",),
)

LLM_CALLS_PER_QUERY = Histogram(
    "georag_llm_calls_per_query",
    "Distribution of total LLM calls made per RAG run (excludes cache hits).",
    buckets=(1, 2, 3, 4, 5, 7, 10, 15, 25),
)

# ---------------------------------------------------------------------------
# Per-tool latency (P1 #16).
# Distribution of seconds spent per agent tool call. Labelled by tool +
# outcome so we can grep for the slow paths AND the failure paths
# independently (a tool that completes in 50 ms but errors 80 % of the
# time is a different problem from one that completes in 5 s).
# ---------------------------------------------------------------------------

TOOL_DURATION = Histogram(
    "georag_tool_duration_seconds",
    "Wall-clock seconds per agent tool call, by tool + outcome.",
    labelnames=("tool", "outcome"),  # outcome: "ok" | "timeout" | "error"
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0),
)

TOOL_RESULT_COUNT = Histogram(
    "georag_tool_result_count",
    "Distribution of result counts returned by each tool (post-quality-gate).",
    labelnames=("tool",),
    buckets=(0, 1, 2, 5, 10, 20, 50, 100, 500, 5000),
)

# ---------------------------------------------------------------------------
# First-token latency (P1 #16).
# Time from POST /internal/queries to the first real `delta` SSE event
# (not the keepalive, not the status frames — the first LLM-emitted token).
# This is the user-perceived "is something happening" moment and the most
# important number for chat UX. Labelled by backend so vLLM vs Anthropic
# latency is comparable in one chart.
# ---------------------------------------------------------------------------

FIRST_TOKEN_LATENCY = Histogram(
    "georag_first_token_latency_seconds",
    "Seconds from request open to first streamed LLM token.",
    labelnames=("backend",),
    buckets=(0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 15.0),
)

# ---------------------------------------------------------------------------
# Out-of-scope refusals (P1 #15).
# Counts queries routed to the FAST refusal path because the LLM classifier
# returned all-False (genuinely off-topic). Sustained > 0 rate is a signal
# that either users are asking the wrong things in the chat box (UX issue
# — surface a "what can I ask?" hint) OR the keyword classifier is
# escalating to LLM too aggressively for queries that ARE in-scope (a
# routing-quality regression).
# ---------------------------------------------------------------------------

OUT_OF_SCOPE_REFUSALS = Counter(
    "georag_out_of_scope_refusals_total",
    "Queries the LLM classifier flagged all-False and we refused immediately.",
)

# ---------------------------------------------------------------------------
# Master-plan §3 Step 7c §04p dual-write outcomes (doc-phase 65).
#
# The Hatchet ingest_pdf.persist step runs the §04p stack alongside the
# v1.49 path as dual-write. Failures were silent during the doc-phase 59
# minio CRLF outage — every ingest had p04p_telemetry.ok=False with only
# a log.warning to surface it. These counters give Prometheus +
# Alertmanager visibility.
#
# Labels kept low-cardinality:
#   - error_kind: "exception" | "preflight_invalid" | "persist_failed" | "other"
#     (NOT the actual error string — that's high-cardinality)
#
# Workspace_id is NOT a label per the metrics.py module convention; an
# operator chasing a specific workspace's failures reads
# silver.parser_run_artifacts.errors JSONB (which has the workspace_id)
# or the structured log line in Loki.
# ---------------------------------------------------------------------------

P04P_DUAL_WRITE_SUCCESS = Counter(
    "georag_p04p_dual_write_success_total",
    "Successful §04p dual-write runs (orchestrator + persist completed end-to-end).",
)

P04P_DUAL_WRITE_FAILURES = Counter(
    "georag_p04p_dual_write_failures_total",
    "§04p dual-write runs that failed (p04p_telemetry.ok == False or exception).",
    labelnames=("error_kind",),
)

# §04i hallucination guard counters (Eval 01 follow-up, 2026-05-20).
#
# Per-layer firing rate is the only way to diagnose which guard is the
# binding constraint on a given prompt/model combo. Without these, the
# rejection_reason free-text column is the only signal and it's not
# aggregatable. With these, the operator can read a single PromQL:
#
#   rate(georag_hallucination_guard_layer_fires_total[10m])
#
# Labels:
#   - layer: "L1_entailment" | "L2_coverage" | "L3_numeric" |
#            "L4_refusal" | "L5_conflict" | "L6_freshness"
#   - outcome: "fire" (guard rejected the answer) | "pass" (clean)
# ---------------------------------------------------------------------------

HALLUCINATION_GUARD_FIRES = Counter(
    "georag_hallucination_guard_layer_fires_total",
    "Hallucination guard layer evaluations, split by layer and outcome.",
    labelnames=("layer", "outcome"),
)

# Orphan-span tracking. Bumped on every answer_runs row where
# orphan_span_count > 0 at lifecycle commit time.
ANSWER_RUNS_WITH_ORPHAN = Counter(
    "georag_answer_runs_with_orphan_total",
    "answer_runs where orphan_span_count > 0 at commit/reject time.",
)

# Total answer_runs (denominator for OrphanSpanRateHigh alert).
ANSWER_RUNS_TOTAL = Counter(
    "georag_answer_runs_total",
    "answer_runs persisted, regardless of lifecycle state.",
    labelnames=("lifecycle",),  # draft|generated|validated|committed|rejected
)

# Committed-without-guard-results — the HallucinationGuardBypassed alert
# uses the ratio of this to ANSWER_RUNS_TOTAL{lifecycle="committed"}.
ANSWER_RUNS_COMMITTED_GUARD_STATUS = Counter(
    "georag_answer_runs_committed_total",
    "Committed answer_runs, split by whether guard_results JSONB is present.",
    labelnames=("guard_results",),  # "present" | "absent"
)


# ---------------------------------------------------------------------------
# Phase 6 — Ingestion reliability metrics.
#
# Spec source: docs/georag-ingestion-reliability-spec.md, Phase 6.
# Alert rules consuming these live in
# docker/prometheus/rules/ingestion-reliability-alerts.yml.
# ---------------------------------------------------------------------------

# Per-run terminal duration (queued → completed | failed | timed_out | cancelled).
# Buckets sized to the realistic NI-43-101 ingest range: tens of seconds for
# small PDFs to ~30 min for the largest scanned reports.
INGESTION_RUN_DURATION = Histogram(
    "georag_ingestion_run_duration_seconds",
    "End-to-end ingestion run duration from started_at to terminal state.",
    labelnames=("status", "triggered_by"),
    buckets=(5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600),
)

# Terminal-state event counter. Drives the "any timed_out in last hour"
# and "failed-rate spike" alerts. Same label set as the histogram so
# dashboards can correlate volume + duration.
INGESTION_RUNS_TOTAL = Counter(
    "georag_ingestion_runs_total",
    "Ingestion runs that reached a terminal state, by status + trigger.",
    labelnames=("status", "triggered_by"),
)

# Stale-run sweep instrumentation. The 15-min cron sets the gauge to
# the number of rows in 'started' state with a stale heartbeat; the
# counter increments once per row it transitions to timed_out.
INGESTION_STALE_RUNS_DETECTED = Gauge(
    "georag_ingestion_active_started_count",
    "Count of ingest_progress rows in 'started' state at the most recent sweep tick.",
)
INGESTION_STALE_RUNS_TOTAL = Counter(
    "georag_ingestion_stale_runs_total",
    "Cumulative count of runs the stale-heartbeat sweep marked timed_out.",
)

# Embed-pending observability. The cron sweep sets this per workspace
# after each tick; the EmbedPendingPassagesStuck alert fires when any
# workspace has a non-zero value for 20+ minutes.
EMBED_PENDING_PASSAGES = Gauge(
    "georag_embed_pending_passages",
    "Per-workspace count of silver.document_passages with embedding_id IS NULL.",
    labelnames=("workspace_id",),
)

# Materialised-view refresh telemetry. Phase 2 + Phase 5 both call
# refresh_views_with_advisory_lock(); both pathways write to this.
MV_REFRESH_DURATION = Histogram(
    "georag_mv_refresh_duration_seconds",
    "Duration of a single MV refresh (REFRESH MATERIALIZED VIEW + log).",
    labelnames=("view_name", "status", "triggered_by"),
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)
MV_REFRESH_FAILURES_TOTAL = Counter(
    "georag_mv_refresh_failures_total",
    "Total MV refresh attempts that ended in status='failed'.",
    labelnames=("view_name",),
)
# Lag gauge — scraped from gold.mv_refresh_log by a small periodic
# Hatchet cron (mv_refresh_lag_publisher). Value = NOW() minus last
# completed finished_at, in seconds.
MV_REFRESH_LAG_SECONDS = Gauge(
    "georag_mv_refresh_lag_seconds",
    "Seconds since the last successful refresh of each registered MV.",
    labelnames=("view_name",),
)

# Phase 5 Tier 2 Qdrant spot-check — miss rate per workspace.
QDRANT_SPOTCHECK_MISS_RATE = Gauge(
    "georag_qdrant_spotcheck_miss_rate",
    "Fraction of sampled embedding_ids missing from the Qdrant collection.",
    labelnames=("workspace_id",),
)

# Outbox lag — oldest still-pending propagation row per target store.
OUTBOX_LAG_SECONDS = Gauge(
    "georag_outbox_lag_seconds",
    "Seconds since the oldest still-pending outbox.pending_propagations row was enqueued.",
    labelnames=("target_store",),
)

# Phase 2b emission latency — from ingest_progress.completed_at to the
# moment the WorkspaceDataUpdated event hits broadcastOn(). Histogram
# bucketed to call out anything > 60s as a slow-broadcast outlier.
WORKSPACE_DATA_UPDATED_EMISSION_LATENCY = Histogram(
    "georag_workspace_data_updated_emission_latency_seconds",
    "Wall-clock seconds from terminal-state ingest completion to "
    "workspace.data_updated broadcast dispatch.",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)

# /v1/viz/readiness probe response time, split by whether the probe was
# workspace-scoped (real backing data) or unscoped (empty-state path).
READINESS_PROBE_DURATION = Histogram(
    "georag_readiness_probe_duration_seconds",
    "Latency of the visualization readiness probe served by /v1/viz/readiness.",
    labelnames=("workspace_scoped",),  # "true" | "false"
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)

# ---------------------------------------------------------------------------
# Agentic-retrieval persistence reliability (persist_node retry exhaustion).
#
# persist_node retries the silver.answer_runs INSERT 3 times with
# exponential backoff (0.5s / 1.0s / 2.0s) before treating the failure
# as terminal. The answer has already been streamed to the caller, so a
# terminal failure is non-fatal for the request — but the lineage row is
# permanently lost and the operator needs paging.
#
# A sustained > 0 rate on this counter means asyncpg can't reach
# Postgres reliably during answer write-out (PgBouncer saturation,
# transient network partition, PG restart loop). Pair with the
# answer_runs INSERT log line (now logger.error with extra={"alert": True})
# in Loki for the failing query payload.
#
# Labels kept low-cardinality:
#   - stage: "answer_runs" (the only stage today; future child-row INSERTs
#     can register their own values).
# ---------------------------------------------------------------------------

AGENTIC_PERSIST_FAILURES = Counter(
    "georag_agentic_persist_failures_total",
    "agentic-retrieval persist_node DB writes that exhausted all retries.",
    labelnames=("stage",),
)
