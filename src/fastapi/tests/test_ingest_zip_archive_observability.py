"""Pin the audit item C invariant: ingest_zip_archive cannot silently fail.

Background — 2026-06-03 audit Theme D
--------------------------------------
The ingest_zip_archive workflow used to have the cameco-recovery silent-
failure shape: retries=0, no on_failure_task, no progress surface. A
mid-extraction crash returned 201 to the user and then vanished from
operator view. The audit closed the gap with:

  1. A new `silver.archive_ingest_runs` parent table + `archive_run_id`
     FK on `silver.ingest_progress` for per-file lineage.
  2. The `_archive_progress` helper module (parallel to `_progress.py`).
  3. The workflow wraps its body in `_archive_progress.archive_lifecycle`
     and registers an `on_failure_task` as the second backstop.

These tests pin the contract — pure module / file inspection so they
run without a live DB. They assert:

  - The workflow has an `on_failure_task` registered.
  - The workflow imports `_archive_progress`.
  - The `_archive_progress` helper exposes the lifecycle helpers
    the workflow depends on.
"""
from __future__ import annotations

import pathlib

import pytest


def test_workflow_has_on_failure_task():
    """The ingest_zip_archive workflow must register an on_failure_task.

    Without it, Hatchet cancellations / worker crashes leave the
    archive_ingest_runs parent row stuck at status='queued' — the
    exact silent-failure shape the audit closed.
    """
    from app.hatchet_workflows import ingest_zip_archive as module

    workflow = module.ingest_zip_archive
    on_failure = getattr(workflow, "_on_failure_task", None)
    assert on_failure is not None, (
        "ingest_zip_archive workflow has no _on_failure_task. Without it, "
        "Hatchet cancellations + worker SIGKILLs leave silver.archive_ingest_runs "
        "parent rows stuck at status='queued' — the same silent-failure "
        "shape the cameco recovery (2026-06-02) caught and the audit Theme D "
        "closed. See _archive_progress.py for the terminal-mark machinery."
    )


def test_workflow_module_imports_archive_progress():
    """Sanity: the workflow file must reach for the _archive_progress helper."""
    path = pathlib.Path(__file__).parents[1] / "app" / "hatchet_workflows" / "ingest_zip_archive.py"
    src = path.read_text(encoding="utf-8")
    assert "_archive_progress" in src, (
        "ingest_zip_archive.py must import _archive_progress — without the "
        "helper the parent row never gets written and observability vanishes."
    )
    assert "archive_lifecycle" in src, (
        "ingest_zip_archive.py must use archive_lifecycle context manager "
        "so unhandled exceptions automatically transition the parent row to "
        "'failed' instead of leaving it stuck."
    )


def test_archive_progress_module_exports_lifecycle_helpers():
    """The helper module must expose the public surface the workflow uses."""
    from app.hatchet_workflows import _archive_progress as ap

    for name in (
        "start_run",
        "mark_extracting",
        "mark_fanning_out",
        "increment_counts",
        "mark_terminal",
        "lookup_archive_run_id_by_run_id",
        "archive_lifecycle",
        "TERMINAL_STATUSES",
    ):
        assert hasattr(ap, name), (
            f"_archive_progress.{name} missing — the ingest_zip_archive "
            "workflow + on_failure_task depend on it."
        )


def test_terminal_statuses_match_db_check_constraint():
    """The Python-side terminal set must match the DB CHECK constraint.

    Drift here means a Python mark_terminal('xyz') call would
    Pydantic/dataclass-pass and then PostgresCheckViolation-fail at the
    UPDATE — the exact silent-INSERT-failure shape that caught ollama
    in the audit (Theme C). Pin both sides together.
    """
    from app.hatchet_workflows._archive_progress import TERMINAL_STATUSES

    assert set(TERMINAL_STATUSES) == {"completed", "failed", "partial", "cancelled"}, (
        f"TERMINAL_STATUSES drifted: {sorted(TERMINAL_STATUSES)!r}. The DB "
        "CHECK constraint archive_ingest_runs_status_valid pins these four "
        "(plus the non-terminal 'queued', 'extracting', 'fanning_out'). "
        "Update the migration AND TERMINAL_STATUSES in lockstep."
    )


def test_migration_creates_archive_ingest_runs_table():
    """Migration must exist and create silver.archive_ingest_runs.

    The FastAPI container doesn't mount the Laravel `database/`
    directory (only `src/fastapi/` is in the image), so the file
    isn't reachable from inside the container. The PHPUnit sibling
    ``tests/Feature/Tenancy/ArchiveIngestRunsMigrationTest.php``
    runs the full shape assertion on the host side.
    """
    migration = (
        pathlib.Path(__file__).parents[2]
        / "database"
        / "migrations"
        / "2026_06_03_040000_create_silver_archive_ingest_runs.php"
    )
    if not migration.exists():
        pytest.skip(
            "Migration not reachable from FastAPI container — host-side "
            "PHPUnit test (ArchiveIngestRunsMigrationTest) is the source "
            "of truth for this assertion."
        )
    src = migration.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS silver.archive_ingest_runs" in src


def test_workflow_marks_partial_when_per_file_errors_present():
    """The workflow's terminal-mark logic must distinguish partial from
    completed based on per-file error counts.

    Without this distinction, an archive where 40 of 47 files succeeded
    + 7 failed would close as 'completed' and operators would never
    re-investigate. Asserting against the source so a refactor that
    drops the partial-status branch fires loudly.
    """
    path = pathlib.Path(__file__).parents[1] / "app" / "hatchet_workflows" / "ingest_zip_archive.py"
    src = path.read_text(encoding="utf-8")
    # The terminal-mark branch — substring-match because the exact ternary
    # shape may evolve. The two literals AND the comparison must coexist.
    assert "'partial'" in src and "'completed'" in src, (
        "ingest_zip_archive workflow must emit either 'partial' or "
        "'completed' as the terminal status (it's the distinction operators "
        "need to know whether to re-investigate)."
    )
    assert "counts['errors']" in src or 'counts["errors"]' in src, (
        "The terminal-mark branch must consult per-file error counts to "
        "decide partial vs completed."
    )
