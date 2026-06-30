"""LLM Incident Diagnosis Agent (Phase 0 agent #10, R0, LLM-calling).

Dispatched on Prometheus alert webhook. Phase 0 also exposes it as an
on-demand endpoint at ``/api/v1/incidents/diagnose``.

Pulls last 1h of Langfuse traces (HTTP API), recent ``workflow_runs``,
and prompt_versions in production at the time of the alert. Sends the
context to the project's vLLM via ``_call_openai_compatible_llm`` using
the ``chat_deep`` profile. Returns structured JSON validated by
Pydantic.

Refusal contract: if the supplied context is empty AND the alert label
is unfamiliar, the agent raises ``AgentRefusalError`` so the wrapper
records ``outcome='refusal'`` (NOT 'failure') and the circuit breaker
does not trip.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.agents import AgentContext, georag_agent
from app.agents.exceptions import AgentRefusalError
from app.agents.runtime import get_runtime

logger = logging.getLogger(__name__)


class IncidentDiagnosis(BaseModel):
    hypothesis: str = Field(..., min_length=1)
    supporting_evidence: list[str] = Field(default_factory=list)
    suggested_mitigations: list[str] = Field(default_factory=list)


async def _resolve_pinned_prompt(pg, agent_name: str) -> dict[str, Any] | None:
    row = await pg.fetchrow(
        """
        SELECT pv.id AS prompt_version_id, pv.prompt_id, pv.version,
               pv.text, pv.parameters, pv.promotion_state
        FROM workspace.agent_prompt_pins pin
        JOIN workspace.prompt_versions pv ON pv.id = pin.prompt_version_id
        WHERE pin.agent_name = $1
        """,
        agent_name,
    )
    return dict(row) if row else None


async def _fetch_langfuse_traces(
    client: httpx.AsyncClient, since: datetime, limit: int
) -> list[dict[str, Any]]:
    host = (
        os.environ.get("LANGFUSE_HOST")
        or os.environ.get("LANGFUSE_HOST_URL")
        or "http://langfuse-web:3000"
    ).rstrip("/")
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not public or not secret:
        logger.info("incident_diagnosis: Langfuse keys unset — skipping trace fetch")
        return []
    creds = base64.b64encode(f"{public}:{secret}".encode()).decode()
    try:
        r = await client.get(
            f"{host}/api/public/traces",
            params={
                "fromTimestamp": since.isoformat(),
                "limit": limit,
            },
            headers={"Authorization": f"Basic {creds}"},
        )
        if r.status_code == 200:
            payload = r.json()
            return payload.get("data") or []
    except httpx.HTTPError as exc:
        logger.warning("incident_diagnosis: Langfuse fetch failed: %s", exc)
    return []


@georag_agent(
    name="LLM Incident Diagnosis Agent",
    risk_tier="R0",
    version="0.1.0",
)
async def llm_incident_diagnosis_run(
    ctx: AgentContext,
    *,
    alert_label: str,
    window_minutes: int = 60,
    trace_limit: int = 25,
    runs_limit: int = 25,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    rt = get_runtime()

    since = datetime.now(UTC) - timedelta(minutes=window_minutes)

    # ---- Gather context ---------------------------------------------------
    workflow_rows = await rt.pg_pool.fetch(
        """
        SELECT run_id, workflow_kind, status, started_at, ended_at,
               failure_reason, trace_id, workspace_id
        FROM workflow.workflow_runs
        WHERE started_at >= $1
        ORDER BY started_at DESC
        LIMIT $2
        """,
        since,
        runs_limit,
    )
    prompt_rows = await rt.pg_pool.fetch(
        """
        SELECT prompt_id, version, promotion_state, promoted_at
        FROM workspace.prompt_versions
        WHERE promotion_state = 'production'
        """
    )
    pinned = await _resolve_pinned_prompt(rt.pg_pool, "LLM Incident Diagnosis Agent")
    if pinned is None or not pinned.get("text"):
        # The Step 6 supplement seeds this; treat absence as a config gap.
        raise AgentRefusalError(
            "no pinned prompt for LLM Incident Diagnosis Agent — "
            "verify workspace.agent_prompt_pins seed (Phase 0 step 6)"
        )

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        traces = await _fetch_langfuse_traces(client, since, trace_limit)

    # Refusal: empty alert label AND no signal anywhere.
    if (
        not alert_label.strip()
        and not workflow_rows
        and not traces
    ):
        raise AgentRefusalError("insufficient context for diagnosis")

    context_blob = {
        "alert_label": alert_label,
        "window_minutes": window_minutes,
        "production_prompts": [
            {
                "prompt_id": r["prompt_id"],
                "version": r["version"],
                "promoted_at": r["promoted_at"].isoformat() if r["promoted_at"] else None,
            }
            for r in prompt_rows
        ],
        "recent_workflow_runs": [
            {
                "run_id": str(r["run_id"]),
                "workflow_kind": r["workflow_kind"],
                "status": r["status"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
                "failure_reason": r["failure_reason"],
                "trace_id": r["trace_id"],
            }
            for r in workflow_rows
        ],
        "langfuse_traces": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "createdAt": t.get("createdAt"),
                "level": t.get("level"),
                "statusMessage": t.get("statusMessage"),
            }
            for t in traces
        ],
    }

    # ---- LLM call ---------------------------------------------------------
    # Local import to avoid the orchestrator's module-level import surface
    # being pulled in for non-LLM agent paths.
    from app.agent.orchestrator import _call_openai_compatible_llm

    parameters = pinned.get("parameters") or {}
    if isinstance(parameters, str):
        parameters = json.loads(parameters)
    temperature = float(parameters.get("temperature", 0.1))

    user_message = (
        "Diagnose this incident from the supplied context. "
        "Return JSON only.\n\n"
        f"```json\n{json.dumps(context_blob, default=str)}\n```"
    )

    raw = await _call_openai_compatible_llm(
        user_message=user_message,
        temperature=temperature,
        system_prompt=pinned["text"],
        enable_thinking=False,
        response_format="json",
    )

    # Tally token usage for the wrapper to write a usage_events row.
    # The orchestrator function returns just the text; we conservatively
    # estimate prompt_tokens from message length when usage isn't surfaced.
    ctx.usage = {
        "model_profile": parameters.get("model_profile", "chat_deep"),
        "model_id": os.environ.get("VLLM_MODEL"),
        "tokens_prompt": int(len(user_message) / 4),
        "tokens_completion": int(len(raw) / 4),
        "projected_cost_usd": 0.0,
    }

    # ---- Validate structured output ---------------------------------------
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Try to recover a JSON block out of a noisy response (defensive).
        first = raw.find("{")
        last = raw.rfind("}")
        if first >= 0 and last > first:
            try:
                parsed = json.loads(raw[first : last + 1])
            except json.JSONDecodeError:
                raise AgentRefusalError(f"LLM returned non-JSON output: {exc}") from exc
        else:
            raise AgentRefusalError(f"LLM returned non-JSON output: {exc}") from exc

    if isinstance(parsed, dict) and parsed.get("refusal"):
        raise AgentRefusalError(str(parsed.get("refusal")))

    try:
        diagnosis = IncidentDiagnosis.model_validate(parsed)
    except ValidationError as exc:
        raise AgentRefusalError(f"LLM output failed schema validation: {exc}") from exc

    return {
        "alert_label": alert_label,
        "window_minutes": window_minutes,
        "context_counts": {
            "workflow_runs": len(workflow_rows),
            "production_prompts": len(prompt_rows),
            "langfuse_traces": len(traces),
        },
        "prompt_version": pinned.get("version"),
        "diagnosis": diagnosis.model_dump(),
    }
