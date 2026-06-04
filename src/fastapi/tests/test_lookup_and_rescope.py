"""Pin ADR-0014 — lookup_and_rescope two-phase workspace scoping.

What this protects
------------------
The 5 support_cockpit agents + support_replay all need to:
  1. Look up a ticket by ticket_id (cross-tenant — they don't pre-know
     the workspace)
  2. Pivot the GUC to the ticket's discovered workspace
  3. Do downstream reads / writes properly scoped

Pre-ADR-0014 this was hand-rolled in 6 places, each with a different
risk of forgetting UUID validation on the pivot value. ADR-0014
extracts the pattern into ``app.db.lookup_and_rescope`` and pins:
- Pivot value must be a UUID (catches malformed ticket.workspace_id
  before SET LOCAL interpolation)
- Bootstrap goes through ``bootstrap_workspace_id(reason=...)``
  allowlist (observable cross-tenant elevation)
- Empty / missing lookup row raises BareConnectionError synchronously

These tests pin the helper contract. The migration of the 6
production sites is tested via the baseline-shrinking allowlist in
``test_scoped_connection.py``.
"""
from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# 1. Helper exists and is exposed
# ---------------------------------------------------------------------------


def test_helper_is_exported_from_app_db() -> None:
    from app.db import lookup_and_rescope

    underlying = getattr(lookup_and_rescope, "__wrapped__", lookup_and_rescope)
    assert inspect.iscoroutinefunction(underlying) or inspect.isasyncgenfunction(
        underlying
    ), "lookup_and_rescope must be an async context manager"


def test_helper_signature_has_required_kwargs() -> None:
    """The helper's required kwargs are load-bearing — the test pins
    them so accidental refactor doesn't break the 6 call sites."""
    from app.db import lookup_and_rescope

    underlying = getattr(lookup_and_rescope, "__wrapped__", lookup_and_rescope)
    sig = inspect.signature(underlying)
    params = sig.parameters

    assert "pool" in params, "must accept pool as first positional arg"
    for required in ("lookup_sql", "lookup_args", "site", "bootstrap_reason"):
        assert required in params, f"missing required kwarg: {required}"
        # All four must be keyword-only (after the *).
        assert params[required].kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{required} must be keyword-only — positional args here are an "
            "easy way to swap site and bootstrap_reason silently"
        )
    # workspace_col is optional + has the right default.
    assert params["workspace_col"].default == "workspace_id", (
        "workspace_col default must stay 'workspace_id' — changing it is "
        "an API break for every existing caller"
    )


# ---------------------------------------------------------------------------
# 2. Bootstrap routes through the reason allowlist
# ---------------------------------------------------------------------------


def test_helper_rejects_unknown_bootstrap_reason() -> None:
    """If a caller passes an off-allowlist reason, bootstrap_workspace_id
    raises ValueError. The helper inherits that — meaning adding a new
    elevation site requires editing ALLOWED_BOOTSTRAP_REASONS first."""
    import asyncio

    from app.db import lookup_and_rescope

    async def _attempt():
        # Pool=None is fine — validation runs before pool.acquire().
        async with lookup_and_rescope(
            pool=None,  # type: ignore[arg-type]
            lookup_sql="SELECT 1 AS workspace_id",
            lookup_args=(),
            site="test.unknown_reason",
            bootstrap_reason="totally.made.up.reason",
        ):
            pytest.fail("should not have entered the context")

    with pytest.raises(ValueError) as exc_info:
        asyncio.get_event_loop().run_until_complete(_attempt())
    assert "allow-list" in str(exc_info.value)


def test_helper_accepts_documented_bootstrap_reason() -> None:
    """The ADR-0014 reason MUST be on the allowlist — otherwise no one
    can actually USE the helper at the documented call sites."""
    from app.hatchet_workflows._workspace_input import ALLOWED_BOOTSTRAP_REASONS

    assert "support_cockpit.elevated_lookup" in ALLOWED_BOOTSTRAP_REASONS, (
        "The ADR-0014 reason 'support_cockpit.elevated_lookup' is missing "
        "from ALLOWED_BOOTSTRAP_REASONS. Without it the 6 migrated call "
        "sites in support_cockpit/* and support_replay.py raise ValueError "
        "on every invocation."
    )


# ---------------------------------------------------------------------------
# 3. ADR-0014 is committed (the architectural decision must stay
#    discoverable from grep)
# ---------------------------------------------------------------------------


def test_adr_0014_exists() -> None:
    """Pin the architectural decision record — a contributor swayed
    by 'just delete the helper and inline it' needs to find the ADR.

    Skipped inside the FastAPI container (it only mounts `/app`, not
    the host `docs/` tree). The CI-side variant runs on the host with
    full repo access via the PHP-side ArchitectureDecisionRecord test
    (if you add one) or this test in a non-containerised pytest run.
    """
    from pathlib import Path

    import app as _app_pkg

    # Try walking up from app/ to find a sibling docs/. Works on host
    # (src/fastapi/app -> src/fastapi -> src -> repo_root), skips when
    # the tree above app/ doesn't expose docs/ (the FastAPI container).
    app_pkg_dir = Path(_app_pkg.__file__).resolve().parent
    for ancestor in (app_pkg_dir, *app_pkg_dir.parents):
        candidate = ancestor / "docs" / "adr" / "0014-workspace-lookup-and-pivot.md"
        if candidate.exists():
            adr_path = candidate
            break
    else:
        pytest.skip(
            "ADR-0014 file not reachable from this pytest run (likely "
            "the FastAPI container which only mounts /app). Run on host "
            "for full coverage."
        )

    contents = adr_path.read_text(encoding="utf-8")
    # Spot-check the three options + the chosen one are documented.
    for marker in (
        "Option A: Leave the 6 sites as-is",
        "Option C: New `lookup_and_rescope` helper",
        "**Option C**",  # The decision line.
    ):
        assert marker in contents, f"ADR-0014 missing section: {marker!r}"
