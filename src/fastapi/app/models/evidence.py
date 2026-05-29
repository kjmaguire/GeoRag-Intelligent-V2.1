"""Evidence-model Pydantic stubs for the GeoRAG FastAPI service.

These models mirror the three new PostGIS tables introduced in addendum §04j
(Module 3 Phase B, 2026-04-20):

    silver.document_revisions
    silver.evidence_items
    silver.structured_record_lineage

Status: STUB — the tables are pending senior-reviewer approval and have not yet
been migrated.  These models are added now so that the FastAPI routers and the
Dagster ingestion assets can reference them at import time without coupling the
application layer to the migration deploy schedule.

No endpoints currently read from or write to these tables.  The write path
will be wired in B8.5 (gated on Module 6 readiness).  The read path (evidence
inspector §10s) belongs to Module 6.

ENUM handling
-------------
evidence_type in the DB uses VARCHAR(32) + CHECK constraint (not a PostgreSQL
ENUM type).  The Pydantic Literal mirrors the four CHECK values exactly.
If a new evidence type is added, update the CHECK constraint, the Literal, and
the type-consistency CHECK in a coordinated PR.

FK relationships
----------------
    DocumentRevision.document_id    → silver.reports.report_id (UUID)
    DocumentRevision.workspace_id   → silver.workspaces.workspace_id (UUID)
    EvidenceItem.workspace_id       → silver.workspaces.workspace_id (UUID)
    EvidenceItem.passage_id         → silver.document_passages.passage_id (UUID, nullable)
    StructuredRecordLineage.evidence_id → silver.evidence_items.evidence_id (UUID)

Coordination with Module 6
--------------------------
When Module 6 creates answer_citation_items it will add:
    evidence_id UUID NULL REFERENCES silver.evidence_items(evidence_id)
No changes to this file are needed for that step.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# EvidenceType literal — mirrors the DB CHECK constraint in evidence_items.
# ---------------------------------------------------------------------------

EvidenceTypeLiteral = Literal[
    "document_passage",
    "structured_record",
    "graph_edge",
    "map_feature",
]


# ---------------------------------------------------------------------------
# DocumentRevision
# ---------------------------------------------------------------------------


class DocumentRevisionCreate(BaseModel):
    """Payload to create a new document revision record.

    Emitted by the ingestion asset each time a file is parsed from Bronze.
    source_sha256 must be the lowercase hex SHA-256 of the Bronze object.
    """

    document_id: UUID = Field(..., description="FK → silver.reports.report_id")
    workspace_id: UUID = Field(..., description="FK → silver.workspaces.workspace_id")
    revision_number: int = Field(..., ge=1, description="Monotonically increasing per document_id; starts at 1")
    source_uri: str = Field(..., min_length=1, description="Bronze URI, e.g. s3://bronze/<path>")
    source_sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="Lowercase hex SHA-256 of the Bronze object",
    )
    ingested_at: datetime = Field(..., description="Timestamp when this revision landed in Bronze")
    parser_name: str = Field(..., min_length=1, max_length=128)
    parser_version: str = Field(..., min_length=1, max_length=64)
    superseded_by_revision_id: UUID | None = Field(
        default=None,
        description="Self-FK pointing to the newer revision that replaced this one; NULL if current",
    )


class DocumentRevisionRead(DocumentRevisionCreate):
    """Full document_revisions record as returned from the database."""

    document_revision_id: UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# EvidenceItem
# ---------------------------------------------------------------------------


class EvidenceItemCreate(BaseModel):
    """Payload to create a new evidence item.

    Exactly one of (passage_id, structured_ref, graph_edge_ref, map_feature_ref)
    must be non-None.  The evidence_type value must match the populated field.
    Application code is responsible for this invariant; the DB CHECK constraint
    is the safety net.
    """

    workspace_id: UUID = Field(..., description="FK → silver.workspaces.workspace_id")
    evidence_type: EvidenceTypeLiteral
    passage_id: UUID | None = Field(
        default=None,
        description="FK → silver.document_passages.passage_id; set when evidence_type='document_passage'",
    )
    structured_ref: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Opaque schema+table+PK tuple for structured-record evidence. "
            "Example: {\"schema\": \"silver\", \"table\": \"collars\", \"pk\": {\"collar_id\": \"...\"}}"
        ),
    )
    graph_edge_ref: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Start/end node IDs + relationship type for graph-edge evidence. "
            "Example: {\"start_node_id\": 123, \"end_node_id\": 456, \"rel_type\": \"HAS_SAMPLE\"}"
        ),
    )
    map_feature_ref: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Tile function + feature bbox + feature properties for map-feature evidence. "
            "Example: {\"tile_function\": \"collars_mvt\", \"bbox\": [...], \"properties\": {...}}"
        ),
    )
    source_uri: str = Field(..., min_length=1, description="Bronze or canonical URI of the source object")
    source_date: date | None = Field(default=None, description="Date of the source document or dataset")
    linked_node_ids: list[Any] | None = Field(
        default=None,
        description="Array of Neo4j node IDs referenced by this evidence item",
    )

    @model_validator(mode="after")
    def exactly_one_ref(self) -> "EvidenceItemCreate":
        """Enforce the exactly-one-ref invariant at the Pydantic layer.

        Exactly one of (passage_id, structured_ref, graph_edge_ref,
        map_feature_ref) must be non-None.  The DB CHECK constraint
        evidence_items_exactly_one_ref is the safety net; this validator
        catches malformed payloads before they reach the database.

        Also validates that evidence_type matches the populated ref field.

        Resolves SCHEMA-03 (Module 6 intake item 4).
        """
        populated = [
            f
            for f, v in [
                ("passage_id", self.passage_id),
                ("structured_ref", self.structured_ref),
                ("graph_edge_ref", self.graph_edge_ref),
                ("map_feature_ref", self.map_feature_ref),
            ]
            if v is not None
        ]
        if len(populated) != 1:
            raise ValueError(
                f"EvidenceItem requires exactly one of (passage_id, structured_ref, "
                f"graph_edge_ref, map_feature_ref) to be non-None; got: {populated}"
            )

        # Validate evidence_type matches the populated ref field.
        type_field_map = {
            "document_passage": "passage_id",
            "structured_record": "structured_ref",
            "graph_edge": "graph_edge_ref",
            "map_feature": "map_feature_ref",
        }
        expected_field = type_field_map.get(self.evidence_type)
        if expected_field and expected_field not in populated:
            raise ValueError(
                f"evidence_type='{self.evidence_type}' expects '{expected_field}' "
                f"to be populated, but got: {populated}"
            )

        return self


class EvidenceItemRead(EvidenceItemCreate):
    """Full evidence_items record as returned from the database."""

    evidence_id: UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# StructuredRecordLineage
# ---------------------------------------------------------------------------


class StructuredRecordLineageCreate(BaseModel):
    """Payload to create a structured-record lineage row.

    One row per structured-record EvidenceItem, tracing it back to the
    exact Bronze object and Dagster run that produced it.
    bronze_sha256 must be the lowercase hex SHA-256 of the Bronze object.
    native_locator is the row-level pointer (schema + table + PK value(s)).
    """

    evidence_id: UUID = Field(..., description="FK → silver.evidence_items.evidence_id")
    bronze_uri: str = Field(..., min_length=1, description="Bronze object URI")
    bronze_sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="Lowercase hex SHA-256 of the Bronze object",
    )
    parser_name: str = Field(..., min_length=1, max_length=128)
    parser_version: str = Field(..., min_length=1, max_length=64)
    ingestion_run_id: UUID = Field(..., description="Dagster run ID that produced this record")
    native_locator: dict[str, Any] = Field(
        ...,
        description=(
            "Row-level pointer into the source format. "
            "Example: {\"schema\": \"silver\", \"table\": \"collars\", \"pk\": {\"collar_id\": \"...\"}}"
        ),
    )


class StructuredRecordLineageRead(StructuredRecordLineageCreate):
    """Full structured_record_lineage record as returned from the database."""

    lineage_id: UUID
    created_at: datetime
