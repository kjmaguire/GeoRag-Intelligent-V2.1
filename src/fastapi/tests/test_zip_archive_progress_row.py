"""The ZIP archive workflow must drive its own silver.ingest_progress row.

Measured on 2026-09-02 on a RedStar delivery: every ``.zip`` uploaded under
the ``archive`` category showed up on the Ingestion Runs page twice — the
member shapefile's run (``Completed 170 rows``) beside a second row for the
same filename reading ``Failed (step 0 of 5)`` with
``{"reason":"stale_heartbeat","detected_by":"stale_run_sweep"}``.

The failed row was the archive's own. ``trigger_ingest_zip_archive`` inserts
it at dispatch time (status ``queued``, step 0) so a cancelled run stays
visible, but ``ingest_zip_archive`` tracked itself in
``silver.archive_ingest_runs`` only and never touched that row again. Nothing
could advance or close it; fifteen minutes later the stale sweep did, and
because ``queued`` is outside the sweep's retry stages the phantom was
permanent. It also blocked the nightly bronze recovery for that key, whose
orphan predicate is "no ingest_progress row".

These tests pin the lifecycle the workflow now runs on that row, without a
live engine or database: the calls are asserted against the source (the
convention test_ingest_zip_archive_observability.py already uses) and the
pure helpers are exercised directly.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock

import pytest

from app.hatchet_workflows import ingest_zip_archive as module

_SRC = (
    pathlib.Path(__file__).parents[1]
    / "app"
    / "hatchet_workflows"
    / "ingest_zip_archive.py"
).read_text(encoding="utf-8")


def _function(name: str) -> ast.AST:
    tree = ast.parse(_SRC)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name == name
        ):
            return node
    raise AssertionError(f"{name} not found in ingest_zip_archive.py")


def _count_keys_subscripted(node: ast.AST) -> set[str]:
    """Every literal key written as ``counts["..."]`` inside *node*."""
    keys: set[str] = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Subscript)
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "counts"
            and isinstance(sub.slice, ast.Constant)
            and isinstance(sub.slice.value, str)
        ):
            keys.add(sub.slice.value)
    return keys


# ---------------------------------------------------------------------------
# The row is adopted, advanced, and closed
# ---------------------------------------------------------------------------


def test_workflow_adopts_the_dispatch_time_row_under_the_callers_run_id() -> None:
    body = ast.get_source_segment(_SRC, _function("run_zip_ingest")) or ""
    assert "ingest_progress.start_run(" in body, (
        "run_zip_ingest must upsert the ingest_progress row the trigger "
        "endpoint created — start_run is an ON CONFLICT DO NOTHING upsert, "
        "so calling it here adopts that row rather than minting a second one."
    )
    assert "run_id=input.run_id" in body, (
        "the upsert must use the caller's run_id, or it lands under a fresh "
        "uuid and the dispatch-time row stays queued forever"
    )


@pytest.mark.parametrize("stage", ["preflight", "parse", "persist"])
def test_workflow_marks_each_stage_on_the_row(stage: str) -> None:
    body = ast.get_source_segment(_SRC, _function("run_zip_ingest")) or ""
    assert f'stage="{stage}"' in body, (
        f"run_zip_ingest must call mark_stage_started(stage={stage!r}); the "
        "first transition is what flips the row from 'queued' to 'started' "
        "and writes its first heartbeat"
    )


def test_workflow_heartbeats_through_download_and_extraction() -> None:
    body = ast.get_source_segment(_SRC, _function("run_zip_ingest")) or ""
    assert "heartbeat_loop(run_id=progress_run_id)" in body, (
        "a multi-GB archive downloads and extracts for longer than the "
        "15-minute sweep window with no stage transition in between; the "
        "ticker is the only thing keeping the row alive meanwhile"
    )
    assert "mark_stage_progress(" in body, (
        "the fan-out loop must report progress on the row — it doubles as "
        "the heartbeat for a 50,000-member archive"
    )


def test_workflow_closes_the_row_with_the_archive_accounting() -> None:
    body = ast.get_source_segment(_SRC, _function("run_zip_ingest")) or ""
    assert "ingest_progress.mark_completed_by_run(" in body
    assert "_DISPATCHED_COUNT_KEYS" in body, (
        "rows_written for an archive is the number of members handed to an "
        "ingester — the sum over _DISPATCHED_COUNT_KEYS"
    )
    assert "_archive_warnings(" in body


def test_failure_hook_closes_the_row_before_the_archive_run() -> None:
    body = ast.get_source_segment(_SRC, _function("on_failure")) or ""
    assert "close_run_after_workflow_failure(" in body, (
        "on_failure must close the ingest_progress row: it is the only hook "
        "that fires when Hatchet cancels the run before the body executes"
    )
    assert body.index("close_run_after_workflow_failure(") < body.index(
        "lookup_archive_run_id_by_run_id("
    ), (
        "the progress row is closed FIRST — the hook used to return early "
        "when no archive_ingest_runs row existed, which is exactly the "
        "cancelled-before-start case where the progress row is the only "
        "trace of the upload"
    )


# ---------------------------------------------------------------------------
# counts: every bucket a branch bumps exists before the loop starts
# ---------------------------------------------------------------------------


def test_every_counts_bucket_a_branch_increments_is_initialised() -> None:
    """``counts["tabular"] += 1`` on a dict without the key is a KeyError.

    The per-file try/except turned that into ``errors += 1``, so a
    standalone .dbf/.dat inside a ZIP was dispatched correctly and then
    reported as a failed member — the archive closed 'partial' with
    "1 of N files failed" for a file that had landed.
    """
    used = _count_keys_subscripted(_function("_ingest_one")) | _count_keys_subscripted(
        _function("run_zip_ingest")
    )
    missing = used - set(module._COUNT_KEYS)
    assert not missing, (
        f"counts buckets incremented but never initialised: {sorted(missing)} "
        "— add them to _COUNT_KEYS in ingest_zip_archive.py"
    )


def test_counts_dict_is_built_from_the_pinned_key_list() -> None:
    body = ast.get_source_segment(_SRC, _function("run_zip_ingest")) or ""
    assert "dict.fromkeys(_COUNT_KEYS, 0)" in body


def test_dispatched_keys_are_a_subset_of_all_keys() -> None:
    assert set(module._DISPATCHED_COUNT_KEYS) <= set(module._COUNT_KEYS)
    assert "tabular" in module._DISPATCHED_COUNT_KEYS


# ---------------------------------------------------------------------------
# _archive_warnings
# ---------------------------------------------------------------------------


def _counts(**overrides: int) -> dict[str, int]:
    counts = dict.fromkeys(module._COUNT_KEYS, 0)
    counts.update(overrides)
    return counts


def test_clean_archive_has_no_warnings() -> None:
    assert (
        module._archive_warnings(
            total=3,
            counts=_counts(spatial=3),
            errors=[],
            unhandled=[],
        )
        == []
    )


def test_member_errors_become_a_named_warning() -> None:
    errors = [{"file": "bad.las", "ext": "las", "error": "boom"}]
    (warning,) = module._archive_warnings(
        total=4,
        counts=_counts(las=3, errors=1),
        errors=errors,
        unhandled=[],
    )
    assert warning["code"] == "archive_member_failed"
    assert "1 of 4" in warning["detail"]
    assert "bad.las" in warning["detail"]


def test_unhandled_members_are_named_rather_than_silently_dropped() -> None:
    (warning,) = module._archive_warnings(
        total=2,
        counts=_counts(pdf=1, unknown=1),
        errors=[],
        unhandled=["README.docx"],
    )
    assert warning["code"] == "archive_member_unhandled"
    assert "README.docx" in warning["detail"]


def test_skipped_members_are_counted() -> None:
    (warning,) = module._archive_warnings(
        total=2,
        counts=_counts(las=1, skipped=1),
        errors=[],
        unhandled=[],
    )
    assert warning["code"] == "archive_member_skipped"
    assert warning["detail"].startswith("1 member file(s)")


def test_names_are_capped_so_the_detail_stays_readable() -> None:
    names = [f"f{i}.txt" for i in range(8)]
    rendered = module._names(names)
    assert "f4.txt" in rendered and "f5.txt" not in rendered
    assert "(+3 more)" in rendered
    assert module._names(names[:2]) == "f0.txt, f1.txt"


# ---------------------------------------------------------------------------
# _progress_row_lifecycle
# ---------------------------------------------------------------------------


async def test_lifecycle_marks_the_row_failed_and_reraises(monkeypatch) -> None:
    mark_failed = AsyncMock(return_value=True)
    monkeypatch.setattr(module.ingest_progress, "mark_failed_by_run", mark_failed)

    with pytest.raises(ValueError, match="zip-bomb"):
        async with module._progress_row_lifecycle("run-1"):
            raise ValueError("zip-bomb guard")

    mark_failed.assert_awaited_once()
    kwargs = mark_failed.await_args.kwargs
    assert kwargs["run_id"] == "run-1"
    assert kwargs["error"].startswith("ValueError: zip-bomb guard")
    assert "stage" not in kwargs, (
        "stage is left unset so the conditional update keeps whatever "
        "mark_stage_started last wrote — that is where the failure happened"
    )


async def test_lifecycle_is_a_no_op_without_a_row(monkeypatch) -> None:
    mark_failed = AsyncMock()
    monkeypatch.setattr(module.ingest_progress, "mark_failed_by_run", mark_failed)

    with pytest.raises(RuntimeError):
        async with module._progress_row_lifecycle(None):
            raise RuntimeError("no row")

    mark_failed.assert_not_awaited()


async def test_lifecycle_does_nothing_on_success(monkeypatch) -> None:
    mark_failed = AsyncMock()
    monkeypatch.setattr(module.ingest_progress, "mark_failed_by_run", mark_failed)

    async with module._progress_row_lifecycle("run-1"):
        pass

    mark_failed.assert_not_awaited()
