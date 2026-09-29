"""Audit AGT-10 (2026-09-29): the phase-0 incident-diagnosis agent must work
on every LLM backend.

It called _call_openai_compatible_llm directly, which resolves
settings.effective_llm_url and raises RuntimeError on cohere (the default),
bedrock and anthropic. It now dispatches through llm_calls._call_llm.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import app.agent.llm_calls as llm_calls
import app.agents.phase0.llm_incident_diagnosis as mod
from app.config import settings


class _Pool:
    async def fetch(self, *args: Any, **kwargs: Any):
        return []

    async def fetchrow(self, *args: Any, **kwargs: Any):
        return {"prompt_version_id": 1, "prompt_id": "p", "version": 3,
                "text": "You diagnose incidents.", "parameters": "{}",
                "promotion_state": "production"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["cohere", "bedrock", "anthropic", "vllm"])
async def test_diagnosis_dispatches_through_call_llm(monkeypatch, backend):
    monkeypatch.setattr(settings, "LLM_BACKEND", backend, raising=False)
    monkeypatch.setattr(
        mod, "get_runtime", lambda: SimpleNamespace(pg_pool=_Pool()),
    )

    async def no_traces(*args, **kwargs):
        return []

    monkeypatch.setattr(mod, "_fetch_langfuse_traces", no_traces)

    seen: dict[str, Any] = {}

    async def fake_call_llm(**kwargs):
        seen.update(kwargs)
        return json.dumps({"hypothesis": "queue backlog"})

    monkeypatch.setattr(llm_calls, "_call_llm", fake_call_llm)

    def _explode(*a, **k):  # pragma: no cover — must not be reached
        raise AssertionError("bypassed the backend dispatcher")

    monkeypatch.setattr(llm_calls, "_call_openai_compatible_llm", _explode)

    ctx = SimpleNamespace(workspace_id="ws-1", usage=None)
    raw_fn = mod.llm_incident_diagnosis_run.__wrapped__
    out = await raw_fn(ctx, alert_label="HighLatency")

    assert out["diagnosis"]["hypothesis"] == "queue backlog"
    assert seen["response_format"] == "json"
    assert seen["audit_label"] == "phase0_llm_incident_diagnosis"
    assert seen["workspace_id"] == "ws-1"
    assert "HighLatency" in seen["context"]
