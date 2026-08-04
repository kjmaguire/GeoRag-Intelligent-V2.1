"""Guard against passage_embedder's cross-tenant embed leak regressing.

embed_pending_passages() runs on a dedicated asyncpg connection opened
directly (`asyncpg.connect(...)`), never wrapped in a transaction. Binding
`app.workspace_id` there with the default `is_local=True` issues `SET LOCAL`,
which Postgres silently no-ops outside a transaction block — so the GUC was
never actually set, and database/raw/phase0's tenant_isolation RLS policy
(which fails OPEN when the GUC reads NULL) let every workspace-wide embed
sweep read and embed every OTHER workspace's unembedded passages too,
tagged in Qdrant with the caller's workspace_id. Found live 2026-08-04.

This test fails if the `bind_workspace_scope(...)` call in that function
ever drops the explicit `is_local=False`.
"""

from __future__ import annotations

import re
from pathlib import Path

import app as _app_pkg


def test_passage_embedder_binds_workspace_scope_session_wide() -> None:
    path = (
        Path(_app_pkg.__file__).resolve().parent
        / "services" / "ingest" / "passage_embedder.py"
    )
    src = path.read_text(encoding="utf-8")

    match = re.search(r"await bind_workspace_scope\((.*?)\)", src, re.DOTALL)
    assert match is not None, (
        "bind_workspace_scope(...) call not found in passage_embedder.py — "
        "did it move or get renamed? Update this test's search if so."
    )

    call_args = match.group(1)
    assert "is_local=False" in call_args, (
        "passage_embedder.py's bind_workspace_scope(...) call is missing "
        "is_local=False. This connection (`pg_conn = await asyncpg.connect(...)`) "
        "is never wrapped in a transaction, so the default is_local=True "
        "(SET LOCAL) silently no-ops and app.workspace_id never actually "
        "binds — reopening the cross-tenant embed leak documented above."
    )
