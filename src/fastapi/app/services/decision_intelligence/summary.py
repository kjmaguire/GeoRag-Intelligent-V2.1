"""Workspace decision summary — doc-phase 119 LIVE.

Aggregates `silver.decision_records` rows for the Eval Dashboard
(§7-B) + Project Intelligence Dashboard (§16.1) + Decision History
admin view (future §9.12).

First downstream consumer of the doc-phase 115 `record_decision`
facade — demonstrates the substrate growing real query surface
area on top of the writer side.

Per master plan §21.6, the data lineage graph UI reads similar
aggregations to plot decision flow over time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import asyncpg


# Mirror of the §21.3 controlled vocabulary so callers can iterate.
ALL_DECISION_TYPES = (
    "target_recommendation",
    "crs_decision",
    "schema_mapping",
    "public_data_import",
    "export_approval",
    "workflow_enablement",
    "conflict_resolution",
    "report_signoff",
)


@dataclass(frozen=True, slots=True)
class DecisionTypeCounts:
    """Per-decision-type aggregate counts for a workspace + window."""

    decision_type: str
    total: int
    accepted: int
    modified: int
    rejected: int
    signed_off: int
    other: int  # any human_decision value not in the 4 above


@dataclass(frozen=True, slots=True)
class WorkspaceDecisionSummary:
    workspace_id: UUID
    window_start: datetime
    window_end: datetime
    total_decisions: int
    decisions_with_audit_anchor: int
    by_type: list[DecisionTypeCounts]
    mean_uncertainty: float | None  # None when no decision has uncertainty set
    latest_decision_at: datetime | None


async def get_workspace_decision_summary(
    conn: asyncpg.Connection,
    *,
    workspace_id: UUID | str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> WorkspaceDecisionSummary:
    """Summarize a workspace's decision history.

    Args:
        conn: asyncpg Connection scoped to the workspace's RLS.
        workspace_id: workspace to summarize.
        window_start: optional lower bound (inclusive). Default = 90
            days ago.
        window_end: optional upper bound (exclusive). Default = now.

    Returns:
        `WorkspaceDecisionSummary` with totals + per-type breakdown +
        audit-anchor coverage + mean uncertainty + latest timestamp.
    """
    if window_end is None:
        window_end = datetime.now(timezone.utc)
    if window_start is None:
        window_start = window_end - timedelta(days=90)

    if window_end <= window_start:
        raise ValueError(
            f"window_end ({window_end}) must be > window_start ({window_start})"
        )

    workspace_str = str(workspace_id) if isinstance(workspace_id, UUID) else workspace_id
    workspace_uuid = workspace_id if isinstance(workspace_id, UUID) else UUID(workspace_str)

    # --- top-level counts + mean uncertainty + latest_decision_at ---
    top = await conn.fetchrow(
        """
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE audit_ledger_id IS NOT NULL) AS with_anchor,
            avg(uncertainty)::float AS mean_uncertainty,
            max(decided_at) AS latest
        FROM silver.decision_records
        WHERE workspace_id = $1::uuid
          AND decided_at >= $2
          AND decided_at < $3
        """,
        workspace_str,
        window_start,
        window_end,
    )

    total = int(top["total"] or 0)
    with_anchor = int(top["with_anchor"] or 0)
    mean_uncertainty = float(top["mean_uncertainty"]) if top["mean_uncertainty"] is not None else None
    latest_decision_at = top["latest"]

    # --- per-type breakdown ---
    type_rows = await conn.fetch(
        """
        SELECT
            decision_type,
            count(*) AS total,
            count(*) FILTER (WHERE human_decision = 'accepted')   AS accepted,
            count(*) FILTER (WHERE human_decision = 'modified')   AS modified,
            count(*) FILTER (WHERE human_decision = 'rejected')   AS rejected,
            count(*) FILTER (WHERE human_decision = 'signed_off') AS signed_off,
            count(*) FILTER (
                WHERE human_decision NOT IN ('accepted','modified','rejected','signed_off')
            ) AS other
        FROM silver.decision_records
        WHERE workspace_id = $1::uuid
          AND decided_at >= $2
          AND decided_at < $3
        GROUP BY decision_type
        ORDER BY decision_type
        """,
        workspace_str,
        window_start,
        window_end,
    )

    by_type = [
        DecisionTypeCounts(
            decision_type=r["decision_type"],
            total=int(r["total"]),
            accepted=int(r["accepted"]),
            modified=int(r["modified"]),
            rejected=int(r["rejected"]),
            signed_off=int(r["signed_off"]),
            other=int(r["other"]),
        )
        for r in type_rows
    ]

    return WorkspaceDecisionSummary(
        workspace_id=workspace_uuid,
        window_start=window_start,
        window_end=window_end,
        total_decisions=total,
        decisions_with_audit_anchor=with_anchor,
        by_type=by_type,
        mean_uncertainty=mean_uncertainty,
        latest_decision_at=latest_decision_at,
    )


__all__ = [
    "ALL_DECISION_TYPES",
    "DecisionTypeCounts",
    "WorkspaceDecisionSummary",
    "get_workspace_decision_summary",
]
