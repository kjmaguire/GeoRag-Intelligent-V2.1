"""Module-level runtime registration.

Production wires this from FastAPI startup (lifespan):

    from app.agents.runtime import register_runtime
    register_runtime(pg_pool=app.state.pg_pool, redis=app.state.redis_client)

Tests and scripts can call ``register_runtime`` directly with their own
pool / client. The wrapper resolves these on every invocation rather than
binding at decoration time, so the same decorated function works in
multiple processes (worker, web, tests).
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
import redis.asyncio as aioredis


@dataclass(slots=True)
class AgentRuntime:
    pg_pool: asyncpg.Pool
    redis: aioredis.Redis


_RUNTIME: AgentRuntime | None = None


def register_runtime(*, pg_pool: asyncpg.Pool, redis: aioredis.Redis) -> None:
    """Register the runtime resources the wrapper uses.

    Safe to call multiple times — last wins. Tests typically call this
    once per fixture.
    """
    global _RUNTIME
    _RUNTIME = AgentRuntime(pg_pool=pg_pool, redis=redis)


def get_runtime() -> AgentRuntime:
    """Return the registered runtime; raise if uninitialised."""
    if _RUNTIME is None:
        raise RuntimeError(
            "agents.runtime not registered — call register_runtime(pg_pool=..., redis=...) "
            "before invoking any @georag_agent function (typically from FastAPI lifespan)."
        )
    return _RUNTIME
