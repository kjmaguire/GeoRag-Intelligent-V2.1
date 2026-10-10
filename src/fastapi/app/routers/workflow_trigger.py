"""Product and operator triggers for Hatchet workflows that had none (HAT-13).

Until 2026-09-29 eight registered workflows could only be started from the
Hatchet UI: nothing in Laravel or FastAPI dispatched them. Kyle's call was to
give each one that is inherently user-initiated a real trigger, and to
schedule none of them. This is the FastAPI half, the same thin pass-through
shape as ``shadow_trigger`` and ``public_geo_trigger``. Laravel has no
Hatchet client, so it POSTs here and this calls ``aio_run_no_wait()`` and
returns the run id without waiting.

    POST /internal/v1/workflows/{workflow}/trigger
    body: {"workspace_id": <uuid|null>, "requested_by": <str>, "input": {...}}

``input`` is the workflow's own input model, validated here. ``workspace_id``
is the scope Laravel has already authorised the user for. This route does
not repeat Laravel's decision about the user, because it never sees one.
What it does check is that everything the input names lives inside that
workspace: the project, the ticket, the audit entry, the workflow run, the
manifest's key prefix. A caller that authorised workspace A cannot use this
route to reach a row in workspace B. The checks run under
``scoped_connection``, so RLS agrees with the explicit ``WHERE``.

Which workflows, and who may ask (the Laravel side decides; see
``App\\Policies\\WorkflowTriggerPolicy``):

    generate_report, score_targets      project members
    workspace_export, restore_workspace,
    lineage_walk, support_packet_assemble,
    support_replay                      admins who are members of the workspace
    llm_incident_diagnosis_run          admins, platform-wide (no workspace)

Not here, on purpose:

* ``field_outcome_learning``. Nothing writes ``targeting.target_outcomes``,
  and every run appends a fresh backtest row for every outcome in the
  project, so it is neither useful nor safe to trigger routinely. It stays
  manual (Hatchet UI).
* ``continuous_learning_loop``. It is a cron now (22:30 UTC). It is cheap,
  makes no LLM call, and accepts an empty input.
* ``nl_summaries``. Manual by design; see its module docstring.

Idempotent on the request id Laravel mints for each click (``export_request_id``,
``score_request_id``, ``restore_request_id``, ``replay_request_id``; see
``REQUEST_ID_FIELDS``). The id is claimed in Redis with ``SET NX`` (1 h) before
the run is dispatched, so a repeated POST answers with the first run's id
instead of starting a second run, and of two concurrent copies exactly one
dispatches. With Redis unavailable the trigger still dispatches (the Laravel
cooldown remains) and the outage is logged.

Auth: ``X-Service-Key`` through the shared ``verify_service_key``, like every
other ``/internal`` route.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.db import scoped_connection
from app.hatchet_workflows.generate_report import GenerateReportInput, generate_report
from app.hatchet_workflows.phase0_agents import (
    AgentRunInput,
    lineage_walk,
    llm_incident_diagnosis_run,
    support_packet_assemble,
)
from app.hatchet_workflows.restore_workspace import (
    RestoreWorkspaceInput,
    restore_workspace,
)
from app.hatchet_workflows.score_targets import ScoreTargetsInput, score_targets
from app.hatchet_workflows.support_replay import SupportReplayInput, support_replay
from app.hatchet_workflows.workspace_export import (
    WorkspaceExportInput,
    workspace_export,
)
from app.services.auth import verify_service_key

log = logging.getLogger("georag.workflow_trigger")

router = APIRouter(prefix="/internal/v1/workflows", tags=["workflows"])

#: The only bucket workspace_export may write to, and so the only bucket a
#: restore manifest may come from. Matches WorkspaceExportInput's default.
EXPORT_BUCKET = "workspace-exports"

#: Starlette renamed HTTP_422_UNPROCESSABLE_ENTITY and deprecated the old
#: name; the number is the contract, so use it.
_UNPROCESSABLE = 422


class TriggerRefused(Exception):
    """A request this route will not dispatch, with the status to answer."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# =============================================================================
