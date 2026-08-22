"""The gateway knows the tenant; the tools must be told.

`invoke_tool()` authenticates a workspace into `ctx.workspace_id` and uses
it for `scoped_connection(...)` on every audit write — then used to
dispatch `await impl(inputs)` with the CALLER'S raw dict. The tenant was
known at dispatch time and never handed on, so a caller that omitted
`workspace_id` got whatever each implementation did with a missing tenant:

  * `_retrieve_qdrant` built `flt = None` and scrolled with
    `scroll_filter=None`. Qdrant has no RLS backstop, so that is not an
    unscoped query — it is every tenant's chunks.
  * `_query_postgis_readonly` rebound to LEGACY_DEFAULT_TENANT_UUID, so an
    authenticated tenant-B query returned tenant-A rows.

These are unit-level and DB-free on purpose. The existing
tests/test_tool_gateway.py is `integration`-marked and currently errors on
a schema CI does not build (the workspace.* tables live only in
database/raw), so it cannot guard this.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

from app.services.tool_gateway import impls


# ---------------------------------------------------------------------------
# _retrieve_qdrant fails closed
# ---------------------------------------------------------------------------
async def test_retrieve_qdrant_refuses_without_a_workspace():
    out = await impls._retrieve_qdrant({"query": "uranium", "k": 5})
    assert out["error"] == "workspace_id is required"
    assert out["hits"] == []
    assert out["count"] == 0


async def test_retrieve_qdrant_refuses_on_empty_string_workspace():
    """Falsy, not just absent — an empty string is the shape an
    under-populated caller dict actually produces."""
    out = await impls._retrieve_qdrant({"query": "u", "workspace_id": ""})
    assert out["error"] == "workspace_id is required"


def test_retrieve_qdrant_never_scrolls_with_a_null_filter():
    """No reachable path may pass scroll_filter=None."""
    src = inspect.getsource(impls._retrieve_qdrant)
    code = [
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    ]
    body = "\n".join(code)
    assert "flt = None" not in body, (
        "an unfiltered scroll is reachable again — Qdrant has no RLS "
        "backstop, so this dumps every tenant's chunks"
    )
    assert "scroll_filter=flt" in body


def test_retrieve_qdrant_checks_tenant_before_building_a_client():
    """The precondition must precede client construction, so the refusal
    costs nothing and reads as a precondition rather than a branch."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(impls._retrieve_qdrant)))
    fn = tree.body[0]
    guard_line = client_line = None
    for node in ast.walk(fn):
        if guard_line is None and isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                if getattr(test.operand, "id", None) == "workspace_id":
                    guard_line = node.lineno
        if client_line is None and isinstance(node, ast.Call):
            if getattr(node.func, "id", None) == "AsyncQdrantClient":
                client_line = node.lineno
    assert guard_line is not None, "no `if not workspace_id` precondition"
    assert client_line is not None, "AsyncQdrantClient construction not found"
    assert guard_line < client_line


# ---------------------------------------------------------------------------
# _query_postgis_readonly fails closed
# ---------------------------------------------------------------------------
def test_postgis_readonly_no_longer_falls_back_to_the_legacy_tenant():
    src = Path(inspect.getsourcefile(impls)).read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "LEGACY_DEFAULT_TENANT_UUID" not in code, (
        "a silent rebind to the legacy default tenant returns another "
        "workspace's rows to an authenticated caller"
    )


def test_postgis_readonly_refuses_without_a_workspace():
    src = inspect.getsource(impls._query_postgis_readonly)
    assert 'return {"error": "workspace_id is required"}' in src


# ---------------------------------------------------------------------------
# The gateway injects its authoritative tenant
# ---------------------------------------------------------------------------
def test_invoke_tool_dispatches_with_ctx_workspace_injected():
    """The dispatch must merge ctx's workspace over the caller's dict.

    Order matters: `{**inputs, "workspace_id": workspace_id_str}` means the
    gateway's authenticated value WINS. Reversing it would let a caller
    override the tenant the gateway just authenticated.
    """
    from app.services.tool_gateway import gateway

    src = inspect.getsource(gateway.invoke_tool)
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "await impl(inputs)" not in code, (
        "the gateway is dispatching the caller's raw dict again"
    )
    assert '{**inputs, "workspace_id": workspace_id_str}' in code
