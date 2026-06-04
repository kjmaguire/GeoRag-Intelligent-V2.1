"""DB-layer helpers — REC#2 (2026-06-03) connection scoping.

`scoped_connection` is the canonical way to acquire a workspace-scoped
asyncpg connection outside the agent context (where ``AgentDeps.acquire_scoped``
covers it). All ad-hoc ``set_config('app.workspace_id', ...)`` calls
should be migrated to this helper so RLS scoping lives in ONE place.
"""
from app.db.scoped_pool import (
    UUID_RE,
    BareConnectionError,
    bind_workspace_scope,
    lookup_and_rescope,
    scoped_connection,
)

__all__ = [
    "BareConnectionError",
    "UUID_RE",
    "bind_workspace_scope",
    "lookup_and_rescope",
    "scoped_connection",
]