# Request / response
# =============================================================================
class WorkflowTriggerRequest(BaseModel):
    workspace_id: UUID | None = Field(
        default=None,
        description="The workspace Laravel authorised the caller for. Every "
                    "resource the input names must live inside it.",
    )
    requested_by: str | None = Field(
        default=None,
        max_length=255,
        description="Who asked. Logged for the operator trail, never used for auth.",
    )
    input: dict[str, Any] = Field(default_factory=dict)


class WorkflowTriggerResponse(BaseModel):
    workflow: str
    workflow_run_id: str
    workspace_id: str | None


# =============================================================================
# Agent kwargs. AgentRunInput.kwargs is a free-form dict that the Phase 0
# wrappers splat into the agent, so it is validated here instead.
# =============================================================================
class _StrictKwargs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineageWalkKwargs(_StrictKwargs):
    target_type: Literal["workflow_run", "audit_ledger_entry", "workspace"]
    target_id: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(default=1000, ge=1, le=1000)


class IncidentDiagnosisKwargs(_StrictKwargs):
    alert_label: str = Field(..., min_length=1, max_length=200)
    window_minutes: int = Field(default=60, ge=1, le=24 * 60)


class SupportPacketKwargs(_StrictKwargs):
    incident_id: str = Field(..., min_length=1, max_length=200)
    trace_id: str | None = Field(default=None, max_length=200)
    requested_by: int | None = None


