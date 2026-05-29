"""§11.2 — cross-store workspace consistency reporter.

Walks every datastore that carries workspace-scoped data and returns a
structured per-store count summary. Useful on its own (operator
diagnostic), and reusable as the assertion step in the §11.3
restore_workspace round-trip integration tests once dry_run=False is
implemented.

The actual counting logic lives in
`app.hatchet_workflows.restore_workspace` (the dry_run path graduated
in Phase G.2). This module re-exports those helpers behind a stable
public API so other modules don't have to import workflow internals.

Usage
=====

    from app.services.cross_store_consistency import count_workspace_footprint

    footprint = await count_workspace_footprint("a0000000-...-001", pool)
    print(footprint.total_rows())     # sum across all stores
    print(footprint.postgres["silver_workspaces"])
    print(footprint.neo4j_nodes)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import asyncpg

# Re-export private helpers from restore_workspace under stable public
# names. Keep these in sync with restore_workspace if its counter
# surface changes (the test in tests/test_section11_consistency.py
# pins the names so a divergence shows up immediately).
from app.hatchet_workflows.restore_workspace import (
    _count_neo4j_nodes,
    _count_postgres_rows,
    _count_qdrant_points,
    _count_redis_keys,
)


@dataclass(frozen=True)
class WorkspaceFootprint:
    """One row per workspace, capturing the cross-store extent of its data.

    Fields:
        workspace_id        — the queried workspace UUID as text
        postgres            — dict of per-table row counts
        postgres_error      — None if PG queries succeeded, else error string
        neo4j_nodes         — count of Neo4j nodes scoped to this workspace
        neo4j_error         — None if Neo4j queries succeeded
        qdrant_points       — count of Qdrant points filtered by workspace_id
        qdrant_error        — None if Qdrant queries succeeded
        redis_keys          — count of Redis keys prefixed by workspace_id
        redis_error         — None if Redis queries succeeded
    """

    workspace_id: str
    postgres: dict[str, int] = field(default_factory=dict)
    postgres_error: str | None = None
    neo4j_nodes: int = -1
    neo4j_error: str | None = None
    qdrant_points: int = -1
    qdrant_error: str | None = None
    redis_keys: int = -1
    redis_error: str | None = None

    def total_rows(self) -> int:
        """Sum of all positive (non-error) counts across stores.

        Errors (recorded as -1) are skipped so partial-availability
        states still produce a useful total.
        """
        pg = sum(v for v in self.postgres.values() if v >= 0)
        n4 = self.neo4j_nodes if self.neo4j_nodes >= 0 else 0
        qd = self.qdrant_points if self.qdrant_points >= 0 else 0
        rd = self.redis_keys if self.redis_keys >= 0 else 0
        return pg + n4 + qd + rd

    def has_any_error(self) -> bool:
        """True if any store returned an error during counting.

        Counts of -1 are NOT inherently errors — a store may simply
        return zero workspace-scoped data legitimately. Only the
        named *_error fields signal a real query failure.
        """
        return any([
            self.postgres_error is not None,
            self.neo4j_error is not None,
            self.qdrant_error is not None,
            self.redis_error is not None,
        ])

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id":   self.workspace_id,
            "postgres":       self.postgres,
            "postgres_error": self.postgres_error,
            "neo4j_nodes":    self.neo4j_nodes,
            "neo4j_error":    self.neo4j_error,
            "qdrant_points":  self.qdrant_points,
            "qdrant_error":   self.qdrant_error,
            "redis_keys":     self.redis_keys,
            "redis_error":    self.redis_error,
            "total_rows":     self.total_rows(),
            "has_any_error":  self.has_any_error(),
        }


async def count_workspace_footprint(
    workspace_id: str, pool: asyncpg.Pool,
) -> WorkspaceFootprint:
    """Count the cross-store footprint of one workspace.

    Each store is queried independently — a failure in one (e.g.,
    Neo4j unreachable) does not block the others. Errors land on the
    corresponding `*_error` field; the count for that store stays at -1.

    Args:
        workspace_id: the workspace UUID as text.
        pool: asyncpg connection pool against the platform Postgres
            (will SET app.workspace_id on the connection it borrows).

    Returns:
        WorkspaceFootprint with per-store counts + errors.
    """
    workspace_str = str(workspace_id)

    pg_counts, pg_err = await _count_postgres_rows(pool, workspace_str)
    n4_count, n4_err = await _count_neo4j_nodes(workspace_str)
    qd_count, qd_err = await _count_qdrant_points(workspace_str)
    rd_count, rd_err = await _count_redis_keys(workspace_str)

    return WorkspaceFootprint(
        workspace_id=workspace_str,
        postgres=pg_counts,
        postgres_error=pg_err,
        neo4j_nodes=n4_count,
        neo4j_error=n4_err,
        qdrant_points=qd_count,
        qdrant_error=qd_err,
        redis_keys=rd_count,
        redis_error=rd_err,
    )


__all__ = [
    "WorkspaceFootprint",
    "count_workspace_footprint",
]
