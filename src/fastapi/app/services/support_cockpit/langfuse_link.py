"""LangFuse trace replay link integration (§10.13) — doc-phase 104.

When a Customer Support Cockpit operator opens a ticket's correlated
trace, this module returns the LangFuse trace URL + ensures the
audit-ledger entry records the access per §25.3.

Reads LangFuse base URL + project id from config; supports the
existing LangFuse stack documented in
`docs/langfuse-langgraph-tooling-setup.md`.

Doc-phase 104 — skeleton.
"""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import asyncpg

from app.services.support_cockpit.access_audit import emit_support_access_audit


def build_langfuse_trace_url(trace_id: str, *, base_url: str | None = None) -> str:
    """Construct a LangFuse UI URL for the given trace_id.

    Pure function — safe to call from anywhere. Reads
    LANGFUSE_BASE_URL env var if base_url not passed; falls back to
    "http://langfuse:3000" for in-cluster.
    """
    if base_url is None:
        base_url = os.environ.get("LANGFUSE_BASE_URL", "http://langfuse:3000")
    return f"{base_url.rstrip('/')}/trace/{trace_id}"


async def open_trace_with_audit(
    conn: asyncpg.Connection,
    *,
    trace_id: str,
    workspace_id: UUID | str,
    ops_user_id: int,
    ticket_id: UUID | str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Return LangFuse trace URL + record the access in audit_ledger.

    Live in doc-phase 118 — combines the existing pure URL builder
    with `emit_support_access_audit` (graduated in doc-phase 116).

    Args:
        conn: asyncpg Connection.
        trace_id: LangFuse trace identifier.
        workspace_id: workspace whose trace is being read.
        ops_user_id: ops user opening the trace.
        ticket_id: optional ticket the access traces back to.
        base_url: optional explicit LangFuse base URL (overrides env).

    Returns:
        `{"url": str, "audit_ledger_id": UUID, "trace_id": str}`.
    """
    if not trace_id or not trace_id.strip():
        raise ValueError("trace_id is required")

    url = build_langfuse_trace_url(trace_id.strip(), base_url=base_url)

    entry = await emit_support_access_audit(
        conn,
        workspace_id=workspace_id,
        ops_user_id=ops_user_id,
        ticket_id=ticket_id,
        access_kind="langfuse_trace_read",
        target_summary=f"Opened LangFuse trace {trace_id}",
        payload={"trace_id": trace_id, "url": url},
    )

    return {
        "url": url,
        "audit_ledger_id": entry.id,
        "trace_id": trace_id,
    }


__all__ = ["build_langfuse_trace_url", "open_trace_with_audit"]
