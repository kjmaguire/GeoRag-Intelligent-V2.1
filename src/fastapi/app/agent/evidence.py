"""Plan §3a — typed evidence objects.

Every retrieval output is normalised into one of six typed evidence
classes before context assembly. The answer generator receives an
:class:`EvidencePacket`, never a heterogeneous list of raw chunks.

This file is the **foundation layer only** — the data shapes + their
discriminator + invariants. The conversion layer that maps tool_results
(the current ``list[tuple[str, Any]]`` shape) into typed evidence, the
diversity-aware reranker, the parent-chunk expander, and the per-intent
budget allocator are all separate downstream wirings (plan §§3b–3f).

Pydantic v2 conventions:

  - Every class inherits from ``BaseModel`` (no ``BaseSettings``).
  - Each evidence class carries a ``kind: Literal[<name>]`` discriminator
    field so :data:`EvidenceUnion` can route on it.
  - ``evidence_id`` defaults to a fresh UUID4 string per construction.
  - Numeric ranges are validated where the plan spec is explicit
    (depth ≥ 0, confidence ∈ [0, 1], authority_rank ∈ [1, 5]).
  - ``model_config = ConfigDict(extra="forbid")`` for early-failure on
    typos at the construction site.

References:
  - Plan §3a verbatim spec (full field lists)
  - `docs/architecture/six_subgraphs_spec.md` — context the evidence
    types interact with (per-intent retrieval profile + answer emphasis)
  - `docs/architecture/user_facing_error_catalog.md` — TableEvidence /
    AssayEvidence drive the "Key numbers" section of the answer format
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "DocumentEvidence",
    "TableEvidence",
    "AssayEvidence",
    "CollarEvidence",
    "SpatialEvidence",
    "GraphEvidence",
    "EvidencePacket",
    "EvidenceUnion",
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _new_evidence_id() -> str:
    """Default factory: fresh UUID4 per evidence object."""
    return str(uuid.uuid4())


class _EvidenceBase(BaseModel):
    """Common fields shared by every evidence subtype.

    The discriminator (``kind``) is declared on each subclass with the
    appropriate ``Literal``; it's typed as ``str`` here just for shared
    introspection — Pydantic narrows it on the concrete class.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    evidence_id: str = Field(
        default_factory=_new_evidence_id,
        description="Stable per-evidence UUID. Used as the foreign key on "
        "downstream answer_citation_items rows.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Source-side confidence in this evidence object. "
        "Distinct from the answer's composite confidence; this is the "
        "retrieval / extraction-time score (e.g. reranker score for "
        "DocumentEvidence, parse confidence for TableEvidence).",
    )


# ---------------------------------------------------------------------------
# DocumentEvidence — a text chunk from an ingested document
# ---------------------------------------------------------------------------


class DocumentEvidence(_EvidenceBase):
    """One text chunk retrieved from `silver.document_passages` (or
    equivalent). Carries enough context to support both the citation
    layer (chunk_id / document_id / page) and the §3b authority-aware
    multi-document synthesis (taxonomy_term_id / authority_rank /
    is_current).
    """

    kind: Literal["document"] = "document"

    document_id: str
    document_title: str
    document_type: str = Field(
        ...,
        description="Plan §1a taxonomy classification — e.g. 'NI 43-101', "
        "'Assessment Report', 'Fact Sheet', 'Press Release'.",
    )
    taxonomy_term_id: int | None = Field(
        default=None,
        description="FK to silver.taxonomy_terms.id once plan §1a's "
        "taxonomy is populated. None during the transitional period.",
    )
    is_current: bool = Field(
        default=True,
        description="False when plan §1h supersession has marked this "
        "document as superseded by a newer version. Retrieval default-"
        "filters to is_current=True; the multi-document synthesis path "
        "opts in to superseded via the envelope.",
    )
    authority_rank: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Plan §3b authority hierarchy: 1=NI 43-101 / FS / "
        "Resource Statement, 2=Govt Assessment, 3=Press Release / "
        "Investor Deck, 4=Historical, 5=Internal Notes / Uncited.",
    )
    page: int = Field(..., ge=0)
    section: str = ""
    chunk_id: str
    parent_chunk_id: str | None = None
    text: str = Field(..., min_length=1)
    char_start: int = Field(..., ge=0)
    char_end: int = Field(..., ge=0)
    extracted_entities: list[str] = Field(default_factory=list)
    vocab_tags: list[str] = Field(
        default_factory=list,
        description="Plan §1d CGI concept URIs that the chunk's text "
        "matched at silver-tagging time. Empty until the §1d-iii Dagster "
        "tagger lands.",
    )
    source_uri: str = ""

    @field_validator("char_end")
    @classmethod
    def _char_range_valid(cls, v: int, info: Any) -> int:
        start = info.data.get("char_start", 0)
        if v < start:
            raise ValueError(
                f"char_end ({v}) must be >= char_start ({start})"
            )
        return v


