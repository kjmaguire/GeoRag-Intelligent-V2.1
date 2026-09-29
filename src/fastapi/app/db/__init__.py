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
from app.db.workspace_sweep import (
    affected_row_count,
    execute_per_workspace,
    fetch_per_workspace,
    list_workspace_ids,
)

__all__ = [
    "BareConnectionError",
    "UUID_RE",
    "affected_row_count",
    "bind_workspace_scope",
    "execute_per_workspace",
    "fetch_per_workspace",
    "list_workspace_ids",
    "lookup_and_rescope",
    "scoped_connection",
]
