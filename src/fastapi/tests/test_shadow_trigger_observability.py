"""Locks the cancellation-observability and dedupe contract in shadow_trigger.

Background: on 2026-06-01 the Cameco upload burst lost 529 ingest_pdf
runs to silent Hatchet CANCELLED events at GROUP_ROUND_ROBIN queue-depth
saturation. The cancellations were invisible because they fired BEFORE
the preflight task wrote the first ``silver.ingest_progress`` row.

Fix (2026-06-02): the trigger endpoint inserts the progress row at
dispatch time (status='queued') so ``ingest_pdf.on_failure_task`` can
resolve the run even when the workflow never runs preflight.

HAT-6/HAT-12 (2026-09-29): the row is now written BEFORE the dispatch, by
``_progress.claim_dispatch``, which also dedupes. Laravel wraps every
trigger call in ``retry(3, 500)``; a slow first dispatch used to be
re-POSTed and dispatched twice because the row did not exist yet. These
tests lock the order (claim, dispatch, stamp the Hatchet id), the dedupe
answer (200, ``dispatched: false``, no dispatch), the rollback of a claim
whose dispatch raised, and the fail-open path when the claim itself fails.
See [[cameco-recovery-2026-06-02]] for the incident.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.hatchet_workflows import _progress
from app.hatchet_workflows.ingest_pdf import IngestPdfInput
from app.hatchet_workflows.ingest_tabular import IngestTabularInput
from app.routers import shadow_trigger

_WS = "a0000000-0000-0000-0000-000000000001"
_PROJECT = "b1000000-0000-0000-0000-000000000010"


def _pdf_payload() -> IngestPdfInput:
    return IngestPdfInput(
        workspace_id=_WS,
        project_id=_PROJECT,
        minio_key=f"reports/{_PROJECT}/sample.pdf",
        file_size=12345,
        correlation_token="test-correlation-token",
    )


def _request(headers: dict | None = None):
    """A request stub whose pg_pool passes the project-lifecycle guard."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value={"lifecycle_state": "active"})
    # asyncpg.Connection.transaction() returns an async context manager.
    conn.transaction = lambda: _AsyncCtx(None)
    conn.is_in_transaction = lambda: True
    pg_pool = SimpleNamespace(acquire=lambda: _AsyncCtx(conn))
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(pg_pool=pg_pool)),
        headers=headers or {},
    )


@pytest.mark.asyncio
async def test_the_row_is_claimed_before_dispatch_and_stamped_after():
    order: list[str] = []
    fake_ref = SimpleNamespace(workflow_run_id="wf-run-deadbeef")

    async def _claim(**kwargs):
        order.append("claim")
        _claim.kwargs = kwargs  # type: ignore[attr-defined]
        return _progress.DispatchClaim(claimed=True, run_id="run-uuid")

    async def _dispatch(payload):
        order.append("dispatch")
        return fake_ref

    async def _stamp(**kwargs):
        order.append("stamp")
        _stamp.kwargs = kwargs  # type: ignore[attr-defined]

    with patch.object(shadow_trigger.ingest_pdf, "aio_run_no_wait", new=_dispatch), \
         patch.object(shadow_trigger.ingest_progress, "claim_dispatch", new=_claim), \
         patch.object(shadow_trigger.ingest_progress, "stamp_workflow_run_id", new=_stamp):
        response = await shadow_trigger.trigger_ingest_pdf(_pdf_payload(), _request())  # type: ignore[arg-type]

    assert order == ["claim", "dispatch", "stamp"], (
        "the queued row must exist BEFORE the dispatch; a Laravel retry that "
        "lands during a slow dispatch has to find it"
    )
    assert response.workflow_run_id == "wf-run-deadbeef"
    assert response.correlation_token == "test-correlation-token"
    assert _stamp.kwargs == {  # type: ignore[attr-defined]
        "run_id": "run-uuid", "workflow_run_id": "wf-run-deadbeef",
    }, (
        "the workflow_run_id from aio_run_no_wait must be persisted so the "
        "on_failure hook and the stale sweep can join the row to its run"
    )
    kwargs = _claim.kwargs  # type: ignore[attr-defined]
    assert kwargs["workspace_id"] == _WS
    assert kwargs["project_id"] == _PROJECT
    assert kwargs["minio_key"] == f"reports/{_PROJECT}/sample.pdf"
    assert kwargs["triggered_by"] == "upload"


@pytest.mark.asyncio
async def test_a_duplicate_is_answered_200_and_not_dispatched():
    dispatch = AsyncMock()
    claim = AsyncMock(return_value=_progress.DispatchClaim(
        claimed=False, run_id="existing-run", workflow_run_id="wf-existing",
    ))
    with patch.object(shadow_trigger.ingest_pdf, "aio_run_no_wait", new=dispatch), \
         patch.object(shadow_trigger.ingest_progress, "claim_dispatch", new=claim):
        response = await shadow_trigger.trigger_ingest_pdf(_pdf_payload(), _request())  # type: ignore[arg-type]

    dispatch.assert_not_awaited()
    assert response.status_code == 200
    assert b'"dispatched":false' in response.body
    assert b"wf-existing" in response.body


