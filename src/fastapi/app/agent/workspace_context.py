"""Typed workspace context for the agent hot path.

Background — 2026-06-03 audit item B
-------------------------------------
The agent code had 8 sites doing
``getattr(state.deps, "workspace_id", None) or "a0000000-0000-0000-0000-000000000001"``.
When the JWT didn't carry a ``workspace_id`` claim (or the workspace
resolver couldn't derive it from ``project_id``), the answer_run /
cache key / lineage row would silently get tagged with the default
tenant — the exact silent-cross-tenant-contamination shape the audit
caught a dozen of in PHP.

The architectural fix is a typed ``WorkspaceContext`` that:

1. Has ONE construction path (``WorkspaceContext.from_state``).
2. Either holds a real ``workspace_id`` OR raises
   ``WorkspaceResolutionError`` — never silently falls back.
3. Records every fallback attempt to ``metrics.WORKSPACE_RESOLUTION_FAILURES``
   (per call site) so ops can watch the rate.

Phased rollout
--------------
**Phase 1 (this commit):** ``from_state`` still falls back to the
default tenant when no workspace_id is available, but increments the
counter + logs at WARN. Behavior unchanged so this commit is
non-breaking; ops can deploy + watch the counter.

**Phase 2 (next sprint, after observing zero counts for a week):**
flip ``ALLOW_DEFAULT_TENANT_FALLBACK`` to ``False`` (or remove the
constant) so ``from_state`` raises ``WorkspaceResolutionError``
instead of falling back.

**Phase 3:** the regression test ``test_no_agent_default_tenant_fallback``
asserts no Python file under ``app/agent/`` uses
``or "a0000000-..."`` for workspace_id resolution. Test exists but
is currently a smoke; sharpen once Phase 2 lands.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.metrics import WORKSPACE_RESOLUTION_FAILURES

log = logging.getLogger(__name__)

# Hardcoded default-tenant UUID — referenced ONCE here so the phase-2
# flip is a single-line change. Do not copy this literal elsewhere in
# the agent code. The regression test in
# ``tests/test_workspace_context.py::test_no_agent_default_tenant_fallback``
# will fail CI if this literal appears in any other agent file.
#
# Audit item B4 (2026-06-03) — promoted from `_` (private) to public
# `LEGACY_DEFAULT_TENANT_UUID` so the 20 production sites outside
# ``app/agent/`` can import the single source of truth instead of
# duplicating the string literal. Pinned by
# ``tests/test_workspace_context_b4_centralisation.py``.
LEGACY_DEFAULT_TENANT_UUID = "a0000000-0000-0000-0000-000000000001"
_LEGACY_DEFAULT_TENANT_UUID = LEGACY_DEFAULT_TENANT_UUID  # backward-compat alias

# Phase 1 of the rollout: fallback still applies, but we count it.
# Flip to False when ready to enforce.
_ALLOW_DEFAULT_TENANT_FALLBACK = True


class WorkspaceResolutionError(RuntimeError):
    """Raised when the agent can't resolve a workspace_id and the
    fallback policy is `enforce` rather than `count-and-warn`.

    Phase 1 doesn't raise this — only Phase 2 will. The class is
    defined now so call sites can already type-narrow and tests can
    assert on the eventual contract.
    """


@dataclass(frozen=True)
class WorkspaceContext:
    """The workspace a hot-path call is operating in.

    Construct via :meth:`from_state` rather than direct __init__ so
    the resolution policy + metric emission is centralized.
    """

    workspace_id: str

    # Tracks whether the value came from a real source or the legacy
    # fallback. Persisted on answer_runs/cache/lineage where useful
    # so ops can see post-hoc which rows are "tagged default by
    # fallback" vs "tagged default because they're actually default".
    is_fallback: bool = False

    @classmethod
    def from_state(
        cls,
        state_deps: Any,
        *,
        site: str,
    ) -> WorkspaceContext:
        """Resolve workspace_id from agent state deps.

        Resolution order:
          1. ``state_deps.workspace_id`` if set + non-empty.
          2. (Phase 1) Legacy default-tenant fallback + metric increment.
          3. (Phase 2) Raise WorkspaceResolutionError.

        ``site`` is a short tag identifying the call site (e.g.
        ``"persist_node"``, ``"orchestrator.cache_key"``). It becomes
        the ``site`` label on
        :data:`metrics.WORKSPACE_RESOLUTION_FAILURES` so ops can
        attribute fallbacks to specific code paths.
        """
        raw = getattr(state_deps, "workspace_id", None)
        if raw is not None and str(raw).strip() != "":
            return cls(workspace_id=str(raw), is_fallback=False)

        # No workspace_id available. Phase-dependent behavior:
        try:
            WORKSPACE_RESOLUTION_FAILURES.labels(site=site).inc()
        except Exception:
            # Metric emission must never crash the hot path.
            pass

        if not _ALLOW_DEFAULT_TENANT_FALLBACK:
            raise WorkspaceResolutionError(
                f"WorkspaceContext.from_state({site!r}): no workspace_id in "
                "state_deps and default-tenant fallback is disabled. The JWT "
                "or upstream middleware did not supply a workspace claim — "
                "fix the caller, don't re-enable the fallback."
            )

        log.warning(
            "WorkspaceContext.from_state(%s): workspace_id missing — falling "
            "back to default tenant. This is Phase-1 observe-only behavior; "
            "non-zero counter rate means a caller path needs fixing before "
            "Phase 2 flips this to a hard error.",
            site,
        )
        return cls(
            workspace_id=_LEGACY_DEFAULT_TENANT_UUID,
            is_fallback=True,
        )

    @classmethod
    def from_explicit(cls, workspace_id: str) -> WorkspaceContext:
        """Construct from a known-non-empty workspace_id.

        Use this when the caller already has a verified workspace_id
        (e.g. from a router that derived it from a JWT claim earlier
        in the request lifecycle). Bypasses the state-deps lookup +
        fallback machinery.

        Raises ValueError on empty input — there's no policy to fall
        back to here; the caller said it had the value, so absence is
        a contract bug.
        """
        if workspace_id is None or str(workspace_id).strip() == "":
            raise ValueError(
                "WorkspaceContext.from_explicit: workspace_id must be non-empty."
            )
        return cls(workspace_id=str(workspace_id), is_fallback=False)
