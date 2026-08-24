"""Terminal ingestion callbacks are retried; progress callbacks are not.

``post_ingestion_progress`` is fire-and-forget for a reason: a broadcast
failure must never fail a workflow that is making real progress, and a
dropped progress event is superseded by the next one.

A TERMINAL status is not like that. The Laravel endpoint reacts to
``completed`` / ``partial`` by bumping ``silver.projects.data_version`` and
dispatching the debounced materialised-view refresh, and this POST is the
only trigger for either. Dropping one does not delay a toast -- it leaves the
MVs unrefreshed until the 03:00 cron and the tile-cache token permanently
stale. A Laravel container restarting for twenty seconds was enough to lose
every document that finished inside the window.

So terminal callbacks retry, and these tests pin the boundary: which statuses
retry, which failures are worth retrying, and that the terminal set is the one
``_progress`` writes rather than a second copy of it.
"""
from __future__ import annotations

import httpx
import pytest

from app.ingest_status import TERMINAL_STATUSES
from app.services import laravel_bridge

KEY = "test-key-32-bytes-or-longer-for-validator-ok"
IDS = {
    "workspace_id": "00000000-0000-0000-0000-000000000001",
    "project_id": "00000000-0000-0000-0000-000000000002",
    "run_id": "00000000-0000-0000-0000-000000000003",
}


@pytest.fixture(autouse=True)
def _fast_and_configured(monkeypatch):
    """Same number of attempts, no wall-clock cost."""
    monkeypatch.setattr(laravel_bridge, "_INGESTION_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", KEY)


def _count_posts(monkeypatch, responder):
    """Stub AsyncClient.post, recording each call. Returns the call list."""
    calls: list[dict] = []

    async def _stub(self, url, json=None, headers=None):
        calls.append({"url": url, "json": json})
        return responder(len(calls), url)

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub)
    return calls


def _refused(_attempt, _url):
    raise httpx.ConnectError("connection refused")


async def test_terminal_status_retries_on_transport_failure(monkeypatch):
    calls = _count_posts(monkeypatch, _refused)

    await laravel_bridge.post_ingestion_progress(
        **IDS, stage="persist", status="completed",
    )

    assert len(calls) == 3, "one initial attempt plus two retries"


async def test_progress_status_does_not_retry(monkeypatch):
    calls = _count_posts(monkeypatch, _refused)

    await laravel_bridge.post_ingestion_progress(
        **IDS, stage="parse", status="started", pct=40,
    )

    assert len(calls) == 1, (
        "a superseded progress event is not worth three sockets"
    )


async def test_retry_stops_as_soon_as_laravel_accepts(monkeypatch):
    def _fail_once_then_ok(attempt, url):
        status = 503 if attempt == 1 else 202
        return httpx.Response(status, request=httpx.Request("POST", url))

    calls = _count_posts(monkeypatch, _fail_once_then_ok)

    await laravel_bridge.post_ingestion_progress(
        **IDS, stage="persist", status="partial",
    )

    assert len(calls) == 2


async def test_client_error_is_not_retried(monkeypatch):
    """A 422 means Laravel understood us and refused.

    Replaying it produces the same rejection three times and delays the
    workflow step for nothing. Only 5xx and transport errors can clear.
    """
    def _unprocessable(_attempt, url):
        return httpx.Response(422, request=httpx.Request("POST", url))

    calls = _count_posts(monkeypatch, _unprocessable)

    await laravel_bridge.post_ingestion_progress(
        **IDS, stage="persist", status="failed",
    )

    assert len(calls) == 1


async def test_a_terminal_callback_still_never_raises(monkeypatch):
    """Exhausting the retries is still a swallowed warning.

    The retry changes how hard we try, not the contract: ingestion that
    actually succeeded must not be failed by a broadcast problem.
    """
    _count_posts(monkeypatch, _refused)

    await laravel_bridge.post_ingestion_progress(
        **IDS, stage="persist", status="completed",
    )


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
async def test_every_terminal_status_retries(monkeypatch, status):
    """No status that closes a run may be left on the fire-and-forget path.

    Parametrised over the shared tuple rather than a list written here, so
    adding a terminal status cannot quietly opt it out -- which is the exact
    shape of the 'partial' drift this module's sibling guards already fixed.
    """
    calls = _count_posts(monkeypatch, _refused)

    await laravel_bridge.post_ingestion_progress(
        **IDS, stage="persist", status=status,
    )

    assert len(calls) == 3


def test_the_bridge_reads_the_canonical_terminal_set():
    """Not a copy of it.

    ``_progress`` writes these statuses and ``laravel_bridge`` decides from
    them; a second literal in either place is how 'partial' became terminal
    to one function and non-terminal to eight.
    """
    from app.hatchet_workflows import _progress

    assert laravel_bridge.TERMINAL_STATUSES is _progress.TERMINAL_STATUSES