@pytest.mark.asyncio
async def test_a_geology_retry_with_the_same_run_id_is_not_dispatched_twice():
    """The double-ingest HAT-6 found: two ingest_tabular runs for one file."""
    payload = IngestTabularInput(
        workspace_id=_WS, project_id=_PROJECT,
        minio_key=f"lithology/{_PROJECT}/20260929_120000_lith.csv",
        run_id="c0000000-0000-4000-8000-000000000001",
    )
    dispatch = AsyncMock(return_value=SimpleNamespace(workflow_run_id="wf-1"))
    claims = iter([
        _progress.DispatchClaim(claimed=True, run_id=payload.run_id or ""),
        _progress.DispatchClaim(
            claimed=False, run_id=payload.run_id or "", workflow_run_id="wf-1",
        ),
    ])
    claim = AsyncMock(side_effect=lambda **_: next(claims))
    with patch.object(shadow_trigger.ingest_tabular, "aio_run_no_wait", new=dispatch), \
         patch.object(shadow_trigger.ingest_progress, "claim_dispatch", new=claim), \
         patch.object(shadow_trigger.ingest_progress, "stamp_workflow_run_id", new=AsyncMock()):
        first = await shadow_trigger.trigger_ingest_tabular(payload, _request())  # type: ignore[arg-type]
        second = await shadow_trigger.trigger_ingest_tabular(payload, _request())  # type: ignore[arg-type]

    assert dispatch.await_count == 1
    assert first.dispatched is True
    assert second.status_code == 200
    assert claim.await_args.kwargs["run_id"] == payload.run_id


@pytest.mark.asyncio
async def test_a_dispatch_that_raises_takes_its_row_back_out():
    release = AsyncMock()
    claim = AsyncMock(return_value=_progress.DispatchClaim(claimed=True, run_id="run-x"))
    with patch.object(
        shadow_trigger.ingest_pdf, "aio_run_no_wait",
        new=AsyncMock(side_effect=RuntimeError("engine down")),
    ), patch.object(shadow_trigger.ingest_progress, "claim_dispatch", new=claim), \
         patch.object(shadow_trigger.ingest_progress, "release_undispatched", new=release):
        with pytest.raises(RuntimeError):
            await shadow_trigger.trigger_ingest_pdf(_pdf_payload(), _request())  # type: ignore[arg-type]

    release.assert_awaited_once_with(run_id="run-x")


@pytest.mark.asyncio
async def test_a_failed_claim_still_dispatches_and_records_the_row():
    """Fail open, the pre-2026-09-29 order: an unreachable progress table
    must not refuse the upload."""
    fake_ref = SimpleNamespace(workflow_run_id="wf-run-deadbeef")
    with patch.object(
        shadow_trigger.ingest_pdf, "aio_run_no_wait", new=AsyncMock(return_value=fake_ref),
    ), patch.object(
        shadow_trigger.ingest_progress, "claim_dispatch",
        new=AsyncMock(side_effect=OSError("db unreachable")),
    ), patch.object(
        shadow_trigger.ingest_progress, "start_run", new=AsyncMock(return_value="run-uuid"),
    ) as start_run_mock:
        response = await shadow_trigger.trigger_ingest_pdf(_pdf_payload(), _request())  # type: ignore[arg-type]

    assert response.workflow_run_id == "wf-run-deadbeef"
    start_run_mock.assert_awaited_once()
    assert start_run_mock.await_args.kwargs["workflow_run_id"] == "wf-run-deadbeef"


@pytest.mark.asyncio
async def test_an_internal_sweep_can_label_its_own_rows():
    claim = AsyncMock(return_value=_progress.DispatchClaim(claimed=True, run_id="r"))
    with patch.object(
        shadow_trigger.ingest_pdf, "aio_run_no_wait",
        new=AsyncMock(return_value=SimpleNamespace(workflow_run_id="wf")),
    ), patch.object(shadow_trigger.ingest_progress, "claim_dispatch", new=claim), \
         patch.object(shadow_trigger.ingest_progress, "stamp_workflow_run_id", new=AsyncMock()):
        await shadow_trigger.trigger_ingest_pdf(  # type: ignore[arg-type]
            _pdf_payload(),
            _request({shadow_trigger.INGEST_TRIGGER_HEADER: "nightly_integrity_sweep"}),
        )
        await shadow_trigger.trigger_ingest_pdf(  # type: ignore[arg-type]
            _pdf_payload(),
            _request({shadow_trigger.INGEST_TRIGGER_HEADER: "anything-else"}),
        )

    labels = [c.kwargs["triggered_by"] for c in claim.await_args_list]
    assert labels == ["nightly_integrity_sweep", "upload"], (
        "the header is honoured only for a value in ALLOWED_TRIGGERS"
    )


class _AsyncCtx:
    """Minimal async-context-manager wrapper for AsyncMock returns."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc, tb):
        return False
