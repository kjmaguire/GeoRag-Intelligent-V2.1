"""Phase-2 strict-tenancy flag (audit 2026-06-29).

WORKSPACE_STRICT_TENANCY=true makes WorkspaceContext.from_state RAISE instead of
falling back to the default tenant when no workspace_id is available. Default
OFF = Phase-1 observe-only (fallback + counter), so this is non-breaking until
deliberately flipped. These tests pin both behaviors so the flip is safe.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agent.workspace_context import (
    LEGACY_DEFAULT_TENANT_UUID,
    WorkspaceContext,
    WorkspaceResolutionError,
)


class _Deps:
    def __init__(self, workspace_id=None):
        self.workspace_id = workspace_id


def test_resolves_real_workspace_id() -> None:
    ctx = WorkspaceContext.from_state(_Deps("ws-real-123"), site="test")
    assert ctx.workspace_id == "ws-real-123"
    assert ctx.is_fallback is False


def test_phase1_falls_back_to_default_when_missing() -> None:
    # Default OFF (Phase 1): missing workspace_id → default tenant, flagged.
    with patch("app.agent.workspace_context._ALLOW_DEFAULT_TENANT_FALLBACK", True):
        ctx = WorkspaceContext.from_state(_Deps(None), site="test")
    assert ctx.workspace_id == LEGACY_DEFAULT_TENANT_UUID
    assert ctx.is_fallback is True


def test_strict_mode_raises_when_missing() -> None:
    # Flag ON: missing workspace_id → hard fail (no silent default-tenant tag).
    with patch("app.agent.workspace_context._ALLOW_DEFAULT_TENANT_FALLBACK", False):
        with pytest.raises(WorkspaceResolutionError):
            WorkspaceContext.from_state(_Deps(None), site="test")


def test_strict_mode_still_resolves_real_id() -> None:
    # Flag ON must NOT break the happy path (real workspace_id present).
    with patch("app.agent.workspace_context._ALLOW_DEFAULT_TENANT_FALLBACK", False):
        ctx = WorkspaceContext.from_state(_Deps("ws-real-456"), site="test")
    assert ctx.workspace_id == "ws-real-456"
    assert ctx.is_fallback is False
