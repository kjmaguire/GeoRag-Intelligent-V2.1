"""Eval 12 R3 follow-up — workspace-prefixed key guard tests.

These tests pin the contract from app.services.seaweedfs_keys.
Failure modes:

  - Writer drops the workspace prefix → WorkspaceKeyMissingPrefix
  - Reader receives a key from a different workspace → blocked
  - Empty/None workspace_id → blocked (defensive)
  - Non-UUID prefix → blocked (someone passed a string ID)
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.seaweedfs_keys import (
    WorkspaceKeyMissingPrefix,
    assert_workspace_key,
    build_workspace_key,
)


WS_A = "a0000000-0000-0000-0000-000000000001"
WS_B = "b0000000-0000-0000-0000-000000000001"


class TestBuild:
    def test_basic_construction(self) -> None:
        key = build_workspace_key(
            workspace_id=WS_A,
            category="bronze",
            relative_path="doc-123/original.pdf",
        )
        assert key == f"{WS_A}/bronze/doc-123/original.pdf"

    def test_nested_relative_path(self) -> None:
        key = build_workspace_key(
            workspace_id=WS_A,
            category="renders",
            relative_path="pages/2026/05/page-42.png",
        )
        assert key == f"{WS_A}/renders/pages/2026/05/page-42.png"

    def test_uuid_object_accepted(self) -> None:
        key = build_workspace_key(
            workspace_id=uuid4(),
            category="exports",
            relative_path="bundle.zip",
        )
        assert key.endswith("/exports/bundle.zip")
        # Prefix is the UUID, stringified.
        assert assert_workspace_key(key)

    def test_strips_leading_slash_on_rel_path(self) -> None:
        # Defensive: callers sometimes pass paths with leading "/".
        # We must not produce double-slashes.
        key = build_workspace_key(
            workspace_id=WS_A,
            category="support",
            relative_path="/packet.zip",
        )
        assert key == f"{WS_A}/support/packet.zip"
        assert "//" not in key


class TestBuildRejects:
    def test_empty_workspace_id(self) -> None:
        with pytest.raises(WorkspaceKeyMissingPrefix):
            build_workspace_key(
                workspace_id="", category="bronze", relative_path="a",
            )

    def test_none_workspace_id(self) -> None:
        with pytest.raises(WorkspaceKeyMissingPrefix):
            build_workspace_key(
                workspace_id=None,  # type: ignore[arg-type]
                category="bronze", relative_path="a",
            )

    def test_non_uuid_workspace_id(self) -> None:
        with pytest.raises(WorkspaceKeyMissingPrefix):
            build_workspace_key(
                workspace_id="my-friendly-tenant-name",
                category="bronze", relative_path="a",
            )

    def test_category_with_slash(self) -> None:
        with pytest.raises(ValueError):
            build_workspace_key(
                workspace_id=WS_A,
                category="bronze/files",  # use relative_path instead
                relative_path="a",
            )

    def test_empty_relative_path(self) -> None:
        with pytest.raises(ValueError):
            build_workspace_key(
                workspace_id=WS_A, category="bronze", relative_path="",
            )


class TestAssertWorkspaceKey:
    def test_valid_key_passes(self) -> None:
        key = f"{WS_A}/bronze/foo.pdf"
        assert assert_workspace_key(key) == WS_A

    def test_unprefixed_key_blocked(self) -> None:
        # The bug we're guarding against: writer forgot the prefix.
        with pytest.raises(WorkspaceKeyMissingPrefix):
            assert_workspace_key("bronze/foo.pdf")

    def test_legacy_friendly_id_blocked(self) -> None:
        with pytest.raises(WorkspaceKeyMissingPrefix):
            assert_workspace_key("acme-corp/bronze/foo.pdf")

    def test_cross_tenant_blocked(self) -> None:
        key = f"{WS_A}/bronze/foo.pdf"
        with pytest.raises(WorkspaceKeyMissingPrefix) as exc:
            assert_workspace_key(key, expected_workspace_id=WS_B)
        assert "Cross-tenant" in str(exc.value)

    def test_same_workspace_passes(self) -> None:
        key = f"{WS_A}/bronze/foo.pdf"
        assert assert_workspace_key(key, expected_workspace_id=WS_A) == WS_A

    def test_empty_key_blocked(self) -> None:
        with pytest.raises(WorkspaceKeyMissingPrefix):
            assert_workspace_key("")
