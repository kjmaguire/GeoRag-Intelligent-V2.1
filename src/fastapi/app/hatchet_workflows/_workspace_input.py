"""Helpers for Hatchet workflow inputs that need a workspace_id (REC#1).

Background
----------
Pre-REC#1, several workflow input models had ``workspace_id`` declared
as a Pydantic Field with ``default=LEGACY_DEFAULT_TENANT_UUID``. That
made it convenient to dispatch a workflow without threading the
workspace through, but it also meant a forgotten dispatcher silently
scoped its work to the default tenant. Same shape as the B-series
audit findings, just at the dispatch layer instead of the request
layer.

The architectural fix is to remove the default — make ``workspace_id``
required on every workflow input model — and provide an EXPLICIT
factory for the small set of legitimate bootstrap callers (Dagster
data backfills, one-off CLI scripts) that genuinely need to scope to
the default tenant.

How to use
----------

Dispatcher with a real workspace context::

    EmbedPendingPassagesInput(
        workspace_id=ws.workspace_id,
        project_id=project_id,
        ...
    )

Bootstrap caller (Dagster asset, CLI repair script)::

    EmbedPendingPassagesInput(
        workspace_id=bootstrap_workspace_id(reason="dagster.nightly_embed"),
        project_id=project_id,
        ...
    )

The ``reason`` argument is mandatory + flows into a Prometheus
counter so ops can see which bootstrap call sites are actually firing.
A spike in counter activity for a previously-quiet reason means a
dispatcher path lost its workspace claim and silently downgraded to
bootstrap mode.
"""
from __future__ import annotations

import logging

from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID
from app.metrics import WORKSPACE_RESOLUTION_FAILURES


log = logging.getLogger(__name__)


# A small enum-style set of allowed bootstrap reasons. New entries
# require a memory note explaining WHY this call path can't carry a
# real workspace claim — almost always one of:
#   - Dagster scheduled job (no request context to derive from)
#   - CLI repair script (operator runs explicitly against default tenant)
#   - Test fixture (test_* paths)
# If a NEW production code path appears wanting an entry here, it's
# probably a sign the architectural fix is "thread the workspace
# through", not "add another bootstrap reason".
ALLOWED_BOOTSTRAP_REASONS: frozenset[str] = frozenset({
    "dagster.scheduled_job",
    "dagster.nightly_embed",
    "dagster.nightly_integrity_sweep",
    "cli.reingest_project",
    "cli.repair_shadow_aggregate",
    "cli.manual_backfill",
    "test.fixture",
    "support_replay.bootstrap_lookup",
    # ADR-0014 lookup-and-rescope: cross-tenant ticket lookups in the
    # support-cockpit workflows + support_replay. Six call sites, all
    # documented elevation points.
    "support_cockpit.elevated_lookup",
    "tool_gateway.unauthenticated_query",
})


def bootstrap_workspace_id(*, reason: str) -> str:
    """Return the legacy default-tenant UUID, recording the bootstrap.

    Every call site that legitimately needs to dispatch work without a
    real workspace context goes through this function. The metric
    increment makes the choice visible — there's no "silent default"
    anywhere downstream.

    Args:
        reason: Short tag describing why this call site is in bootstrap
            mode. Must be in ALLOWED_BOOTSTRAP_REASONS (extend that
            constant with a memory note when adding a new one).

    Raises:
        ValueError: when ``reason`` isn't on the allow-list. The
        intent is to make adding a new bootstrap site an EXPLICIT
        architectural decision, not a copy-paste convenience.
    """
    if reason not in ALLOWED_BOOTSTRAP_REASONS:
        raise ValueError(
            f"bootstrap_workspace_id: reason={reason!r} not on the "
            f"allow-list. Either add it to ALLOWED_BOOTSTRAP_REASONS "
            f"(with a memory note explaining WHY this path can't carry "
            f"a real workspace claim), or thread the workspace context "
            f"through instead — that's almost always the right fix."
        )
    try:
        WORKSPACE_RESOLUTION_FAILURES.labels(
            site=f"bootstrap_workspace_id:{reason}"
        ).inc()
    except Exception:
        # Metric emission must never crash the dispatcher.
        pass
    log.info(
        "bootstrap_workspace_id: dispatching as default tenant for reason=%s. "
        "If this rate increases unexpectedly, a dispatcher path lost its "
        "workspace claim and silently downgraded.",
        reason,
    )
    return LEGACY_DEFAULT_TENANT_UUID


__all__ = ["bootstrap_workspace_id", "ALLOWED_BOOTSTRAP_REASONS"]
