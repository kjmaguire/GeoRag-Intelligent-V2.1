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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Parser-stub injection (same pattern as other Phase tests so ingest_pdf
# imports cleanly outside the dagster install).
def _ensure_parser_stub_module():
    if "georag_dagster.parsers.pdf_report" in sys.modules:
        return
    pkg_root = sys.modules.get("georag_dagster") or types.ModuleType("georag_dagster")
    pkg_parsers = types.ModuleType("georag_dagster.parsers")
    mod = types.ModuleType("georag_dagster.parsers.pdf_report")
    mod._FIGURE_TEMPDIR_ROOT = "/tmp/georag_figures"

    def _figure_tempdir(sha256: str) -> str:
        import os as _os
        d = f"{mod._FIGURE_TEMPDIR_ROOT}/{sha256}"
        _os.makedirs(d, exist_ok=True)
        return d

    mod._figure_tempdir = _figure_tempdir
    mod.parse_pdf_report = MagicMock()
    pkg_parsers.pdf_report = mod
    pkg_root.parsers = pkg_parsers
    sys.modules["georag_dagster"] = pkg_root
    sys.modules["georag_dagster.parsers"] = pkg_parsers
    sys.modules["georag_dagster.parsers.pdf_report"] = mod


_ensure_parser_stub_module()


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
    end = src.index("async def p04p_dual_write", start)
    body = src[start:end]
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
    # Find the line decorating embed_verify
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if "async def embed_verify" in ln:
            decorator_line = lines[i - 1]
            assert 'execution_timeout="60s"' in decorator_line
            assert 'execution_timeout="2m"' not in decorator_line
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

    with patch.object(mod.asyncpg, "create_pool", AsyncMock(return_value=fake_pool)):
        await embed_verify(_make_input(), _make_ctx())

    assert len(fetch_calls) == 1