def _validate_kwargs(model: type[BaseModel], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        return model.model_validate(kwargs).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise TriggerRefused(
            _UNPROCESSABLE,
            f"invalid kwargs: {exc.errors(include_url=False, include_context=False)}",
        ) from exc


# =============================================================================
# Per-workflow rules. `prepare` is pure (shape, workspace consistency);
# `check` runs inside scoped_connection for the authorised workspace.
# =============================================================================
Prepare = Callable[[Any, str | None], Any]
Check = Callable[[asyncpg.Connection, Any, str], Awaitable[None]]


def _same_workspace(declared: UUID | str | None, scope: str | None) -> None:
    if scope is None or declared is None or str(declared) != scope:
        raise TriggerRefused(
            _UNPROCESSABLE,
            "input.workspace_id must equal the authorised workspace_id",
        )


async def _exists(conn: asyncpg.Connection, sql: str, *args: Any, what: str) -> None:
    if not await conn.fetchval(sql, *args):
        # 404, not 403: the caller must not learn whether the row exists in
        # some other workspace.
        raise TriggerRefused(status.HTTP_404_NOT_FOUND, f"{what} not found in this workspace")


def _prepare_project_scoped(validated: Any, scope: str | None) -> Any:
    _same_workspace(validated.workspace_id, scope)
    return validated


async def _check_project(conn: asyncpg.Connection, validated: Any, scope: str) -> None:
    await _exists(
        conn,
        "SELECT EXISTS (SELECT 1 FROM silver.projects "
        "WHERE project_id = $1::uuid AND workspace_id = $2::uuid)",
        str(validated.project_id), scope, what="project",
    )


def _prepare_export(validated: WorkspaceExportInput, scope: str | None) -> WorkspaceExportInput:
    _same_workspace(validated.workspace_id, scope)
    if validated.bucket != EXPORT_BUCKET:
        raise TriggerRefused(
            _UNPROCESSABLE,
            f"workspace exports are written to {EXPORT_BUCKET!r} only",
        )
    return validated


async def _check_workspace(conn: asyncpg.Connection, validated: Any, scope: str) -> None:
    await _exists(
        conn,
        "SELECT EXISTS (SELECT 1 FROM silver.workspaces WHERE workspace_id = $1::uuid)",
        scope, what="workspace",
    )


def _prepare_restore(validated: RestoreWorkspaceInput, scope: str | None) -> RestoreWorkspaceInput:
    _same_workspace(validated.workspace_id, scope)
    # Only a workspace_export object for THIS workspace. A file:// URI would
    # read the worker's own filesystem, and another workspace's key prefix
    # would restore that tenant's rows into this one.
    prefix = f"s3://{EXPORT_BUCKET}/{scope}/"
    uri = validated.snapshot_manifest_uri
    if not uri.startswith(prefix) or ".." in uri or len(uri) <= len(prefix):
        raise TriggerRefused(
            _UNPROCESSABLE,
            f"snapshot_manifest_uri must be a workspace export under {prefix}",
        )
    return validated


def _prepare_replay(validated: SupportReplayInput, scope: str | None) -> SupportReplayInput:
    if scope is None:
        raise TriggerRefused(_UNPROCESSABLE, "workspace_id is required")
    if not validated.dry_run:
        # The input model says a live replay needs operator AND workspace-
        # owner consent. No consent flow exists, so this route never
        # dispatches one.
        raise TriggerRefused(
            _UNPROCESSABLE,
            "support_replay is dispatched with dry_run=true only",
        )
    if validated.workspace_id is not None:
        _same_workspace(validated.workspace_id, scope)
    # Hand the workflow the workspace that was authorised here. Without it the
    # support agents look the ticket up under the default tenant to discover the
    # workspace, which the NOBYPASSRLS worker role can do only for a ticket in
    # the default tenant (ops.support_* is STRICT RLS) -- so a replay for any
    # other workspace was accepted at this route and then died in the worker.
    return validated.model_copy(update={"workspace_id": UUID(scope)})


async def _check_ticket(conn: asyncpg.Connection, validated: SupportReplayInput, scope: str) -> None:
    await _exists(
        conn,
        "SELECT EXISTS (SELECT 1 FROM ops.support_tickets "
        "WHERE ticket_id = $1::uuid AND workspace_id = $2::uuid)",
        str(validated.ticket_id), scope, what="support ticket",
    )


def _prepare_lineage(validated: AgentRunInput, scope: str | None) -> AgentRunInput:
    _same_workspace(validated.workspace_id, scope)
    kwargs = _validate_kwargs(LineageWalkKwargs, validated.kwargs)
    if kwargs["target_type"] == "workspace" and kwargs["target_id"] != scope:
        raise TriggerRefused(
            _UNPROCESSABLE,
            "a workspace lineage walk must target the authorised workspace",
        )
    return validated.model_copy(update={"kwargs": kwargs})


async def _check_lineage(conn: asyncpg.Connection, validated: AgentRunInput, scope: str) -> None:
    target_type = validated.kwargs["target_type"]
    target_id = validated.kwargs["target_id"]
    if target_type == "audit_ledger_entry":
        try:
            UUID(target_id)
        except ValueError as exc:
            raise TriggerRefused(
                _UNPROCESSABLE, "audit_ledger_entry target_id must be a UUID",
            ) from exc
        await _exists(
            conn,
            "SELECT EXISTS (SELECT 1 FROM audit.audit_ledger "
            "WHERE id = $1::uuid AND workspace_id = $2::uuid)",
            target_id, scope, what="audit ledger entry",
        )
    elif target_type == "workflow_run":
        await _exists(
            conn,
            "SELECT EXISTS (SELECT 1 FROM workflow.workflow_runs "
            "WHERE (run_id::text = $1 OR engine_run_id = $1) AND workspace_id = $2::uuid)",
            target_id, scope, what="workflow run",
        )


def _prepare_support_packet(validated: AgentRunInput, scope: str | None) -> AgentRunInput:
    _same_workspace(validated.workspace_id, scope)
    kwargs = _validate_kwargs(SupportPacketKwargs, validated.kwargs)
    return validated.model_copy(update={"kwargs": kwargs})


def _prepare_incident(validated: AgentRunInput, scope: str | None) -> AgentRunInput:
    if scope is not None or validated.workspace_id is not None:
        raise TriggerRefused(
            _UNPROCESSABLE,
            "llm_incident_diagnosis_run is platform-wide; send no workspace_id",
        )
    kwargs = _validate_kwargs(IncidentDiagnosisKwargs, validated.kwargs)
    return validated.model_copy(update={"kwargs": kwargs})


# =============================================================================
# Registry
# =============================================================================
@dataclass(frozen=True)
class TriggerSpec:
    workflow: Any  # hatchet_sdk Workflow; its generics add nothing here
    input_model: type[BaseModel]
    requires_workspace: bool
    prepare: Prepare
    check: Check | None = None


TRIGGERS: dict[str, TriggerSpec] = {
    "generate_report": TriggerSpec(
        generate_report, GenerateReportInput, True, _prepare_project_scoped, _check_project,
    ),
    "score_targets": TriggerSpec(
        score_targets, ScoreTargetsInput, True, _prepare_project_scoped, _check_project,
    ),
    "workspace_export": TriggerSpec(
        workspace_export, WorkspaceExportInput, True, _prepare_export, _check_workspace,
    ),
    "restore_workspace": TriggerSpec(
        restore_workspace, RestoreWorkspaceInput, True, _prepare_restore, _check_workspace,
    ),
    "support_replay": TriggerSpec(
        support_replay, SupportReplayInput, True, _prepare_replay, _check_ticket,
    ),
    "lineage_walk": TriggerSpec(
        lineage_walk, AgentRunInput, True, _prepare_lineage, _check_lineage,
    ),
    "support_packet_assemble": TriggerSpec(
        support_packet_assemble, AgentRunInput, True, _prepare_support_packet, _check_workspace,
    ),
    "llm_incident_diagnosis_run": TriggerSpec(
        llm_incident_diagnosis_run, AgentRunInput, False, _prepare_incident,
    ),
}


# =============================================================================
# Route
# =============================================================================
@router.post(
    "/{workflow_name}/trigger",
    response_model=WorkflowTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_service_key)],
)
async def trigger_workflow(
    workflow_name: str,
    body: WorkflowTriggerRequest,
    request: Request,
) -> WorkflowTriggerResponse:
    """Validate, scope-check and enqueue one workflow run (202, does not wait)."""
    spec = TRIGGERS.get(workflow_name)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown workflow")

    scope = str(body.workspace_id) if body.workspace_id is not None else None
    if spec.requires_workspace and scope is None:
        raise HTTPException(
            status_code=_UNPROCESSABLE,
            detail=f"{workflow_name} requires workspace_id",
        )

    try:
        validated = spec.input_model.model_validate(body.input)
    except ValidationError as exc:
        raise HTTPException(
            status_code=_UNPROCESSABLE,
            detail={
                "workflow": workflow_name,
                "errors": exc.errors(include_url=False, include_context=False),
            },
        ) from exc

    try:
        validated = spec.prepare(validated, scope)
        if spec.check is not None and scope is not None:
            async with scoped_connection(
                request.app.state.pg_pool,
                workspace_id=scope,
                site=f"routers.workflow_trigger.{workflow_name}",
            ) as conn:
                await spec.check(conn, validated, scope)
    except TriggerRefused as refused:
        log.info(
            "workflow_trigger refused: workflow=%s workspace=%s status=%d detail=%s",
            workflow_name, scope, refused.status_code, refused.detail,
        )
        raise HTTPException(status_code=refused.status_code, detail=refused.detail) from refused

    redis = getattr(request.app.state, "redis_client", None)
    claim = await _claim_request_id(redis, workflow_name, scope, validated)
    if isinstance(claim, _AlreadyDispatched):
        log.info(
            "workflow_trigger duplicate: workflow=%s run=%s workspace=%s requested_by=%s",
            workflow_name, claim.run_id, scope, body.requested_by,
        )
        return WorkflowTriggerResponse(
            workflow=workflow_name, workflow_run_id=claim.run_id, workspace_id=scope,
        )

    # If this raises, the claim is left in place until it expires. Whether the
    # engine started the run is unknown (a deadline can expire after it did), and
    # a second run is the one thing the claim exists to prevent. Nobody retries
    # with this id: Laravel mints a new one per click.
    ref = await spec.workflow.aio_run_no_wait(validated)
    await _remember_request_id(redis, claim, ref.workflow_run_id)
    log.info(
        "workflow_trigger dispatched: workflow=%s run=%s workspace=%s requested_by=%s",
        workflow_name, ref.workflow_run_id, scope, body.requested_by,
    )
    return WorkflowTriggerResponse(
        workflow=workflow_name,
        workflow_run_id=ref.workflow_run_id,
        workspace_id=scope,
    )


