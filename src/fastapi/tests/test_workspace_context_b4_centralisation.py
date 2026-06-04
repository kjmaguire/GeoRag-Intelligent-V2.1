"""Pin the audit item B4 invariant: the legacy default-tenant UUID
literal appears in exactly one canonical source file, and production
sites import the centralised constant instead.

Background
----------
B1-B3 introduced ``WorkspaceContext`` and migrated the 8 agent-code
fallback sites under ``app/agent/`` to ``WorkspaceContext.from_state``.
B4 extended the same centralisation to the 20 production files OUTSIDE
``app/agent/`` that had the same hardcoded literal:

  - 5 phase10 agents (customer_response_drafting / escalation_routing /
    root_cause_investigation / support_packet / ticket_triage)
  - 5 support_cockpit services (same names)
  - 6 hatchet_workflows (embed_pending_passages /
    enrich_passage_context / ingest_pdf / nightly_ingestion_integrity /
    support_replay / train_target_model)
  - 1 router (visualizations)
  - 1 service (tool_gateway/impls)

Each site now imports ``LEGACY_DEFAULT_TENANT_UUID`` from
``app.agent.workspace_context`` and (for runtime-fallback sites) emits
the ``WORKSPACE_RESOLUTION_FAILURES`` metric with a site label, so the
Phase-2 cutover ("flip fallback to raise") becomes a single-line change
in workspace_context.py rather than a 30-site sweep.

What this test pins
-------------------
1. ``LEGACY_DEFAULT_TENANT_UUID`` is the public name and matches the
   well-known UUID.
2. The string literal ``"a0000000-0000-0000-0000-000000000001"``
   appears in at most TWO production files: the canonical source
   (``workspace_context.py``) and the resolution-policy docstring
   (``services/workspace_resolution.py`` — explicit "no fallback"
   wording, not a fallback site).
3. Every migrated production file imports the centralised constant.

Why pin on file content
-----------------------
Same rationale as the other tenancy regression tests in this repo:
zero DB, runs in CI, catches drift before review. If a future
contributor copy-pastes the literal into a new file, this test fails
and points them at the centralised constant.
"""
from __future__ import annotations

from pathlib import Path


# Path resolution that works in both layouts:
#   - host: src/fastapi/tests/...  → app at src/fastapi/app
#   - container: /app/tests/...    → app at /app/app
# Use the actual `app` package location resolved via import.
import app as _app_pkg

APP = Path(_app_pkg.__file__).resolve().parent
REPO_ROOT = APP.parent.parent  # for relative_to() display only


def test_constant_is_public_and_correct() -> None:
    from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID

    assert LEGACY_DEFAULT_TENANT_UUID == "a0000000-0000-0000-0000-000000000001", (
        "LEGACY_DEFAULT_TENANT_UUID must match the well-known default-tenant "
        "UUID. Drift here means RLS-scoped queries in legacy code paths will "
        "scope to the WRONG tenant, which is silent cross-tenant contamination."
    )


def test_literal_appears_only_in_canonical_source() -> None:
    """At most TWO production files may carry the literal: the canonical
    definition site and the resolution-policy docstring."""
    literal = "a0000000-0000-0000-0000-000000000001"
    hits: list[Path] = []
    for py in APP.rglob("*.py"):
        # Skip pycache + the canonical site + the "we don't fall back"
        # docstring file (explicit anti-pattern documentation).
        if "__pycache__" in py.parts:
            continue
        if literal in py.read_text(encoding="utf-8", errors="ignore"):
            hits.append(py)

    allowed = {
        APP / "agent" / "workspace_context.py",
        APP / "services" / "workspace_resolution.py",
    }
    extras = [p for p in hits if p not in allowed]
    extras_list = "\n  ".join(str(p.relative_to(APP.parent)) for p in extras)
    assert not extras, (
        "The legacy default-tenant UUID literal must only appear in "
        f"{sorted(p.name for p in allowed)}. Found in unexpected files:\n  "
        f"{extras_list}\n"
        "Import `LEGACY_DEFAULT_TENANT_UUID` from "
        "`app.agent.workspace_context` instead of duplicating the literal. "
        "Centralisation lets the Phase-2 fallback-to-raise cutover be a "
        "single edit (audit item B4)."
    )


