"""support_replay Hatchet workflow (§10.10 / §25.1).

Doc-phase 98 skeleton → doc-phase 146 graduation.

Per §25.1 the workflow re-executes a failed run with the same inputs
in dry-run mode so support can identify root cause. Today's graduation
takes the **practical interpretation**: instead of attempting a real
dry-run re-execution of the original Hatchet workflow (which needs
deeper Hatchet APIs for fetching past run inputs + dispatching child
runs), we **run the §25.4 support-agent chain** against the ticket
the replay is for, producing a `diff_summary` from the chain results.

Real workflow re-execution lands when Hatchet's run-replay API
integration ships; until then this graduation gives operators an
observable replay-row + audit anchor + diagnostic context.

What's live in this graduation:

  - Inserts a row into ops.support_replay_runs (status='running')
  - Runs triage → investigate → packet → draft → route via the
    §25.4 chain (doc-phases 136 / 139 / 140 / 143 / 144)
  - Composes `diff_summary` from chain outcomes
  - UPDATEs the replay_runs row with status='completed' + completed_at
  - Emits a `support.replay.completed` audit anchor

Dry run (2026-10 Hatchet audit, finding 7). ``dry_run`` is threaded into every
agent of the chain. A dry run reads what the chain reads and returns what it
would have done, but writes NOTHING to the ticket: no re-triage UPDATE, no trace
link, no ``support.packet.assembled`` / ``...triaged`` / ``...investigated`` /
``...response_drafted`` / ``...escalation_routed`` anchor, no drafted response,
no assignment, no row lock. The agents run in a READ ONLY transaction, so a code
path that forgot the flag fails instead of mutating a live ticket. The replay's
own bookkeeping row and its ``support.replay.completed`` anchor are still
written: they are the record that support looked, not a change to the ticket.
Before this, a "dry run" re-triaged the ticket, overwrote its customer-visible
draft and (when asked) reassigned it.

Idempotency (same finding). ``ops.support_replay_runs.replay_request_id`` is
UNIQUE and the INSERT is ``ON CONFLICT DO NOTHING``: a second dispatch with the
same key (a Laravel retry after a read timeout, a Hatchet re-delivery) finds the
first run's row and returns it instead of running the chain again. The task has
``retries=0`` and never re-runs by itself, so this key is what stops a retried
*dispatch* from doubling every audit anchor.

A row never stays 'running'. The body closes it as 'failed' on ANY exit that
is not a normal completion, ``BaseException`` included (a cancellation is a
``CancelledError``), and ``on_failure`` closes it when the body never got the
chance (timeout, worker loss).

Workspace scope (same audit, finding 22). ``ops.support_*`` carry a STRICT RLS
policy. The agents used to find a ticket's workspace by reading it under the
legacy default tenant, which the AWS role (NOBYPASSRLS) can do only for a ticket
IN the default tenant. The trigger route already knows the workspace -- it
checked the ticket belongs to it -- so it now sends ``workspace_id`` in the
input and every step is scoped straight to it. See
``app.services.support_cockpit._scope``.

Trigger. Since 2026-09-29 (HAT-13) an admin who belongs to the ticket's
workspace starts it with Laravel
``POST /api/v1/admin/workspaces/{workspace}/workflows/support_replay``, which
calls FastAPI ``POST /internal/v1/workflows/support_replay/trigger``. That
route checks the ticket belongs to the workspace and only dispatches
``dry_run=true``, because no consent flow exists for a live replay. No cron.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, NamedTuple
from uuid import UUID

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.db import BareConnectionError, scoped_connection
from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet
from app.services.support_cockpit._scope import ticket_connection
from app.services.support_cockpit.customer_response_drafting import (
    draft_customer_response,
)
from app.services.support_cockpit.escalation_routing import (
    route_escalation,
)
from app.services.support_cockpit.root_cause_investigation import (
    investigate_ticket,
)
from app.services.support_cockpit.support_packet import (
    build_support_packet,
)
from app.services.support_cockpit.ticket_triage import triage_ticket

log = logging.getLogger("georag.hatchet.support_replay")

DRY_RUN_NOTE = "dry run: nothing was written to the ticket"


# =============================================================================
# IO models
# =============================================================================
class SupportReplayInput(BaseModel):
    ticket_id: UUID
    original_workflow_run_id: str = Field(
        ..., description="Hatchet run id of the workflow being replayed."
    )
    initiated_by_user_id: int = Field(
        ..., description="ops user driving the replay."
    )
    dry_run: bool = Field(
        default=True,
        description="If true, side-effect-bearing steps are skipped or "
                    "stubbed. Default true; false requires explicit "
                    "operator + workspace-owner consent.",
    )
    replay_request_id: UUID = Field(
        ...,
        description="Idempotency key. ops.support_replay_runs.replay_request_id "
                    "is UNIQUE: a second dispatch with the same key returns the "
                    "first run's row and runs nothing.",
    )
    workspace_id: UUID | None = Field(
        default=None,
        description="The workspace the dispatcher authorised the ticket for "
                    "(POST /internal/v1/workflows/support_replay/trigger fills "
                    "it from its own scope). Given, every step is scoped "
                    "straight to it. Omitted -- a run started by hand from the "
                    "Hatchet UI -- the workspace is discovered under the "
                    "default tenant, which a NOBYPASSRLS role can do only for "
                    "a ticket in the default tenant.",
    )


class SupportReplayOutput(BaseModel):
    replay_id: UUID
    success: bool
    diff_summary: str | None = None
    replay_workflow_run_id: str | None = None
    error: str | None = None
    triage_decision: str | None = None
    investigation_trace_id: str | None = None
    response_word_count: int | None = None
    routing_decision: str | None = None
    duplicate: bool = Field(
        default=False,
        description="True when this dispatch repeated a replay_request_id that "
                    "had already been claimed: nothing ran, and the fields "
                    "above describe the first run's row.",
    )


# =============================================================================
# Workflow registration
# =============================================================================
support_replay = hatchet.workflow(
    name="support_replay",
    input_validator=SupportReplayInput,
)


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_dsn = build_dsn


# =============================================================================
# Replay-row bookkeeping
# =============================================================================
_TICKET_LOOKUP_SQL = """
    SELECT ticket_id::text AS ticket_id,
           workspace_id::text AS workspace_id
      FROM ops.support_tickets
     WHERE ticket_id = $1::uuid