# =============================================================================
# Request-id dedupe
# =============================================================================
#: The input field in which Laravel mints a request id for each click, by
#: workflow. Workflows not listed mint none; the Laravel cooldown is their only
#: guard against a double dispatch.
REQUEST_ID_FIELDS: dict[str, str] = {
    "generate_report": "export_request_id",
    "score_targets": "score_request_id",
    "restore_workspace": "restore_request_id",
    "support_replay": "replay_request_id",
}

#: How long a request id is remembered. Well past any retry of one POST; short
#: enough that the keyspace stays small.
DEDUPE_TTL_SECONDS = 3600

#: Value of a claimed key until its dispatch returns a run id.
_IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class _AlreadyDispatched:
    run_id: str


def _dedupe_key(workflow_name: str, scope: str | None, validated: Any) -> str | None:
    field = REQUEST_ID_FIELDS.get(workflow_name)
    request_id = getattr(validated, field, None) if field else None
    if request_id is None:
        return None
    # The workspace is part of the key: a request id minted for one workspace
    # must never answer with a run reference from another.
    return f"workflow_trigger:{workflow_name}:{scope or '-'}:{str(request_id).lower()}"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


async def _claim_request_id(
    redis: Any, workflow_name: str, scope: str | None, validated: Any,
) -> str | _AlreadyDispatched | None:
    """Claim the request id before dispatching; the key to settle it, or the answer.

    A POST whose id was already claimed must not start a second run: Laravel
    retries a dispatch it could not confirm, and the same click can arrive twice.
    ``SET NX`` makes the claim atomic, so of two concurrent copies exactly one
    dispatches. Returns

    * the Redis key when this request owns the dispatch (settle it with
      ``_remember_request_id``),
    * ``_AlreadyDispatched`` with the first run's id when an earlier request
      already started one (the caller answers with that run, as the first did),
    * ``None`` when there is nothing to dedupe on, or Redis cannot be used. The
      dedupe is a safety net under the Laravel cooldown, not a gate: with Redis
      down a trigger still dispatches, and the failure is logged.

    409 while the first request is still dispatching.
    """
    key = _dedupe_key(workflow_name, scope, validated) if redis is not None else None
    if key is None:
        return None
    try:
        if await redis.set(key, _IN_PROGRESS, ex=DEDUPE_TTL_SECONDS, nx=True):
            return key
        previous = _text(await redis.get(key))
    except Exception:  # noqa: BLE001 — a dedupe outage must not stop the trigger
        log.warning(
            "workflow_trigger: request-id dedupe unavailable, dispatching without it: "
            "workflow=%s workspace=%s", workflow_name, scope, exc_info=True,
        )
        return None
    if previous is None:
        # Expired between the two calls: nothing to answer with, nothing to settle.
        return None
    if previous == _IN_PROGRESS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a trigger with this request id was already received and has no run yet",
        )
    return _AlreadyDispatched(previous)


async def _remember_request_id(redis: Any, claim: str | None, run_id: str) -> None:
    """Replace the claim with the run it produced, so a repeat gets that run back."""
    if claim is None:
        return
    try:
        await redis.set(claim, run_id, ex=DEDUPE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — the run exists; do not fail the response over its bookkeeping
        log.warning("workflow_trigger: could not record run %s under %s", run_id, claim, exc_info=True)
