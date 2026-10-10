"""REQUIRE_LIVE_DB: a job that provisions the database must not be able to skip its tests.

THE PROBLEM
    The live-Postgres modules guard themselves with ``pytest.skip`` so a laptop
    without the stack still gets a clean run: no Postgres at ``PG_DSN``, a
    relation the raw SQL layer should have created, a role the migrations
    should have made. In a developer's shell that is the right answer. In the
    two CI jobs that stand a Postgres up and migrate it for exactly these
    modules (``integration-postgres`` and ``cron-sweeps-app-role``) it is the
    wrong one: a service that fails to start, a ``db:apply-raw`` that applies
    nothing, or a renamed table turns the job's whole reason for existing into
    a column of "s", and ``pytest`` exits 0.

    The HAT-1 regression test (cron sweeps reading fail-closed tables as
    ``georag_app``) and the SEC-4 export-scope test were both one skipped
    fixture away from being decoration.

THE SWITCH
    ``REQUIRE_LIVE_DB=1`` (also ``true``/``yes``/``on``), set in the job that
    provisions the database, turns every skip into a failure, with the skip's
    own reason in the message, EXCEPT the few skips that depend on what is IN
    the database rather than on the database being there
    (:data:`ALLOWED_SKIP_REASONS`, matched exactly).

    It is implemented once, as report hooks, so it covers every way a test
    can skip (``pytest.skip`` in a fixture or at module level, ``skipif``,
    ``importorskip``) and a skip site added next month is covered without its
    author knowing the switch exists. ``tests/conftest.py`` registers the hooks;
    ``tests/test_require_live_db_switch.py`` runs them in a real nested pytest.

    Unset (the default, and every other job), nothing changes.

DO NOT set it in ``python-test``. That job deliberately leaves
``POSTGRES_USER`` unset so the live-database modules skip there.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import pytest

REQUIRE_LIVE_DB_ENV = "REQUIRE_LIVE_DB"

#: Skips a REQUIRE_LIVE_DB run tolerates, matched EXACTLY against the skip
#: reason. Each depends on what the database or a client delivery contains,
#: not on the database being reachable. Adding an entry is a decision that a
#: gate may legitimately do less than its name says: write why next to it.
ALLOWED_SKIP_REASONS: frozenset[str] = frozenset({
    # test_train_models.py: the smoke trains a model that has to exist already;
    # a CI database seeded with migrations alone has none.
    "No target_models rows in this DB",
    # test_train_models.py: needs one of two specific seeded workspaces.
    "No suitable workspace in this DB",
    # test_discover_trace_write_integration.py: reads a client delivery that is
    # never committed to the repository.
    "RedStar delivery not present",
})

_SKIPPED_PREFIX = "Skipped: "


def live_db_required(environ: Mapping[str, str] | None = None) -> bool:
    """True when this run is in a job that provisions the database."""
    env = os.environ if environ is None else environ
    return env.get(REQUIRE_LIVE_DB_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def skip_reason(report: Any) -> str | None:
    """The reason a skipped report carries, or None if it is not a plain skip.

    A skipped ``TestReport`` / ``CollectReport`` stores ``longrepr`` as
    ``(path, lineno, "Skipped: <reason>")``. An xfail that failed also reports
    outcome "skipped" but carries ``wasxfail``; that one is expected, not a skip.
    """
    if not getattr(report, "skipped", False) or hasattr(report, "wasxfail"):
        return None
    longrepr = getattr(report, "longrepr", None)
    is_triple = isinstance(longrepr, tuple) and len(longrepr) == 3
    reason = str(longrepr[2]) if is_triple else str(longrepr or "")
    return reason[len(_SKIPPED_PREFIX):] if reason.startswith(_SKIPPED_PREFIX) else reason


def enforce(report: Any, environ: Mapping[str, str] | None = None) -> bool:
    """Turn an environment skip into a failure, in place. True if it did.

    Changes ``report.outcome`` to "failed" and ``report.longrepr`` to a message
    that names the skip reason and what to do about it. A failed collection
    report is a collection error, so a module-level skip aborts the run.
    """
    if not live_db_required(environ):
        return False
    reason = skip_reason(report)
    if reason is None or reason in ALLOWED_SKIP_REASONS:
        return False

    report.outcome = "failed"
    report.longrepr = (
        f"{REQUIRE_LIVE_DB_ENV}=1: this test was SKIPPED, and a job that provisions the "
        "live database is not allowed to skip it.\n"
        f"  skip reason: {reason}\n"
        "  Either the database / role / relation it needs is missing from the job "
        "(fix the job), or the skip depends on what is IN the database and not on "
        "the database being there; in that case add the exact reason to "
        "ALLOWED_SKIP_REASONS in tests/_live_db.py, with the reason it is acceptable."
    )
    return True


# --------------------------------------------------------------------------- #
# Hooks. Imported by tests/conftest.py (and loaded with `-p tests._live_db` by
# the nested-pytest tests), so they stay in this module with the logic.
# --------------------------------------------------------------------------- #


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any):
    outcome = yield
    enforce(outcome.get_result())


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: Any):
    outcome = yield
    enforce(outcome.get_result())


# --------------------------------------------------------------------------- #
# Shared fixture data for the modules that hang rows off a canary workspace.
# --------------------------------------------------------------------------- #

#: The workspace / project `test_ingest_progress_state_machine.py` provisions and
#: the modules that exercise the same ingest_progress rows reuse.
CANARY_WORKSPACE_ID = "a0000000-0000-0000-0000-00000000feed"
CANARY_PROJECT_ID = "b1000000-0000-0000-0000-0000000000a0"


async def ensure_canary_workspace_and_project(
    dsn: str,
    *,
    workspace_id: str = CANARY_WORKSPACE_ID,
    project_id: str = CANARY_PROJECT_ID,
    name: str = "state-machine-tests",
) -> None:
    """Create the canary workspace and project if they are not there.

    Idempotent (ON CONFLICT DO NOTHING) and byte-for-byte the INSERTs
    `test_ingest_progress_state_machine.py` has always run, so whichever module
    runs first provisions it and the others find it. They used to SKIP when it
    was missing ("run test_ingest_progress_state_machine.py first"), which made
    their result depend on module order: run alone, on a freshly migrated
    database, every test in them skipped.

    Needs a login that can INSERT into silver.workspaces / silver.projects (the
    CI jobs connect as the superuser); a login that cannot is an environment
    problem and the caller turns the error into a skip.
    """
    import asyncpg  # noqa: PLC0415 — keeps this module importable without a driver

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO silver.workspaces (workspace_id, name, slug)
            VALUES ($1::uuid, $2::text, $2::text || '-' || substring($1::text from 1 for 8))
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            workspace_id, name,
        )
        await conn.execute(
            """
            INSERT INTO silver.projects (
                project_id, project_name, slug, workspace_id,
                crs_datum, orientation_reference, status
            ) VALUES (
                $1::uuid, $3::text,
                $3::text || '-' || substring($1::text from 1 for 8),
                $2::uuid, 'EPSG:4326', 'grid', 'active'
            )
            ON CONFLICT (project_id) DO NOTHING
            """,
            project_id, workspace_id, name,
        )
    finally:
        await conn.close()
