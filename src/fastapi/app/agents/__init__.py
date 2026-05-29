"""GeoRAG agent operational contract — wrapper library (Phase 0 step 5.1).

Every agent in Phase 4+ rides on the ``@georag_agent`` decorator. The
decorator handles the cross-cutting concerns documented in master plan
§35.1 — timeouts, idempotency, prompt-version pinning, circuit breaking,
cost attribution, audit, dry-run — so agent authors only write the
business logic.

Minimal usage::

    from app.agents import georag_agent, AgentContext

    @georag_agent(name="Tenant Isolation Auditor", risk_tier="R0", version="0.1.0")
    async def tenant_isolation_audit(ctx: AgentContext, *, sample_size: int = 50) -> dict:
        ...
        return {"violations": 0, "sampled": sample_size}

The decorator returns an awaitable callable that, when invoked, performs
all of the above and returns the agent's value.
"""

from .context import AgentContext, AgentOutcome
from .exceptions import (
    AgentCircuitOpenError,
    AgentTimeoutError,
    AgentRefusalError,
)
from .runtime import register_runtime, get_runtime
from .wrapper import georag_agent, AgentResult

__all__ = [
    "georag_agent",
    "AgentContext",
    "AgentOutcome",
    "AgentResult",
    "AgentCircuitOpenError",
    "AgentTimeoutError",
    "AgentRefusalError",
    "register_runtime",
    "get_runtime",
]
