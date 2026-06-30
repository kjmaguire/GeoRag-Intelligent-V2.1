"""Locks the WorkspaceContext fallback policy.

Background — 2026-06-03 audit item B
-------------------------------------
The agent code used to fall back to the default-tenant UUID
(`"a0000000-0000-0000-0000-000000000001"`) when ``state.deps.workspace_id``
was missing. The audit established this as a silent cross-tenant
contamination bug — answer_runs, cache keys, and lineage rows would
get tagged with the default tenant for any caller whose JWT didn't
carry a workspace claim.

The architectural fix is the :class:`WorkspaceContext` type in
``app.agent.workspace_context``, which centralizes the resolution
policy and emits a Prometheus counter on every fallback. Phase 1
(this audit pass) still falls back; Phase 2 flips the fallback to
``WorkspaceResolutionError``.

These tests pin the contract:

1. The default-tenant UUID literal appears in EXACTLY ONE place
   (``workspace_context.py``) so the Phase-2 flip is a single-line
   change.
2. The metric ``WORKSPACE_RESOLUTION_FAILURES`` exists and is
   labelled by ``site`` so ops can attribute fallbacks to call sites.
3. ``WorkspaceContext.from_explicit`` rejects empty input (no
   policy to fall back to when the caller said it had a value).
"""
from __future__ import annotations

import pathlib
import re

import pytest

_LEGACY_DEFAULT_TENANT_UUID = "a0000000-0000-0000-0000-000000000001"


def test_workspace_context_module_exists():
    """The typed context module must exist — it's the single source of
    truth for the default-tenant fallback policy."""
    from app.agent import workspace_context  # noqa: F401


def test_default_tenant_literal_purged_from_agent_directory():
    """The hardcoded default-tenant UUID literal must appear in
    exactly ONE file under ``app/agent/``: ``workspace_context.py``.

    Scope is intentionally narrow — ``app/agent/`` is what audit
    item B2 migrated. The same literal still appears in
    ``app/agents/phase10/``, ``app/services/support_cockpit/``,
    ``app/hatchet_workflows/``, ``app/routers/visualizations.py``,
    and ``app/services/tool_gateway/impls.py``. Those are tracked
    as a Theme I extension follow-up in
    ``docs/handover/AUDIT_AND_FIX_REPORT.md`` — each needs the same
    ``WorkspaceContext.from_state`` migration.

    When the follow-up lands, broaden this test's ``allowed_dirs``
    to include those paths so the Phase-2 hard-fail flip moves
    everything together.

    Excludes:
      - tests/ (assertions reference the literal by definition)
      - workspace_context.py (the canonical site)
      - any path containing _deprecated (legacy / archived code)
    """
    agent_root = pathlib.Path(__file__).parents[1] / "app" / "agent"
    violations: list[str] = []
    for py_file in agent_root.rglob("*.py"):
        if "_deprecated" in py_file.parts:
            continue
        if py_file.name == "workspace_context.py":
            continue
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _LEGACY_DEFAULT_TENANT_UUID in text:
            violations.append(str(py_file.relative_to(agent_root)))

    assert violations == [], (
        f"The default-tenant UUID literal {_LEGACY_DEFAULT_TENANT_UUID!r} "
        "is back in app/agent/ outside workspace_context.py:\n"
        + "\n".join(f"  {v}" for v in sorted(violations))
        + "\n\nUse WorkspaceContext.from_state(deps, site=\"...\").workspace_id "
        "instead. The single-source-of-truth invariant is what makes "
        "the Phase-2 flip to hard-fail a one-line change."
    )


def test_metric_exists_and_labelled_by_site():
    """The Prometheus counter must exist and carry a ``site`` label."""
    from app.metrics import WORKSPACE_RESOLUTION_FAILURES

    # prometheus_client.Counter exposes the labelname tuple via _labelnames.
    # The label IS public API for ops dashboards; pinning it here prevents
    # an accidental rename / removal.
    assert WORKSPACE_RESOLUTION_FAILURES._labelnames == ("site",), (
        "WORKSPACE_RESOLUTION_FAILURES must label by `site` so ops can "
        "attribute fallback events to specific call paths. Renaming the "
        "label silently breaks every alert + dashboard."
    )


def test_from_explicit_rejects_empty():
    """Constructor for known-good values must refuse empty input."""
    from app.agent.workspace_context import WorkspaceContext

    with pytest.raises(ValueError):
        WorkspaceContext.from_explicit("")

    with pytest.raises(ValueError):
        WorkspaceContext.from_explicit(None)  # type: ignore[arg-type]


def test_from_state_increments_metric_on_fallback():
    """When workspace_id is missing on state_deps, the counter for the
    given ``site`` label must increment by 1."""
    from types import SimpleNamespace

    from app.agent.workspace_context import WorkspaceContext
    from app.metrics import WORKSPACE_RESOLUTION_FAILURES

    # Sample current value (counters are monotonic; we read pre/post).
    before = WORKSPACE_RESOLUTION_FAILURES.labels(site="test_metric_increment")._value.get()
    ctx = WorkspaceContext.from_state(
        SimpleNamespace(workspace_id=None),
        site="test_metric_increment",
    )
    after = WORKSPACE_RESOLUTION_FAILURES.labels(site="test_metric_increment")._value.get()

    assert ctx.is_fallback is True
    assert ctx.workspace_id == _LEGACY_DEFAULT_TENANT_UUID
    assert after - before == 1, (
        f"WORKSPACE_RESOLUTION_FAILURES counter did not increment on "
        f"fallback (before={before}, after={after}). The metric is the "
        "primary observability signal for Phase 1 of the rollout — "
        "without it the team can't know when it's safe to flip to "
        "hard-fail (Phase 2)."
    )


def test_from_state_passes_through_real_value():
    """When workspace_id IS set, no fallback fires."""
    from types import SimpleNamespace

    from app.agent.workspace_context import WorkspaceContext

    ctx = WorkspaceContext.from_state(
        SimpleNamespace(workspace_id="b0000000-0000-0000-0000-000000000099"),
        site="test_passthrough",
    )
    assert ctx.is_fallback is False
    assert ctx.workspace_id == "b0000000-0000-0000-0000-000000000099"


def test_agent_files_import_workspace_context_at_fallback_sites():
    """Verify the 8 known fallback sites all reach for WorkspaceContext.

    Pins the migration done in 2026-06-03 audit item B2. If any of
    these files stops importing WorkspaceContext at the relevant site,
    something has likely been refactored to bypass the contract.
    """
    app_root = pathlib.Path(__file__).parents[1] / "app"
    files = [
        "agent/agentic_retrieval/nodes.py",
        "agent/orchestrator/__init__.py",
        "agent/tools.py",
    ]
    for relpath in files:
        path = app_root / relpath
        text = path.read_text(encoding="utf-8")
        assert re.search(r"from app\.agent\.workspace_context import WorkspaceContext", text), (
            f"{relpath} must import WorkspaceContext — it was one of the "
            "8 fallback sites migrated by the audit. A missing import "
            "means a regression re-introduced an `or \"a0000000-...\"` "
            "fallback. Re-run the migration documented in "
            "AUDIT_AND_FIX_REPORT.md item B2."
        )