# ---------------------------------------------------------------------------
# TableEvidence — structured table extracted from a PDF/XLSX
# ---------------------------------------------------------------------------


class TableEvidence(_EvidenceBase):
    """A structured table from a document. Plan §1e — tables are NEVER
    flattened to text chunks; they live as their own typed evidence so
    the LLM gets cells + units + headers as a coherent object.
    """

    kind: Literal["table"] = "table"

    document_id: str
    page: int = Field(..., ge=0)
    table_id: str
    column_names: list[str]
    cell_values: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Row-major. Each entry is a dict keyed by column name "
        "with cell value (str | int | float | None). Adjacent rows around "
        "a matching row are included by the parent/section expansion "
        "(plan §3d).",
    )
    units: dict[str, str] = Field(
        default_factory=dict,
        description="column_name → unit string (e.g. 'g/t', 'm', '%'). "
        "Used by the §4b NUMERIC_GROUNDING_FAILED guard to verify the "
        "LLM didn't transpose unit families.",
    )
    caption: str | None = None
    section_heading: str | None = None
    source_uri: str = ""


# ---------------------------------------------------------------------------
# AssayEvidence — one drill-hole assay row from silver.assays_v2
# ---------------------------------------------------------------------------


class AssayEvidence(_EvidenceBase):
    """A single assay interval row. The numeric-grounding bedrock for
    plan §4b: every reported assay value in an LLM answer must be
    traceable to an AssayEvidence in the EvidencePacket (cite-by-value
    cross-check happens at validate time).
    """

    kind: Literal["assay"] = "assay"

    project_id: str
    property_id: str | None = None
    hole_id: str
    sample_id: str | None = None
    depth_from_m: float = Field(..., ge=0.0)
    depth_to_m: float = Field(..., ge=0.0)
    interval_length_m: float = Field(..., ge=0.0)
    commodity: str
    commodity_uri: str | None = Field(
        default=None,
        description="CGI concept URI when entity resolution has matched "
        "the commodity string to a vocabulary term (plan §1d / §2c).",
    )
    value: float
    unit: str
    lab: str | None = None
    method: str | None = None
    is_composite: bool = False
    qaqc_flags: list[str] = Field(
        default_factory=list,
        description="QA/QC flags from silver.data_quality_flags relating "
        "to this assay row. Plan §1g rules: assay_outlier_3sigma, "
        "assay_negative_value, assay_unit_mismatch_within_hole, "
        "assay_interval_inverted.",
    )
    database_row_id: int | None = None
    source_document_id: str | None = None

    @field_validator("depth_to_m")
    @classmethod
    def _depth_range_valid(cls, v: float, info: Any) -> float:
        depth_from = info.data.get("depth_from_m")
        if depth_from is not None and v < depth_from:
            raise ValueError(
                f"depth_to_m ({v}) must be >= depth_from_m ({depth_from})"
            )
        return v


# ---------------------------------------------------------------------------
# CollarEvidence — a drillhole collar with coordinates + survey
# ---------------------------------------------------------------------------


class CollarEvidence(_EvidenceBase):
    """A drillhole collar record. Used to expand AssayEvidence with hole-
    level context (azimuth/dip/total_depth) at §3d expansion time, and
    as the geometry source for spatial answers (plan §2g).
    """

    kind: Literal["collar"] = "collar"

    hole_id: str
    easting: float
    northing: float
    elevation: float | None = None
    crs: str = Field(
        ...,
        min_length=1,
        description="EPSG code as a string (e.g. 'EPSG:26913'). Required — "
        "plan §1g `collar_missing_crs` rule blocks ingest readiness when "
        "absent. Plan §2g spatial path refuses cross-CRS comparison.",
    )
    azimuth: float | None = Field(default=None, ge=0.0, le=360.0)
    dip: float | None = Field(default=None, ge=-90.0, le=90.0)
    total_depth: float | None = Field(default=None, ge=0.0)
    drill_program: str | None = None
    source: str = ""


# ---------------------------------------------------------------------------
# SpatialEvidence — result of a PostGIS spatial query
# ---------------------------------------------------------------------------


