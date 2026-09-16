"""A workspace that runs out of budget must be told so, not left hanging.

`_run_and_finalise` puts ("quota_exceeded", exc) on the status queue when
WorkspaceQuotaExceeded is raised -- the §35.1 hard stop, set by
cost_burn_watcher when accrued spend hits the ceiling. Its comment says
this exists "so the SSE stream can translate it into HTTP 429 +
Retry-After".

The SSE stream had no branch for it, and no else. Control fell back to
`await status_queue.get()` on a queue whose only producer had already
returned, so the generator blocked forever: no terminal frame, the
request holding one of five supervisor-llm slots for its full 270s, and
the user watching a spinner resolve into the frontend's two-minute
watchdog blaming the realtime channel.

The one thing they were never told is the true and simple reason: the
workspace is out of budget, and an administrator can fix it.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.agent.errors import ErrorCode, classify_error
from app.agent.llm_calls import WorkspaceQuotaExceeded

ROUTER = Path(__file__).resolve().parents[1] / "app" / "routers" / "queries.py"


def test_the_consumer_has_a_branch_for_every_kind_the_producer_sends() -> None:
    """The bug was an unhandled message kind, so assert on the pairing."""
    source = ROUTER.read_text(encoding="utf-8")

    produced = set(re.findall(r'status_queue\.put\(\(\s*"(\w+)"', source))
    # `status` is pushed through a helper rather than inline; it is handled.
    produced.add("status")

    handled: set[str] = set()
    for match in re.finditer(r'kind (?:==|in) ([^\n:]+)', source):
        handled.update(re.findall(r'"(\w+)"', match.group(1)))

    unhandled = produced - handled
    assert not unhandled, (
        f"{sorted(unhandled)} are pushed onto status_queue but no branch "
        f"consumes them. The loop has no `else`, so an unhandled kind blocks "
        f"on the next get() forever and the stream never terminates."
    )


def test_quota_exceeded_is_one_of_them() -> None:
    """Named explicitly so the generic test above cannot silently stop covering it."""
    source = ROUTER.read_text(encoding="utf-8")
    assert '"quota_exceeded"' in source
    assert re.search(r'kind in \([^)]*"quota_exceeded"', source), (
        "quota_exceeded must be consumed by the same re-raise branch as error, "
        "so _guarded_stream can classify it into a terminal `failed` frame"
    )


def test_the_user_is_told_it_is_a_budget_stop_not_a_mystery() -> None:
    code, message = classify_error(WorkspaceQuotaExceeded("workspace out of budget"))

    assert code is ErrorCode.QUOTA_EXCEEDED, (
        "WorkspaceQuotaExceeded is a RuntimeError, so without an explicit "
        "branch it falls through every isinstance test to INTERNAL_ERROR"
    )
    # INTERNAL_ERROR's message would say an unexpected error occurred and the
    # team has been notified — untrue of a deliberate, configured stop.
    assert "unexpected error" not in message.lower()
    assert "budget" in message.lower()


def test_it_is_not_described_as_something_waiting_will_fix() -> None:
    """RATE_LIMITED clears by waiting. A monthly ceiling does not."""
    _, quota = classify_error(WorkspaceQuotaExceeded("x"))
    rate_limited = classify_error(RuntimeError("rate limit exceeded"))[1]

    assert quota != rate_limited
    assert "wait a moment" not in quota.lower()
    assert "administrator" in quota.lower() or "next month" in quota.lower()
