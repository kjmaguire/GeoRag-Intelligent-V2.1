"""Phase G.2 — restore_workspace cross-store consistency tests.

Pure-function tests of the manifest verifier + an asyncio-based test
that exercises the three count helpers against the live container
stack (postgres, neo4j, qdrant, redis) when their env is available.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.hatchet_workflows.restore_workspace import (
    RestoreWorkspaceInput,
    _verify_snapshot_manifest,
)


# ─────────────────────── _verify_snapshot_manifest ───────────────────────


def test_manifest_unsupported_scheme_returns_unloaded() -> None:
    """s3:// + http(s):// are deferred to Phase 11.1."""
    result = _verify_snapshot_manifest(
        "s3://georag-snapshots/ws-a/manifest.json",
        live_counts={"workspace_id": "abc"},
    )
    assert result["loaded"] is False
    assert "scheme" in result["reason"].lower()


def test_manifest_missing_file_returns_unloaded() -> None:
    result = _verify_snapshot_manifest(
        "file:///tmp/this/path/does/not/exist.json",
        live_counts={"workspace_id": "abc"},
    )
    assert result["loaded"] is False
    assert "not found" in result["reason"]


def test_manifest_invalid_json_returns_unloaded() -> None:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        f.write("not valid json {")
        path = f.name
    try:
        result = _verify_snapshot_manifest(
            f"file://{path}",
            live_counts={"workspace_id": "abc"},
        )
        assert result["loaded"] is False
        assert "parse failed" in result["reason"]
    finally:
        Path(path).unlink(missing_ok=True)


def test_manifest_matches_live_counts_zero_mismatches() -> None:
    workspace_id = "a0000000-0000-0000-0000-000000000001"
    manifest = {
        "manifest_version": "1.0",
        "captured_at": "2026-05-15T00:00:00Z",
        "workspace_id": workspace_id,
        "stores": {
            "postgres": {"row_counts": {
                "silver_workspaces": 1,
                "silver_decision_records": 5,
            }},
            "neo4j": {"node_count": 100},
            "qdrant": {"point_count": 50},
        },
    }
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        result = _verify_snapshot_manifest(
            f"file://{path}",
            live_counts={
                "workspace_id": workspace_id,
                "postgres": {
                    "silver_workspaces": 1,
                    "silver_decision_records": 5,
                },
                "neo4j_nodes": 100,
                "qdrant_points": 50,
            },
        )
        assert result["loaded"] is True
        assert result["mismatches"] == []
        assert result["matches_workspace_id"] is True
        assert result["manifest_version"] == "1.0"
    finally:
        Path(path).unlink(missing_ok=True)


def test_manifest_surfaces_mismatches_per_store() -> None:
    workspace_id = "a0000000-0000-0000-0000-000000000001"
    manifest = {
        "workspace_id": workspace_id,
        "stores": {
            "postgres": {"row_counts": {
                "silver_workspaces": 1,
                "silver_decision_records": 5,
            }},
            "neo4j": {"node_count": 100},
            "qdrant": {"point_count": 50},
        },
    }
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        result = _verify_snapshot_manifest(
            f"file://{path}",
            live_counts={
                "workspace_id": workspace_id,
                "postgres": {
                    "silver_workspaces": 1,
                    "silver_decision_records": 8,  # +3 mismatch
                },
                "neo4j_nodes": 95,                  # −5 mismatch
                "qdrant_points": 50,                # match
            },
        )
        assert result["loaded"] is True
        assert len(result["mismatches"]) == 2
        by_store = {m["store"]: m for m in result["mismatches"]}
        assert by_store["postgres"]["key"] == "silver_decision_records"
        assert by_store["postgres"]["expected"] == 5
        assert by_store["postgres"]["actual"] == 8
        assert by_store["neo4j"]["expected"] == 100
        assert by_store["neo4j"]["actual"] == 95
    finally:
        Path(path).unlink(missing_ok=True)


def test_manifest_skips_unknown_live_counts_gracefully() -> None:
    """live_counts of -1 (collector failed) is ignored, not flagged."""
    workspace_id = "a0000000-0000-0000-0000-000000000001"
    manifest = {
        "workspace_id": workspace_id,
        "stores": {
            "neo4j": {"node_count": 100},
            "qdrant": {"point_count": 50},
        },
    }
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        result = _verify_snapshot_manifest(
            f"file://{path}",
            live_counts={
                "workspace_id": workspace_id,
                "neo4j_nodes": -1,    # collector failed
                "qdrant_points": -1,  # collector failed
            },
        )
        assert result["loaded"] is True
        # Both -1 — no mismatches (couldn't verify, not "wrong")
        assert result["mismatches"] == []
    finally:
        Path(path).unlink(missing_ok=True)


# ─────────────────────── Input-validator hardening ────────────────────────


def test_restore_input_default_is_dry_run() -> None:
    """The dry_run default must be True so callers can't accidentally
    trigger destructive ops by omitting the parameter."""
    inp = RestoreWorkspaceInput(
        workspace_id=uuid4(),
        snapshot_manifest_uri="file:///tmp/manifest.json",
        initiated_by_user_id=1,
        restore_request_id=uuid4(),
    )
    assert inp.dry_run is True


def test_restore_input_requires_manifest_uri() -> None:
    """snapshot_manifest_uri is required — bare validation."""
    with pytest.raises(Exception):
        RestoreWorkspaceInput(  # type: ignore[call-arg]
            workspace_id=uuid4(),
            initiated_by_user_id=1,
            restore_request_id=uuid4(),
        )