class SpatialEvidence(_EvidenceBase):
    """A spatial query result — output of ST_DWithin / ST_Contains /
    ST_Intersects / ST_Distance via the (future) §2g spatial node.

    Triggers the MapLibre map render in the UI when included in an
    EvidencePacket (plan §6b — handled by the chat message renderer
    reading `evidence` for any `kind == 'spatial'`).
    """

    kind: Literal["spatial"] = "spatial"

    geometry_type: Literal[
        "point", "polygon", "polyline", "multipoint", "multipolygon", "multipolyline"
    ]
    crs: str = Field(..., min_length=1)
    spatial_operation: Literal[
        "within", "intersects", "contains", "distance", "buffer"
    ]
    result_value: float | None = Field(
        default=None,
        description="For 'distance' operations, the distance in metres "
        "(canonical unit per plan §2g). For 'within'/'intersects'/etc. "
        "operations, None — the geometry list itself is the answer.",
    )
    intersecting_entities: list[str] = Field(default_factory=list)
    source_layer: str = ""
    source_document_id: str | None = None


# ---------------------------------------------------------------------------
# GraphEvidence — a Neo4j path / relationship result
# ---------------------------------------------------------------------------


class GraphEvidence(_EvidenceBase):
    """A path or relationship subgraph from the Neo4j knowledge graph.
    Returned by the `traverse_knowledge_graph` tool, scoped to the
    workspace via the canonical Cypher pattern.
    """

    kind: Literal["graph"] = "graph"

    node_ids: list[str] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    path: str = Field(
        default="",
        description="Human-readable path string for citation rendering — "
        "e.g. '(:Project)-[:HAS_DEPOSIT]->(:Deposit)-[:HOSTS]->(:Mineral)'.",
    )
    relationship_types: list[str] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    vocab_concept_uris: list[str] = Field(
        default_factory=list,
        description="CGI concept URIs for the typed entities along the "
        "path. Empty until §1d Neo4j-side vocab nodes are populated.",
    )
    source: str = ""


# ---------------------------------------------------------------------------
# Discriminated union + EvidencePacket
# ---------------------------------------------------------------------------


EvidenceUnion = Annotated[
    DocumentEvidence | TableEvidence | AssayEvidence | CollarEvidence | SpatialEvidence | GraphEvidence,
    Field(discriminator="kind"),
]
"""Discriminated union over all six evidence subtypes.

Pydantic v2 routes the deserialiser on the ``kind`` field; constructing
``EvidencePacket(**dict)`` from a JSON dump correctly re-types each
member of ``evidence``.
"""


class EvidencePacket(BaseModel):
    """The bundle the answer generator receives — never a raw chunk list.

    Carries per-packet token accounting (system_prompt_tokens +
    total_tokens + remaining_budget) so the LLM caller can verify it
    fits inside MAX_CONTEXT_TOKENS BEFORE issuing the request. Plan §3a:
    'Answer generator receives EvidencePacket, never raw chunks.'
    """

    model_config = ConfigDict(extra="forbid")

    query_id: str
    query_text: str = Field(..., min_length=1)
    tool_plan: str = Field(
        default="",
        description="Comma-joined tool names that contributed to this "
        "packet — same shape as `silver.query_traces.tool_plan`.",
    )
    evidence: list[EvidenceUnion] = Field(
        default_factory=list,
        description="Typed evidence members. Order is post-rerank + post-"
        "expansion — what the LLM literally sees, top-to-bottom.",
    )
    total_tokens: int = Field(
        default=0,
        ge=0,
        description="Sum of token counts across all evidence members + "
        "the query block. Computed by the assembler after expansion.",
    )
    system_prompt_tokens: int = Field(
        default=0,
        ge=0,
        description="Plan §0b token-budget audit value for the static "
        "system prompt at construction time. Mirror of "
        "silver.query_traces.system_prompt_tokens.",
    )
    remaining_budget: int = Field(
        default=0,
        description="MAX_CONTEXT_TOKENS - system_prompt_tokens - "
        "total_tokens. May go negative when over budget — the assembler "
        "logs + drops lowest-authority members until remaining_budget "
        "≥ 0 before the LLM call.",
    )

    def by_kind(self, kind: str) -> list[EvidenceUnion]:
        """Return every evidence member of the given kind. Convenience
        for the assembler's per-kind budget pass (plan §3f)."""
        return [e for e in self.evidence if e.kind == kind]

    def evidence_ids(self) -> list[str]:
        """All evidence_ids in order — used by the citation persister to
        attach answer_citation_items.evidence_id FK rows."""
        return [e.evidence_id for e in self.evidence]
