"""A failed FIRST attempt must not make the run terminal.

The four geology ingesters (tabular, spatial, well logs, geophysics) have
``retries=1`` and used to close their ``silver.ingest_progress`` row 'failed'
from an outer ``except`` on EVERY attempt, then re-raise. 'failed' is
terminal and immutable, so on the retry ``start_run`` /
``mark_stage_started`` / ``mark_completed_by_run`` were all no-ops against the
row: a retry that SUCCEEDED left the run 'failed', and because
``mark_completed_by_run`` returned False the completion broadcast (and, for
tabular, the gold promotion) never fired. The data had landed and nothing
downstream was told.

``_progress.is_final_attempt`` decides: the body closes the row only on the
last attempt (or for an exception Hatchet will not retry); the workflow's
``on_failure_task`` is the backstop.

Two layers here:
  * unit - the helper, and each workflow's except block, with the progress
    writers mocked (first attempt: no terminal write; last attempt: one);
  * integration - the real state machine in Postgres: attempt 1 fails and the
    row is still open, attempt 2 succeeds and the row is completed and the
    completion is broadcast. That is the claim, end to end.
"""
from __future__ import annotations

import contextlib
import os
import types
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hatchet_sdk import NonRetryableException

from app.hatchet_workflows import _progress

WS = "a0000000-0000-0000-0000-00000000feed"
PROJECT = "b1000000-0000-0000-0000-0000000000a0"
RUN = "c2000000-0000-0000-0000-000000000009"


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
def _ctx(attempt: int, of: int) -> Any:
    return types.SimpleNamespace(attempt_number=attempt, max_attempts=of)


def test_only_the_last_attempt_is_final() -> None:
    assert _progress.is_final_attempt(_ctx(1, 2), RuntimeError("x")) is False
    assert _progress.is_final_attempt(_ctx(2, 2), RuntimeError("x")) is True
    assert _progress.is_final_attempt(_ctx(1, 1), RuntimeError("x")) is True  # retries=0
    assert _progress.is_final_attempt(_ctx(3, 3), RuntimeError("x")) is True


def test_an_exception_hatchet_will_not_retry_is_final_on_any_attempt() -> None:
    assert _progress.is_final_attempt(_ctx(1, 2), NonRetryableException("no")) is True


def test_a_context_that_cannot_say_is_never_final() -> None:
    """Not closing early is always safe (the failure hook closes the row);
    closing early is the bug."""
    assert _progress.is_final_attempt(object(), RuntimeError("x")) is False
    assert _progress.is_final_attempt(None, RuntimeError("x")) is False
    assert _progress.is_final_attempt(types.SimpleNamespace(attempt_number="?", max_attempts=2)) is False


def test_the_real_mock_context_reports_the_tasks_own_retries() -> None:
    """hatchet_sdk's mock context carries retries + 1 as max_attempts, so
    ``retry_count=1`` of a ``retries=1`` task is the last attempt."""
    from app.hatchet_workflows.ingest_well_logs import run_ingest_well_logs

    first = run_ingest_well_logs._create_mock_context(None, retry_count=0)
    last = run_ingest_well_logs._create_mock_context(None, retry_count=1)
    assert (first.attempt_number, first.max_attempts) == (1, 2)
    assert (last.attempt_number, last.max_attempts) == (2, 2)
    assert _progress.is_final_attempt(first, RuntimeError("x")) is False
    assert _progress.is_final_attempt(last, RuntimeError("x")) is True


# ---------------------------------------------------------------------------
# Each ingester's except block (progress writers mocked)
# ---------------------------------------------------------------------------
class _Progress:
    """The progress writers an ingester calls, replaced and recording."""

    def __init__(self) -> None:
        self.failed = AsyncMock(return_value=True)
        self.broadcast = AsyncMock()

    def patches(self) -> list[Any]:
        return [
            patch.object(_progress, "start_run", AsyncMock(return_value=RUN)),
            patch.object(_progress, "mark_stage_started", AsyncMock()),
            patch.object(_progress, "mark_completed_by_run", AsyncMock(return_value=True)),
            patch.object(_progress, "mark_failed_by_run", self.failed),
            patch.object(_progress, "broadcast_terminal", self.broadcast),
        ]


def _exploding_store(exc: Exception) -> MagicMock:
    store = MagicMock()
    store.get_file = MagicMock(side_effect=exc)
    return store


