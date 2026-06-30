"""Workspace-prefixed SeaweedFS key construction (Eval 12 R3 follow-up).

Every object in SeaweedFS that carries tenant data MUST be keyed with
the workspace_id as the first path component:

    {workspace_id}/{category}/{relative_path}

If a writer ever drops the prefix, the object becomes globally
readable to any process that can list the bucket — a cross-tenant
leak just waiting to be discovered. This module centralises the
construction so the prefix is impossible to forget.

Usage
-----
    from app.services.seaweedfs_keys import build_workspace_key

    key = build_workspace_key(
        workspace_id=ctx.workspace_id,
        category="bronze",
        relative_path=f"{doc_id}/original.pdf",
    )
    # → "a0000000-…/bronze/<doc_id>/original.pdf"

The validator helper ``assert_workspace_key`` is for read paths and
test assertions — it parses an inbound key and raises
``WorkspaceKeyMissingPrefix`` when the prefix is missing or doesn't
match the expected workspace.
"""
from __future__ import annotations

import re
from uuid import UUID

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


class WorkspaceKeyMissingPrefix(ValueError):
    """Raised when a SeaweedFS key doesn't start with a workspace UUID.

    The fail-loud-on-write contract: any production code path that
    constructs an object key without the prefix is a tenant-leak bug.
    Callers should not catch this — let it propagate to surface as a
    Sentry 5xx so the bug is visible.
    """


def build_workspace_key(
    *,
    workspace_id: str | UUID,
    category: str,
    relative_path: str,
) -> str:
    """Construct a workspace-prefixed object key.

    Args:
      workspace_id: UUID of the owning workspace. Required.
      category: Logical bucket-within-bucket, e.g. "bronze", "renders",
        "support", "exports".
      relative_path: Per-category path relative to the category root.
        May contain forward slashes for nested layouts.

    Raises:
      WorkspaceKeyMissingPrefix: when workspace_id is empty / None
        (defensive — callers should never pass a falsy workspace_id;
        if they do, we want to crash here rather than silently write a
        leaked object).
      ValueError: when category contains "/" or relative_path is empty.
    """
    ws = str(workspace_id or "").strip()
    if not ws:
        raise WorkspaceKeyMissingPrefix(
            "workspace_id is required for every SeaweedFS object key; "
            "got empty/None. This is a tenant-leak bug — fix the caller."
        )
    if not _UUID_RE.match(ws):
        raise WorkspaceKeyMissingPrefix(
            f"workspace_id={ws!r} is not a UUID — refusing to construct "
            "an object key that wouldn't isolate by tenant."
        )
    if "/" in category:
        raise ValueError(
            f"category={category!r} must not contain '/'. Use the "
            "relative_path parameter for nested structure."
        )
    if not relative_path:
        raise ValueError("relative_path must be non-empty")

    # Strip any leading slashes from relative_path so the joined key
    # never has the form "{ws}/{cat}//{rest}".
    rel = relative_path.lstrip("/")
    return f"{ws}/{category}/{rel}"


def assert_workspace_key(
    key: str,
    *,
    expected_workspace_id: str | UUID | None = None,
) -> str:
    """Parse + validate a SeaweedFS key.

    Returns the workspace UUID extracted from the prefix. Raises
    ``WorkspaceKeyMissingPrefix`` when the key has no UUID prefix
    (= bug; the writer forgot the prefix), or when the prefix doesn't
    match ``expected_workspace_id`` (= cross-tenant access attempt).
    """
    if not key:
        raise WorkspaceKeyMissingPrefix("empty key")

    head, _, _ = key.partition("/")
    if not _UUID_RE.match(head):
        raise WorkspaceKeyMissingPrefix(
            f"key {key!r} does not start with a workspace UUID — "
            "writer omitted the prefix. Object is globally accessible "
            "to anyone who can list the bucket. Treat as P1."
        )

    if expected_workspace_id is not None:
        expected = str(expected_workspace_id).strip().lower()
        if head.lower() != expected:
            raise WorkspaceKeyMissingPrefix(
                f"key {key!r} belongs to workspace {head}, not "
                f"expected {expected}. Cross-tenant access blocked."
            )

    return head


__all__ = [
    "WorkspaceKeyMissingPrefix",
    "build_workspace_key",
    "assert_workspace_key",
]
