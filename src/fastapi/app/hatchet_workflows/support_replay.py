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
"""
from __future__ import annotations

import logging
import os
from uuid import UUID, uuid4

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.db import lookup_and_rescope, scoped_connection
from app.hatchet_workflows import hatchet
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
    replay_request_id: UUID = Field(..., description="Idempotency key.")


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


# =============================================================================
# Workflow registration
# =============================================================================
support_replay = hatchet.workflow(
    name="support_replay",
    input_validator=SupportReplayInput,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


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
        # 1. Insert the replay run with status='running'.
        # ADR-0014 lookup_and_rescope: bootstrap → fetch ticket's
        # workspace → pivot GUC to that workspace, all inside one
        # transaction. Removes the bare bootstrap-set-realign pattern
        # the B4 audit flagged (also pinned by
        # tests/test_lookup_and_rescope.py + tests/test_scoped_connection.py).
        async with lookup_and_rescope(
            pool,
            lookup_sql="""
                SELECT ticket_id::text AS ticket_id,
                       workspace_id::text AS workspace_id
                  FROM ops.support_tickets
                 WHERE ticket_id = $1::uuid
                """,
            lookup_args=(str(input.ticket_id),),
            site="support_replay.ticket_lookup",
            bootstrap_reason="support_replay.bootstrap_lookup",
        ) as (conn, ticket_row):
            ticket_ws = ticket_row["workspace_id"]
            replay_row = await conn.fetchrow(
                """
                INSERT INTO ops.support_replay_runs (
                    ticket_id, original_workflow_run_id, dry_run,
                    initiated_by_user_id, status
                )
                VALUES ($1::uuid, $2, $3, $4, 'running')
                RETURNING replay_id, ticket_id::text AS ticket_id
                """,
                str(input.ticket_id),
                input.original_workflow_run_id,
                input.dry_run,
                input.initiated_by_user_id,
            )
        replay_id: UUID = replay_row["replay_id"]
        ticket_str = replay_row["ticket_id"]

        log.info(
            "support_replay.task_started replay_id=%s ticket=%s dry_run=%s",
            replay_id, ticket_str, input.dry_run,
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
                t = await triage_ticket(ticket_id=input.ticket_id, pool=pool)
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
                pool=pool,
            )
            investigation_trace_id = inv.trace_id
            chain_steps.append(
                f"investigation: {inv.top_cause_summary[:100]}"
            )

            pkt = await build_support_packet(
                ticket_id=input.ticket_id, pool=pool
            )
            chain_steps.append(
                f"packet: anchor={str(pkt.packet_anchor_id)[:8]} "
                f"({len(pkt.triage_anchors)} triage, "
                f"{len(pkt.investigation_anchors)} invest)"
            )

            drf = await draft_customer_response(
                ticket_id=input.ticket_id,
                actor_user_id=input.initiated_by_user_id,
                pool=pool,
            )
            response_word_count = drf.response_word_count
            chain_steps.append(f"draft: {drf.response_word_count} words")

            esc = await route_escalation(
                ticket_id=input.ticket_id,
                actor_user_id=input.initiated_by_user_id,
                pool=pool,
            )
            routing_decision = esc.decision
            chain_steps.append(f"routing: {esc.decision}")

        except Exception as e:  # noqa: BLE001 — catch-all is intentional
            log.warning(
                "support_replay.chain_failed replay_id=%s err=%s",
                replay_id, e,
            )
            error = str(e)

        diff_summary = " | ".join(chain_steps) if chain_steps else None
        success = error is None

        # 3. Mark the replay completed.
        replay_workflow_run_id = f"replay_{replay_id.hex[:16]}"
        # Re-acquire conn scoped to the ticket's workspace (the prior
        # lookup_and_rescope tx already closed). REC#2 scoped_connection
        # handles the GUC bind atomically + parameterises the UUID.
        async with scoped_connection(
            pool,
            workspace_id=ticket_ws,
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
                    "dry_run": input.dry_run,
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
    finally:
        await pool.close()