"""

# ON CONFLICT (replay_request_id) names the unique index
# support_replay_runs_request_id_uq. No row back means the key was already
# claimed, by this ticket's earlier dispatch or by another workspace's row this
# scope cannot read.
_CLAIM_SQL = """
    INSERT INTO ops.support_replay_runs (
        ticket_id, original_workflow_run_id, dry_run,
        initiated_by_user_id, status, workspace_id, replay_request_id
    )
    VALUES ($1::uuid, $2, $3, $4, 'running', $5::uuid, $6::uuid)
    ON CONFLICT (replay_request_id) DO NOTHING
    RETURNING replay_id
"""

_EXISTING_SQL = """
    SELECT replay_id, ticket_id::text AS ticket_id, status, diff_summary,
           replay_workflow_run_id
      FROM ops.support_replay_runs
     WHERE replay_request_id = $1::uuid
"""

# Conditional on status = 'running': a row that reached a terminal state stays
# there, whichever of the body, the failure hook or a late completion gets here
# first.
_FAIL_BY_ID_SQL = """
    UPDATE ops.support_replay_runs
       SET status = 'failed',
           diff_summary = COALESCE(diff_summary, $2),
           completed_at = now()
     WHERE replay_id = $1::uuid
       AND status = 'running'
 RETURNING replay_id
"""

_FAIL_BY_REQUEST_SQL = """
    UPDATE ops.support_replay_runs
       SET status = 'failed',
           diff_summary = COALESCE(diff_summary, $2),
           completed_at = now()
     WHERE replay_request_id = $1::uuid
       AND status = 'running'
 RETURNING replay_id
