"""Pin REC#2 (2026-06-03) — canonical workspace-scoped connection helper.

What this protects
------------------
The codebase had 20+ files that each hand-rolled the GUC-setting dance:

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.workspace_id', $1, false)", wsid)
        ...

Three audits in 2026-05 caught variants of "the same dance but
slightly wrong" — wrong GUC name, wrong SET LOCAL scope flag, missing
transaction, missing parameter binding (Theme G injection). REC#2
collapses this to ONE helper (``app.db.scoped_connection``) and pins
the contract here.

Pinned invariants
-----------------
1. The helper exists with the documented signature + error type.
2. Missing / empty / non-UUID workspace_id raises ``BareConnectionError``
   — silent default-tenant fallback is impossible.
3. The reference call site (``tool_gateway/impls.py``) uses the helper.
4. No production file under ``app/services/support_cockpit/`` or
   ``app/agents/phase10/`` ships a new bespoke
   ``set_config('app.workspace_id', ...)`` — they must use the helper
   instead (Phase-2 migration tracked separately; this is the *new
   sites only* gate, with explicit allowlist for the legacy sites
   awaiting migration).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. Helper exists + has the documented shape
# ---------------------------------------------------------------------------


def test_scoped_connection_is_importable_from_app_db() -> None:
    from app.db import BareConnectionError, UUID_RE, scoped_connection

    # scoped_connection must be an async context manager. Pure async
    # generators decorated with @asynccontextmanager have __wrapped__
    # pointing at the underlying coroutine fn.
    underlying = getattr(scoped_connection, "__wrapped__", scoped_connection)
    assert inspect.iscoroutinefunction(underlying) or inspect.isasyncgenfunction(
        underlying
    ), "scoped_connection must be an async context manager"

    # Error type is the typed signal; ops + tests narrow on it.
    assert issubclass(BareConnectionError, RuntimeError)

    # UUID regex is centralised here. Reusing it elsewhere is fine;
    # redefining it is not.
    assert isinstance(UUID_RE, re.Pattern)


# ---------------------------------------------------------------------------
# 2. Validation: missing / empty / malformed workspace_id is loud
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoped_connection_raises_on_empty_workspace_id() -> None:
    from app.db import BareConnectionError, scoped_connection

    # No pool needed — validation runs before pool.acquire().
    with pytest.raises(BareConnectionError) as exc_info:
        async with scoped_connection(
            pool=None,  # type: ignore[arg-type]
            workspace_id="",
            site="test.empty",
        ):
            pass
    assert "test.empty" in str(exc_info.value), (
        "Error message must name the call site so ops can trace which "
        "code path tried to silently scope to nothing."
    )


@pytest.mark.asyncio
async def test_scoped_connection_raises_on_none_workspace_id() -> None:
    from app.db import BareConnectionError, scoped_connection

    with pytest.raises(BareConnectionError):
        async with scoped_connection(
            pool=None,  # type: ignore[arg-type]
            workspace_id=None,  # type: ignore[arg-type]
            site="test.none",
        ):
            pass


@pytest.mark.asyncio
async def test_scoped_connection_raises_on_non_uuid_workspace_id() -> None:
    from app.db import BareConnectionError, scoped_connection

    # SQL injection class: anything that doesn't match UUID shape gets
    # interpolated into SET LOCAL (asyncpg can't parameterise GUC
    # names). Refuse early.
    with pytest.raises(BareConnectionError) as exc_info:
        async with scoped_connection(
            pool=None,  # type: ignore[arg-type]
            workspace_id="not-a-uuid'; DROP TABLE silver.workspaces; --",
            site="test.injection",
        ):
            pass
    assert "injection" in str(exc_info.value).lower(), (
        "Error message must call out the injection vector explicitly — "
        "the next contributor who hits this needs the context."
    )


# ---------------------------------------------------------------------------
# 3. Reference site uses the helper
# ---------------------------------------------------------------------------


def test_tool_gateway_uses_scoped_connection() -> None:
    """REC#2 reference site is tool_gateway/impls.py. If a refactor
    reverts the bespoke GUC dance, the reference for the other 19
    sites is gone too — fail fast.
    """
    import app as _app_pkg

    impls = (
        Path(_app_pkg.__file__).resolve().parent
        / "services"
        / "tool_gateway"
        / "impls.py"
    )
    src = impls.read_text(encoding="utf-8")

    assert "from app.db import scoped_connection" in src, (
        "tool_gateway/impls.py must import scoped_connection from "
        "app.db — it's the REC#2 reference. If you ripped this out, "
        "you reverted the demo for the rest of the migration."
    )
    assert "scoped_connection(" in src, (
        "tool_gateway/impls.py must actually CALL scoped_connection, "
        "not just import it."
    )


# ---------------------------------------------------------------------------
# 4. New bespoke set_config('app.workspace_id', ...) sites are forbidden
# ---------------------------------------------------------------------------


def test_no_new_bespoke_workspace_id_set_config_outside_allowlist() -> None:
    """Hard pin against re-introducing the bespoke GUC-setting pattern.

    The allowlist names every existing site that still does its own
    ``set_config('app.workspace_id', ...)``. Phase-2 of REC#2 migrates
    each of these to ``scoped_connection``; this test makes the list
    DECREASE over time, never grow.

    To add a NEW production file to the allowlist, you must explain why
    in the PR description (almost certainly the answer is "I should
    use scoped_connection instead").
    """
    import app as _app_pkg

    app_root = Path(_app_pkg.__file__).resolve().parent

    # Pattern: a Python string literal containing the asyncpg SQL form
    # `"SELECT set_config('app.workspace_id', $1, ...)"`. Excludes:
    #   - psycopg2 sync calls (use `%s`) — different driver
    #   - docstring / comment mentions in backticks or rST (no `"SELECT` prefix)
    #   - the canonical helpers in scoped_pool.py (allowlisted separately)
    # The `"SELECT` prefix is the load-bearing discriminator — only true
    # asyncpg migration targets begin the SQL string that way.
    pattern = re.compile(r'"SELECT set_config\(\s*\'app\.workspace_id\'\s*,\s*\$1')

    # Allowlist of legacy sites awaiting Phase-2 migration. Every entry
    # here has hand-rolled GUC plumbing that scoped_connection can
    # replace. Baseline captured 2026-06-03 via:
    #   grep -rl "set_config(\s*['\"]app\.workspace_id" src/fastapi/app/
    # As each file migrates, delete it from this list. The CI guard
    # enforces the list shrinks monotonically — adding entries requires
    # an explicit PR-description justification.
    LEGACY_AWAITING_MIGRATION = {
        # Canonical implementations — KEEP. These define the pattern.
        # (deps.py uses `SET LOCAL ... = '{wid}'` form, not set_config(),
        # so it's not in this allowlist — same intent, different syntax.
        # db/__init__.py only re-exports the helpers; no actual call.)
        "db/scoped_pool.py",
        # audit/__init__.py legitimately tolerates empty-string GUC
        # values (audit-context save+restore pattern); can't migrate to
        # bind_workspace_scope which rejects empty values.
        "audit/__init__.py",
        # agent/ — internal agent helpers migrated 2026-06-03 (REC#2
        # Phase-2 sweep): entity_resolver, geospatial_planner,
        # parent_expansion, project_geometry. All use bind_workspace_scope.
        # agents/phase0 — bootstrap / tenant-isolation auditor (allowed
        # to run cross-tenant for the audit query). Migrated 2026-06-03.
        # agents/phase10 (5 sites).
        "agents/phase10/customer_response_drafting.py",
        "agents/phase10/escalation_routing.py",
        "agents/phase10/root_cause_investigation.py",
        "agents/phase10/support_packet.py",
        "agents/phase10/ticket_triage.py",
        # hatchet_workflows — 14 files migrated 2026-06-03 in the
        # REC#2 Phase-2 wave-3 sweep (_restore_pg_from_export,
        # embed_pending_passages_smoke, field_outcome_learning,
        # ingest_pdf, ingest_zip_archive, ocr_quality_check, re_ocr_page,
        # repair_shadow_aggregate, restore_workspace, shadow_diff,
        # train_source_trust, train_target_model, what_changed_detector,
        # workspace_export). 16 sites collapsed.
        # _archived/shadow_diff.py is in the archived/ tree — DEAD CODE,
        # left in the allowlist as a sentinel (will be deleted in a
        # separate cleanup).
        "hatchet_workflows/_archived/shadow_diff.py",
        # support_replay.py remains: has the support_cockpit-style
        # mid-transaction GUC realignment pattern (set default → read
        # ticket workspace → realign). NOT a mechanical migration.
        "hatchet_workflows/support_replay.py",
        # OCR persist helper — migrated 2026-06-03.
        # routers (4 sites remaining after REC#2 Phase-2 sweep).
        # citation_feedback.py migrated 2026-06-03 via REC#2 Phase 2.
        # visualizations.py migrated REC#1 to typed Depends; the GUC
        # ref left here is in legacy comments only.
        "routers/interpretation.py",
        "routers/shadow_trigger.py",
        "routers/target_recommendation_cockpit.py",
        "routers/visualizations.py",
        # services/ — most of the ingest pipeline migrated 2026-06-03
        # in the second REC#2 Phase-2 wave (claim_ledger 5 sites,
        # cluster_runner 2 sites, context_enricher, derive_intervals 2
        # sites, kg_sync, passage_embedder (+ bug fix), tiff_ocr_ingester,
        # qdrant_fallback, silver_dq_flag_writer, hypothesis_generator,
        # shap_writer, trace_writer).
        # support_cockpit — ADR-0014 lookup_and_rescope landed.
        # customer_response_drafting.py migrated as the reference.
        # The other 4 use the same uniform pattern; follow-up PR can
        # migrate them mechanically per the reference. Each is ~25
        # lines of bespoke GUC plumbing collapsed to ~13 lines using
        # lookup_and_rescope (+ adds UUID validation on the pivot).
        # ticket_triage.py has a SECOND set_config call inside its
        # cron-style batch list-tickets path — that one is a single
        # scope (not two-phase) and should migrate to scoped_connection
        # instead of lookup_and_rescope.
        "services/support_cockpit/escalation_routing.py",
        "services/support_cockpit/root_cause_investigation.py",
        "services/support_cockpit/support_packet.py",
        "services/support_cockpit/ticket_triage.py",
        # services/tool_gateway/gateway.py migrated 2026-06-03 (REC#2
        # Phase-2 sweep — 10 bespoke sites replaced with scoped_connection).
    }

    found: set[str] = set()
    for py in app_root.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        rel = str(py.relative_to(app_root)).replace("\\", "/")
        src = py.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(src):
            found.add(rel)

    new_offenders = found - LEGACY_AWAITING_MIGRATION
    extras_in_allowlist = LEGACY_AWAITING_MIGRATION - found

    msg_parts = []
    if new_offenders:
        msg_parts.append(
            "New bespoke set_config('app.workspace_id', ...) sites detected:\n  "
            + "\n  ".join(sorted(new_offenders))
            + "\n\nUse `from app.db import scoped_connection` instead. See "
            "tool_gateway/impls.py as the reference migration."
        )
    if extras_in_allowlist:
        msg_parts.append(
            "These files are in LEGACY_AWAITING_MIGRATION but no longer "
            "contain the pattern — remove them from the allowlist (the "
            "list should shrink over time, not contain dead entries):\n  "
            + "\n  ".join(sorted(extras_in_allowlist))
        )
    assert not msg_parts, "\n\n".join(msg_parts)
