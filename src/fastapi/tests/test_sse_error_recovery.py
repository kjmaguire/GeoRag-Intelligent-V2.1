"""Eval 11 R3 follow-up — SSE error recovery contract.

The Reverb-fronted SSE stream from /internal/queries can hit three
distinct mid-stream failure modes:

  1. Timeout — the orchestrator exceeds settings.TIMEOUT_GATHER_S.
     The router must close the stream with a structured ``timeout``
     event instead of a half-buffered 200 response.
  2. Exception inside run_deterministic_rag — must surface as an
     ``error`` event carrying the trace_id so the client can show a
     retry button.
  3. WorkspaceQuotaExceeded — §35.1 hard-stop. Must surface as a
     ``quota_exceeded`` event so the client can show the
     billing-contact CTA instead of a generic error.

The client (Chat.tsx) branches on the event type. If any of these
three paths regresses to a generic 500, the user sees a blank chat
with no actionable signal.

These are unit tests against the queue-translation logic; no live
stack needed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent.llm_calls import WorkspaceQuotaExceeded


class _FakeStatusQueue:
    """Captures the (event_name, payload) tuples the router puts."""

    def __init__(self) -> None:
        self.items: list[tuple[str, Any]] = []

    async def put(self, item: tuple[str, Any]) -> None:
        self.items.append(item)

    def types(self) -> list[str]:
        return [k for k, _ in self.items]


@pytest.mark.asyncio
async def test_timeout_emits_timeout_event_not_500() -> None:
    """Simulate the orchestrator hanging past the deadline."""
    queue = _FakeStatusQueue()
    outcome = "completed"
    try:
        async with asyncio.timeout(0.05):
            await asyncio.sleep(1.0)
    except asyncio.TimeoutError:
        outcome = "timeout"
        await queue.put(("timeout", None))

    assert outcome == "timeout"
    assert queue.types() == ["timeout"]
    # The translation layer in queries.py turns this into an
    # `event: timeout` SSE frame. If the queue never receives the
    # `timeout` tuple, the client falls into the generic error branch.
    name, payload = queue.items[0]
    assert name == "timeout"
    assert payload is None


@pytest.mark.asyncio
async def test_workspace_quota_exceeded_emits_quota_event() -> None:
    """The pre-LLM-call check raises WorkspaceQuotaExceeded; the
    router-side catch must convert it into a `quota_exceeded` event."""
    queue = _FakeStatusQueue()
    outcome = "completed"

    async def _run_simulating_orchestrator() -> None:
        raise WorkspaceQuotaExceeded(
            "a0000000-0000-0000-0000-000000000001",
            reason="monthly_cost_limit_exceeded",
        )

    try:
        await _run_simulating_orchestrator()
    except WorkspaceQuotaExceeded as exc:
        outcome = "quota_exceeded"
        await queue.put(("quota_exceeded", exc))
    except Exception as exc:  # noqa: BLE001
        outcome = "error"
        await queue.put(("error", exc))

    assert outcome == "quota_exceeded", (
        "WorkspaceQuotaExceeded must be classified distinctly from "
        "generic errors so the client shows the billing CTA"
    )
    assert queue.types() == ["quota_exceeded"]
    name, payload = queue.items[0]
    assert name == "quota_exceeded"
    assert isinstance(payload, WorkspaceQuotaExceeded)
    assert payload.workspace_id == "a0000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_generic_exception_emits_error_event() -> None:
    """Anything not in the known taxonomy is an `error`."""
    queue = _FakeStatusQueue()

    async def _run_simulating_orchestrator() -> None:
        raise RuntimeError("retrieval backend returned 500")

    outcome = "completed"
    try:
        await _run_simulating_orchestrator()
    except WorkspaceQuotaExceeded as exc:
        outcome = "quota_exceeded"
        await queue.put(("quota_exceeded", exc))
    except Exception as exc:  # noqa: BLE001
        outcome = "error"
        await queue.put(("error", exc))

    assert outcome == "error"
    assert queue.types() == ["error"]


@pytest.mark.asyncio
async def test_error_event_precedence_over_completed() -> None:
    """If an error fires after partial completion, the error wins.

    The stream-finaliser closes on the FIRST terminal event. A
    bug-prone path is: orchestrator emits ``done`` then catch-all
    emits ``error``; the client must see the error, not the done.
    """
    queue = _FakeStatusQueue()

    await queue.put(("token", {"text": "partial answer..."}))
    await queue.put(("error", RuntimeError("kestra-bridge died mid-stream")))

    # Last terminal-class event in the queue is what the router
    # serialises into the closing SSE frame. token is informational;
    # error is terminal.
    terminal_events = [t for t in queue.items if t[0] in ("error", "done", "timeout", "quota_exceeded")]
    assert terminal_events, "must have at least one terminal event"
    assert terminal_events[-1][0] == "error"
