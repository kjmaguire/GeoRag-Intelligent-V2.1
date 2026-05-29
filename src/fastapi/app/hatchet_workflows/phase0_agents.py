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
from pydantic import BaseModel, Field

from app.agents import AgentContext, register_runtime
from app.agents.phase0 import (
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
tenant_isolation_audit = hatchet.workflow(
    name="tenant_isolation_audit",
    on_crons=["0 2 * * *"],
    input_validator=AgentRunInput,
)


@tenant_isolation_audit.task(execution_timeout="10m")
async def _run_tenant_isolation(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _tenant_isolation_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return r.value or {}


# =============================================================================
# 2. Lineage Reporter — on-demand
# =============================================================================
lineage_walk = hatchet.workflow(
    name="lineage_walk",
    input_validator=AgentRunInput,
)


@lineage_walk.task(execution_timeout="60s")
async def _run_lineage_walk(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _lineage_walk_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return r.value or {}


# =============================================================================
# 3. Storage Tiering Agent — daily 03:00 UTC
# =============================================================================
storage_tiering_run = hatchet.workflow(
    name="storage_tiering_run",
    on_crons=["0 3 * * *"],
    input_validator=AgentRunInput,
)


@storage_tiering_run.task(execution_timeout="30m")
async def _run_storage_tiering(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _storage_tiering_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return r.value or {}


# =============================================================================
# 4. Index Health Agent — every 6 h
# =============================================================================
index_health_check = hatchet.workflow(
    name="index_health_check",
    on_crons=["0 */6 * * *"],
    input_validator=AgentRunInput,
)


@index_health_check.task(execution_timeout="5m")
async def _run_index_health(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _index_health_check_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return r.value or {}


# =============================================================================
# 5. Store Reconciliation Agent — nightly 04:00 UTC
# =============================================================================
store_reconciliation_run = hatchet.workflow(
    name="store_reconciliation_run",
    on_crons=["0 4 * * *"],
    input_validator=AgentRunInput,
)


@store_reconciliation_run.task(execution_timeout="20m")
async def _run_store_recon(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _store_recon_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return r.value or {}


# =============================================================================
# 6. Model Upgrade Watch Agent — daily 05:00 UTC
# =============================================================================
model_upgrade_watch_run = hatchet.workflow(
    name="model_upgrade_watch_run",
    on_crons=["0 5 * * *"],
    input_validator=AgentRunInput,
)


@model_upgrade_watch_run.task(execution_timeout="2m")
async def _run_model_upgrade_watch(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _model_upgrade_watch_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return r.value or {}


# =============================================================================
# 7. vLLM Security Check Agent — daily 01:00 UTC
# =============================================================================
vllm_security_check_run = hatchet.workflow(
    name="vllm_security_check_run",
    on_crons=["0 1 * * *"],
    input_validator=AgentRunInput,
)


@vllm_security_check_run.task(execution_timeout="2m")
async def _run_vllm_security(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _vllm_security_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return r.value or {}


# =============================================================================
# 8. Model Cost Summary Agent — daily 06:00 UTC
# =============================================================================
model_cost_summary_run = hatchet.workflow(
    name="model_cost_summary_run",
    on_crons=["0 6 * * *"],
    input_validator=AgentRunInput,
)


@model_cost_summary_run.task(execution_timeout="5m")
async def _run_model_cost_summary(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _model_cost_summary_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return r.value or {}


# =============================================================================
# 9. LLM Incident Diagnosis Agent — on-demand (dispatched on Prometheus alert)
# =============================================================================
llm_incident_diagnosis_run = hatchet.workflow(
    name="llm_incident_diagnosis_run",
    input_validator=AgentRunInput,
)


@llm_incident_diagnosis_run.task(execution_timeout="3m")
async def _run_llm_incident(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _llm_incident_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return r.value or {}


# =============================================================================
# 10. Support Packet Agent — on-demand (also exposed via FastAPI route)
# =============================================================================
support_packet_assemble = hatchet.workflow(
    name="support_packet_assemble",
    input_validator=AgentRunInput,
)


@support_packet_assemble.task(execution_timeout="5m")
async def _run_support_packet(input: AgentRunInput, ctx: Context) -> dict:
    async with _agent_runtime():
        r = await _support_packet_agent(ctx=_ctx_from(input, ctx), **input.kwargs)
        return r.value or {}


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
]
