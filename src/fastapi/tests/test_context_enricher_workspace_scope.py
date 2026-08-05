"""Guard against context_enricher's cross-tenant enrichment leak regressing.

enrich_passage_context() runs on a dedicated asyncpg connection opened
directly (`asyncpg.connect(...)`), never wrapped in a transaction. Binding
`app.workspace_id` there with the default `is_local=True` issues `SET LOCAL`,
which Postgres silently no-ops outside a transaction block — so the GUC was
never actually set, and the canonical RLS policy on silver.document_passages
(which admits all rows OPEN when the GUC reads NULL) let the nightly
enrich_passage_context_wf cron read and mutate every OTHER workspace's
pending passages too — the query's own SQL WHERE clause has no workspace_id
filter at all; RLS was the only thing meant to scope it. Same bug class as
passage_embedder.py's cross-tenant embed leak (fixed earlier the same
session); this instance was found in a full-app review, 2026-08-05.

This test fails if the `bind_workspace_scope(...)` call in that function
ever drops the explicit `is_local=False`.
"""

from __future__ import annotations

import re
from pathlib import Path

import app as _app_pkg


def test_context_enricher_binds_workspace_scope_session_wide() -> None:
    path = (
        Path(_app_pkg.__file__).resolve().parent
        / "services" / "ingest" / "context_enricher.py"
    )
    src = path.read_text(encoding="utf-8")

    match = re.search(r"await bind_workspace_scope\((.*?)\)", src, re.DOTALL)
    assert match is not None, (
        "bind_workspace_scope(...) call not found in context_enricher.py — "
        "did it move or get renamed? Update this test's search if so."
    )

    call_args = match.group(1)
    assert "is_local=False" in call_args, (
        "context_enricher.py's bind_workspace_scope(...) call is missing "
        "is_local=False. This connection (`pg_conn = await asyncpg.connect(...)`) "
        "is never wrapped in a transaction, so the default is_local=True "
        "(SET LOCAL) silently no-ops and app.workspace_id never actually "
        "binds — reopening the cross-tenant enrichment leak documented above."
    )
