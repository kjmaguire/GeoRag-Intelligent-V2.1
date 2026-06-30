"""Lineage payload models — Phase 1 / Step 1.5.

Pydantic shapes for the five new columns on ``silver.answer_runs`` that
capture session-level provenance. The plan calls this the "session-level
lineage artifact". Persisted atomically with the answer-run row by the
orchestrator (fail-closed when ``GEO_ANSWER_OIUR_ENABLED=True``); exposed
to the audit layer via ``GET /v1/answer_runs/{id}/lineage``.

The models are deliberately compact — every chunk considered (not just
cited) is stored, so the per-row JSONB payload stays well below Postgres's
TOAST threshold for typical retrieval sets. Large fields like the LLM
context text are NOT included here; only their references.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

SourceTypeLiteral = Literal[
    "qdrant",
    "neo4j",
    "postgis",
    "public_geoscience",
    "hybrid",
    "other",
]


class RetrievedSource(BaseModel):
    """One chunk that entered the LLM context (cited or not)."""

    source_type: SourceTypeLiteral = Field(
        ...,
        description="Origin store / surface of the chunk.",
    )
    chunk_id: str | None = Field(
        default=None,
        description="Stable chunk identifier — Qdrant point UUID, Neo4j node id, or store-specific reference.",
    )
    pdf_id: UUID | None = Field(
        default=None,
        description="Parent document UUID (``bronze.source_files`` / ``silver.pdf`` row). NULL for non-PDF chunks.",
    )
    score: float | None = Field(
        default=None,
        description="Retrieval / rerank score. NULL when the upstream store does not surface one.",
    )
    cited: bool = Field(
        default=False,
        description="True when this chunk's marker appears in the final answer's citations list.",
    )


class FiltersApplied(BaseModel):
    """Scope filter snapshot at query time. Empty fields = no narrowing."""

    project_id: UUID | None = None
    workspace_id: UUID | None = None
    jurisdiction_codes: list[str] = Field(default_factory=list)
    date_range_from: str | None = Field(
        default=None, description="ISO-8601 date or NULL when unbounded."
    )
    date_range_to: str | None = Field(default=None)
    data_types: list[str] = Field(
        default_factory=list,
        description="Selected data surfaces (drill_logs, assays, technical_reports, maps, …).",
    )


class QaQcFiltersApplied(BaseModel):
    """QA/QC exclusion snapshot at query time."""

    silver_review_excluded_batches: list[str] = Field(default_factory=list)
    failed_crm_batches: list[str] = Field(default_factory=list)
    custom_exclusions: list[str] = Field(default_factory=list)


class LineagePayload(BaseModel):
    """Full lineage record persisted alongside an answer.

    The orchestrator builds this from in-scope data and writes it to
    ``silver.answer_runs`` in the same UPDATE that finalises the row. The
    ``session_id`` is split out as its own column for indexing; everything
    else lives in the JSONB columns.
    """

    session_id: UUID | None = None
    retrieved_sources: list[RetrievedSource] = Field(default_factory=list)
    filters_applied: FiltersApplied = Field(default_factory=FiltersApplied)
    qaqc_filters_applied: QaQcFiltersApplied = Field(default_factory=QaQcFiltersApplied)
    answer_schema_version: str | None = Field(
        default=None,
        description="OIUR schema version used for this answer. NULL when the OIUR flag was off.",
    )

    def to_db_columns(self) -> dict[str, Any]:
        """Render the payload as the 5 column values the UPDATE expects.

        Returns a dict keyed by column name. JSONB columns are serialised
        as plain Python dicts/lists — the orchestrator wraps them with
        ``json.dumps`` before binding into asyncpg.
        """
        return {
            "session_id": str(self.session_id) if self.session_id else None,
            "lineage_retrieved_sources": [s.model_dump(mode="json") for s in self.retrieved_sources],
            "lineage_filters_applied": self.filters_applied.model_dump(mode="json"),
            "lineage_qaqc_filters_applied": self.qaqc_filters_applied.model_dump(mode="json"),
            "answer_schema_version": self.answer_schema_version,
        }


__all__ = [
    "FiltersApplied",
    "LineagePayload",
    "QaQcFiltersApplied",
    "RetrievedSource",
    "SourceTypeLiteral",
]
