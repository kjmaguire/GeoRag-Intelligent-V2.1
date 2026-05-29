"""Per-invocation context the wrapper passes to the agent function."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID, uuid4


AgentOutcome = Literal[
    "success",
    "refusal",
    "failure",
    "timeout",
    "circuit_open",
    "deduped",
]


@dataclass(slots=True)
class AgentContext:
    """Context the agent receives as its first positional argument.

    The wrapper populates this from the invocation kwargs + runtime config.
    Agent functions read but never mutate it.
    """

    invocation_id: UUID = field(default_factory=uuid4)
    workspace_id: UUID | None = None
    actor_id: int | None = None
    actor_kind: str = "system"
    trace_id: str | None = None

    # Risk-tier-specific fields used to compute the idempotency key.
    document_id: str | None = None         # R2
    export_request_id: str | None = None   # R3
    sync_target: str | None = None         # R4
    sync_request_id: str | None = None     # R4
    target_id: str | None = None           # R5
    signoff_session_id: str | None = None  # R5

    # Behaviour modifiers
    dry_run: bool = False                  # R3+ honor this; R0/R2 ignore
    bypass_idempotency: bool = False       # R2+ may force re-run (admin/debug)

    # Resolved at decoration / invocation time and made available to the
    # agent for prompt lookups, downstream calls, etc.
    agent_name: str = ""
    agent_version: str = ""
    risk_tier: str = "R0"

    # Free-form per-invocation metadata the agent may attach (e.g. cost
    # attribution from inner LLM calls). The wrapper reads `usage` after
    # the agent returns to write usage_events.
    usage: dict[str, Any] = field(default_factory=dict)

    # §35.1 Langfuse parent-correlation fields. Set by the calling
    # orchestrator (Hatchet workflow / LangGraph node) so the per-agent
    # Langfuse trace can be re-anchored to its parent in the UI.
    parent_workflow_run_id: UUID | None = None
    parent_graph_run_id: UUID | None = None

    @property
    def is_dry_run(self) -> bool:
        return self.dry_run and self.risk_tier in ("R3", "R4", "R5")
