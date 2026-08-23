"""Every ingest workflow must be able to close its own progress row.

Hatchet's ``on_failure_task`` is the only thing that runs when the engine
cancels a workflow *before* its body does — concurrency-queue expiry, a
manual cancel, a worker SIGTERM. ``start_run`` has already created the
ingest_progress row by then, and nothing else will close it.

ingest_pdf, ingest_zip_archive and tiff_normalize grew hooks after the
Cameco incident (529 runs silently CANCELLED). ingest_spatial,
ingest_tabular and ingest_well_logs never did, so a cancelled shapefile or
assay-CSV upload sat at 'queued' — showing as in-progress in the UI — until
the 15-minute stale sweep timed it out.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.hatchet_workflows import _progress
from app.hatchet_workflows.ingest_spatial import ingest_spatial
from app.hatchet_workflows.ingest_tabular import ingest_tabular
from app.hatchet_workflows.ingest_well_logs import ingest_well_logs
from app.hatchet_workflows.ingest_zip_archive import ingest_zip_archive
from app.hatchet_workflows.tiff_normalize import tiff_normalize

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_RUN = "c2000000-0000-0000-0000-000000000007"


class _Ctx:
    """Stands in for Hatchet's Context inside an on_failure hook."""

    def __init__(self, errors: dict | None = None):
        self.task_run_errors = errors if errors is not None else {}


class TestEveryIngestWorkflowRegistersAFailureHook:
    @pytest.mark.parametrize(
        "workflow",
        [
            ingest_spatial,
            ingest_tabular,
            ingest_well_logs,
            # The three that already had one — kept here so a refactor that
            # drops a hook is caught for all six, not just the new three.
            ingest_zip_archive,
            tiff_normalize,
        ],
        ids=lambda wf: wf.name,
    )
    def test_a_failure_hook_is_registered(self, workflow) -> None:
        hook = getattr(workflow, "_on_failure_task", None)
        assert hook is not None, (
            f"{workflow.name} has no on_failure_task — a Hatchet cancellation "
            "leaves its ingest_progress row non-terminal until the stale sweep "
            "finds it 15 minutes later."
        )

    def test_ingest_pdf_too(self) -> None:
        # Imported separately: ingest_pdf is heavy and the parametrize list
        # above already exercises the assertion shape.
        from app.hatchet_workflows.ingest_pdf import ingest_pdf

        assert getattr(ingest_pdf, "_on_failure_task", None) is not None


