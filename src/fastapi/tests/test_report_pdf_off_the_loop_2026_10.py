"""Audit 2026-10 finding 16: the report builder renders its PDF off the event loop.

``export_package`` called ``render_pdf_from_markdown`` (WeasyPrint: synchronous,
CPU-bound, seconds for a long report) directly in an async node, so the loop was
held for the whole render.
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.report_builder import nodes
from app.services.report_builder.state import ReportBuilderState
from app.services.report_builder.templates import REPORT_RISK_TIERS


def _state() -> ReportBuilderState:
    return ReportBuilderState(
        report_id=uuid4(),
        workspace_id=uuid4(),
        project_id=uuid4(),
        report_type="weekly_project_digest",
        risk_tier=REPORT_RISK_TIERS["weekly_project_digest"],
        requested_by_user_id=1,
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_the_pdf_render_runs_in_a_worker_thread_and_the_loop_stays_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.report_builder.renderers import pdf_renderer

    render_thread: list[threading.Thread] = []

    def slow_render(markdown: str, *, title: str | None = None, **_kw: object) -> bytes:
        render_thread.append(threading.current_thread())
        time.sleep(0.4)  # a long WeasyPrint layout pass
        return b"%PDF-1.7 fake"

    monkeypatch.setattr(pdf_renderer, "render_pdf_from_markdown", slow_render)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    state = await nodes.export_package(_state())
    task.cancel()

    assert render_thread and render_thread[0] is not threading.main_thread()
    assert ticks >= 8, f"the event loop only turned {ticks} times during a 0.4 s render"
    assert state.pdf_uri == "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.7 fake").decode()


@pytest.mark.asyncio
async def test_a_failed_render_still_falls_back_to_the_markdown_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.report_builder.renderers import pdf_renderer

    def broken(markdown: str, *, title: str | None = None, **_kw: object) -> bytes:
        raise RuntimeError("fontconfig cache is not writable")

    monkeypatch.setattr(pdf_renderer, "render_pdf_from_markdown", broken)

    state = await nodes.export_package(_state())

    assert state.pdf_uri is not None
    assert state.pdf_uri.startswith("data:text/markdown;base64,")
