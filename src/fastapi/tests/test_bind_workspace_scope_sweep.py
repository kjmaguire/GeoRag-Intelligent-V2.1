"""Repo-wide AST sweep: no `bind_workspace_scope` outside a transaction.

The runtime guard in scoped_pool.py fails loudly, but only on a path a
test or a production request actually executes. Several offenders found on
2026-08-21 live in nightly Hatchet crons and bulk-import routines that no
test reaches, and one of them (cluster_runner.py's project lookup) was a
cross-tenant write against a FAIL-OPEN policy. Waiting for those to fire in
production is not a plan.

WHO OWNS THE TRANSACTION
------------------------
A `bind_workspace_scope` call is only this file's business when the
enclosing function OWNS its connection — i.e. the function itself calls
`pool.acquire()` or `asyncpg.connect()`. A helper that receives `conn` as
a parameter (`_set_rls_gucs(conn, ...)`, `upsert_flag(conn, ...)`) is
delegating transaction ownership to its caller by design, and demanding a
transaction there would be wrong as well as noisy.

For an owning function, the call is acceptable when EITHER:

  * it is lexically inside `async with <something>.transaction():`, so SET
    LOCAL applies; or
  * it passes `is_local=False`, the documented form for a dedicated,
    non-pooled connection that is closed when done.

`is_local=False` on a POOLED connection is a different bug — the GUC
outlives the caller and leaks into whoever checks the connection out next
— and this check cannot see that. scoped_pool's docstring covers it. The
point here is only that the silent-no-op class cannot come back.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

_FUNC = (ast.AsyncFunctionDef, ast.FunctionDef)


def _opens_transaction(node: ast.AST) -> bool:
    items = getattr(node, "items", [])
    return any(
        isinstance(i.context_expr, ast.Call)
        and getattr(i.context_expr.func, "attr", None) == "transaction"
        for i in items
    )


def _acquires_own_connection(fn: ast.AST) -> bool:
    """Does this function get its own connection, rather than being handed one?"""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            attr = getattr(node.func, "attr", None)
            if attr == "acquire":
                return True
            # asyncpg.connect(...) / _pool.connect(...)
            if attr == "connect":
                return True
    return False


class _Sweeper(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.tx_depth = 0
        self.fn_stack: list[ast.AST] = []
        self.offenders: list[tuple[str, int, str]] = []

    def _visit_func(self, node):
        self.fn_stack.append(node)
        saved, self.tx_depth = self.tx_depth, 0  # a nested def is its own scope
        self.generic_visit(node)
        self.tx_depth = saved
        self.fn_stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def _visit_with(self, node):
        opened = _opens_transaction(node)
        if opened:
            self.tx_depth += 1
        self.generic_visit(node)
        if opened:
            self.tx_depth -= 1

    visit_With = _visit_with
    visit_AsyncWith = _visit_with

    def visit_Call(self, node: ast.Call) -> None:
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == "bind_workspace_scope":
            explicit_session = any(
                kw.arg == "is_local"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is False
                for kw in node.keywords
            )
            owns = bool(self.fn_stack) and _acquires_own_connection(self.fn_stack[-1])
            if owns and self.tx_depth == 0 and not explicit_session:
                fname = getattr(self.fn_stack[-1], "name", "<module>")
                self.offenders.append((self.rel, node.lineno, fname))
        self.generic_visit(node)


def _sweep() -> tuple[list[tuple[str, int, str]], int]:
    offenders: list[tuple[str, int, str]] = []
    scanned = 0
    for path in sorted(APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        if "bind_workspace_scope" not in src:
            continue
        scanned += 1
        sweeper = _Sweeper(str(path.relative_to(APP.parent)).replace("\\", "/"))
        sweeper.visit(ast.parse(src))
        offenders.extend(sweeper.offenders)
    return offenders, scanned


def test_no_bind_workspace_scope_outside_a_transaction():
    offenders, scanned = _sweep()
    assert scanned > 10, f"sweep only reached {scanned} files — glob is wrong"
    assert not offenders, (
        "bind_workspace_scope called outside a transaction, in a function "
        "that acquires its OWN connection, without is_local=False. SET LOCAL "
        "is discarded there, so the workspace GUC is silently NOT bound and "
        "every following query runs unscoped:\n"
        + "\n".join(f"  {f}:{ln}  in {fn}()" for f, ln, fn in offenders)
    )


# ---------------------------------------------------------------------------
# A sweep that cannot fail is decoration.
# ---------------------------------------------------------------------------
def _offenders_of(src: str) -> list[tuple[str, int, str]]:
    sweeper = _Sweeper("fake.py")
    sweeper.visit(ast.parse(src))
    return sweeper.offenders


def test_flags_an_owning_function_with_no_transaction():
    """The exact shape found in cluster_runner.py and claim_ledger.py."""
    bad = (
        "async def f(pool, ws):\n"
        "    async with pool.acquire() as conn:\n"
        "        await bind_workspace_scope(conn, workspace_id=ws, site='x')\n"
    )
    assert len(_offenders_of(bad)) == 1


def test_accepts_a_transaction():
    good = (
        "async def f(pool, ws):\n"
        "    async with pool.acquire() as conn:\n"
        "        async with conn.transaction():\n"
        "            await bind_workspace_scope(conn, workspace_id=ws, site='x')\n"
    )
    assert _offenders_of(good) == []


def test_accepts_explicit_session_scope_on_a_dedicated_connection():
    good = (
        "async def f(ws):\n"
        "    conn = await asyncpg.connect(dsn)\n"
        "    await bind_workspace_scope(conn, workspace_id=ws, site='x',"
        " is_local=False)\n"
    )
    assert _offenders_of(good) == []


def test_ignores_a_helper_that_is_handed_a_connection():
    """`_set_rls_gucs(conn, ...)` delegates transaction ownership upward.

    Demanding a transaction inside it would be wrong, not merely noisy —
    the caller may legitimately hold one already.
    """
    helper = (
        "async def _set_rls_gucs(conn, ws):\n"
        "    await bind_workspace_scope(conn, workspace_id=ws, site='x')\n"
    )
    assert _offenders_of(helper) == []


def test_a_nested_transaction_does_not_leak_into_a_sibling_function():
    """Depth is per-function, so one correct function cannot vouch for the
    next one down the file."""
    src = (
        "async def good(pool, ws):\n"
        "    async with pool.acquire() as conn:\n"
        "        async with conn.transaction():\n"
        "            await bind_workspace_scope(conn, workspace_id=ws, site='x')\n"
        "\n"
        "async def bad(pool, ws):\n"
        "    async with pool.acquire() as conn:\n"
        "        await bind_workspace_scope(conn, workspace_id=ws, site='y')\n"
    )
    offenders = _offenders_of(src)
    assert len(offenders) == 1
    assert offenders[0][2] == "bad"
