"""FastAPI Depends() factories that promote workspace_id to a typed
required parameter at every router boundary (REC#1, 2026-06-03).

Background
----------
The B1-B4 audit work centralised the fallback constant + added a
WORKSPACE_RESOLUTION_FAILURES counter, but workspace_id remained
*optional* in every router signature. Each consumer had to do its own
``if hasattr(request.state, "workspace_id")`` dance, and a forgotten
check fell silently back to the default tenant.

This module flips workspace_id from "look it up, hope it's there" to
"declare what you need, the type system enforces it":

  - ``Depends(require_workspace_context)`` — router takes a typed
    ``WorkspaceContext`` as a positional kwarg; the dependency raises
    HTTPException(401) if no workspace_id is on the auth context.
    The router CANNOT execute without one.

  - ``Depends(optional_workspace_context)`` — for the small set of
    routers that genuinely admit unauthenticated traffic (health,
    metrics, public tiles). Returns None explicitly so the absence is
    a type-system fact, not a silent default.

Migration path
--------------
Phase 1 (this commit): the factories exist + one reference router
(``visualizations.py``) is migrated. Phase 2 (follow-up) migrates the
other 6 routers identified in the B4 sweep. Phase 3 flips
``_ALLOW_DEFAULT_TENANT_FALLBACK = False`` in
``app/agent/workspace_context.py`` so anything that still resolves via
``WorkspaceContext.from_state`` raises instead of falling back; the
typed Depends path is untouched because it never reached that fallback
to begin with.

Pinned by tests/test_workspace_dependency.py.
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status

from app.agent.workspace_context import WorkspaceContext


log = logging.getLogger(__name__)


async def require_workspace_context(request: Request) -> WorkspaceContext:
    """Resolve a non-empty workspace_id from request.state, or 401.

    Use this in every router that touches workspace-scoped data
    (silver.* writes, answer_runs, evidence_items, etc.). The 401
    response is intentionally explicit — a router that needs workspace
    context but didn't get one is an auth/middleware contract bug, not
    a "fall back to the default tenant" situation. Forcing the
    HTTPException makes the bug visible at the request level instead
    of silently corrupting cross-tenant data.

    Composes cleanly with the existing extract_user_context Depends:
    that one populates request.state.workspace_id from the JWT claim,
    this one type-narrows the result + raises if absent.

    Raises:
        HTTPException(401): when request.state has no workspace_id OR
        when the value is the empty string. The detail string names
        the route so ops can attribute 401s to a specific endpoint
        without scraping the full URL.
    """
    raw = getattr(request.state, "workspace_id", None)
    if raw is None or str(raw).strip() == "":
        # Pull the route path for the error detail — operators reading
        # 401 spikes in Loki need to know WHICH route is missing the
        # workspace claim, not just "some route is".
        path = getattr(request.scope, "get", lambda *_: None)("path") or "unknown"
        if path == "unknown":
            path = request.url.path
        log.warning(
            "require_workspace_context: no workspace_id on request.state "
            "for path=%s. Caller didn't thread the JWT workspace claim "
            "through. Returning 401 instead of silently scoping to the "
            "default tenant.",
            path,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                f"no workspace_id on auth context (route={path}). "
                "Mint the JWT with the workspace_id claim, or use "
                "Depends(optional_workspace_context) if this route "
                "genuinely admits unauthenticated traffic."
            ),
        )
    return WorkspaceContext(workspace_id=str(raw), is_fallback=False)


async def optional_workspace_context(request: Request) -> WorkspaceContext | None:
    """Return a WorkspaceContext if request.state carries one, else None.

    Use sparingly — only for routers that legitimately serve
    unauthenticated traffic (health, metrics, public-tile passthrough).
    The returned None is a TYPE-SYSTEM signal: every consumer must
    branch on it explicitly. There is no fallback to the default
    tenant; that's the whole point of REC#1.

    Routers that take this dependency should branch like:
        if ws is None:
            # unauthenticated / public path — do not touch workspace data
            ...
        else:
            # scoped path — ws.workspace_id is non-empty by construction
            ...
    """
    raw = getattr(request.state, "workspace_id", None)
    if raw is None or str(raw).strip() == "":
        return None
    return WorkspaceContext(workspace_id=str(raw), is_fallback=False)


# Convenience aliases used in router signatures, e.g.
#     def endpoint(..., ws: RequiredWorkspace) -> ...:
# Equivalent to Depends(require_workspace_context) but easier to read.
RequiredWorkspace = Depends(require_workspace_context)
OptionalWorkspace = Depends(optional_workspace_context)


__all__ = [
    "RequiredWorkspace",
    "OptionalWorkspace",
    "require_workspace_context",
    "optional_workspace_context",
]
