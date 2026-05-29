"""Phase 2 Step 3 — placeholder Hatchet workflow used by the Step 3
``integrations_trigger`` smoke.

A flow-registry-shaped no-op so the route can dispatch *something* without
waiting on Step 4's first real flow. The Step 3 verifier runs this to
confirm the auth + registry + Hatchet handoff path works end-to-end.

Step 4's ``public_geoscience_pull`` workflow lands later and replaces this
as the canonical first flow; the placeholder stays in the registry as a
diagnostic surface during Phase 2 development (operators can hit it from
Kestra' HTTP piece when debugging connectivity).

Pool: ``ai`` (no I/O — fast). Action: ``phase2_smoke``.
"""

from __future__ import annotations

import logging

from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet


log = logging.getLogger("georag.hatchet.phase2_smoke")


class Phase2SmokeInput(BaseModel):
    """Generic dict input — Kestra' HTTP piece sends arbitrary JSON."""

    flow_name: str = Field(default="phase2_smoke")
    note: str | None = Field(default=None, description="Free-form caller note for tracing.")


class Phase2SmokeOut(BaseModel):
    flow_name: str
    note: str | None
    workflow_run_id: str


phase2_smoke = hatchet.workflow(
    name="phase2_smoke",
    input_validator=Phase2SmokeInput,
)


@phase2_smoke.task(execution_timeout="30s", retries=0)
async def echo(input: Phase2SmokeInput, ctx: Context) -> Phase2SmokeOut:
    log.info(
        "phase2_smoke fired flow_name=%s note=%s run_id=%s",
        input.flow_name, input.note, ctx.workflow_run_id,
    )
    return Phase2SmokeOut(
        flow_name=input.flow_name,
        note=input.note,
        workflow_run_id=ctx.workflow_run_id,
    )


__all__ = ["phase2_smoke", "Phase2SmokeInput", "Phase2SmokeOut"]
