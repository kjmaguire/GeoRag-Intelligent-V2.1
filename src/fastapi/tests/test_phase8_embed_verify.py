"""Phase 8 (2026-05-22) — embed_verify simplification tests.

Verifies the polling loop (6 × 15 s = 90 s worst case) has been replaced
with a single check + dispatch. Idempotency of embed_pending_passages
makes this safe; cron backstop catches anything that slips.

Covers:
  - no project_id → skipped
  - unembedded==0 → exits without dispatching
  - unembedded>0 → dispatches embed_pending_passages_wf once
  - dispatch raises → returns ok=false with error
  - no asyncio.sleep calls (no poll loop)
  - execution_timeout dropped from 2m to 60s

These tests inspect the task source + behavior at the function level
rather than running the full Hatchet workflow harness.

Run with:
    pytest src/fastapi/tests/test_phase8_embed_verify.py -v
"""

from __future__ import annotations

import inspect
import sys
import types
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _get_embed_verify_func():
    """Pull the underlying coroutine function out of the Hatchet task
    decorator wrapper so we can call it directly with mocked inputs."""
    from app.hatchet_workflows import ingest_pdf as mod
    # Hatchet wraps the task function — find the original async def
    for name in dir(mod):
        obj = getattr(mod, name)
        if name == "embed_verify":
            # Unwrap the task decorator if present
            return getattr(obj, "_fn", obj) if hasattr(obj, "_fn") else obj
    raise RuntimeError("embed_verify task not found")


def _make_input(project_id="11111111-2222-3333-4444-555555555555",
                workspace_id="a0000000-0000-0000-0000-000000000001"):
    from app.hatchet_workflows.ingest_pdf import IngestPdfInput
    return IngestPdfInput(
        workspace_id=workspace_id,
        project_id=project_id,
        minio_key="reports/test/foo.pdf",
        file_size=1024,
        correlation_token="tok",
    )


@asynccontextmanager
async def _passthrough_scope(pool, workspace_id, site):
    """embed_verify's workspace bind, minus the real connection it needs."""
    async with pool.acquire() as conn:
        yield conn


def _make_ctx():
    ctx = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# 1. Source no longer contains a poll loop (no asyncio.sleep inside embed_verify)
# ---------------------------------------------------------------------------

def test_embed_verify_no_poll_loop_in_source():
    """Phase 8 acceptance: source must not contain the 15-second sleep
    that the polling loop used."""
    from app.hatchet_workflows import ingest_pdf as mod

    src = inspect.getsource(mod)
    # Locate the embed_verify function body
    start = src.index("async def embed_verify")
    body = src[start:]
    assert "asyncio.sleep(15)" not in body
    assert "for _ in range(6)" not in body
    assert "unembedded_history" not in body


# ---------------------------------------------------------------------------
# 2. execution_timeout dropped from "2m" to "60s"
# ---------------------------------------------------------------------------

def test_embed_verify_execution_timeout_shortened():
    """The poll loop ran up to 90 s; the simplified task runs in seconds.
    Confirm the decorator timeout was tightened to match."""
    from app.hatchet_workflows import ingest_pdf as mod
    src = inspect.getsource(mod)
    # Find the decorator of embed_verify. It spans several lines since the retry
    # backoff was added (2026-10-10), so read from its opening line.
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if "async def embed_verify" in ln:
            start = i - 1
            while start > 0 and not lines[start].startswith("@ingest_pdf.task("):
                start -= 1
            decorator = " ".join(lines[start:i])
            assert 'execution_timeout="60s"' in decorator
            assert 'execution_timeout="2m"' not in decorator
            return
    pytest.fail("embed_verify decorator not found")


# ---------------------------------------------------------------------------
# 3. embed_verify skips when project_id is empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_verify_skips_without_project_id():
    embed_verify = _get_embed_verify_func()
    inp = _make_input()
    # Force project_id empty via attribute override
    object.__setattr__(inp, "project_id", "")
    result = await embed_verify(inp, _make_ctx())
    assert result == {"ok": True, "skipped": True, "reason": "no project_id"}


# ---------------------------------------------------------------------------
# 4. embed_verify exits clean when unembedded count is 0
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_embed_verify_exits_when_zero_unembedded():
    embed_verify = _get_embed_verify_func()

    # Patch the asyncpg.create_pool to return a pool whose acquire()
    # produces a connection whose fetchrow returns count=0.
    fake_conn = MagicMock()
    fake_conn.fetchrow = AsyncMock(return_value={"unembedded": 0})
    fake_acquire_cm = MagicMock()
    fake_acquire_cm.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_acquire_cm.__aexit__ = AsyncMock(return_value=None)

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=fake_acquire_cm)
    fake_pool.close = AsyncMock()

    from app.hatchet_workflows import ingest_pdf as mod

    # Spy on dispatch to confirm it does NOT fire
    dispatch_spy = AsyncMock()
    fake_embed_module = types.SimpleNamespace(
        EmbedPendingPassagesInput=MagicMock(),
        embed_pending_passages_wf=MagicMock(aio_run_no_wait=dispatch_spy),
    )

    with patch.object(mod.asyncpg, "create_pool", AsyncMock(return_value=fake_pool)), \
            patch.object(mod, "_scoped_acquire", _passthrough_scope), \
            patch.dict(
                sys.modules,
                {"app.hatchet_workflows.embed_pending_passages": fake_embed_module},
            ):
        result = await embed_verify(_make_input(), _make_ctx())

    assert result == {"ok": True, "unembedded_final": 0}
    dispatch_spy.assert_not_called()


