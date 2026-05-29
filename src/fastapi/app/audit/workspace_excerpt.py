"""Workspace audit ledger excerpt — doc-phase 121 LIVE.

Reader-side query that returns a paginated view of audit_ledger
rows for one workspace + time window. Powers two surfaces:

1. **Customer-visible audit history** (§25.3) — workspace owners
   see every action against their data, including `support_access`
   rows that ops emitted on their behalf.
2. **DR runbook detail** (§11.4) — operators viewing a workspace's
   recent activity during an incident.

Pure reader; never mutates. Honors RLS via `app.workspace_id` GUC
(the calling middleware sets it on the connection before invoking).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True, slots=True)
class AuditExcerptEntry:
    id: UUID
    created_at: datetime
    action_type: str
    actor_kind: str
    actor_id: int | None
    target_schema: str | None
    target_table: str | None
    target_id: str | None
    payload: dict[str, Any]
    trace_id: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceAuditExcerpt:
    workspace_id: UUID
    window_start: datetime
    window_end: datetime
    page: int
    page_size: int
    total_rows_in_window: int
    entries: list[AuditExcerptEntry]
    has_more: bool   # True when more pages exist beyond this one


_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 500


async def get_workspace_audit_excerpt(
    conn: asyncpg.Connection,
    *,
    workspace_id: UUID | str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
    action_type_filter: str | None = None,
) -> WorkspaceAuditExcerpt:
    """Return a page of audit_ledger rows for the workspace + window.

    Args:
        conn: asyncpg Connection (workspace_id GUC should be set by
            caller's middleware for RLS).
        workspace_id: workspace whose ledger we read.
        window_start: optional lower bound (inclusive). Default = 30
            days ago.
        window_end: optional upper bound (exclusive). Default = now.
        page: 1-based page number.
        page_size: page size; clamped to [1, 500].
        action_type_filter: optional substring filter on action_type
            (e.g., 'decision.' surfaces only decision-related rows;
            'support_access' surfaces only ops accesses).

    Returns:
        `WorkspaceAuditExcerpt` with total count + page entries +
        has_more flag.
    """
    if page < 1:
        raise ValueError(f"page must be >= 1; got {page}")
    page_size = max(1, min(page_size, _MAX_PAGE_SIZE))

    if window_end is None:
        window_end = datetime.now(timezone.utc)
    if window_start is None:
        window_start = window_end - timedelta(days=30)

    if window_end <= window_start:
        raise ValueError(
            f"window_end ({window_end}) must be > window_start ({window_start})"
        )

    workspace_str = str(workspace_id) if isinstance(workspace_id, UUID) else workspace_id
    workspace_uuid = workspace_id if isinstance(workspace_id, UUID) else UUID(workspace_str)

    # Total count — needed for has_more + paginate-from-end UI.
    count_sql = """
        SELECT count(*) AS n
        FROM audit.audit_ledger
        WHERE workspace_id = $1::uuid
          AND created_at >= $2
          AND created_at < $3
    """
    count_args: list[Any] = [workspace_str, window_start, window_end]
    if action_type_filter:
        count_sql += " AND action_type ILIKE $4"
        count_args.append(f"%{action_type_filter}%")

    total = int(await conn.fetchval(count_sql, *count_args) or 0)

    # Page rows (DESC by created_at — newest first, customer-friendly).
    offset = (page - 1) * page_size
    rows_sql = """
        SELECT
            id, created_at, action_type, actor_kind, actor_id,
            target_schema, target_table, target_id, payload, trace_id
        FROM audit.audit_ledger
        WHERE workspace_id = $1::uuid
          AND created_at >= $2
          AND created_at < $3
    """
    rows_args: list[Any] = [workspace_str, window_start, window_end]
    if action_type_filter:
        rows_sql += " AND action_type ILIKE $4"
        rows_args.append(f"%{action_type_filter}%")
        rows_sql += f" ORDER BY created_at DESC, id DESC LIMIT $5 OFFSET $6"
    else:
        rows_sql += " ORDER BY created_at DESC, id DESC LIMIT $4 OFFSET $5"
    rows_args.extend([page_size, offset])

    rows = await conn.fetch(rows_sql, *rows_args)

    def _payload_to_dict(p: Any) -> dict[str, Any]:
        if isinstance(p, dict):
            return p
        if isinstance(p, str):
            try:
                return json.loads(p)
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    entries = [
        AuditExcerptEntry(
            id=r["id"],
            created_at=r["created_at"],
            action_type=r["action_type"],
            actor_kind=r["actor_kind"],
            actor_id=r["actor_id"],
            target_schema=r["target_schema"],
            target_table=r["target_table"],
            target_id=r["target_id"],
            payload=_payload_to_dict(r["payload"]),
            trace_id=r["trace_id"],
        )
        for r in rows
    ]

    has_more = (offset + len(entries)) < total

    return WorkspaceAuditExcerpt(
        workspace_id=workspace_uuid,
        window_start=window_start,
        window_end=window_end,
        page=page,
        page_size=page_size,
        total_rows_in_window=total,
        entries=entries,
        has_more=has_more,
    )


__all__ = [
    "AuditExcerptEntry",
    "WorkspaceAuditExcerpt",
    "get_workspace_audit_excerpt",
]
