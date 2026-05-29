"""Tests for the request-scoped active-history contextvar.

The queries router stashes the incoming `history` payload onto the
`_active_history` contextvar before invoking `run_deterministic_rag`.
The orchestrator's agentic-retrieval dispatch reads from the contextvar,
coerces the raw history dicts into `ConversationTurn` objects, and
forwards them to `run_agentic_retrieval(history=...)`.

Same per-task-isolation pattern as `set_active_context_envelope` — these
tests cover:

  - Default (None) when nothing's been set
  - set_active_history() writes are observable via the get path
  - Empty list is preserved as empty (NOT None)
  - Multiple concurrent contexts don't clobber each other (asyncio task
    isolation invariant)
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent.orchestrator import (
    _active_history,
    set_active_history,
)


def test_default_is_none():
    # Reset for this test — contextvars carry state across tests in the
    # same task, which is fine for production but noisy for unit tests.
    _active_history.set(None)
    assert _active_history.get() is None


def test_set_active_history_writes_observable_via_get():
    _active_history.set(None)
    history = [
        {"turn_index": 0, "role": "user", "text": "first"},
        {"turn_index": 1, "role": "assistant", "text": "ack"},
    ]
    set_active_history(history)
    assert _active_history.get() == history


def test_set_active_history_accepts_empty_list():
    """An empty list is a valid signal — 'history was supplied but
    contained no turns' is different from 'history was never supplied'."""
    _active_history.set(None)
    set_active_history([])
    assert _active_history.get() == []


def test_set_active_history_accepts_none():
    set_active_history([{"turn_index": 0, "role": "user", "text": "x"}])
    set_active_history(None)
    assert _active_history.get() is None


@pytest.mark.asyncio
async def test_concurrent_tasks_do_not_clobber_each_other():
    """Two asyncio tasks set different histories — each must see its
    OWN value when it reads back, not the other's."""
    set_active_history(None)
    seen: dict[str, list | None] = {}

    async def _task(label: str, payload: list):
        # Within an asyncio.Task, contextvars copy on task creation.
        # asyncio.to_thread / asyncio.create_task carries the parent
        # context. set inside a task should ONLY affect that task.
        set_active_history(payload)
        await asyncio.sleep(0)
        seen[label] = _active_history.get()

    await asyncio.gather(
        _task("a", [{"turn_index": 0, "role": "user", "text": "alpha"}]),
        _task("b", [{"turn_index": 0, "role": "user", "text": "beta"}]),
    )
    # Each task saw its OWN value.
    assert seen["a"][0]["text"] == "alpha"
    assert seen["b"][0]["text"] == "beta"