# ---------------------------------------------------------------------------
# 5. embed_verify dispatches when unembedded > 0
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_embed_verify_dispatches_when_unembedded_remains():
    embed_verify = _get_embed_verify_func()

    fake_conn = MagicMock()
    fake_conn.fetchrow = AsyncMock(return_value={"unembedded": 47})
    fake_acquire_cm = MagicMock()
    fake_acquire_cm.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_acquire_cm.__aexit__ = AsyncMock(return_value=None)

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=fake_acquire_cm)
    fake_pool.close = AsyncMock()

    from app.hatchet_workflows import ingest_pdf as mod

    dispatch_spy = AsyncMock()
    fake_embed_module = types.SimpleNamespace(
        EmbedPendingPassagesInput=lambda **kw: types.SimpleNamespace(**kw),
        embed_pending_passages_wf=MagicMock(aio_run_no_wait=dispatch_spy),
    )

    with patch.object(mod.asyncpg, "create_pool", AsyncMock(return_value=fake_pool)), \
            patch.object(mod, "_scoped_acquire", _passthrough_scope), \
            patch.dict(
                sys.modules,
                {"app.hatchet_workflows.embed_pending_passages": fake_embed_module},
            ):
        result = await embed_verify(_make_input(), _make_ctx())

    assert result == {
        "ok": True, "redispatched": True, "unembedded_observed": 47,
    }
    dispatch_spy.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. embed_verify returns ok=false when dispatch raises
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_embed_verify_dispatch_failure_returns_ok_false():
    embed_verify = _get_embed_verify_func()

    fake_conn = MagicMock()
    fake_conn.fetchrow = AsyncMock(return_value={"unembedded": 5})
    fake_acquire_cm = MagicMock()
    fake_acquire_cm.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_acquire_cm.__aexit__ = AsyncMock(return_value=None)

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=fake_acquire_cm)
    fake_pool.close = AsyncMock()

    from app.hatchet_workflows import ingest_pdf as mod

    dispatch_spy = AsyncMock(side_effect=RuntimeError("hatchet unreachable"))
    fake_embed_module = types.SimpleNamespace(
        EmbedPendingPassagesInput=lambda **kw: types.SimpleNamespace(**kw),
        embed_pending_passages_wf=MagicMock(aio_run_no_wait=dispatch_spy),
    )

    with patch.object(mod.asyncpg, "create_pool", AsyncMock(return_value=fake_pool)), \
            patch.object(mod, "_scoped_acquire", _passthrough_scope), \
            patch.dict(
                sys.modules,
                {"app.hatchet_workflows.embed_pending_passages": fake_embed_module},
            ):
        result = await embed_verify(_make_input(), _make_ctx())

    assert result["ok"] is False
    assert "hatchet unreachable" in result["error"]
    assert result["unembedded_observed"] == 5
    dispatch_spy.assert_awaited_once()


# ---------------------------------------------------------------------------
# 7. Single SELECT round-trip (not 6)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_embed_verify_single_select_roundtrip():
    embed_verify = _get_embed_verify_func()

    fake_conn = MagicMock()
    fetch_calls = []

    async def _fake_fetchrow(*args, **kwargs):
        fetch_calls.append((args, kwargs))
        return {"unembedded": 0}
    fake_conn.fetchrow = _fake_fetchrow

    fake_acquire_cm = MagicMock()
    fake_acquire_cm.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_acquire_cm.__aexit__ = AsyncMock(return_value=None)

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=fake_acquire_cm)
    fake_pool.close = AsyncMock()

    from app.hatchet_workflows import ingest_pdf as mod

    with patch.object(mod.asyncpg, "create_pool", AsyncMock(return_value=fake_pool)), \
            patch.object(mod, "_scoped_acquire", _passthrough_scope):
        await embed_verify(_make_input(), _make_ctx())

    unembedded_fetches = [
        call for call in fetch_calls
        if "count(*) AS unembedded" in call[0][0]
    ]
    assert len(unembedded_fetches) == 1


# ---------------------------------------------------------------------------
# The unembedded count runs with the workspace bound
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_unembedded_count_binds_the_workspace_before_it_reads():
    """silver.document_passages is fail-closed: on a bare connection the count
    is always 0, so every run was closed 'all chunks embedded' immediately."""
    from app.hatchet_workflows import ingest_pdf as mod

    events: list[str] = []

    class _Conn:
        def transaction(self):
            @asynccontextmanager
            async def _txn():
                events.append("begin")
                yield
                events.append("commit")

            return _txn()

        async def fetchrow(self, *_a, **_k):
            events.append("count")
            return {"unembedded": 3}

    @asynccontextmanager
    async def _acquire():
        yield _Conn()

    pool = types.SimpleNamespace(acquire=_acquire, close=AsyncMock())

    async def _bind(conn, *, workspace_id, site, **_kw):
        events.append(f"bind:{workspace_id}:{site}")

    dispatch = AsyncMock()
    fake_embed_module = types.SimpleNamespace(
        EmbedPendingPassagesInput=lambda **kw: types.SimpleNamespace(**kw),
        embed_pending_passages_wf=MagicMock(aio_run_no_wait=dispatch),
    )
    with patch.object(mod.asyncpg, "create_pool", AsyncMock(return_value=pool)), \
            patch.object(mod, "bind_workspace_scope", _bind), \
            patch.object(mod.ingest_progress, "mark_started", AsyncMock()), \
            patch.dict(
                sys.modules,
                {"app.hatchet_workflows.embed_pending_passages": fake_embed_module},
            ):
        result = await _get_embed_verify_func()(_make_input(), _make_ctx())

    assert events[:3] == [
        "begin",
        "bind:a0000000-0000-0000-0000-000000000001:hatchet.ingest_pdf.embed_verify",
        "count",
    ]
    assert result["unembedded_observed"] == 3 and result["redispatched"] is True
    dispatch.assert_awaited_once()