class TestCloseRunAfterWorkflowFailure:
    """The shared implementation the three new hooks delegate to."""

    @pytest.mark.asyncio
    async def test_it_marks_the_row_failed_with_the_real_exception(self) -> None:
        with (
            patch.object(_progress, "get_run", AsyncMock(
                return_value={"current_stage": "parse"},
            )),
            patch.object(
                _progress, "mark_failed_by_run", AsyncMock(return_value=True),
            ) as marked,
        ):
            out = await _progress.close_run_after_workflow_failure(
                workflow_name="ingest_spatial",
                workspace_id=_WS,
                project_id=None,
                minio_key=f"spatial/{_PJ}/faults.gpkg",
                run_id=_RUN,
                ctx=_Ctx({"parse": "GDAL: not a geopackage"}),
            )

        assert out == {
            "updated": True, "run_id": _RUN, "current_stage": "parse",
        }
        kwargs = marked.await_args.kwargs
        assert kwargs["run_id"] == _RUN
        assert kwargs["stage"] == "parse"
        # The real upstream error, not a placeholder — otherwise every
        # failure reads the same in the Ingestion Runs UI and a recurring
        # one cannot be root-caused from there at all.
        assert "GDAL: not a geopackage" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_a_cancellation_with_no_captured_error_says_so(self) -> None:
        """The common case: the body never ran, so there is no exception.

        Recording that plainly is more useful than an invented reason.
        """
        with (
            patch.object(_progress, "get_run", AsyncMock(return_value=None)),
            patch.object(
                _progress, "mark_failed_by_run", AsyncMock(return_value=True),
            ) as marked,
        ):
            await _progress.close_run_after_workflow_failure(
                workflow_name="ingest_tabular",
                workspace_id=_WS,
                project_id=None,
                minio_key=f"collars/{_PJ}/collars.csv",
                run_id=_RUN,
                ctx=_Ctx({}),
            )

        assert "no task_run_errors available" in marked.await_args.kwargs["error"]
        assert marked.await_args.kwargs["stage"] == "unknown"

    @pytest.mark.asyncio
    async def test_it_resolves_the_run_from_the_key_when_run_id_is_absent(
        self,
    ) -> None:
        """`run_id` is optional on all three input models."""
        with (
            patch.object(
                _progress, "lookup_active_run_id", AsyncMock(return_value=_RUN),
            ) as lookup,
            patch.object(_progress, "get_run", AsyncMock(return_value=None)),
            patch.object(
                _progress, "mark_failed_by_run", AsyncMock(return_value=True),
            ) as marked,
        ):
            out = await _progress.close_run_after_workflow_failure(
                workflow_name="ingest_well_logs",
                workspace_id=_WS,
                project_id=None,
                minio_key=f"well_logs/{_PJ}/hole.las",
                run_id=None,
                ctx=_Ctx(),
            )

        assert lookup.await_args.kwargs["minio_key"].endswith("hole.las")
        assert marked.await_args.kwargs["run_id"] == _RUN
        assert out["updated"] is True

    @pytest.mark.asyncio
    async def test_no_resolvable_run_is_reported_not_raised(self) -> None:
        """A cancellation before dispatch leaves no row to close.

        Raising here would fail the failure hook itself, which Hatchet
        surfaces as a second, more confusing error on top of the first.
        """
        with (
            patch.object(
                _progress, "lookup_active_run_id", AsyncMock(return_value=None),
            ),
            patch.object(
                _progress, "mark_failed_by_run", AsyncMock(),
            ) as marked,
        ):
            out = await _progress.close_run_after_workflow_failure(
                workflow_name="ingest_spatial",
                workspace_id=_WS,
                project_id=None,
                minio_key=f"spatial/{_PJ}/faults.gpkg",
                run_id=None,
                ctx=_Ctx(),
            )

        assert out == {"updated": False, "reason": "no_active_run"}
        marked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_cannot_clobber_a_row_the_body_already_closed(self) -> None:
        """mark_failed_by_run is a conditional update returning False.

        The hook and the body's own except-block both fire on an ordinary
        exception, so this overlap is the normal case, not an edge one.
        """
        with (
            patch.object(_progress, "get_run", AsyncMock(
                return_value={"current_stage": "persist"},
            )),
            patch.object(
                _progress, "mark_failed_by_run", AsyncMock(return_value=False),
            ),
            patch(
                "app.services.laravel_bridge.post_ingestion_progress",
                AsyncMock(),
            ) as broadcast,
        ):
            out = await _progress.close_run_after_workflow_failure(
                workflow_name="ingest_tabular",
                workspace_id=_WS,
                project_id=_PJ,
                minio_key=f"excel/{_PJ}/book.xlsx",
                run_id=_RUN,
                ctx=_Ctx(),
            )

        assert out["updated"] is False
        # No transition means no second "your upload failed" broadcast for a
        # run the user was already told about.
        broadcast.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_broadcasts_to_laravel_on_a_real_transition(self) -> None:
        with (
            patch.object(_progress, "get_run", AsyncMock(
                return_value={"current_stage": "parse"},
            )),
            patch.object(
                _progress, "mark_failed_by_run", AsyncMock(return_value=True),
            ),
            patch(
                "app.services.laravel_bridge.post_ingestion_progress",
                AsyncMock(),
            ) as broadcast,
        ):
            await _progress.close_run_after_workflow_failure(
                workflow_name="ingest_spatial",
                workspace_id=_WS,
                project_id=_PJ,
                minio_key=f"spatial/{_PJ}/faults.gpkg",
                run_id=_RUN,
                ctx=_Ctx(),
            )

        broadcast.assert_awaited_once()
        assert broadcast.await_args.kwargs["status"] == "failed"

    @pytest.mark.asyncio
    async def test_a_broadcast_failure_does_not_undo_the_terminal_write(
        self,
    ) -> None:
        """Closing the row is the job; telling Laravel is best-effort."""
        with (
            patch.object(_progress, "get_run", AsyncMock(
                return_value={"current_stage": "parse"},
            )),
            patch.object(
                _progress, "mark_failed_by_run", AsyncMock(return_value=True),
            ),
            patch(
                "app.services.laravel_bridge.post_ingestion_progress",
                AsyncMock(side_effect=RuntimeError("laravel is down")),
            ),
        ):
            out = await _progress.close_run_after_workflow_failure(
                workflow_name="ingest_spatial",
                workspace_id=_WS,
                project_id=_PJ,
                minio_key=f"spatial/{_PJ}/faults.gpkg",
                run_id=_RUN,
                ctx=_Ctx(),
            )

        assert out["updated"] is True

    @pytest.mark.asyncio
    async def test_a_context_without_task_run_errors_is_tolerated(self) -> None:
        """Hatchet only populates it for on_failure hooks, and versions vary."""
        with (
            patch.object(_progress, "get_run", AsyncMock(return_value=None)),
            patch.object(
                _progress, "mark_failed_by_run", AsyncMock(return_value=True),
            ) as marked,
        ):
            await _progress.close_run_after_workflow_failure(
                workflow_name="ingest_spatial",
                workspace_id=_WS,
                project_id=None,
                minio_key=f"spatial/{_PJ}/a.gpkg",
                run_id=_RUN,
                ctx=object(),
            )

        assert "no task_run_errors available" in marked.await_args.kwargs["error"]
