"""Pin REC#1 (2026-06-03) — workspace_id is a type-required parameter
at every boundary, not an optional value with a silent default.

Background
----------
REC#1 closes the bug class "router forgot to check request.state for
a workspace_id, silently scoped its work to the default tenant." The
mechanism is two-pronged:

  1. ``app/agent/workspace_dependency.py`` provides
     ``require_workspace_context`` (raises 401 if missing) and
     ``optional_workspace_context`` (returns None explicitly) Depends
     factories. Every router declares which it wants; the type system
     enforces.
  2. Hatchet workflow input models that previously had
     ``workspace_id: str = Field(default=LEGACY_DEFAULT_TENANT_UUID)``
     now have ``Field(...)`` (required). Legitimate default-tenant
     callers go through
     ``hatchet_workflows._workspace_input.bootstrap_workspace_id(reason=...)``
     which logs + counts the bootstrap.

What this test pins
-------------------
1. The Depends factories exist + have the documented shapes.
2. ``require_workspace_context`` raises 401 when request.state has no
   workspace_id (no silent fallback).
3. ``optional_workspace_context`` returns None when absent (no silent
   fallback either — explicit absence).
4. The two migrated workflow input models have NO default on
   workspace_id (Pydantic raises on instantiation without it).
5. ``bootstrap_workspace_id`` rejects unknown reasons (every bootstrap
   site is an explicit allow-list entry).
6. The reference router (visualizations) uses OptionalWorkspace.

Why pin file content + behavior
--------------------------------
Behavior tests catch the FastAPI integration (Depends invocation,
HTTPException shape). File-content tests catch refactors that revert
the Pydantic Field default without thinking through the consequences.
The combination is load-bearing for the Phase-2 cutover where
WorkspaceContext.from_state flips to hard-raise — anything still
calling from_state must already be migrated to Depends, or that
cutover breaks production.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def _make_request(workspace_id: str | None = None):
    """Tiny FastAPI Request stub — enough for the Depends factory."""
    state = SimpleNamespace()
    if workspace_id is not None:
        state.workspace_id = workspace_id
    return SimpleNamespace(
        state=state,
        scope={"path": "/test/route"},
        url=SimpleNamespace(path="/test/route"),
    )


# ---------------------------------------------------------------------------
# 1. Factories exist + are importable
# ---------------------------------------------------------------------------


def test_factories_exist_and_are_async() -> None:
    from app.agent.workspace_dependency import (
        OptionalWorkspace,
        RequiredWorkspace,
        optional_workspace_context,
        require_workspace_context,
    )

    assert inspect.iscoroutinefunction(require_workspace_context), (
        "require_workspace_context must be async — FastAPI awaits Depends "
        "results, and a sync function in the chain blocks the event loop."
    )
    assert inspect.iscoroutinefunction(optional_workspace_context)
    # Convenience aliases must be Depends() instances, not the raw fn.
    assert hasattr(RequiredWorkspace, "dependency"), (
        "RequiredWorkspace must be a Depends(require_workspace_context) "
        "alias so routers can declare `ws: WorkspaceContext = "
        "RequiredWorkspace` without wrapping themselves."
    )
    assert hasattr(OptionalWorkspace, "dependency")


# ---------------------------------------------------------------------------
# 2. require_workspace_context raises 401 (no silent fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_raises_401_when_workspace_id_missing() -> None:
    from app.agent.workspace_dependency import require_workspace_context

    req = _make_request(workspace_id=None)
    with pytest.raises(HTTPException) as exc_info:
        await require_workspace_context(req)
    assert exc_info.value.status_code == 401, (
        "Missing workspace_id must yield 401, not silently fall back. "
        "Silent fallback is the bug class REC#1 closes."
    )
    # Detail must name the route so ops can attribute 401 spikes.
    assert "/test/route" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_require_raises_401_when_workspace_id_empty_string() -> None:
    from app.agent.workspace_dependency import require_workspace_context

    req = _make_request(workspace_id="")
    with pytest.raises(HTTPException) as exc_info:
        await require_workspace_context(req)
    assert exc_info.value.status_code == 401, (
        "Empty-string workspace_id must yield 401. The B4 sweep found "
        "callers that passed '' through the chain; 'present-but-empty' "
        "is the same bug as 'absent' and must not silently fall back."
    )


@pytest.mark.asyncio
async def test_require_returns_typed_context_on_success() -> None:
    from app.agent.workspace_context import WorkspaceContext
    from app.agent.workspace_dependency import require_workspace_context

    req = _make_request(workspace_id="b1111111-2222-3333-4444-555555555555")
    ws = await require_workspace_context(req)
    assert isinstance(ws, WorkspaceContext)
    assert ws.workspace_id == "b1111111-2222-3333-4444-555555555555"
    assert ws.is_fallback is False, (
        "A real workspace_id on auth context must produce is_fallback=False. "
        "The Phase-2 cutover (raise on fallback) leans on this flag."
    )


# ---------------------------------------------------------------------------
# 3. optional_workspace_context explicit None, no fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_optional_returns_none_when_workspace_id_missing() -> None:
    from app.agent.workspace_dependency import optional_workspace_context

    req = _make_request(workspace_id=None)
    result = await optional_workspace_context(req)
    assert result is None, (
        "Absence must produce None, not a fallback WorkspaceContext. "
        "The type-system signal `WorkspaceContext | None` makes callers "
        "branch on absence explicitly; a silent fallback erases the "
        "type information that's the whole point of the change."
    )


@pytest.mark.asyncio
async def test_optional_returns_typed_context_when_present() -> None:
    from app.agent.workspace_context import WorkspaceContext
    from app.agent.workspace_dependency import optional_workspace_context

    req = _make_request(workspace_id="c1111111-2222-3333-4444-555555555555")
    ws = await optional_workspace_context(req)
    assert isinstance(ws, WorkspaceContext)
    assert ws.workspace_id == "c1111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# 4. Pydantic workflow input models REQUIRE workspace_id (no default)
# ---------------------------------------------------------------------------


def test_embed_pending_passages_input_requires_workspace_id() -> None:
    from pydantic import ValidationError

    from app.hatchet_workflows.embed_pending_passages import (
        EmbedPendingPassagesInput,
    )

    # No default — constructing without workspace_id must raise.
    with pytest.raises(ValidationError):
        EmbedPendingPassagesInput()
    # Explicit value succeeds.
    obj = EmbedPendingPassagesInput(
        workspace_id="d1111111-2222-3333-4444-555555555555"
    )
    assert obj.workspace_id == "d1111111-2222-3333-4444-555555555555"


def test_enrich_passage_context_input_requires_workspace_id() -> None:
    from pydantic import ValidationError

    from app.hatchet_workflows.enrich_passage_context import (
        EnrichPassageContextInput,
    )

    with pytest.raises(ValidationError):
        EnrichPassageContextInput()
    obj = EnrichPassageContextInput(
        workspace_id="e1111111-2222-3333-4444-555555555555"
    )
    assert obj.workspace_id == "e1111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# 5. bootstrap_workspace_id rejects unknown reasons
# ---------------------------------------------------------------------------


def test_bootstrap_workspace_id_rejects_unknown_reason() -> None:
    from app.hatchet_workflows._workspace_input import bootstrap_workspace_id

    with pytest.raises(ValueError) as exc_info:
        bootstrap_workspace_id(reason="some.random.path")
    msg = str(exc_info.value)
    assert "allow-list" in msg, (
        "The error message must point readers at "
        "ALLOWED_BOOTSTRAP_REASONS — adding a new bootstrap site "
        "should be an explicit architectural decision, not a quick "
        "copy-paste fix."
    )


def test_bootstrap_workspace_id_returns_legacy_uuid_for_allowed_reason() -> None:
    from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID
    from app.hatchet_workflows._workspace_input import bootstrap_workspace_id

    result = bootstrap_workspace_id(reason="dagster.nightly_embed")
    assert result == LEGACY_DEFAULT_TENANT_UUID


def test_bootstrap_allow_list_is_a_frozenset() -> None:
    """A mutable allow-list invites runtime additions; frozenset prevents."""
    from app.hatchet_workflows._workspace_input import ALLOWED_BOOTSTRAP_REASONS

    assert isinstance(ALLOWED_BOOTSTRAP_REASONS, frozenset), (
        "ALLOWED_BOOTSTRAP_REASONS must be a frozenset so test fixtures + "
        "runtime code cannot silently add new reasons. Adding a reason "
        "is a source-change requiring code review."
    )


# ---------------------------------------------------------------------------
# 6. Reference router (visualizations) uses the typed Depends
# ---------------------------------------------------------------------------


def test_visualizations_router_uses_optional_workspace_depends() -> None:
    """Belt-and-suspenders pin — the migration of visualizations.py
    to OptionalWorkspace is the reference for the other 6 routers."""
    import app as _app_pkg

    router_path = (
        Path(_app_pkg.__file__).resolve().parent / "routers" / "visualizations.py"
    )
    src = router_path.read_text(encoding="utf-8")

    assert "from app.agent.workspace_dependency import" in src, (
        "visualizations.py must import from workspace_dependency. If "
        "you reverted to the raw `hasattr(request.state, ...)` pattern, "
        "you reverted REC#1's reference site."
    )
    assert "OptionalWorkspace" in src, (
        "visualizations.py must use OptionalWorkspace (chart gallery "
        "genuinely serves anonymous traffic). RequiredWorkspace would "
        "break the demo path."
    )


# ---------------------------------------------------------------------------
# 7. No new Pydantic Field defaults shipping the legacy UUID literal
# ---------------------------------------------------------------------------


def test_no_workflow_input_defaults_legacy_uuid() -> None:
    """File-content sweep: no Pydantic Field in hatchet_workflows/
    may use LEGACY_DEFAULT_TENANT_UUID as a default. The bootstrap
    helper is the single sanctioned path."""
    import re

    import app as _app_pkg

    wf_dir = Path(_app_pkg.__file__).resolve().parent / "hatchet_workflows"
    pattern = re.compile(
        r"workspace_id\s*:\s*str\s*=\s*Field\s*\(\s*default\s*=\s*LEGACY_DEFAULT_TENANT_UUID",
        re.MULTILINE,
    )
    offenders: list[str] = []
    for py in wf_dir.glob("*.py"):
        if py.name.startswith("_"):
            continue
        src = py.read_text(encoding="utf-8")
        if pattern.search(src):
            offenders.append(py.name)
    assert not offenders, (
        f"Workflow input models with LEGACY_DEFAULT_TENANT_UUID as a Pydantic "
        f"Field default: {offenders}. REC#1 requires `Field(...)` for "
        f"workspace_id; bootstrap callers use "
        f"`bootstrap_workspace_id(reason=...)` instead."
    )
