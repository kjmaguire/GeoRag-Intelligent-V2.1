"""A rejected upload must fail its run and write no report row.

WHY THIS FILE EXISTS NOW AND NOT BEFORE
    The logic is unchanged; it just became reachable. It used to be 75
    lines inside ``_persist_body``, a 695-line function that needs a
    Hatchet Context, a parsed PDF and a database before any of it runs, so
    the rejection path had no unit and therefore no unit test. L1097's fix
    said to lift exactly this block out first; these are the tests that
    justify having done so.

WHAT IT PROTECTS
    The incident recorded in the function's own docstring. parse()
    short-circuits with ``parser_used="skipped"`` when preflight rejects a
    file — password-protected, missing %PDF- magic, over 2 GB, inactive
    project. The old code fell through to the normal persist path: an empty
    "(untitled)" silver.reports row landed, embed_verify saw zero
    unembedded passages and marked the run COMPLETED, and the user saw a
    successful ingest of a file the system had refused to read.

    So the assertions are mostly about what must NOT happen: no report row,
    no passage count, and a run that reaches a terminal FAILED state
    carrying the actual reason.

THE ASYMMETRY WORTH KNOWING
    The workflow SUCCEEDS here. Returning rather than raising is
    deliberate — a rejected upload is a user-input problem, not a system
    fault, and raising would burn Hatchet retries re-reading a file that
    will never parse. The cost is that the on_failure hook never fires, so
    the terminal broadcast is the only thing that flips the UI before its
    next poll. Two tests below pin that.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows.ingest_pdf import (
    IngestPdfInput,
    _persist_preflight_rejection,
)

WS = UUID("a0000000-0000-0000-0000-00000000feed")
PROJECT = "b1000000-0000-0000-0000-0000000000a0"
RUN = "c2000000-0000-0000-0000-000000000007"

pytestmark = pytest.mark.asyncio


def make_input(**overrides: Any) -> IngestPdfInput:
    payload: dict[str, Any] = {
        "workspace_id": WS,
        "project_id": PROJECT,
        "minio_key": "reports/b1000000/locked.pdf",
        "file_size": 1024,
        "correlation_token": "tok-1",
    }
    payload.update(overrides)
    return IngestPdfInput(**payload)


PRE = {"sha256": "d" * 64, "page_count": 12, "error": "file is password-protected"}
PARSED = {
    "parser_used": "skipped",
    "warnings": [
        {"code": "preflight_rejected", "message": "file is password-protected"},
    ],
}


class Harness:
    def __init__(self, *, run_id: str | None = RUN) -> None:
        self.lookup = AsyncMock(return_value=run_id)
        self.mark_failed = AsyncMock()
        self.broadcast = AsyncMock()

    def patches(self):  # noqa: ANN201
        return (
            patch.object(ingest_progress, "lookup_active_run_id", self.lookup),
            patch.object(ingest_progress, "mark_failed_by_run", self.mark_failed),
            patch("app.services.laravel_bridge.post_ingestion_progress",
                  self.broadcast),
        )


async def run(harness: Harness, *, input_=None, pre=None, parsed=None):
    contexts = harness.patches()
    for ctx in contexts:
        ctx.start()
    try:
        return await _persist_preflight_rejection(
            input_ or make_input(),
            pre=dict(PRE if pre is None else pre),
            parsed=dict(PARSED if parsed is None else parsed),
        )
    finally:
        for ctx in reversed(contexts):
            ctx.stop()


class TestNoPhantomReport:
    async def test_it_returns_a_skipped_result_with_no_report_id(self) -> None:
        """The whole incident in one assertion: report_id must be None.

        A non-None report_id here is the "(untitled)" row that made a
        refused file look ingested.
        """
        out = await run(Harness())

        assert out.report_id is None
        assert out.parser_used == "skipped"
        assert out.passages_written == 0
        assert out.parse_quality_pct == 0.0

    async def test_it_carries_the_facts_preflight_did_establish(self) -> None:
        """Rejected is not the same as unknown. The sha and page count were
        read before the rejection and are the only way to recognise a
        re-upload of the same file."""
        out = await run(Harness())

        assert out.sha256 == "d" * 64
        assert out.page_count == 12

    async def test_it_reports_how_many_warnings_the_parse_carried(self) -> None:
        out = await run(Harness())
        assert out.warnings_count == 1

    async def test_it_does_not_raise(self) -> None:
        """Deliberate: a refused file is a user-input problem, not a system
        fault. Raising would burn Hatchet retries re-reading a file that
        will never parse."""
        out = await run(Harness())
        assert out is not None


class TestTheRunReachesATerminalFailedState:
    async def test_it_marks_the_run_failed_at_the_preflight_stage(self) -> None:
        harness = Harness()

        await run(harness)

        assert harness.mark_failed.await_count == 1
        kwargs = harness.mark_failed.await_args.kwargs
        assert kwargs["run_id"] == RUN
        assert kwargs["stage"] == "preflight"

    async def test_the_recorded_error_names_the_real_reason(self) -> None:
        """This string reaches silver.ingest_progress.error_text and from
        there the IngestionRuns page. "preflight rejected the file" alone
        tells a geologist nothing about what to do."""
        harness = Harness()

        await run(harness)

        error = harness.mark_failed.await_args.kwargs["error"]
        assert error.startswith("preflight_rejected: ")
        assert "password-protected" in error

    async def test_it_falls_back_to_the_parse_warning_when_pre_has_no_error(
        self,
    ) -> None:
        """The two sources disagree in practice: preflight records `error`,
        but a rejection discovered during parse only leaves a warning."""
        harness = Harness()

        await run(harness, pre={"sha256": "e" * 64, "page_count": 3})

        assert "password-protected" in (
            harness.mark_failed.await_args.kwargs["error"])

    async def test_it_still_says_something_when_nothing_explained_why(
        self,
    ) -> None:
        harness = Harness()

        await run(
            harness,
            pre={"sha256": "f" * 64, "page_count": 0},
            parsed={"parser_used": "skipped", "warnings": []},
        )

        assert harness.mark_failed.await_args.kwargs["error"] == (
            "preflight_rejected: preflight rejected the file")

    async def test_no_active_run_means_nothing_to_close(self) -> None:
        """A rejection can arrive for an upload whose progress row was
        already closed by the stale sweep. Marking a run that is not there
        is not an error; inventing one would be."""
        harness = Harness(run_id=None)

        out = await run(harness)

        assert harness.mark_failed.await_count == 0
        assert out.report_id is None

    async def test_a_workspaceless_input_does_not_look_the_run_up(self) -> None:
        """lookup_active_run_id is workspace-scoped; calling it without one
        would either error or, worse, match another tenant's row."""
        harness = Harness()
        # workspace_id is a required UUID on the model, so the only way to
        # reach the guard is to blank it after construction.
        payload = make_input()
        object.__setattr__(payload, "workspace_id", None)

        await run(harness, input_=payload)

        assert harness.lookup.await_count == 0