async def _run(task: Any, payload: Any, module: str, *, retry_count: int, prog: _Progress) -> None:
    patches = [
        patch(f"{module}.get_storage_client", return_value=_exploding_store(OSError("s3 blip"))),
        *prog.patches(),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(OSError, match="s3 blip"):
            await task.aio_mock_run(payload, retry_count=retry_count)


def _tabular() -> tuple[Any, Any, str]:
    from app.hatchet_workflows.ingest_tabular import IngestTabularInput, run_ingest_tabular

    return (
        run_ingest_tabular,
        IngestTabularInput(
            workspace_id=WS, project_id=PROJECT, minio_key="bronze/p/collars.csv", run_id=RUN,
        ),
        "app.hatchet_workflows.ingest_tabular",
    )


def _spatial() -> tuple[Any, Any, str]:
    from app.hatchet_workflows.ingest_spatial import IngestSpatialInput, run_ingest_spatial

    return (
        run_ingest_spatial,
        IngestSpatialInput(
            workspace_id=WS, project_id=PROJECT, minio_key="bronze/p/faults.geojson", run_id=RUN,
        ),
        "app.hatchet_workflows.ingest_spatial",
    )


def _well_logs() -> tuple[Any, Any, str]:
    from app.hatchet_workflows.ingest_well_logs import IngestWellLogsInput, run_ingest_well_logs

    return (
        run_ingest_well_logs,
        IngestWellLogsInput(
            workspace_id=WS, project_id=PROJECT, minio_key="bronze/p/eagle.las", run_id=RUN,
        ),
        "app.hatchet_workflows.ingest_well_logs",
    )


def _geophysics() -> tuple[Any, Any, str]:
    from app.hatchet_workflows.ingest_geophysics import (
        IngestGeophysicsInput,
        run_ingest_geophysics,
    )

    return (
        run_ingest_geophysics,
        IngestGeophysicsInput(
            workspace_id=WS, project_id=PROJECT, minio_key="bronze/p/survey.xyz", run_id=RUN,
        ),
        "app.hatchet_workflows.ingest_geophysics",
    )


@pytest.mark.parametrize("make", [_tabular, _spatial, _well_logs, _geophysics])
async def test_a_first_attempt_failure_leaves_the_row_open(make: Any) -> None:
    task, payload, module = make()
    prog = _Progress()

    await _run(task, payload, module, retry_count=0, prog=prog)

    prog.failed.assert_not_awaited()
    prog.broadcast.assert_not_awaited()


@pytest.mark.parametrize("make", [_tabular, _spatial, _well_logs, _geophysics])
async def test_the_last_attempt_closes_the_row(make: Any) -> None:
    task, payload, module = make()
    prog = _Progress()

    await _run(task, payload, module, retry_count=1, prog=prog)

    prog.failed.assert_awaited_once()
    assert prog.failed.await_args.kwargs["run_id"] == RUN
    assert "s3 blip" in prog.failed.await_args.kwargs["error"]


async def test_spatial_tells_the_page_only_when_the_failure_is_final() -> None:
    """The early 'failed' broadcast was sent on every attempt, so the page
    showed a failure for a run whose retry then succeeded."""
    task, payload, module = _spatial()

    first = _Progress()
    await _run(task, payload, module, retry_count=0, prog=first)
    first.broadcast.assert_not_awaited()

    last = _Progress()
    await _run(task, payload, module, retry_count=1, prog=last)
    last.broadcast.assert_awaited_once()
    assert last.broadcast.await_args.kwargs["status"] == "failed"
    assert "s3 blip" in last.broadcast.await_args.kwargs["message"]


# ---------------------------------------------------------------------------
# End to end against the real state machine
# ---------------------------------------------------------------------------
pg = pytest.mark.integration


async def _dsn() -> str:
    return _progress._dsn()


@pytest.fixture
async def seeded_project():  # noqa: ANN201
    """A workspace + project to hang progress rows on, and a fresh pool."""
    if not os.environ.get("POSTGRES_USER"):
        pytest.skip("postgres env not configured")
    import asyncpg

    if _progress._pool is not None:
        with contextlib.suppress(Exception):
            await _progress._pool.close()
        _progress._pool = None

    ws, project = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Postgres: {exc}")
    try:
        await conn.execute(
            "INSERT INTO silver.workspaces (workspace_id, name, slug) "
            "VALUES ($1::uuid, 'retry-test', $2)", ws, f"retry-{ws[:8]}",
        )
        await conn.execute(
            "INSERT INTO silver.projects (project_id, project_name, slug, workspace_id, "
            "crs_datum, orientation_reference, status) "
            "VALUES ($1::uuid, 'retry-test', $2, $3::uuid, 'EPSG:4326', 'grid', 'active')",
            project, f"retry-{project[:8]}", ws,
        )
    finally:
        await conn.close()
    try:
        yield ws, project
    finally:
        if _progress._pool is not None:
            with contextlib.suppress(Exception):
                await _progress._pool.close()
            _progress._pool = None
        conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
        try:
            await conn.execute("DELETE FROM silver.ingest_progress WHERE workspace_id = $1::uuid", ws)
            await conn.execute("DELETE FROM silver.projects WHERE project_id = $1::uuid", project)
            await conn.execute("DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", ws)
        finally:
            await conn.close()


@pg
async def test_a_retry_that_succeeds_completes_the_run_and_broadcasts(
    seeded_project: tuple[str, str],
) -> None:
    """The reported defect, end to end.

    Attempt 1 raises; the row must still be open. Attempt 2 succeeds; the row
    must be completed and the completion broadcast must fire. Before the fix
    attempt 1 closed the row 'failed', attempt 2's ``mark_completed_by_run``
    was a no-op that returned False, and the broadcast was skipped.
    """
    from app.hatchet_workflows.ingest_well_logs import IngestWellLogsInput, run_ingest_well_logs
    from tests.test_ingest_well_logs_workflow import COLLAR, FakeConn, las_result

    ws, project = seeded_project
    run_id = str(uuid.uuid4())
    key = f"uploads/eagle/{run_id}.las"
    payload = IngestWellLogsInput(workspace_id=ws, project_id=project, minio_key=key, run_id=run_id)

    store = MagicMock()
    store.get_file = MagicMock(side_effect=[OSError("s3 blip"), None])
    broadcast = AsyncMock()
    module = "app.hatchet_workflows.ingest_well_logs"
    with (
        patch(f"{module}.get_storage_client", return_value=store),
        patch("georag_geoparsers.las_parser.parse_las_file", MagicMock(return_value=las_result())),
        patch(f"{module}.asyncpg.connect", AsyncMock(return_value=FakeConn())),
        patch(f"{module}.bind_workspace_scope", AsyncMock()),
        patch(f"{module}._collar_index", AsyncMock(return_value={"EAGLE PT #1": COLLAR})),
        patch.object(_progress, "broadcast_terminal", broadcast),
    ):
        with pytest.raises(OSError, match="s3 blip"):
            await run_ingest_well_logs.aio_mock_run(payload, retry_count=0)

        after_first = await _progress.get_run(run_id=run_id)
        assert after_first is not None
        assert after_first["status"] == "started", (
            "attempt 1 of 2 must leave the row open for the retry; "
            f"it is {after_first['status']!r} with error_text={after_first['error_text']!r}"
        )
        broadcast.assert_not_awaited()

        out = await run_ingest_well_logs.aio_mock_run(payload, retry_count=1)

    assert out.curves_written == 1
    done = await _progress.get_run(run_id=run_id)
    assert done is not None
    assert done["status"] in {"completed", "partial"}, done["status"]
    assert done["error_text"] is None
    broadcast.assert_awaited_once()
    assert broadcast.await_args.kwargs["run_id"] == run_id


@pg
async def test_two_failed_attempts_still_end_failed(seeded_project: tuple[str, str]) -> None:
    from app.hatchet_workflows.ingest_well_logs import IngestWellLogsInput, run_ingest_well_logs

    ws, project = seeded_project
    run_id = str(uuid.uuid4())
    payload = IngestWellLogsInput(
        workspace_id=ws, project_id=project, minio_key=f"uploads/eagle/{run_id}.las", run_id=run_id,
    )
    store = MagicMock()
    store.get_file = MagicMock(side_effect=OSError("s3 blip"))
    module = "app.hatchet_workflows.ingest_well_logs"
    with patch(f"{module}.get_storage_client", return_value=store):
        for retry_count in (0, 1):
            with pytest.raises(OSError, match="s3 blip"):
                await run_ingest_well_logs.aio_mock_run(payload, retry_count=retry_count)
            row = await _progress.get_run(run_id=run_id)
            assert row is not None
            assert row["status"] == ("started" if retry_count == 0 else "failed")

    assert "s3 blip" in (row["error_text"] or "")


@pg
async def test_the_failure_hook_closes_a_row_the_body_left_open(
    seeded_project: tuple[str, str],
) -> None:
    """The change is only safe because ``on_failure_task`` is the backstop:
    when the last attempt dies before its own handler runs (worker killed,
    run cancelled) the hook is what turns the open row 'failed'."""
    from app.hatchet_workflows.ingest_well_logs import IngestWellLogsInput, run_ingest_well_logs

    ws, project = seeded_project
    run_id = str(uuid.uuid4())
    key = f"uploads/eagle/{run_id}.las"
    payload = IngestWellLogsInput(workspace_id=ws, project_id=project, minio_key=key, run_id=run_id)
    store = MagicMock()
    store.get_file = MagicMock(side_effect=OSError("s3 blip"))
    module = "app.hatchet_workflows.ingest_well_logs"
    with patch(f"{module}.get_storage_client", return_value=store):
        with pytest.raises(OSError):
            await run_ingest_well_logs.aio_mock_run(payload, retry_count=0)
    assert (await _progress.get_run(run_id=run_id))["status"] == "started"

    with patch("app.services.laravel_bridge.post_ingestion_progress", AsyncMock()):
        # What Hatchet's failure hook runs once the retries are exhausted.
        result = await _progress.close_run_after_workflow_failure(
            workflow_name="ingest_well_logs",
            workspace_id=ws, project_id=project, minio_key=key, run_id=run_id,
            ctx=types.SimpleNamespace(task_run_errors={"run_ingest_well_logs": "OSError: s3 blip"}),
        )

    assert result["updated"] is True
    row = await _progress.get_run(run_id=run_id)
    assert row["status"] == "failed"
    assert "s3 blip" in row["error_text"]