"""


class _Claim(NamedTuple):
    workspace_id: str
    #: Set when THIS dispatch created the row and so owns running the chain.
    replay_id: UUID | None
    #: Set when the replay_request_id had already been claimed.
    existing: asyncpg.Record | None


async def _claim_replay(pool: asyncpg.Pool, input: SupportReplayInput) -> _Claim:
    """Read the ticket under its workspace and insert the replay row, once.

    The row is written scoped to the ticket's workspace, with ``workspace_id``
    set: the raw RLS layer makes the column NOT NULL, and the INSERT used to
    leave it out.
    """
    ticket_str = str(input.ticket_id)
    request_str = str(input.replay_request_id)
    async with ticket_connection(
        pool,
        ticket_id=ticket_str,
        lookup_sql=_TICKET_LOOKUP_SQL,
        site="support_replay.ticket_lookup",
        bootstrap_reason="support_replay.bootstrap_lookup",
        workspace_id=input.workspace_id,
    ) as (conn, ticket_row):
        ticket_ws: str = ticket_row["workspace_id"]
        replay_id = await conn.fetchval(
            _CLAIM_SQL,
            ticket_str,
            input.original_workflow_run_id,
            input.dry_run,
            input.initiated_by_user_id,
            ticket_ws,
            request_str,
        )
        if replay_id is not None:
            return _Claim(ticket_ws, replay_id, None)
        existing = await conn.fetchrow(_EXISTING_SQL, request_str)
    if existing is None or existing["ticket_id"] != ticket_str:
        # A key that is not this ticket's: a caller bug, or a collision with
        # another workspace's row. Running would hide it; fail loud instead.
        raise RuntimeError(
            f"replay_request_id {request_str} is already in use by a replay of "
            f"a different ticket; refusing to run ticket {ticket_str}"
        )
    return _Claim(ticket_ws, None, existing)


def _duplicate_output(existing: asyncpg.Record) -> SupportReplayOutput:
    failed = existing["status"] == "failed"
    return SupportReplayOutput(
        replay_id=existing["replay_id"],
        success=not failed,
        diff_summary=existing["diff_summary"],
        replay_workflow_run_id=existing["replay_workflow_run_id"],
        error="the first dispatch of this replay_request_id failed" if failed else None,
        duplicate=True,
    )


async def _close_row_as_failed(
    pool: asyncpg.Pool, *, workspace_id: str, replay_id: UUID, reason: str,
) -> bool:
    """Close a 'running' replay row as failed. Never raises, never masks.

    Called while an exception -- possibly a cancellation -- is already in
    flight, so it is shielded from that cancellation and swallows its own
    failure: the original exception is the one that matters.
    """
    async def _update() -> bool:
        async with scoped_connection(
            pool, workspace_id=workspace_id, site="support_replay.mark_failed",
        ) as conn:
            return await conn.fetchval(_FAIL_BY_ID_SQL, str(replay_id), reason[:2000]) is not None

    try:
        closed = await asyncio.shield(_update())
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 — see docstring
        log.error(
            "support_replay: could not close replay row %s as failed (%s): %s",
            replay_id, reason, exc,
        )
        return False
    if closed:
        log.warning("support_replay: replay %s closed as failed: %s", replay_id, reason)
    return closed


# =============================================================================
# The task
# =============================================================================
@support_replay.task(execution_timeout="1h", retries=0)
async def execute(input: SupportReplayInput, ctx: Context) -> SupportReplayOutput:
    """Run the §25.4 support-agent chain as a replay diagnostic.

    Doc-phase 146 graduation. Inserts a support_replay_runs row,
    invokes the 5-stage chain against the ticket, composes a
    diff_summary, marks the row completed.
    """
    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        claim = await _claim_replay(pool, input)
        if claim.replay_id is None:
            assert claim.existing is not None
            log.info(
                "support_replay.duplicate_dispatch replay_request_id=%s replay_id=%s "
                "status=%s -- nothing run",
                input.replay_request_id, claim.existing["replay_id"],
                claim.existing["status"],
            )
            return _duplicate_output(claim.existing)

        try:
            return await _run_replay(
                pool, input, workspace_id=claim.workspace_id, replay_id=claim.replay_id,
            )
        except BaseException as exc:
            # Any exit that is not a normal return -- a database error in the
            # bookkeeping below, a cancellation (CancelledError is a
            # BaseException), the chain failure re-raised on purpose -- must not
            # leave the row 'running'. Conditional on status, so a row that
            # already reached a terminal state is left alone.
            await _close_row_as_failed(
                pool,
                workspace_id=claim.workspace_id,
                replay_id=claim.replay_id,
                reason=f"{type(exc).__name__}: {exc}",
            )
            raise
    finally:
        await pool.close()


async def _run_replay(
    pool: asyncpg.Pool,
    input: SupportReplayInput,
    *,
    workspace_id: str,
    replay_id: UUID,
) -> SupportReplayOutput:
    ticket_str = str(input.ticket_id)
    dry_run = input.dry_run
    # Every agent gets the workspace the dispatcher authorised (no discovery under
    # the default tenant) and the dry_run flag (no writes, READ ONLY transaction).
    scope: dict[str, Any] = {
        "pool": pool, "workspace_id": workspace_id, "dry_run": dry_run,
    }

    log.info(
        "support_replay.task_started replay_id=%s ticket=%s dry_run=%s",
        replay_id, ticket_str, dry_run,
    )

    # 2. Run the §25.4 chain. Each step is wrapped in try/except
    #    so a failure mid-chain still produces a useful diff_summary.
    chain_steps: list[str] = []
    triage_decision: str | None = None
    investigation_trace_id: str | None = None
    response_word_count: int | None = None
    routing_decision: str | None = None
    error: str | None = None

    try:
        # Re-triage may be a no-op if already triaged (refuses on
        # closed/resolved). Catch + carry on.
        try:
            t = await triage_ticket(ticket_id=input.ticket_id, **scope)
            triage_decision = (
                f"{t.prior_severity}/{t.prior_category} → "
                f"{t.new_severity}/{t.new_category}"
            )
            chain_steps.append(f"triage: {triage_decision}")
        except ValueError as e:
            chain_steps.append(f"triage: skipped ({e})")

        inv = await investigate_ticket(
            ticket_id=input.ticket_id,
            actor_user_id=input.initiated_by_user_id,
            **scope,
        )
        # A dry run never links the trace, so its id points at nothing: leave it
        # out rather than hand an operator an id they can not look up.
        investigation_trace_id = None if dry_run else inv.trace_id
        chain_steps.append(
            f"investigation: {inv.top_cause_summary[:100]}"
        )

        pkt = await build_support_packet(ticket_id=input.ticket_id, **scope)
        anchor = (
            f"anchor={str(pkt.packet_anchor_id)[:8]}"
            if pkt.packet_anchor_id is not None else "not anchored"
        )
        chain_steps.append(
            f"packet: {anchor} "
            f"({len(pkt.triage_anchors)} triage, "
            f"{len(pkt.investigation_anchors)} invest)"
        )

        drf = await draft_customer_response(
            ticket_id=input.ticket_id,
            actor_user_id=input.initiated_by_user_id,
            **scope,
        )
        response_word_count = drf.response_word_count
        chain_steps.append(f"draft: {drf.response_word_count} words")

        esc = await route_escalation(
            ticket_id=input.ticket_id,
            actor_user_id=input.initiated_by_user_id,
            **scope,
        )
        routing_decision = esc.decision
        chain_steps.append(f"routing: {esc.decision}")

    except Exception as e:  # noqa: BLE001 — catch-all is intentional
        log.warning(
            "support_replay.chain_failed replay_id=%s err=%s",
            replay_id, e,
        )
        error = str(e)

    summary_parts = ([DRY_RUN_NOTE] if dry_run else []) + chain_steps
    if error is not None:
        summary_parts.append(f"failed: {error}")
    diff_summary = " | ".join(summary_parts) if summary_parts else None
    success = error is None

    # 3. Mark the replay completed.
    replay_workflow_run_id = f"replay_{replay_id.hex[:16]}"
    # Re-acquire conn scoped to the ticket's workspace (the claim's
    # transaction already closed). REC#2 scoped_connection handles the GUC
    # bind atomically + parameterises the UUID. Conditional on 'running': a
    # row the failure hook already closed stays closed.
    async with scoped_connection(
        pool,
        workspace_id=workspace_id,
        site="support_replay.completion",
    ) as conn:
        await conn.execute(
            """
            UPDATE ops.support_replay_runs
               SET status = $1,
                   diff_summary = $2,
                   replay_workflow_run_id = $3,
                   completed_at = now()
             WHERE replay_id = $4::uuid
               AND status = 'running'
            """,
            "completed" if success else "failed",
            diff_summary,
            replay_workflow_run_id,
            str(replay_id),
        )

        # 4. Audit anchor (cross-workspace ops access per §25.3).
        await emit_audit(
            conn,
            action_type="support.replay.completed",
            actor_id=input.initiated_by_user_id,
            actor_kind="agent",
            target_schema="ops",
            target_table="support_replay_runs",
            target_id=str(replay_id),
            payload={
                "evaluator": "synthetic_stub",
                "doc_phase": 146,
                "ticket_id": ticket_str,
                "original_workflow_run_id": input.original_workflow_run_id,
                "dry_run": dry_run,
                "success": success,
                "chain_steps_count": len(chain_steps),
                "triage_decision": triage_decision,
                "investigation_trace_id": investigation_trace_id,
                "response_word_count": response_word_count,
                "routing_decision": routing_decision,
                "error": error,
            },
        )

    log.info(
        "support_replay.task_completed replay_id=%s success=%s "
        "chain_steps=%d routing=%s",
        replay_id, success, len(chain_steps), routing_decision,
    )

    # Phase 3 — admin.support-cockpit surface refresh. The Foundry/
    # SupportCockpit page reads ops.support_replay_runs (indirectly
    # via audit + query_audit_log); on a fresh replay the traces list
    # should re-fetch so operators see the new run land. Best-effort.
    try:
        from app.services.laravel_bridge import post_admin_surface_updated
        await post_admin_surface_updated(
            surface="support-cockpit",
            affected_props=["traces"],
            payload={
                "replay_id": str(replay_id),
                "success": success,
                "routing_decision": routing_decision,
                "chain_steps": len(chain_steps),
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "support_replay: admin.support-cockpit broadcast failed "
            "replay_id=%s err=%s", replay_id, exc,
        )

    if not success:
        # The row says 'failed' and the anchor carries the error; the run has to
        # say so too. Returning success=False left Hatchet reporting a failed
        # replay as a green run (2026-10 audit, finding 17).
        raise RuntimeError(f"support_replay chain failed: {error}")

    return SupportReplayOutput(
        replay_id=replay_id,
        success=success,
        diff_summary=diff_summary,
        replay_workflow_run_id=replay_workflow_run_id,
        error=error,
        triage_decision=triage_decision,
        investigation_trace_id=investigation_trace_id,
        response_word_count=response_word_count,
        routing_decision=routing_decision,
    )


# =============================================================================
# Failure hook
# =============================================================================
def _failure_reason(ctx: object | None) -> str:
    """Why the run died, from Hatchet's own per-task error map."""
    try:
        errors = getattr(ctx, "task_run_errors", None) or {}
    except Exception:  # noqa: BLE001 — diagnostics must not block the hook
        errors = {}
    if errors:
        return "; ".join(f"{name}: {msg}" for name, msg in errors.items())[:2000]
    return (
        "no task_run_errors available (worker crash/cancellation with no "
        "captured exception)"
    )


