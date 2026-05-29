"""Tests for the 2026-05-23 ingest_pdf hardening (P + Q + R).

Background: the TIFF smoke test exposed an OOM cascade where three
concurrent docling parses on the 36 GB host pushed total memory past
the kernel's edge and the OOM killer SIGKILLed docling subprocesses,
which then tripped an asyncio.to_thread fallback that ran the same
memory-hungry parse in-process, killing the whole worker. Two fixes:

  Q. Per-workspace concurrency cap on ingest_pdf — at most one parse
     per workspace at a time. Different workspaces parallel; same
     workspace queues.
  R. Remove the in-process asyncio.to_thread fallback — on
     BrokenProcessPool, reset the pool and raise so Hatchet retries.

These tests lock both contracts.
"""
from __future__ import annotations

from hatchet_sdk import ConcurrencyLimitStrategy


def test_ingest_pdf_has_per_workspace_singleton_concurrency():
    """Q — concurrency cap is configured per-workspace, singleton, queue."""
    from app.hatchet_workflows.ingest_pdf import ingest_pdf

    cfg = ingest_pdf.config
    assert cfg.concurrency is not None, (
        "ingest_pdf must declare concurrency — without it the parse step "
        "can be invoked by multiple concurrent uploads, each loading "
        "docling+PaddleOCR+RapidOCR models (~3-4 GB each) and OOM-killing "
        "the host. See [[tiff-smoke-2026-05-23]] for the root cause."
    )

    expr = cfg.concurrency
    assert expr.expression == "input.workspace_id", (
        f"Expected per-workspace grouping, got {expr.expression!r}"
    )
    assert expr.max_runs == 1, (
        f"Expected max_runs=1 (one parse per workspace), got {expr.max_runs}"
    )
    assert expr.limit_strategy == ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN, (
        f"Expected GROUP_ROUND_ROBIN (queue subsequent uploads, never "
        f"cancel an in-flight parse), got {expr.limit_strategy}"
    )


def test_no_asyncio_to_thread_fallback_in_parse():
    """R — the in-process fallback must NOT reappear.

    The original 2026-05-22 code caught any Exception from the
    subprocess and fell back to ``asyncio.to_thread(_run_parser_subprocess, ...)``.
    That path:
      1. Re-loaded the same OCR models that just OOMed in the
         subprocess — typically OOMing the parent worker process too.
      2. Ran a multi-minute synchronous parse on the asyncio default
         executor, blocking Hatchet heartbeats until the worker was
         marked dead and the task re-queued.

    Both behaviours are wrong. The fix is to detect BrokenProcessPool
    specifically, reset the pool, and raise so Hatchet's retries=1
    backoff handles it.
    """
    import inspect

    from app.hatchet_workflows import ingest_pdf as ingest_pdf_mod

    src = inspect.getsource(ingest_pdf_mod)
    # The fallback line itself MUST be gone — no `asyncio.to_thread(`
    # invocation inside the parse function.
    assert "asyncio.to_thread(_run_parser_subprocess" not in src, (
        "ingest_pdf.parse must not re-introduce the in-process fallback. "
        "On BrokenProcessPool, reset the pool and raise — let Hatchet "
        "retry. See [[tiff-smoke-2026-05-23]] for why the fallback was "
        "actively harmful."
    )
    # And the new behaviour: there must be a BrokenProcessPool except
    # clause that calls _reset_parse_pool() and raises.
    assert "except BrokenProcessPool" in src
    assert "_reset_parse_pool()" in src


def test_reset_parse_pool_helper_exists_and_shuts_down():
    """R — _reset_parse_pool() exists and clears the cached pool."""
    from app.hatchet_workflows import ingest_pdf as ingest_pdf_mod

    assert hasattr(ingest_pdf_mod, "_reset_parse_pool"), (
        "_reset_parse_pool() must exist so the BrokenProcessPool handler "
        "can rebuild the pool on the next parse."
    )

    # Force a pool into existence, then reset, then check the cache
    # variable is None and a fresh _get_parse_pool() returns a NEW pool.
    pool_a = ingest_pdf_mod._get_parse_pool()
    assert pool_a is not None
    ingest_pdf_mod._reset_parse_pool()
    assert ingest_pdf_mod._PARSE_POOL is None, (
        "_reset_parse_pool must clear the module-level _PARSE_POOL cache"
    )
    pool_b = ingest_pdf_mod._get_parse_pool()
    assert pool_b is not pool_a, (
        "after reset, _get_parse_pool must return a fresh pool, not the "
        "shutdown one"
    )
    # Clean up the test's pool so the next test gets a fresh one too.
    ingest_pdf_mod._reset_parse_pool()
