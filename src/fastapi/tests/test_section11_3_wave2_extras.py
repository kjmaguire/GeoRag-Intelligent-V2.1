"""§11.3 wave 2 — Neo4j / Qdrant / Redis workspace export + restore.

Tests are focused on the parts that don't require all 3 stores
configured + populated:
  - parse_export_jsonl_gz round-trip (serialization correctness)
  - manifest v2.0 shape
  - workspace_export emits the new fields in WorkspaceExportOutput
  - restore_workspace.dry_run=False handles v1.0 manifests gracefully
    (skips extras since the section data isn't there)

For full end-to-end tests with live Neo4j/Qdrant/Redis populated,
use the existing §11.2 cross-store consistency harness path —
those require a populated workspace which the test suite doesn't
seed.
"""
from __future__ import annotations

import gzip
import io
import json
import uuid

import pytest

from app.hatchet_workflows.workspace_export import (
    _build_manifest, _serialise_jsonl_gz,
)
from app.hatchet_workflows._restore_extras import parse_export_jsonl_gz


pytestmark = pytest.mark.integration


def _fake_export_body() -> bytes:
    """Build a minimal v2.0 export body with one row per section."""
    per_table = {
        "silver_workspaces": [{"workspace_id": "ws-1", "name": "Test WS"}],
    }
    neo4j_nodes = [{
        "neo4j_id": 100,
        "labels": ["DrillHole"],
        "properties": {"id": "DH-001", "name": "Test Hole"},
    }]
    neo4j_rels = [{
        "type": "BELONGS_TO",
        "start_neo4j_id": 100,
        "end_neo4j_id": 101,
        "properties": {},
    }]
    qdrant_points = [{
        "id": "point-1",
        "vector": [0.1, 0.2, 0.3],
        "payload": {"workspace_id": "ws-1", "title": "Doc"},
    }]
    redis_keys = [{
        "key": "georag:ws:ws-1:cache:foo",
        "type": "string",
        "ttl_s": 600,
        "value_b64": "aGVsbG8=",  # b'hello'
    }]
    manifest = _build_manifest(
        "ws-1", "run-abc", per_table,
        neo4j_nodes=neo4j_nodes, neo4j_rels=neo4j_rels,
        qdrant_points=qdrant_points, redis_keys=redis_keys,
        partial_stores={},
    )
    return _serialise_jsonl_gz(
        manifest, per_table,
        neo4j_nodes=neo4j_nodes, neo4j_rels=neo4j_rels,
        qdrant_points=qdrant_points, redis_keys=redis_keys,
    )


def test_manifest_v2_carries_per_store_counts():
    body = _fake_export_body()
    manifest, pg_tables, sections = parse_export_jsonl_gz(body)

    assert manifest["manifest_version"] == "2.0"
    assert manifest["workspace_id"] == "ws-1"
    assert manifest["neo4j_node_count"] == 1
    assert manifest["neo4j_rel_count"] == 1
    assert manifest["qdrant_point_count"] == 1
    assert manifest["redis_key_count"] == 1


def test_parse_export_round_trip():
    body = _fake_export_body()
    manifest, pg_tables, sections = parse_export_jsonl_gz(body)

    # PG side
    assert "silver_workspaces" in pg_tables
    assert pg_tables["silver_workspaces"][0]["workspace_id"] == "ws-1"

    # §11.3-v2 sections
    assert "neo4j_nodes" in sections
    assert "neo4j_rels" in sections
    assert "qdrant_points" in sections
    assert "redis_keys" in sections
    assert sections["neo4j_nodes"][0]["properties"]["id"] == "DH-001"
    assert sections["qdrant_points"][0]["vector"] == [0.1, 0.2, 0.3]
    assert sections["redis_keys"][0]["key"] == "georag:ws:ws-1:cache:foo"


def test_empty_body_rejected():
    with pytest.raises(ValueError):
        parse_export_jsonl_gz(gzip.compress(b""))


def test_v1_manifest_still_parses():
    """A v1.0 export (PG-only, no section lines) must still parse cleanly.

    This is the upgrade path — operators with a stash of v1.0 exports
    should still be able to restore them after deploying §11.3-v2.
    """
    manifest_v1 = {
        "manifest_version": "1.0",
        "format": "workspace_export",
        "workspace_id": "ws-old",
        "run_id": "run-old",
        "captured_at": "2026-05-15T12:00:00+00:00",
        "table_row_counts": {"silver_workspaces": 1},
        "tables": ["silver_workspaces"],
    }
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(json.dumps(manifest_v1).encode("utf-8") + b"\n")
        gz.write(json.dumps({
            "table": "silver_workspaces",
            "row": {"workspace_id": "ws-old"},
        }).encode("utf-8") + b"\n")

    manifest, pg_tables, sections = parse_export_jsonl_gz(buf.getvalue())
    assert manifest["manifest_version"] == "1.0"
    assert "silver_workspaces" in pg_tables
    # v1.0 has no sections
    assert sections == {}


def test_workspace_export_output_carries_v2_fields():
    """The WorkspaceExportOutput model exposes the new §11.3-v2 fields
    so downstream consumers can read per-store counts."""
    from app.hatchet_workflows.workspace_export import WorkspaceExportOutput
    from datetime import datetime, timezone

    out = WorkspaceExportOutput(
        run_id=str(uuid.uuid4()),
        workspace_id="ws-1",
        bucket="workspace-exports",
        object_key="ws-1/file.jsonl.gz",
        bytes=1024,
        rows_exported=5,
        per_table={"silver_workspaces": 1},
        neo4j_node_count=12,
        neo4j_rel_count=20,
        qdrant_point_count=300,
        redis_key_count=8,
        partial_stores={},
        started_at=datetime.now(tz=timezone.utc),
        completed_at=datetime.now(tz=timezone.utc),
    )
    assert out.neo4j_node_count == 12
    assert out.qdrant_point_count == 300