def test_migrated_sites_import_the_constant() -> None:
    """Every file migrated in B4 must import the constant.

    If a contributor reverts an import while keeping the constant
    reference, the file would NameError at runtime; this test surfaces
    it at PR time.
    """
    migrated = [
        # phase10 agents (5)
        APP / "agents" / "phase10" / "customer_response_drafting.py",
        APP / "agents" / "phase10" / "escalation_routing.py",
        APP / "agents" / "phase10" / "root_cause_investigation.py",
        APP / "agents" / "phase10" / "support_packet.py",
        APP / "agents" / "phase10" / "ticket_triage.py",
        # support_cockpit services — ADR-0014 lookup_and_rescope landed
        # 2026-06-04 across escalation_routing, root_cause_investigation,
        # support_packet, AND customer_response_drafting (the original
        # reference). All four no longer import LEGACY_DEFAULT_TENANT_UUID;
        # they're pinned by tests/test_lookup_and_rescope.py +
        # tests/test_scoped_connection.py instead. ticket_triage.py keeps
        # the import because its cron-style batch path still uses the
        # constant directly (single-scope, not two-phase).
        APP / "services" / "support_cockpit" / "ticket_triage.py",
        # hatchet_workflows (6 in B4, then 4 after REC#1).
        # REC#1 (2026-06-03) superseded B4 for embed_pending_passages.py
        # + enrich_passage_context.py — those workflow input models now
        # have REQUIRED workspace_id (no Pydantic default), so the
        # LEGACY_DEFAULT_TENANT_UUID import was removed entirely.
        # Bootstrap callers go through _workspace_input.bootstrap_workspace_id
        # instead. The B4 invariant on those 2 files is replaced by
        # tests/test_workspace_dependency.py::test_no_workflow_input_defaults_legacy_uuid.
        APP / "hatchet_workflows" / "ingest_pdf.py",
        APP / "hatchet_workflows" / "nightly_ingestion_integrity.py",
        # support_replay.py migrated to ADR-0014 lookup_and_rescope +
        # scoped_connection (2026-06-04), which removed the
        # LEGACY_DEFAULT_TENANT_UUID + WORKSPACE_RESOLUTION_FAILURES
        # imports entirely. Pinned by tests/test_lookup_and_rescope.py
        # + tests/test_scoped_connection.py.
        APP / "hatchet_workflows" / "train_target_model.py",
        # router + service (2)
        APP / "routers" / "visualizations.py",
        APP / "services" / "tool_gateway" / "impls.py",
    ]

    missing: list[str] = []
    for p in migrated:
        assert p.exists(), f"Migrated file vanished: {p.relative_to(REPO_ROOT)}"
        src = p.read_text(encoding="utf-8", errors="ignore")
        if "LEGACY_DEFAULT_TENANT_UUID" not in src:
            missing.append(str(p.relative_to(APP.parent)))
    assert not missing, (
        "These B4-migrated files no longer reference "
        "LEGACY_DEFAULT_TENANT_UUID:\n  " + "\n  ".join(missing) + "\n"
        "If the site genuinely no longer needs the constant (it now "
        "raises / resolves from real auth), remove it from this test's "
        "migrated[] list AND from the test_literal_appears_only_in_canonical "
        "allowlist if applicable."
    )


def test_runtime_fallback_sites_emit_resolution_metric() -> None:
    """The 6 runtime-fallback sites must increment WORKSPACE_RESOLUTION_FAILURES
    so ops can watch the Phase-1 → Phase-2 rollout.

    Pydantic Field defaults DON'T need the metric (they only fire when
    nothing else supplied a workspace_id at workflow-input time, and
    that's a class-construction context, not a hot path).

    Sites that DO need the metric: every place where we have a runtime
    `if not workspace_id: fall back` pattern.
    """
    metric_emitting_sites = [
        APP / "hatchet_workflows" / "ingest_pdf.py",
        # support_replay.py no longer emits the metric because the
        # bootstrap-lookup-realign pattern was replaced by
        # lookup_and_rescope (which counts elevation via its own
        # bootstrap_reason allowlist + metric). 2026-06-04 ADR-0014 swap.
        APP / "hatchet_workflows" / "train_target_model.py",
        APP / "routers" / "visualizations.py",
        APP / "services" / "tool_gateway" / "impls.py",
    ]
    missing = []
    for p in metric_emitting_sites:
        src = p.read_text(encoding="utf-8", errors="ignore")
        if "WORKSPACE_RESOLUTION_FAILURES" not in src:
            missing.append(str(p.relative_to(APP.parent)))
    assert not missing, (
        "These runtime-fallback sites do not emit "
        "WORKSPACE_RESOLUTION_FAILURES:\n  " + "\n  ".join(missing) + "\n"
        "Phase-1 of the rollout requires the metric so ops can observe "
        "fallback rate before Phase-2 flips to hard-raise. Without the "
        "metric the rollout is flying blind on that path."
    )
