"""Phase 1 step 2 — Hatchet workflow wrappers for the 10 Phase 0 agents.

Each wrapper:
  - declares a workflow with name + (where applicable) cron schedule
  - opens a small asyncpg pool + redis client per run
  - registers the agents.runtime so the @georag_agent decorator works
  - invokes the underlying agent and returns its summary

Schedules per Phase 0 kickoff §Step 6:

    tenant_isolation_audit          0 2  * * *    nightly 02:00 UTC
    storage_tiering_run             0 3  * * *    daily   03:00 UTC
    store_reconciliation_run        0 4  * * *    nightly 04:00 UTC
    model_upgrade_watch_run         0 5  * * *    daily   05:00 UTC
    vllm_security_check_run         0 1  * * *    daily   01:00 UTC
    model_cost_summary_run          0 6  * * *    daily   06:00 UTC
    index_health_check              0 */6 * * *    every 6 h

On-demand only (no cron — triggered via FastAPI route or manual run):

    lineage_walk
    llm_incident_diagnosis_run
    support_packet_assemble

Pool assignment for the worker pool split (Step 2):

    ingestion pool  (georag-hatchet-worker-ingestion):
        outbox_dispatcher, storage_tiering_run, index_health_check,
        store_reconciliation_run

    ai pool  (georag-hatchet-worker-ai):
        audit_ledger_verify, tenant_isolation_audit, lineage_walk,
        model_upgrade_watch_run, vllm_security_check_run,
        model_cost_summary_run, llm_incident_diagnosis_run,
        support_packet_assemble

Total registered with the engine: 10 agent workflows + 2 system workflows
(audit_ledger_verify + outbox_dispatcher) = 12, matching the kickoff.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
import redis.asyncio as aioredis
from hatchet_sdk import Context
from pydantic import BaseModel, ConfigDict, Field

from app.agents import AgentContext, register_runtime
from app.agents.phase0 import (
    graph_tenant_audit as _graph_tenant_audit_agent,
    index_health_check as _index_health_check_agent,
    lineage_walk as _lineage_walk_agent,
    llm_incident_diagnosis_run as _llm_incident_agent,
    model_cost_summary_run as _model_cost_summary_agent,
    model_upgrade_watch_run as _model_upgrade_watch_agent,
    storage_tiering_run as _storage_tiering_agent,
    store_reconciliation_run as _store_recon_agent,
    support_packet_assemble as _support_packet_agent,
    tenant_isolation_audit as _tenant_isolation_agent,
    vllm_security_check_run as _vllm_security_agent,
)
from app.hatchet_workflows import hatchet


# =============================================================================
# Per-task runtime helper
# =============================================================================
def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _redis_url() -> str:
    pw = os.environ.get("REDIS_PASSWORD", "")
    host = os.environ.get("REDIS_HOST", "redis")
    port = os.environ.get("REDIS_PORT", "6379")
    auth = f":{pw}@" if pw else ""
    return f"redis://{auth}{host}:{port}/0"


@asynccontextmanager
async def _agent_runtime():
    """Open a small pool + redis client, register the agents.runtime,
    yield, and clean up. Suitable for one-shot Hatchet task runs.
    """
    pool = await asyncpg.create_pool(
        _build_dsn(), min_size=1, max_size=2, statement_cache_size=0
    )
    redis = aioredis.from_url(_redis_url(), decode_responses=True)
    register_runtime(pg_pool=pool, redis=redis)
    try:
        yield
    finally:
        await pool.close()
        await redis.aclose()


# =============================================================================
# Shared input model — every workflow accepts an optional workspace_id +
# a free-form payload dict for agent-specific kwargs. The default workspace
# is None (system-wide invocation, e.g. nightly cron sweep).
# =============================================================================
class AgentRunInput(BaseModel):
    workspace_id: UUID | None = Field(
        default=None,
        description="If set, agent runs scoped to this workspace; if None, runs system-wide.",
    )
    actor_id: int | None = Field(default=None)
    trace_id: str | None = Field(default=None)
    kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Agent-specific keyword arguments passed through.",
    )


def _ctx_from(input: AgentRunInput, hctx: Context) -> AgentContext:
    return AgentContext(
        workspace_id=input.workspace_id,
        actor_id=input.actor_id,
        actor_kind="workflow",
        trace_id=input.trace_id or hctx.workflow_run_id,
    )


# =============================================================================
# 1. Tenant Isolation Auditor — nightly 02:00 UTC
# =============================================================================
class TenantIsolationAuditOutput(BaseModel):
    """Output schema for the tenant_isolation_audit workflow.

    Mirrors the agent summary dict in
    ``app.agents.phase0.tenant_isolation_auditor.tenant_isolation_audit``.
    Conditional keys (``note``, ``kestra_error``, ``kestra_skip_reason``)
    are accepted via ``extra="allow"`` since they only appear on specific
    branches (no workspaces, kestra failure, etc).
    """

    model_config = ConfigDict(extra="allow")

    tables_probed: int = 0
    probes_run: int = 0
    violations: int = 0
    violation_details: list[dict[str, Any]] = Field(default_factory=list)
    set_local_violations: list[dict[str, Any]] = Field(default_factory=list)
    kestra_escalated: bool | None = None


tenant_isolation_audit = hatchet.workflow(
    name="tenant_isolation_audit",
    on_crons=["0 2 * * *"],
    input_validator=AgentRunInput,
)


@tenant_isolation_audit.task(execution_timeout="10m")
async def _run_tenant_isolation(
    input: AgentRunInput, ctx: Context
) -> TenantIsolationAuditOutput:
    async with _agent_runtime():
        r = await _tenant_isolation_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return TenantIsolationAuditOutput.model_validate(r.value or {})


# =============================================================================
# 1b. Graph Tenant Auditor — Z-roadmap Z.9, nightly 02:30 UTC
#
# Sibling to tenant_isolation_audit (PG-side, 02:00 UTC). Offset by 30
# minutes so the two auditors don't contend for the Hatchet ai-pool
# slot or write to silver.tenant_isolation_audit at the same instant.
# =============================================================================
graph_tenant_audit = hatchet.workflow(
    name="graph_tenant_audit",
    on_crons=["30 2 * * *"],
    input_validator=AgentRunInput,
)


@graph_tenant_audit.task(execution_timeout="10m")
async def _run_graph_tenant_audit(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _graph_tenant_audit_agent(
            ctx=_ctx_from(input, ctx), **input.kwargs
        )
        return r.value or {}


# =============================================================================
# 2. Lineage Reporter — on-demand
# =============================================================================
class LineageWalkOutput(BaseModel):
    """Output schema for the lineage_walk workflow.

    Mirrors ``app.agents.phase0.lineage_reporter.lineage_walk``.
    """

    model_config = ConfigDict(extra="allow")

    target_type: str | None = None
    target_id: str | None = None
    chain_length: int = 0
    broken_at: list[dict[str, Any]] = Field(default_factory=list)
    is_intact: bool = True
    entries: list[dict[str, Any]] = Field(default_factory=list)


lineage_walk = hatchet.workflow(
    name="lineage_walk",
    input_validator=AgentRunInput,
)


@lineage_walk.task(execution_timeout="60s")
async def _run_lineage_walk(
    input: AgentRunInput, ctx: Context
) -> LineageWalkOutput:
    async with _agent_runtime():
        r = await _lineage_walk_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return LineageWalkOutput.model_validate(r.value or {})


# =============================================================================
# 3. Storage Tiering Agent — daily 03:00 UTC
# =============================================================================
class StorageTieringRunOutput(BaseModel):
    """Output schema for the storage_tiering_run workflow.

    Mirrors ``app.agents.phase0.storage_tiering.storage_tiering_run``.
    The ``fatal`` key is set when aioboto3 is missing (early-return path).
    """

    model_config = ConfigDict(extra="allow")

    rules_evaluated: int = 0
    objects_moved: int = 0
    objects_skipped: int = 0
    errors: int = 0
    silver_uri_rewrites: int = 0
    per_rule: list[dict[str, Any]] = Field(default_factory=list)


storage_tiering_run = hatchet.workflow(
    name="storage_tiering_run",
    on_crons=["0 3 * * *"],
    input_validator=AgentRunInput,
)


@storage_tiering_run.task(execution_timeout="30m")
async def _run_storage_tiering(
    input: AgentRunInput, ctx: Context
) -> StorageTieringRunOutput:
    async with _agent_runtime():
        r = await _storage_tiering_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return StorageTieringRunOutput.model_validate(r.value or {})


# =============================================================================
# 4. Index Health Agent — every 6 h
# =============================================================================
class IndexHealthCheckOutput(BaseModel):
    """Output schema for the index_health_check workflow.

    Mirrors ``app.agents.phase0.index_health.index_health_check``.
    ``qdrant_reachability`` and ``neo4j_page_cache_hit_ratio`` carry
    mixed types (dict/string/None) depending on probe outcome.
    """

    model_config = ConfigDict(extra="allow")

    slow_queries_flagged: int = 0
    bloat_findings: int = 0
    hypopg_suggestions: int = 0
    zero_hit_indices: int = 0
    qdrant_reachability: Any = None
    neo4j_page_cache_hit_ratio: Any = None


index_health_check = hatchet.workflow(
    name="index_health_check",
    on_crons=["0 */6 * * *"],
    input_validator=AgentRunInput,
)


@index_health_check.task(execution_timeout="5m")
async def _run_index_health(
    input: AgentRunInput, ctx: Context
) -> IndexHealthCheckOutput:
    async with _agent_runtime():
        r = await _index_health_check_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return IndexHealthCheckOutput.model_validate(r.value or {})


# =============================================================================
# 5. Store Reconciliation Agent — nightly 04:00 UTC
# =============================================================================
class StoreReconciliationRunOutput(BaseModel):
    """Output schema for the store_reconciliation_run workflow.

    Mirrors ``app.agents.phase0.store_reconciliation.store_reconciliation_run``.
    """

    model_config = ConfigDict(extra="allow")

    dead_lettered: int = 0
    stuck: int = 0
    missing_in_b: int = 0
    cross_store_drift: dict[str, Any] = Field(default_factory=dict)


store_reconciliation_run = hatchet.workflow(
    name="store_reconciliation_run",
    on_crons=["0 4 * * *"],
    input_validator=AgentRunInput,
)


@store_reconciliation_run.task(execution_timeout="20m")
async def _run_store_recon(
    input: AgentRunInput, ctx: Context
) -> StoreReconciliationRunOutput:
    async with _agent_runtime():
        r = await _store_recon_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return StoreReconciliationRunOutput.model_validate(r.value or {})


# =============================================================================
# 6. Model Upgrade Watch Agent — daily 05:00 UTC
# =============================================================================
class ModelUpgradeWatchRunOutput(BaseModel):
    """Output schema for the model_upgrade_watch_run workflow.

    Mirrors ``app.agents.phase0.model_upgrade_watch.model_upgrade_watch_run``.
    ``vllm`` and ``model`` are sub-dicts whose shapes vary by branch
    (checked vs not-checked vs error).
    """

    model_config = ConfigDict(extra="allow")

    vllm: dict[str, Any] = Field(default_factory=lambda: {"checked": False})
    model: dict[str, Any] = Field(default_factory=lambda: {"checked": False})
    notifications_emitted: int = 0
    errors: int = 0


model_upgrade_watch_run = hatchet.workflow(
    name="model_upgrade_watch_run",
    on_crons=["0 5 * * *"],
    input_validator=AgentRunInput,
)


@model_upgrade_watch_run.task(execution_timeout="2m")
async def _run_model_upgrade_watch(
    input: AgentRunInput, ctx: Context
) -> ModelUpgradeWatchRunOutput:
    async with _agent_runtime():
        r = await _model_upgrade_watch_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return ModelUpgradeWatchRunOutput.model_validate(r.value or {})


# =============================================================================
# 7. vLLM Security Check Agent — daily 01:00 UTC
# =============================================================================
class VllmSecurityCheckRunOutput(BaseModel):
    """Output schema for the vllm_security_check_run workflow.

    Mirrors ``app.agents.phase0.vllm_security_check.vllm_security_check_run``.
    Conditional keys (``note``, ``error_message``, ``http_status``) appear
    on the unset-version / fetch-error branches.
    """

    model_config = ConfigDict(extra="allow")

    checked: bool = False
    current_vllm: str = ""
    advisories_seen: int = 0
    matches: list[dict[str, Any]] = Field(default_factory=list)
    alerts_emitted: int = 0
    errors: int = 0


vllm_security_check_run = hatchet.workflow(
    name="vllm_security_check_run",
    on_crons=["0 1 * * *"],
    input_validator=AgentRunInput,
)


@vllm_security_check_run.task(execution_timeout="2m")
async def _run_vllm_security(
    input: AgentRunInput, ctx: Context
) -> VllmSecurityCheckRunOutput:
    async with _agent_runtime():
        r = await _vllm_security_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return VllmSecurityCheckRunOutput.model_validate(r.value or {})


# =============================================================================
# 8. Model Cost Summary Agent — daily 06:00 UTC
# =============================================================================
class ModelCostSummaryRunOutput(BaseModel):
    """Output schema for the model_cost_summary_run workflow.

    Mirrors ``app.agents.phase0.model_cost_summary.model_cost_summary_run``.
    """

    model_config = ConfigDict(extra="allow")

    rollup_date: str = ""
    rows_aggregated: int = 0
    buckets_upserted: int = 0
    ceilings_evaluated: int = 0
    warnings_emitted: int = 0
    errors: int = 0


model_cost_summary_run = hatchet.workflow(
    name="model_cost_summary_run",
    on_crons=["0 6 * * *"],
    input_validator=AgentRunInput,
)


@model_cost_summary_run.task(execution_timeout="5m")
async def _run_model_cost_summary(
    input: AgentRunInput, ctx: Context
) -> ModelCostSummaryRunOutput:
    async with _agent_runtime():
        r = await _model_cost_summary_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return ModelCostSummaryRunOutput.model_validate(r.value or {})


# =============================================================================
# 9. LLM Incident Diagnosis Agent — on-demand (dispatched on Prometheus alert)
# =============================================================================
class LlmIncidentDiagnosisRunOutput(BaseModel):
    """Output schema for the llm_incident_diagnosis_run workflow.

    Mirrors
    ``app.agents.phase0.llm_incident_diagnosis.llm_incident_diagnosis_run``.
    ``diagnosis`` is the LLM's structured payload (already validated by
    ``IncidentDiagnosis`` upstream then ``model_dump()``-ed).
    """

    model_config = ConfigDict(extra="allow")

    alert_label: str = ""
    window_minutes: int = 0
    context_counts: dict[str, int] = Field(default_factory=dict)
    prompt_version: str | None = None
    diagnosis: dict[str, Any] = Field(default_factory=dict)


llm_incident_diagnosis_run = hatchet.workflow(
    name="llm_incident_diagnosis_run",
    input_validator=AgentRunInput,
)


@llm_incident_diagnosis_run.task(execution_timeout="3m")
async def _run_llm_incident(
    input: AgentRunInput, ctx: Context
) -> LlmIncidentDiagnosisRunOutput:
    async with _agent_runtime():
        r = await _llm_incident_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return LlmIncidentDiagnosisRunOutput.model_validate(r.value or {})


# =============================================================================
# 10. Support Packet Agent — on-demand (also exposed via FastAPI route)
# =============================================================================
class SupportPacketAssembleOutput(BaseModel):
    """Output schema for the support_packet_assemble workflow.

    Mirrors ``app.agents.phase0.support_packet.support_packet_assemble``.
    """

    model_config = ConfigDict(extra="allow")

    packet_id: str = ""
    incident_id: str = ""
    storage_uri: str | None = None
    bundle_bytes: int = 0
    counts: dict[str, Any] = Field(default_factory=dict)
    upload_ok: bool = False
    upload_error: str | None = None
    kestra_dispatched: bool = False
    kestra_error: str | None = None


support_packet_assemble = hatchet.workflow(
    name="support_packet_assemble",
    input_validator=AgentRunInput,
)


@support_packet_assemble.task(execution_timeout="5m")
async def _run_support_packet(
    input: AgentRunInput, ctx: Context
) -> SupportPacketAssembleOutput:
    async with _agent_runtime():
        r = await _support_packet_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return SupportPacketAssembleOutput.model_validate(r.value or {})


# =============================================================================
# Pool routing — worker.py looks these up by WORKER_POOL env.
# =============================================================================
INGESTION_AGENT_WORKFLOWS = [
    storage_tiering_run,
    index_health_check,
    store_reconciliation_run,
]

AI_AGENT_WORKFLOWS = [
    tenant_isolation_audit,
    graph_tenant_audit,
    lineage_walk,
    model_upgrade_watch_run,
    vllm_security_check_run,
    model_cost_summary_run,
    llm_incident_diagnosis_run,
    support_packet_assemble,
]

ALL_AGENT_WORKFLOWS = INGESTION_AGENT_WORKFLOWS + AI_AGENT_WORKFLOWS

__all__ = [
    "tenant_isolation_audit",
    "graph_tenant_audit",
    "lineage_walk",
    "storage_tiering_run",
    "index_health_check",
    "store_reconciliation_run",
    "model_upgrade_watch_run",
    "vllm_security_check_run",
    "model_cost_summary_run",
    "llm_incident_diagnosis_run",
    "support_packet_assemble",
    "INGESTION_AGENT_WORKFLOWS",
    "AI_AGENT_WORKFLOWS",
    "ALL_AGENT_WORKFLOWS",
    # Typed output models (closes §B.7.1 untyped gap)
    "TenantIsolationAuditOutput",
    "LineageWalkOutput",
    "StorageTieringRunOutput",
    "IndexHealthCheckOutput",
    "StoreReconciliationRunOutput",
    "ModelUpgradeWatchRunOutput",
    "VllmSecurityCheckRunOutput",
    "ModelCostSummaryRunOutput",
    "LlmIncidentDiagnosisRunOutput",
    "SupportPacketAssembleOutput",
]
