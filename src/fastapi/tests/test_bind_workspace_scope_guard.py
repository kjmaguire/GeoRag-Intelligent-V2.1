"""`SET LOCAL` outside a transaction is silently discarded — refuse it.

`bind_workspace_scope(is_local=True)` issues
`set_config('app.workspace_id', $1, true)`. PostgreSQL discards SET LOCAL
outside a transaction block, so any caller that acquired a bare pooled
connection got a SILENT no-op and then ran every following query with no
workspace GUC bound.

Whether that leaks depends on the shape of each table's policy, and this
codebase has both shapes:

  * silver.document_passages is FAIL-CLOSED —
    `workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid`
    resolves to NULL and matches nothing, so an unbound GUC yields zero
    rows.
  * silver.projects is FAIL-OPEN —
    `NULLIF(current_setting(...), '') IS NULL OR workspace_id = ...`
    and an unbound GUC satisfies the first branch, making every
    workspace's rows visible.

"It happens to be fail-closed on the tables we looked at" is not a
tenancy guarantee, which is why the bind refuses rather than warns.

Both defects this guard was written for were real:
  * qdrant_fallback._pg_trgm_search bound on a bare `pool.acquire()`
    under a comment reading "Mandatory GUC for the RLS policy";
  * cluster_runner.py:245 did the same and then ran
    `SELECT project_id FROM silver.projects WHERE slug = $1` — no
    workspace predicate, fail-open policy, and a GLOBAL unique index on
    slug, so it could resolve to another tenant's project and write the
    import into it.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from app.db.scoped_pool import BareConnectionError, bind_workspace_scope

WS = "a0000000-0000-0000-0000-00000000feed"


class _Conn:
    """asyncpg.Connection stand-in with a controllable transaction state."""

    def __init__(self, *, in_transaction: bool) -> None:
        self._in_transaction = in_transaction
        self.executed: list[tuple] = []

    def is_in_transaction(self) -> bool:
        return self._in_transaction

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        return "SELECT 1"


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
async def test_is_local_outside_a_transaction_is_refused():
    conn = _Conn(in_transaction=False)
    with pytest.raises(BareConnectionError) as exc:
        await bind_workspace_scope(conn, workspace_id=WS, site="test")
    # The message has to teach the fix, not just complain.
    msg = str(exc.value)
    assert "SET LOCAL" in msg
    assert "conn.transaction()" in msg
    assert "is_local=False" in msg
    # Nothing was executed — it refused before touching the connection.
    assert conn.executed == []


async def test_is_local_inside_a_transaction_binds():
    conn = _Conn(in_transaction=True)
    await bind_workspace_scope(conn, workspace_id=WS, site="test")
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "set_config" in sql
    assert args[0] == WS
    assert args[1] is True  # SET LOCAL


async def test_session_scope_outside_a_transaction_is_allowed():
    """is_local=False is the documented escape hatch for a dedicated,
    non-pooled connection that is closed when done (ingest_zip_archive)."""
    conn = _Conn(in_transaction=False)
    await bind_workspace_scope(
        conn, workspace_id=WS, site="test", is_local=False,
    )
    assert len(conn.executed) == 1
    assert conn.executed[0][1][1] is False  # session-scoped


async def test_validation_still_runs_before_the_transaction_check():
    """A bad workspace_id must be rejected on its own terms, so the error
    a caller sees names the real problem rather than the transaction."""
    conn = _Conn(in_transaction=False)
    with pytest.raises(BareConnectionError, match="non-UUID"):
        await bind_workspace_scope(conn, workspace_id="not-a-uuid", site="test")


# ---------------------------------------------------------------------------
# The two call sites the guard was written for
# ---------------------------------------------------------------------------
def _fn_source(module, name: str) -> str:
    return inspect.getsource(getattr(module, name))


def test_qdrant_fallback_binds_inside_a_transaction():
    from app.services import qdrant_fallback

    src = _fn_source(qdrant_fallback, "_pg_trgm_search")
    tree = ast.parse(textwrap.dedent(src))

    # Find the bind call and prove an `async with ... transaction()` encloses it.
    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0
            self.bind_depths: list[int] = []

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            opens_tx = any(
                isinstance(i.context_expr, ast.Call)
                and getattr(i.context_expr.func, "attr", None) == "transaction"
                for i in node.items
            )
            self.depth += 1 if opens_tx else 0
            self.generic_visit(node)
            self.depth -= 1 if opens_tx else 0

        def visit_Await(self, node: ast.Await) -> None:
            call = node.value
            if isinstance(call, ast.Call) and getattr(
                call.func, "id", getattr(call.func, "attr", None),
            ) == "bind_workspace_scope":
                self.bind_depths.append(self.depth)
            self.generic_visit(node)

    v = _Visitor()
    v.visit(tree)
    assert v.bind_depths, "no bind_workspace_scope call found"
    assert all(d > 0 for d in v.bind_depths), (
        "bind_workspace_scope is outside conn.transaction() — SET LOCAL "
        "would be discarded and the RLS GUC never bound"
    )


def _fallback_sql_only() -> str:
    """The fallback module with its comments stripped.

    Every fix to this module leaves a comment quoting the code it
    replaced, so a test that greps the raw source matches its own
    explanation and passes for the wrong reason.
    """
    src = Path(
        inspect.getsourcefile(__import__(
            "app.services.qdrant_fallback", fromlist=["x"],
        )),
    ).read_text(encoding="utf-8")
    return "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )


def test_qdrant_fallback_selects_columns_that_exist():
    """The fallback named `passage_text` and `document_revision_id`.

    silver.document_passages has neither — verified against a
    freshly-migrated schema and against live production (28 columns). The
    columns are `text` and `document_id`. Every call therefore raised
    UndefinedColumnError into the blanket handler and returned [], so the
    fallback for a Qdrant outage never once produced a result.
    """
    code = _fallback_sql_only()

    assert "passage_text," not in code.split("jsonb_build_object")[0]
    assert "document_revision_id::text" not in code
    assert "FROM silver.document_passages" in code


def test_qdrant_fallback_scores_the_query_against_an_extent():
    """`similarity()` could not have matched anything, ever.

    It is a SYMMETRIC Jaccard over both trigram sets, so a 40-character
    query against a 5,000-character passage scores about 0.008 even on a
    perfect substring match — an order of magnitude below the 0.1 gate the
    module used. Two separate defects (wrong columns, wrong function) each
    independently guaranteed an empty result, which is why fixing the
    first one in isolation did not make the fallback work.

    `strict_word_similarity` normalises against the best word-aligned
    extent of the passage instead of its whole length.
    """
    code = _fallback_sql_only()

    assert "strict_word_similarity" in code
    assert "similarity(text, $1)" not in code, (
        "the symmetric similarity() call is back; it cannot clear any "
        "threshold at passage length"
    )


def test_the_query_is_the_needle_not_the_haystack():
    """Argument order is the fix, not just the function name.

    `strict_word_similarity(a, b)` scores a's trigrams against the best
    extent of b. Passing the passage first would search the QUERY for an
    extent matching the passage and reproduce the original arithmetic
    failure under a function name that looks correct.
    """
    code = _fallback_sql_only()

    assert "strict_word_similarity($1, text)" in code
    assert "strict_word_similarity(text, $1)" not in code, (
        "arguments are reversed — this scores the passage against the "
        "query and is as unmatchable as the symmetric call it replaced"
    )


def test_cluster_runner_project_lookup_is_inside_a_transaction():
    """cluster_runner.py's slug lookup has no workspace predicate and
    silver.projects' policy is fail-open, so an unbound GUC made it
    cross-tenant."""
    src = Path(
        inspect.getsourcefile(__import__(
            "app.services.ingest.cluster_runner", fromlist=["x"],
        )),
    ).read_text(encoding="utf-8")
    lines = src.splitlines()
    idx = next(
        i for i, ln in enumerate(lines)
        if "SELECT project_id::text FROM silver.projects WHERE slug" in ln
    )
    # Walk back to the nearest _set_rls_gucs and check a transaction opened
    # between it and the top of the block.
    window = "\n".join(lines[max(0, idx - 12):idx])
    assert "_set_rls_gucs" in window
    assert "conn.transaction()" in window, (
        "the slug lookup binds RLS GUCs outside a transaction"
    )


def test_no_duplicated_bind_in_cluster_runner():
    src = Path(
        inspect.getsourcefile(__import__(
            "app.services.ingest.cluster_runner", fromlist=["x"],
        )),
    ).read_text(encoding="utf-8")
    # The sed artefact: the identical bind call twice in a row.
    assert src.count('site="ingest.cluster_runner"') == 1
