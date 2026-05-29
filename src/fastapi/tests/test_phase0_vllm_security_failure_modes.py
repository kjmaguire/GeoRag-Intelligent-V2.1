"""Eval 11 R3 follow-up — Phase 0 vllm_security_check failure-mode tests.

The agent's primary failure modes per its docstring:
  1. GitHub Advisory DB unreachable (httpx.HTTPError) — must NOT raise,
     must return summary with errors=1 and a note.
  2. GitHub returns non-200 (rate-limited, 502 from CDN) — same.
  3. VLLM_VERSION env unset — informational scan only; must NOT match.

These are unit tests against the agent's HTTP-failure handling using
a monkeypatched httpx.AsyncClient.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.agents.phase0.vllm_security_check import vllm_security_check_run


class _FakeCtx:
    workspace_id = "a0000000-0000-0000-0000-000000000001"
    trace_id = "test-trace-id"


class _FakeRuntime:
    pg_pool = None


@pytest.fixture(autouse=True)
def _register_fake_runtime() -> None:
    """Register a real fake runtime via register_runtime so all the
    @georag_agent decorator's internal lookups see a valid object.

    More robust than monkeypatching N call sites — the agent runtime
    is a process-singleton and register_runtime accepts our fake.
    """
    from app.agents.runtime import register_runtime  # noqa: PLC0415

    class _FakePool:
        async def fetchrow(self, *a, **kw): return None
        async def fetch(self, *a, **kw): return []
        async def execute(self, *a, **kw): return None

        # async context manager protocol for `async with pool.acquire():`
        def acquire(self): return self
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    register_runtime(pg_pool=_FakePool(), redis=None)


@pytest.fixture(autouse=True)
def _stub_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_runtime() reaches into app.state; stub it so the test runs
    without a live FastAPI lifespan.

    The @georag_agent decorator wraps the function and looks up a
    timeout policy via app.agents.runtime.get_runtime BEFORE the
    function body runs, so we patch BOTH the decorator's lookup path
    (app.agents.wrapper._load_timeout_policy) and the function's own
    get_runtime import.
    """
    monkeypatch.setattr(
        "app.agents.phase0.vllm_security_check.get_runtime",
        lambda: _FakeRuntime(),
    )
    monkeypatch.setattr(
        "app.agents.runtime.get_runtime",
        lambda: _FakeRuntime(),
    )
    # Bypass the timeout-policy DB lookup. The wrapper expects a
    # dict-shaped row (asyncpg.Record), so we hand back a default
    # policy with circuit breaker disabled and generous timeouts.
    _default_policy = {
        "agent_name": "vLLM Security Check Agent",
        "risk_tier": "R0",
        "soft_timeout_ms": 60_000,
        "hard_timeout_ms": 120_000,
        "retry_count": 0,
        "circuit_breaker_scope": "none",
        "failure_threshold": 0,
        "cool_down_seconds": 0,
    }
    monkeypatch.setattr(
        "app.agents.wrapper._load_timeout_policy",
        AsyncMock(return_value=_default_policy),
    )
    # The wrapper also calls _circuit_check / _circuit_record /
    # _write_usage_event / emit_audit / _idempotency_lookup — all
    # touch the runtime's pg_pool. Stub each to no-op so the inner
    # function can run and return its dict.
    monkeypatch.setattr(
        "app.agents.wrapper._circuit_check",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.agents.wrapper._circuit_record",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.agents.wrapper._write_usage_event",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.agents.wrapper._idempotency_lookup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.agents.wrapper.emit_audit",
        AsyncMock(return_value=None),
    )
    # emit_audit writes to pg_pool; stub it so we don't need a DB.
    monkeypatch.setattr(
        "app.agents.phase0.vllm_security_check.emit_audit",
        AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_github_unreachable_returns_summary_not_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network failure to the Advisory DB must not crash the agent.

    The agent runs on a Hatchet schedule — a transient GitHub outage
    would otherwise kill the workflow and surface as a Sentry alert
    storm. The contract is degrade-gracefully: errors=1, no matches.
    """
    monkeypatch.setenv("VLLM_VERSION", "v0.21.0")

    class _RaisingClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw):
            raise httpx.ConnectError("github unreachable")

    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)

    # @georag_agent wraps `invoke` which reads ctx from kwargs and
    # auto-constructs one if absent. Don't pass ctx positionally.
    result = await vllm_security_check_run()
    # The @georag_agent decorator wraps the dict in an AgentResult.
    if hasattr(result, "value"):
        assert result.value is not None, (
            f"wrapper returned value=None; outcome={result.outcome!r} "
            f"error={result.error!r}"
        )
        summary = result.value
    else:
        summary = result
    assert summary["errors"] == 1
    assert "github unreachable" in summary.get("error_message", "")
    assert summary["matches"] == []
    assert summary["alerts_emitted"] == 0


@pytest.mark.asyncio
async def test_github_returns_502_recorded_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """502 from GitHub CDN — agent must record + return, not raise."""
    monkeypatch.setenv("VLLM_VERSION", "v0.21.0")

    class _BadStatusResponse:
        status_code = 502
        def json(self): return []

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw):
            return _BadStatusResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    # @georag_agent wraps `invoke` which reads ctx from kwargs and
    # auto-constructs one if absent. Don't pass ctx positionally.
    result = await vllm_security_check_run()
    # The @georag_agent decorator wraps the dict in an AgentResult.
    if hasattr(result, "value"):
        assert result.value is not None, (
            f"wrapper returned value=None; outcome={result.outcome!r} "
            f"error={result.error!r}"
        )
        summary = result.value
    else:
        summary = result
    assert summary["errors"] == 1
    assert summary["http_status"] == 502
    assert summary["matches"] == []


@pytest.mark.asyncio
async def test_vllm_version_unset_skips_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VLLM_VERSION unset is the cold-start case (Docker image without
    the env passthrough). The agent runs the informational scan but
    must not raise on any advisory."""
    monkeypatch.delenv("VLLM_VERSION", raising=False)

    class _OkResponse:
        status_code = 200
        def json(self):
            return [
                {
                    "ghsa_id": "GHSA-test-1234",
                    "summary": "Test advisory",
                    "severity": "high",
                    "html_url": "https://github.com/example",
                    "published_at": "2026-05-01T00:00:00Z",
                    "vulnerabilities": [
                        {
                            "package": {"name": "vllm", "ecosystem": "pip"},
                            "vulnerable_version_range": "< 0.22.0",
                        }
                    ],
                }
            ]

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw):
            return _OkResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    # @georag_agent wraps `invoke` which reads ctx from kwargs and
    # auto-constructs one if absent. Don't pass ctx positionally.
    result = await vllm_security_check_run()
    # The @georag_agent decorator wraps the dict in an AgentResult.
    if hasattr(result, "value"):
        assert result.value is not None, (
            f"wrapper returned value=None; outcome={result.outcome!r} "
            f"error={result.error!r}"
        )
        summary = result.value
    else:
        summary = result
    assert summary["checked"] is True
    assert summary["advisories_seen"] == 1
    # No version → no match — purely informational.
    assert summary["matches"] == []
    assert "VLLM_VERSION env unset" in summary.get("note", "")
