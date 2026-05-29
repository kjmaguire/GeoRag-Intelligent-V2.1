"""Z.1 / Appendix C §5 — external-LLM egress profile gate tests.

These tests pin the contract for
:mod:`app.agent.egress_gate` and the wiring into
:func:`app.agent.llm_calls._call_anthropic_llm`:

  1. A workspace whose ``silver.workspace_settings.extra_payload`` carries
     ``{"allow_external_llm": true}`` PASSES the gate — the Anthropic
     call is allowed to proceed and the mock SDK is invoked exactly once.

  2. A workspace with ``{"allow_external_llm": false}`` is REFUSED — the
     gate raises :class:`ExternalLlmEgressBlocked` with reason
     ``flag_disabled`` and the mock Anthropic SDK is NEVER touched.

  3. A workspace whose settings row exists but has no ``allow_external_llm``
     key (typical for legacy rows) is REFUSED — default-deny — with
     reason ``flag_not_set``. The mock Anthropic SDK is NEVER touched.

  4. A workspace with no settings row at all is REFUSED — default-deny —
     with reason ``flag_not_set``. The mock Anthropic SDK is NEVER touched.

  5. A call with no workspace context (``workspace_id=None``) is REFUSED
     immediately — reason ``missing_workspace`` — without even hitting
     the DB. The mock Anthropic SDK is NEVER touched.

  6. A DB lookup failure (asyncpg raises) is treated as default-deny with
     reason ``flag_not_set`` (folded for caller-visible determinism;
     the underlying ``db_error`` cause is logged inside the gate).

  7. The raised exception carries
     :attr:`~app.agent.guards.GuardErrorCode.EGRESS_BLOCKED` so the
     orchestrator's typed-guard handler can surface the user-facing
     message defined in ``lang/en/guard_errors.php``.

The tests run standalone — they do not require the live FastAPI stack
or a real PostgreSQL. The Anthropic SDK is mocked via
:mod:`unittest.mock` so a missing API key or absent ``anthropic``
package does not gate the suite. The DB pool is a fake whose
``fetchrow`` is parameterised per-test.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.egress_gate import (
    ExternalLlmEgressBlocked,
    assert_external_llm_allowed,
    evaluate_external_llm_policy,
)
from app.agent.guards import GuardErrorCode


# ---------------------------------------------------------------------------
# Test fixtures — fake asyncpg pool + fake Anthropic SDK
# ---------------------------------------------------------------------------


_WORKSPACE_ALLOWED = "a0000000-0000-0000-0000-000000000001"
_WORKSPACE_DENIED = "b0000000-0000-0000-0000-000000000002"
_WORKSPACE_MISSING_KEY = "c0000000-0000-0000-0000-000000000003"
_WORKSPACE_NO_ROW = "d0000000-0000-0000-0000-000000000004"
_WORKSPACE_DB_ERROR = "e0000000-0000-0000-0000-000000000005"


def _make_pool(
    *,
    rows: dict[str, Any] | None = None,
    raise_on_fetch: bool = False,
) -> Any:
    """Build a fake asyncpg.Pool whose acquire() yields a fake conn whose
    fetchrow() returns the value at ``rows[workspace_id]``.

    ``rows`` keys are workspace UUID strings, values are dicts that
    mimic the asyncpg Record interface (``row["extra_payload"]``).
    When the key is absent, fetchrow() returns None (no row case).
    When ``raise_on_fetch`` is True, fetchrow() raises RuntimeError
    (DB error case).
    """
    rows = rows or {}

    async def _fetchrow(sql: str, workspace_id: str):
        if raise_on_fetch:
            raise RuntimeError("simulated asyncpg failure")
        return rows.get(workspace_id)

    conn = SimpleNamespace(fetchrow=_fetchrow)

    class _AcquireCM:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = SimpleNamespace(acquire=MagicMock(return_value=_AcquireCM()))
    return pool


def _row(extra_payload: dict | str | None) -> dict[str, Any]:
    """Mimic an asyncpg Record returned by SELECT extra_payload ..."""
    return {"extra_payload": extra_payload}


# ---------------------------------------------------------------------------
# Standalone gate logic (works whether or not the Anthropic SDK exists)
# ---------------------------------------------------------------------------


class TestEvaluatePolicy:
    """Direct unit tests on :func:`evaluate_external_llm_policy`."""

    @pytest.mark.asyncio
    async def test_flag_true_is_allowed(self):
        pool = _make_pool(rows={
            _WORKSPACE_ALLOWED: _row({"allow_external_llm": True}),
        })
        allowed, reason = await evaluate_external_llm_policy(
            workspace_id=_WORKSPACE_ALLOWED, pg_pool=pool,
        )
        assert allowed is True
        assert reason == "flag_enabled"

    @pytest.mark.asyncio
    async def test_flag_false_is_blocked(self):
        pool = _make_pool(rows={
            _WORKSPACE_DENIED: _row({"allow_external_llm": False}),
        })
        allowed, reason = await evaluate_external_llm_policy(
            workspace_id=_WORKSPACE_DENIED, pg_pool=pool,
        )
        assert allowed is False
        assert reason == "flag_disabled"

    @pytest.mark.asyncio
    async def test_key_missing_is_blocked_default_deny(self):
        pool = _make_pool(rows={
            _WORKSPACE_MISSING_KEY: _row({"unrelated_pref": "value"}),
        })
        allowed, reason = await evaluate_external_llm_policy(
            workspace_id=_WORKSPACE_MISSING_KEY, pg_pool=pool,
        )
        assert allowed is False
        assert reason == "flag_not_set"

    @pytest.mark.asyncio
    async def test_no_row_is_blocked_default_deny(self):
        # _WORKSPACE_NO_ROW not in rows → fetchrow returns None
        pool = _make_pool(rows={})
        allowed, reason = await evaluate_external_llm_policy(
            workspace_id=_WORKSPACE_NO_ROW, pg_pool=pool,
        )
        assert allowed is False
        assert reason == "flag_not_set"

    @pytest.mark.asyncio
    async def test_no_workspace_is_blocked(self):
        pool = _make_pool(rows={})
        allowed, reason = await evaluate_external_llm_policy(
            workspace_id=None, pg_pool=pool,
        )
        assert allowed is False
        assert reason == "missing_workspace"

    @pytest.mark.asyncio
    async def test_empty_workspace_is_blocked(self):
        # The gate treats an empty string the same as None.
        pool = _make_pool(rows={})
        allowed, reason = await evaluate_external_llm_policy(
            workspace_id="", pg_pool=pool,
        )
        assert allowed is False
        assert reason == "missing_workspace"

    @pytest.mark.asyncio
    async def test_db_error_is_blocked_default_deny(self):
        pool = _make_pool(raise_on_fetch=True)
        allowed, reason = await evaluate_external_llm_policy(
            workspace_id=_WORKSPACE_DB_ERROR, pg_pool=pool,
        )
        assert allowed is False
        # DB error is folded into flag_not_set for caller-side determinism;
        # the db_error path is logged inside the gate.
        assert reason == "flag_not_set"

    @pytest.mark.asyncio
    async def test_json_string_payload_is_parsed(self):
        # Some asyncpg configurations return jsonb as a raw JSON string
        # (no codec). The gate must handle both shapes.
        pool = _make_pool(rows={
            _WORKSPACE_ALLOWED: _row(json.dumps({"allow_external_llm": True})),
        })
        allowed, reason = await evaluate_external_llm_policy(
            workspace_id=_WORKSPACE_ALLOWED, pg_pool=pool,
        )
        assert allowed is True
        assert reason == "flag_enabled"

    @pytest.mark.asyncio
    async def test_non_bool_value_is_blocked(self):
        # A misconfigured row that stored a string "true" instead of a
        # JSON boolean must NOT be coerced — default-deny.
        pool = _make_pool(rows={
            _WORKSPACE_ALLOWED: _row({"allow_external_llm": "true"}),
        })
        allowed, reason = await evaluate_external_llm_policy(
            workspace_id=_WORKSPACE_ALLOWED, pg_pool=pool,
        )
        assert allowed is False
        assert reason == "flag_not_set"


class TestAssertGate:
    """The raise-on-refuse public entry point used by ``_call_anthropic_llm``."""

    @pytest.mark.asyncio
    async def test_allowed_returns_none(self):
        pool = _make_pool(rows={
            _WORKSPACE_ALLOWED: _row({"allow_external_llm": True}),
        })
        result = await assert_external_llm_allowed(
            workspace_id=_WORKSPACE_ALLOWED, pg_pool=pool,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_denied_raises_with_guard_code(self):
        pool = _make_pool(rows={
            _WORKSPACE_DENIED: _row({"allow_external_llm": False}),
        })
        with pytest.raises(ExternalLlmEgressBlocked) as excinfo:
            await assert_external_llm_allowed(
                workspace_id=_WORKSPACE_DENIED, pg_pool=pool,
            )
        exc = excinfo.value
        assert exc.workspace_id == _WORKSPACE_DENIED
        assert exc.reason == "flag_disabled"
        assert exc.guard_code is GuardErrorCode.EGRESS_BLOCKED
        # User-visible message is the catalog default (matches
        # lang/en/guard_errors.php EGRESS_BLOCKED).
        assert "External LLM access is disabled" in exc.user_message

    @pytest.mark.asyncio
    async def test_missing_workspace_raises(self):
        pool = _make_pool(rows={})
        with pytest.raises(ExternalLlmEgressBlocked) as excinfo:
            await assert_external_llm_allowed(
                workspace_id=None, pg_pool=pool,
            )
        assert excinfo.value.reason == "missing_workspace"
        assert excinfo.value.guard_code is GuardErrorCode.EGRESS_BLOCKED


# ---------------------------------------------------------------------------
# Wiring into _call_anthropic_llm — the call site refuses BEFORE invoking SDK
# ---------------------------------------------------------------------------


def _make_fake_anthropic_client(final_text: str = "ok"):
    """Build a fake AsyncAnthropic-shaped client with usage telemetry."""
    text_block = SimpleNamespace(type="text", text=final_text)
    fake_usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    fake_msg = SimpleNamespace(content=[text_block], usage=fake_usage)
    return SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=fake_msg),
            stream=AsyncMock(side_effect=AssertionError(
                "stream() must not be called in egress-gate tests"
            )),
        )
    )


class TestAnthropicCallSiteWiring:
    """Pin the contract that ``_call_anthropic_llm`` runs the gate FIRST,
    before any SDK construction or network egress."""

    @pytest.mark.asyncio
    async def test_allowed_workspace_passes_through_to_anthropic(self):
        from app.agent.llm_calls import _call_anthropic_llm

        pool = _make_pool(rows={
            _WORKSPACE_ALLOWED: _row({"allow_external_llm": True}),
        })
        client = _make_fake_anthropic_client(final_text="grounded answer")

        result = await _call_anthropic_llm(
            user_message="CONTEXT:\nfoo\n\nUSER QUESTION: bar\n\nANSWER:",
            temperature=0.1,
            client=client,
            model="claude-opus-4-7",
            system_prompt="system",
            workspace_id=_WORKSPACE_ALLOWED,
            pg_pool=pool,
        )

        assert result == "grounded answer"
        # Confirm the SDK WAS actually invoked exactly once.
        assert client.messages.create.await_count == 1

    @pytest.mark.asyncio
    async def test_denied_workspace_blocks_before_sdk_call(self):
        from app.agent.llm_calls import _call_anthropic_llm

        pool = _make_pool(rows={
            _WORKSPACE_DENIED: _row({"allow_external_llm": False}),
        })
        client = _make_fake_anthropic_client()

        with pytest.raises(ExternalLlmEgressBlocked) as excinfo:
            await _call_anthropic_llm(
                user_message="CONTEXT:\nfoo\n\nUSER QUESTION: bar\n\nANSWER:",
                temperature=0.1,
                client=client,
                model="claude-opus-4-7",
                system_prompt="system",
                workspace_id=_WORKSPACE_DENIED,
                pg_pool=pool,
            )

        assert excinfo.value.reason == "flag_disabled"
        assert excinfo.value.guard_code is GuardErrorCode.EGRESS_BLOCKED
        # CRITICAL: the Anthropic SDK MUST NOT have been touched. No
        # prompt content, system text, or workspace data left the
        # trust boundary.
        assert client.messages.create.await_count == 0
        assert client.messages.stream.await_count == 0

    @pytest.mark.asyncio
    async def test_missing_settings_row_blocks_before_sdk_call(self):
        """Default-deny — no row in silver.workspace_settings → refuse."""
        from app.agent.llm_calls import _call_anthropic_llm

        pool = _make_pool(rows={})  # no row for any workspace
        client = _make_fake_anthropic_client()

        with pytest.raises(ExternalLlmEgressBlocked) as excinfo:
            await _call_anthropic_llm(
                user_message="CONTEXT:\nfoo\n\nUSER QUESTION: bar\n\nANSWER:",
                temperature=0.1,
                client=client,
                model="claude-opus-4-7",
                system_prompt="system",
                workspace_id=_WORKSPACE_NO_ROW,
                pg_pool=pool,
            )

        assert excinfo.value.reason == "flag_not_set"
        assert client.messages.create.await_count == 0

    @pytest.mark.asyncio
    async def test_no_workspace_blocks_before_sdk_call(self):
        """System-level callers MUST supply workspace_id to use Anthropic."""
        from app.agent.llm_calls import _call_anthropic_llm

        pool = _make_pool(rows={})
        client = _make_fake_anthropic_client()

        with pytest.raises(ExternalLlmEgressBlocked) as excinfo:
            await _call_anthropic_llm(
                user_message="CONTEXT:\nfoo\n\nUSER QUESTION: bar\n\nANSWER:",
                temperature=0.1,
                client=client,
                model="claude-opus-4-7",
                system_prompt="system",
                workspace_id=None,
                pg_pool=pool,
            )

        assert excinfo.value.reason == "missing_workspace"
        # The gate short-circuits before touching the pool at all.
        pool.acquire.assert_not_called()
        assert client.messages.create.await_count == 0
