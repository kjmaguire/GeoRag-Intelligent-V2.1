"""Phase 5 (2026-05-22) — subprocess pool sizing + memory guard tests.

Covers:
  - _compute_parse_max_workers: env override (valid + invalid), psutil-
    missing fallback, default min(cpu_count, 4), floor at 1.
  - _wait_for_memory_headroom: immediate-OK path, wait-then-OK path,
    timeout → MemoryError, env defaults, psutil-missing no-op.

These run inside the fastapi container (or any environment with psutil
installed). When psutil is genuinely absent, fallback paths are tested
by injecting a stub module.

Run with:
    pytest src/fastapi/tests/test_phase5_parse_pool.py -v
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


# Same parser-stub injection used by Phase 1/3 tests so importing
# ingest_pdf doesn't need the real dagster install on host.
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


# ---------------------------------------------------------------------------
# 1. PARSE_SUBPROCESS_MAX_WORKERS env override (valid int)
# ---------------------------------------------------------------------------

def test_compute_max_workers_env_override_valid(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.setenv("PARSE_SUBPROCESS_MAX_WORKERS", "8")
    assert mod._compute_parse_max_workers() == 8


def test_compute_max_workers_env_override_one(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.setenv("PARSE_SUBPROCESS_MAX_WORKERS", "1")
    assert mod._compute_parse_max_workers() == 1


# ---------------------------------------------------------------------------
# 2. Env override invalid → falls back to computed default
# ---------------------------------------------------------------------------

def test_compute_max_workers_env_override_invalid_falls_back(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.setenv("PARSE_SUBPROCESS_MAX_WORKERS", "not-an-int")
    out = mod._compute_parse_max_workers()
    # Computed default = min(cpu_count, 4); always >= 1
    assert 1 <= out <= 4


# ---------------------------------------------------------------------------
# 3. Env override floor at 1 (negative or zero treated as 1)
# ---------------------------------------------------------------------------

def test_compute_max_workers_env_override_zero_floors_to_one(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.setenv("PARSE_SUBPROCESS_MAX_WORKERS", "0")
    assert mod._compute_parse_max_workers() == 1


def test_compute_max_workers_env_override_negative_floors_to_one(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.setenv("PARSE_SUBPROCESS_MAX_WORKERS", "-5")
    assert mod._compute_parse_max_workers() == 1


# ---------------------------------------------------------------------------
# 4. Env unset → computed default uses cpu_count
# ---------------------------------------------------------------------------

def test_compute_max_workers_default_uses_cpu_count(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.delenv("PARSE_SUBPROCESS_MAX_WORKERS", raising=False)
    # Mock os.cpu_count via the module's os ref
    monkeypatch.setattr(mod.os, "cpu_count", lambda: 6)
    out = mod._compute_parse_max_workers()
    # min(6, 4) = 4
    assert out == 4


def test_compute_max_workers_default_caps_at_four(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.delenv("PARSE_SUBPROCESS_MAX_WORKERS", raising=False)
    monkeypatch.setattr(mod.os, "cpu_count", lambda: 32)
    assert mod._compute_parse_max_workers() == 4


def test_compute_max_workers_default_respects_low_cpu_count(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.delenv("PARSE_SUBPROCESS_MAX_WORKERS", raising=False)
    monkeypatch.setattr(mod.os, "cpu_count", lambda: 2)
    assert mod._compute_parse_max_workers() == 2


# ---------------------------------------------------------------------------
# 5. psutil missing → falls back to 1
# ---------------------------------------------------------------------------

def test_compute_max_workers_no_psutil_falls_back_to_one(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.delenv("PARSE_SUBPROCESS_MAX_WORKERS", raising=False)
    # Force ImportError on psutil
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert mod._compute_parse_max_workers() == 1


# ---------------------------------------------------------------------------
# 6. _wait_for_memory_headroom — ample memory returns immediately
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_for_memory_returns_immediately_when_ample(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod

    # 10 GB available > 1500 MB threshold
    fake_psutil = types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(
            available=10 * 1024 * 1024 * 1024,
        ),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    # Should not raise + should not sleep
    sleep_calls = []
    real_sleep = mod.asyncio.sleep

    async def _track_sleep(seconds, *a, **kw):
        sleep_calls.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(mod.asyncio, "sleep", _track_sleep)
    await mod._wait_for_memory_headroom(min_free_mb=1500, max_wait_s=10)
    assert sleep_calls == []  # never slept — ample memory


# ---------------------------------------------------------------------------
# 7. _wait_for_memory_headroom — waits then succeeds when memory recovers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_for_memory_waits_then_recovers(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod

    # First two checks return 500 MB; third returns 2000 MB
    sequence = iter([
        500 * 1024 * 1024,   # tight
        500 * 1024 * 1024,   # still tight
        2000 * 1024 * 1024,  # recovered
    ])

    def _vm():
        return types.SimpleNamespace(available=next(sequence))

    fake_psutil = types.SimpleNamespace(virtual_memory=_vm)
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    # Fast-forward asyncio.sleep so the test doesn't actually wait
    async def _fast_sleep(seconds):
        return None
    monkeypatch.setattr(mod.asyncio, "sleep", _fast_sleep)

    await mod._wait_for_memory_headroom(
        min_free_mb=1500, max_wait_s=10, poll_interval_s=2.0,
    )
    # Iterator exhausted on third call → success


# ---------------------------------------------------------------------------
# 8. _wait_for_memory_headroom — raises MemoryError after timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_for_memory_times_out_to_memoryerror(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod

    fake_psutil = types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(
            available=100 * 1024 * 1024,  # constantly tight
        ),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    # Synthesize fake monotonic clock so we control wall-time progression
    times = iter([0.0, 1.0, 2.0, 3.0, 10.0, 11.0])

    async def _fast_sleep(seconds):
        return None
    monkeypatch.setattr(mod.asyncio, "sleep", _fast_sleep)

    class _FakeLoop:
        def time(self):
            return next(times)
    monkeypatch.setattr(
        mod.asyncio, "get_running_loop", lambda: _FakeLoop(),
    )

    with pytest.raises(MemoryError) as exc_info:
        await mod._wait_for_memory_headroom(
            min_free_mb=1500, max_wait_s=5, poll_interval_s=2.0,
        )
    assert "100MB available" in str(exc_info.value) or "MB available" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 9. _wait_for_memory_headroom — psutil missing is a graceful no-op
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_for_memory_no_psutil_is_noop(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.setitem(sys.modules, "psutil", None)

    # Must return None immediately, no exception
    out = await mod._wait_for_memory_headroom(min_free_mb=1500, max_wait_s=5)
    assert out is None


# ---------------------------------------------------------------------------
# 10. _get_parse_pool — singleton (same instance on repeated calls)
# ---------------------------------------------------------------------------

def test_get_parse_pool_is_singleton(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod

    # Reset module-level pool so the test creates a fresh one
    monkeypatch.setattr(mod, "_PARSE_POOL", None)
    p1 = mod._get_parse_pool()
    p2 = mod._get_parse_pool()
    assert p1 is p2


# ---------------------------------------------------------------------------
# 11. _get_parse_pool — max_workers reflects PARSE_SUBPROCESS_MAX_WORKERS env
# ---------------------------------------------------------------------------

def test_get_parse_pool_honors_max_workers_env(monkeypatch):
    from app.hatchet_workflows import ingest_pdf as mod
    monkeypatch.setattr(mod, "_PARSE_POOL", None)
    monkeypatch.setenv("PARSE_SUBPROCESS_MAX_WORKERS", "3")

    pool = mod._get_parse_pool()
    # ProcessPoolExecutor stores it on _max_workers (CPython internals)
    assert getattr(pool, "_max_workers", None) == 3