class TestTheTerminalBroadcast:
    async def test_it_tells_the_ui_the_run_failed(self) -> None:
        """The workflow SUCCEEDS on this path, so the on_failure hook never
        fires. Without this the row sits stale in the UI until its poll."""
        harness = Harness()

        await run(harness)

        assert harness.broadcast.await_count == 1
        kwargs = harness.broadcast.await_args.kwargs
        assert kwargs["status"] == "failed"
        assert kwargs["stage"] == "preflight"
        assert kwargs["run_id"] == RUN
        assert "password-protected" in kwargs["message"]

    async def test_the_message_is_written_for_a_geologist(self) -> None:
        harness = Harness()

        await run(harness)

        assert harness.broadcast.await_args.kwargs["message"].startswith(
            "Upload rejected: ")

    async def test_a_broadcast_failure_does_not_break_the_rejection(
        self,
    ) -> None:
        """The progress row is already terminal by then. Letting a
        websocket problem turn a clean rejection into a workflow error
        would put the run back on Hatchet's retry path for a file that
        cannot parse."""
        harness = Harness()
        harness.broadcast.side_effect = RuntimeError("reverb is down")

        out = await run(harness)

        assert out.report_id is None
        assert harness.mark_failed.await_count == 1

    async def test_no_project_means_no_broadcast(self) -> None:
        """The Laravel bridge is project-scoped; there is nowhere to send
        it. The run is still closed."""
        harness = Harness()
        payload = make_input()
        object.__setattr__(payload, "project_id", None)

        await run(harness, input_=payload)

        assert harness.broadcast.await_count == 0
        assert harness.mark_failed.await_count == 1
