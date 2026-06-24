"""Locks the cancellation-observability contract in shadow_trigger.

Background: on 2026-06-01 the Cameco upload burst lost 529 ingest_pdf
runs to silent Hatchet CANCELLED events at GROUP_ROUND_ROBIN queue-depth
saturation. The cancellations were invisible because they fired BEFORE
the preflight task wrote the first ``silver.ingest_progress`` row.

Fix: the trigger endpoint now inserts the progress row at dispatch time
(status='queued', workflow_run_id=ref.workflow_run_id) so the existing
``ingest_pdf.on_failure_task`` hook can resolve the run and transition
it to 'failed' even when the workflow never runs preflight.

This test locks that contract by calling the trigger handler directly
with mocked Hatchet + progress dependencies and asserting that
``start_run`` is invoked with the same ``workflow_run_id`` the workflow
client returned. See [[cameco-recovery-2026-06-02]] for the incident.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.hatchet_workflows.ingest_pdf import IngestPdfInput
from app.routers import shadow_trigger


@pytest.mark.asyncio
async def test_trigger_writes_queued_progress_row_with_workflow_run_id():
    """start_run() must be called with the workflow_run_id returned by
    aio_run_no_wait so on_failure can lookup_active_run_id afterwards."""
    payload = IngestPdfInput(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="b1000000-0000-0000-0000-000000000010",
        minio_key="reports/b1000000-0000-0000-0000-000000000010/sample.pdf",
        file_size=12345,
        correlation_token="test-correlation-token",
    )

    # Build a request stub whose .app.state.pg_pool is a context-manager
    # mock that yields a connection mock supporting set_config + lifecycle
    # check (returns "active").
    lifecycle_row = {"lifecycle_state": "active"}
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=lifecycle_row)
    # asyncpg.Connection.transaction() returns an async context manager.
    conn.transaction = lambda: _AsyncCtx(None)
    pg_pool = SimpleNamespace(acquire=lambda: _AsyncCtx(conn))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pg_pool=pg_pool)))

    fake_ref = SimpleNamespace(workflow_run_id="wf-run-deadbeef")

    with patch.object(
        shadow_trigger.ingest_pdf, "aio_run_no_wait", new=AsyncMock(return_value=fake_ref)
    ), patch.object(
        shadow_trigger.ingest_progress, "start_run", new=AsyncMock(return_value="run-uuid")
    ) as start_run_mock:
        response = await shadow_trigger.trigger_ingest_pdf(payload, request)  # type: ignore[arg-type]

    assert response.workflow_run_id == "wf-run-deadbeef"
    assert response.correlation_token == "test-correlation-token"
    start_run_mock.assert_awaited_once()
    kwargs = start_run_mock.await_args.kwargs
    assert kwargs["workflow_run_id"] == "wf-run-deadbeef", (
        "start_run must persist the workflow_run_id from aio_run_no_wait so "
        "the on_failure hook (and operators correlating with the Hatchet UI) "
        "can join silver.ingest_progress rows back to the originating run."
    )
    assert kwargs["triggered_by"] == "upload"
    assert kwargs["workspace_id"] == str(payload.workspace_id)
    assert kwargs["project_id"] == str(payload.project_id)
    assert kwargs["minio_key"] == payload.minio_key


class _AsyncCtx:
    """Minimal async-context-manager wrapper for AsyncMock returns."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc, tb):
        return False
