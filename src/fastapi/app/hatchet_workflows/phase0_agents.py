"""Hatchet workflow wrappers for the retained Phase 0 agents.

Each wrapper:
  - declares a workflow with name + (where applicable) cron schedule
  - opens a small asyncpg pool + redis client per run
  - registers the agents.runtime so the @georag_agent decorator works
  - invokes the underlying agent and returns its summary

Schedules. The hours are NOT the Phase 0 kickoff §Step 6 ones any more:
every fixed-hour slot moved on 2026-09-16, when the nightly shutdown
window shrank to eight and a half hours a day (08:30-17:00 Pacific) and
closed 00:00-16:30 UTC, the half hour to 17:00 being the startup sweep's
own head start. The relative order and stagger are what the kickoff
actually specified, and those survived intact.

    tenant_isolation_audit          0 17 * * *     nightly 17:00 UTC
    storage_tiering_run             0 18 * * *     daily   18:00 UTC
    store_reconciliation_run        0 19 * * *     nightly 19:00 UTC
    model_upgrade_watch_run         0 20 * * *     daily   20:00 UTC
    model_cost_summary_run          0 22 * * *     daily   22:00 UTC
    index_health_check              0 */6 * * *    every 6 h

On-demand only, no cron. Since 2026-09-29 (HAT-13) Laravel dispatches all
three through FastAPI ``POST /internal/v1/workflows/{name}/trigger``
(``app/routers/workflow_trigger.py``):

    lineage_walk                admin, workspace-scoped
    llm_incident_diagnosis_run  admin, platform-wide
    support_packet_assemble     admin, workspace-scoped

``app/routers/phase0_ops.py`` still runs the last two INLINE (no Hatchet run)
at ``/api/v1/incidents/diagnose`` and ``/api/v1/support/packets/assemble``;
nothing in Laravel calls those.

Removed 2026-09-29 (HAT-13): ``graph_tenant_audit``, the 17:30 UTC cron
wrapping ``app.agents.phase0.graph_tenant_auditor``. It audited Neo4j,
which was removed on 2026-07-28 (CLAUDE.md hard rule 9), and still fired
nightly, writing an ``auditor='neo4j_graph'`` row for a store that does
not exist. The agent module itself is left in place, unregistered.

Pool assignment for the worker pool split (Step 2).

This module contributes exactly two tuples, defined at the bottom of the
file and consumed by ``worker.py``'s ``POOLS``:

    INGESTION_AGENT_WORKFLOWS:
        storage_tiering_run, index_health_check, store_reconciliation_run

    AI_AGENT_WORKFLOWS:
        tenant_isolation_audit, lineage_walk,
        model_upgrade_watch_run, model_cost_summary_run,
        llm_incident_diagnosis_run, support_packet_assemble

Those tuples are the contract — read them, not this docstring, if the two
ever disagree. Non-agent workflows that also sit in those pools
(outbox_dispatcher, ingest_pdf, audit_ledger_verify, the backup crons, …)
are added directly in ``worker.py`` and are deliberately NOT listed here.

The split is latent, not active: ``WORKER_POOL`` selects a pool at boot and
defaults to ``all`` (``POOLS["all"] = ingestion + ai``), which is what
docker-compose sets and what every deployed worker actually runs. Splitting
the workers across two pools is still supported — that is why the tuples are
kept separate — but no environment does it today, so do not assume a
workflow listed under "ingestion" is isolated from the ai workload.

Only ``app.agents.phase0`` modules are exported here. Later domain-agent
phases are not worker workflows and must not be added to these pool lists.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
import redis.asyncio as aioredis
from hatchet_sdk import Context, NonRetryableException
from pydantic import BaseModel, ConfigDict, Field

from app.agents import AgentContext, AgentResult, register_runtime
from app.agents.phase0 import (
    index_health_check as _index_health_check_agent,
)
from app.agents.phase0 import (
    lineage_walk as _lineage_walk_agent,
)
from app.agents.phase0 import (
    llm_incident_diagnosis_run as _llm_incident_agent,
)
from app.agents.phase0 import (
    model_cost_summary_run as _model_cost_summary_agent,
)
from app.agents.phase0 import (
    model_upgrade_watch_run as _model_upgrade_watch_agent,
)
from app.agents.phase0 import (
    storage_tiering_run as _storage_tiering_agent,
)
from app.agents.phase0 import (
    store_reconciliation_run as _store_recon_agent,
)
from app.agents.phase0 import (
    support_packet_assemble as _support_packet_agent,
)
from app.agents.phase0 import (
    tenant_isolation_audit as _tenant_isolation_agent,
)
from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet

# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_build_dsn = build_dsn


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


def _ctx_from(
    input: AgentRunInput,
    hctx: Context,
    *,
    document_id: str | None = None,
    bypass_idempotency: bool = False,
) -> AgentContext:
    """Build the wrapper context for one workflow run.

    ``document_id`` and ``bypass_idempotency`` exist for the two R2 agents.
    The wrapper refuses to compute an R2 idempotency key without a
    ``workspace_id`` AND a ``document_id`` (``agents.wrapper``), so an R2 agent
    called with neither fails before its first line runs. Policy-level agents
    (Storage Tiering: nightly, no workspace, no document) bypass idempotency;
    per-document agents (Support Packet: one packet per incident) pass the
    document they dedupe on.
    """
    return AgentContext(
        workspace_id=input.workspace_id,
        actor_id=input.actor_id,
        actor_kind="workflow",
        trace_id=input.trace_id or hctx.workflow_run_id,
        document_id=document_id,
        bypass_idempotency=bypass_idempotency,
    )


class AgentRunFailedError(RuntimeError):
    """The wrapped agent did not produce a result (failure, timeout, breaker open)."""


#: Wrapper outcomes that carry a usable ``value``. ``deduped`` is an R2 replay of
#: the stored result of an earlier identical run.
_USABLE_OUTCOMES = frozenset({"success", "deduped"})


def _agent_value(r: AgentResult[Any], workflow: str) -> dict[str, Any]:
    """Return the agent's result dict, or raise so the Hatchet run goes red.

    ``@georag_agent`` never raises: failure, timeout, an open circuit breaker
    and a refusal all come back as an ``AgentResult`` whose ``value`` is
    ``None``. Every task here used to validate ``r.value or {}`` into its
    output model, so each of those became a green run carrying default
    values, which for a verdict agent read as a clean bill of health
    (``violations=0``, ``is_intact=True``).

    A refusal is raised too, but as non-retryable. It is a deliberate "I will
    not answer" (only the incident-diagnosis agent raises one: unfamiliar
    alert, too little context, model output that is not the schema). The
    circuit breaker still does not count it, but the *workflow's* job is to
    return a diagnosis and it returned none, so an operator reading the run
    must see that. Retrying the same inputs cannot change a refusal.
    """
    if r.outcome in _USABLE_OUTCOMES:
        return r.value or {}
    detail = f"{workflow}: agent outcome={r.outcome}" + (f" ({r.error})" if r.error else "")
    if r.outcome == "refusal":
        raise NonRetryableException(detail)
    raise AgentRunFailedError(detail)


# =============================================================================
# 1. Tenant Isolation Auditor — nightly 17:00 UTC
# =============================================================================
class TenantIsolationAuditOutput(BaseModel):
    """Output schema for the tenant_isolation_audit workflow.

    Mirrors the agent summary dict in
    ``app.agents.phase0.tenant_isolation_auditor.tenant_isolation_audit``.
    Conditional keys (``note``, ``escalation_enqueued``,
    ``escalation_error``) are accepted via ``extra="allow"`` since they
    only appear on specific branches (no workspaces, enqueue failure, etc).
    """

    model_config = ConfigDict(extra="allow")

    tables_probed: int = 0
    probes_run: int = 0
    # None means "the audit did not report", never "no violations": a 0 or an
    # empty list here is a clean bill of health for tenant isolation.
    violations: int | None = None
    violation_details: list[dict[str, Any]] | None = None
    set_local_violations: list[dict[str, Any]] | None = None
    kestra_escalated: bool | None = None


tenant_isolation_audit = hatchet.workflow(
    name="tenant_isolation_audit",
    on_crons=["0 17 * * *"],
    input_validator=AgentRunInput,
)


@tenant_isolation_audit.task(execution_timeout="10m")
async def _run_tenant_isolation(
    input: AgentRunInput, ctx: Context
) -> TenantIsolationAuditOutput:
    async with _agent_runtime():
        r = await _tenant_isolation_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return TenantIsolationAuditOutput.model_validate(_agent_value(r, "tenant_isolation_audit"))


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
    # None = the walk did not report. ``True`` / ``[]`` assert an intact chain.
    broken_at: list[dict[str, Any]] | None = None
    is_intact: bool | None = None
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
        return LineageWalkOutput.model_validate(_agent_value(r, "lineage_walk"))


# =============================================================================
# 3. Storage Tiering Agent — daily 18:00 UTC
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
    errors: int | None = None
    silver_uri_rewrites: int = 0
    per_rule: list[dict[str, Any]] = Field(default_factory=list)


storage_tiering_run = hatchet.workflow(
    name="storage_tiering_run",
    on_crons=["0 18 * * *"],
    input_validator=AgentRunInput,
)


@storage_tiering_run.task(execution_timeout="30m")
async def _run_storage_tiering(
    input: AgentRunInput, ctx: Context
) -> StorageTieringRunOutput:
    async with _agent_runtime():
        # Policy-level run (nightly cron: no workspace, no document), so there is
        # nothing for an R2 idempotency key to be built from. Re-running is safe:
        # each move is gated on the object still being in its source tier.
        r = await _storage_tiering_agent(
            ctx=_ctx_from(input, ctx, bypass_idempotency=True), **input.kwargs
        )
        return StorageTieringRunOutput.model_validate(_agent_value(r, "storage_tiering_run"))


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

    # Finding counts default to None, not 0: 0 reads as "checked, healthy".
    slow_queries_flagged: int | None = None
    bloat_findings: int | None = None
    hypopg_suggestions: int | None = None
    zero_hit_indices: int | None = None
    qdrant_reachability: Any = None
    neo4j_page_cache_hit_ratio: Any = None
    # Non-zero means a finding could not be written to
    # silver.corpus_health_findings at all. Since migration
    # 2026_08_19_070000 made workspace_id nullable, the system-wide (cron)
    # path persists its cluster-scoped findings as NULL-workspace rows, so
    # this is now a genuine error signal rather than the steady state it used
    # to be — see index_health.py's `system_wide` comment.
    findings_unpersisted: int | None = None
    findings: list[dict[str, Any]] | None = None


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
        return IndexHealthCheckOutput.model_validate(_agent_value(r, "index_health_check"))


# =============================================================================
# 5. Store Reconciliation Agent — nightly 19:00 UTC
# =============================================================================
class StoreReconciliationRunOutput(BaseModel):
    """Output schema for the store_reconciliation_run workflow.

    Mirrors ``app.agents.phase0.store_reconciliation.store_reconciliation_run``.
    """

    model_config = ConfigDict(extra="allow")

    # None = the reconciliation did not report; 0 / {} assert "no drift found".
    dead_lettered: int | None = None
    stuck: int | None = None
    missing_in_b: int | None = None
    # Propagations with no workspace_id: not recordable as findings (the column
    # is NOT NULL), so they are counted here rather than lost or crashed on.
    unscoped_skipped: int | None = None
    # Keyed by workspace id, then by store; see store_reconciliation.py.
    cross_store_drift: dict[str, Any] | None = None
    # Set when no cross-store comparison could be made at all (nothing to compare).
    cross_store_skipped: str | None = None


store_reconciliation_run = hatchet.workflow(
    name="store_reconciliation_run",
    on_crons=["0 19 * * *"],
    input_validator=AgentRunInput,
)


@store_reconciliation_run.task(execution_timeout="20m")
async def _run_store_recon(
    input: AgentRunInput, ctx: Context
) -> StoreReconciliationRunOutput:
    async with _agent_runtime():
        r = await _store_recon_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return StoreReconciliationRunOutput.model_validate(_agent_value(r, "store_reconciliation_run"))


# =============================================================================
# 6. Model Upgrade Watch Agent — daily 20:00 UTC
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
    errors: int | None = None


model_upgrade_watch_run = hatchet.workflow(
    name="model_upgrade_watch_run",
    on_crons=["0 20 * * *"],
    input_validator=AgentRunInput,
)


@model_upgrade_watch_run.task(execution_timeout="2m")
async def _run_model_upgrade_watch(
    input: AgentRunInput, ctx: Context
) -> ModelUpgradeWatchRunOutput:
    async with _agent_runtime():
        r = await _model_upgrade_watch_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return ModelUpgradeWatchRunOutput.model_validate(_agent_value(r, "model_upgrade_watch_run"))


# =============================================================================
# 8. Model Cost Summary Agent — daily 22:00 UTC
# =============================================================================
class ModelCostSummaryRunOutput(BaseModel):
    """Output schema for the model_cost_summary_run workflow.

    Mirrors ``app.agents.phase0.model_cost_summary.model_cost_summary_run``.
    """

    model_config = ConfigDict(extra="allow")

    rollup_date: str | None = None
    rows_aggregated: int = 0
    buckets_upserted: int = 0
    ceilings_evaluated: int = 0
    warnings_emitted: int = 0
    errors: int | None = None


model_cost_summary_run = hatchet.workflow(
    name="model_cost_summary_run",
    # Moved 2026-08-21 to 15:00 UTC; 06:00 was inside the window then, which
    # ran 06:00-14:00 UTC and was closed by an Azure Container Apps sweep
    # scaling hatchet-worker-cc to zero at both DST candidate hours. ADR-0022
    # retired that on 2026-09-08 — one timezone-aware EventBridge schedule
    # scaling every ECS service to --desired-count 0 — and on 2026-09-16 the
    # window shrank to 08:30-17:00 Pacific, closing 00:00-16:30 UTC, which is
    # what moved this to 22:00. See
    # tests/test_crons_avoid_the_shutdown_window.py, which derives the span
    # from the Terraform rather than from this comment.
    on_crons=["0 22 * * *"],
    input_validator=AgentRunInput,
)


@model_cost_summary_run.task(execution_timeout="5m")
async def _run_model_cost_summary(
    input: AgentRunInput, ctx: Context
) -> ModelCostSummaryRunOutput:
    async with _agent_runtime():
        r = await _model_cost_summary_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return ModelCostSummaryRunOutput.model_validate(_agent_value(r, "model_cost_summary_run"))


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

    alert_label: str | None = None
    window_minutes: int = 0
    context_counts: dict[str, int] = Field(default_factory=dict)
    prompt_version: str | None = None
    diagnosis: dict[str, Any] | None = None


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
        return LlmIncidentDiagnosisRunOutput.model_validate(_agent_value(r, "llm_incident_diagnosis_run"))


# =============================================================================
# 10. Support Packet Agent — on-demand (also exposed via FastAPI route)
# =============================================================================
class SupportPacketAssembleOutput(BaseModel):
    """Output schema for the support_packet_assemble workflow.

    Mirrors ``app.agents.phase0.support_packet.support_packet_assemble``.
    """

    model_config = ConfigDict(extra="allow")

    packet_id: str | None = None
    incident_id: str | None = None
    storage_uri: str | None = None
    bundle_bytes: int = 0
    counts: dict[str, Any] = Field(default_factory=dict)
    upload_ok: bool = False
    upload_error: str | None = None
    # Renamed 2026-07-25 (Kestra retirement) — the on-call handoff now goes
    # through the outbox `external_webhook` target rather than a direct POST
    # to Kestra's execution API.
    dispatch_enqueued: bool = False
    dispatch_error: str | None = None


support_packet_assemble = hatchet.workflow(
    name="support_packet_assemble",
    input_validator=AgentRunInput,
)


@support_packet_assemble.task(execution_timeout="5m")
async def _run_support_packet(
    input: AgentRunInput, ctx: Context
) -> SupportPacketAssembleOutput:
    async with _agent_runtime():
        # The incident is the document the R2 idempotency key dedupes on (see the
        # support_packet module docstring). No incident_id leaves it None, the
        # wrapper refuses, and the run fails loudly instead of building a key.
        incident_id = input.kwargs.get("incident_id")
        r = await _support_packet_agent(
            ctx=_ctx_from(input, ctx, document_id=str(incident_id) if incident_id else None),
            **input.kwargs,
        )
        return SupportPacketAssembleOutput.model_validate(_agent_value(r, "support_packet_assemble"))


# =============================================================================
# Pool routing — worker.py looks these up by WORKER_POOL env.
# =============================================================================
INGESTION_AGENT_WORKFLOWS: tuple = (
    storage_tiering_run,
    index_health_check,
    store_reconciliation_run,
)

AI_AGENT_WORKFLOWS: tuple = (
    tenant_isolation_audit,
    lineage_walk,
    model_upgrade_watch_run,
    model_cost_summary_run,
    llm_incident_diagnosis_run,
    support_packet_assemble,
)

ALL_AGENT_WORKFLOWS = INGESTION_AGENT_WORKFLOWS + AI_AGENT_WORKFLOWS

__all__ = [
    "tenant_isolation_audit",
    "lineage_walk",
    "storage_tiering_run",
    "index_health_check",
    "store_reconciliation_run",
    "model_upgrade_watch_run",
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
    "ModelCostSummaryRunOutput",
    "LlmIncidentDiagnosisRunOutput",
    "SupportPacketAssembleOutput",
]