async def close_replay_after_workflow_failure(
    input: SupportReplayInput, ctx: object | None,
) -> dict[str, Any]:
    """Close the replay row the body left 'running'.

    The body closes its own row on every exit it gets to run. This is the backstop
    for the ones it does not: the 1-hour execution timeout, a worker killed
    mid-run, a cancel that lands before the body starts. The row is found by the
    run's ``replay_request_id`` -- the body's own ``replay_id`` is generated
    inside the task and is not in the workflow input. A no-op on a row that is
    already terminal, and on a run that never claimed one.
    """
    reason = _failure_reason(ctx)
    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=1, statement_cache_size=0
    )
    try:
        try:
            workspace_id = (
                str(input.workspace_id) if input.workspace_id is not None else None
            )
            if workspace_id is None:
                async with ticket_connection(
                    pool,
                    ticket_id=str(input.ticket_id),
                    lookup_sql=_TICKET_LOOKUP_SQL,
                    site="support_replay.on_failure",
                    bootstrap_reason="support_replay.bootstrap_lookup",
                ) as (_conn, ticket_row):
                    workspace_id = ticket_row["workspace_id"]
            async with scoped_connection(
                pool, workspace_id=workspace_id, site="support_replay.on_failure",
            ) as conn:
                replay_id = await conn.fetchval(
                    _FAIL_BY_REQUEST_SQL, str(input.replay_request_id), reason,
                )
        except BareConnectionError as exc:
            # No such ticket in scope: the body never claimed a row.
            log.warning(
                "support_replay.on_failure: nothing to close for request %s: %s",
                input.replay_request_id, exc,
            )
            return {"updated": False, "reason": "no_ticket"}
    finally:
        await pool.close()
    if replay_id is None:
        return {"updated": False, "reason": "no_running_row"}
    log.warning(
        "support_replay.on_failure: closed replay %s as failed: %s", replay_id, reason,
    )
    return {"updated": True, "replay_id": str(replay_id)}


@support_replay.on_failure_task(
    name="on_failure",
    execution_timeout="30s",
    schedule_timeout="30m",
    retries=2,
)
async def on_failure(input: SupportReplayInput, ctx: Context) -> dict[str, Any]:
    """Backstop: never leave a replay row 'running' after the run has died."""
    return await close_replay_after_workflow_failure(input, ctx)
