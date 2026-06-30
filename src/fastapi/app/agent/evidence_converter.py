"""Plan §3a — tool_results → EvidencePacket bridge.

The legacy retrieval state shape is::

    state.tool_results: list[tuple[str, Any]]
        # [("search_documents", [chunk_dict, ...]),
        #  ("query_assay_data", [row_dict, ...]),
        #  ...]

This module converts that heterogeneous list into the typed
:class:`EvidencePacket` from plan §3a. Once this converter is wired
into the agentic graph (likely between ``assemble_node`` and
``validate_node`` so validators can read typed evidence), the answer
generator + the citation-guard arm + the (future) MapLibre trigger
all operate on a single typed packet instead of duck-typed dicts.

Design choices:

  1. **Pure function.** No I/O. Takes plain inputs (tool_results, query
     metadata) and returns a fresh EvidencePacket. Failures on
     individual records are logged and skipped, not raised — the
     converter is on the success path and must not break answers.

  2. **Defensive field access.** Tool payloads vary in shape across
     the codebase's history; some entries are dicts, some are
     namespace-like objects, some are raw text. Each extractor uses
     ``.get(...)`` style access with sensible defaults so a
     missing-field tool entry produces evidence with whatever fields
     ARE present, rather than failing the whole packet.

  3. **Per-tool dispatch.** Each known tool name maps to one
     extractor function. Unknown tool names produce a
     DocumentEvidence with ``document_type='unknown'`` so the data
     isn't lost — visible in the trace + flagged for review.

  4. **Token estimation.** Each evidence object's contribution to
     ``EvidencePacket.total_tokens`` is computed via the cheap
     chars-divided-by-4 proxy (the same proxy plan §0b uses for the
     system-prompt budget). Precise tokenization stays out of the
     critical path.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.evidence import (
    AssayEvidence,
    CollarEvidence,
    DocumentEvidence,
    EvidencePacket,
    EvidenceUnion,
    GraphEvidence,
    SpatialEvidence,
    TableEvidence,
)

logger = logging.getLogger(__name__)


__all__ = [
    "build_evidence_packet",
    "extract_document_evidence",
    "extract_assay_evidence",
    "extract_collar_evidence",
    "extract_spatial_evidence",
    "extract_graph_evidence",
    "estimate_evidence_tokens",
]


# ---------------------------------------------------------------------------
# Defensive field accessors — tool payloads are heterogeneous
# ---------------------------------------------------------------------------


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """Try multiple field names on dict-like OR object-like input.

    Tool layer history: ``search_documents`` returns dict rows with
    keys like ``chunk_id``, but ``query_assay_data`` returns asyncpg
    Record objects accessed by attribute. This helper tries .get on a
    dict, then getattr on an object, for each candidate name in order.
    """
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        else:
            val = getattr(obj, name, _UNSET)
            if val is not _UNSET:
                return val
    return default


_UNSET = object()


def _as_float(v: Any, default: float | None = None) -> float | None:
    """Coerce a value to float, returning ``default`` on failure."""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v: Any, default: int | None = None) -> int | None:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return str(v)


def _as_list_of_str(v: Any) -> list[str]:
    """Coerce list-ish input to list[str]; drop non-string entries."""
    if not v:
        return []
    if not isinstance(v, (list, tuple, set)):
        return []
    return [str(x) for x in v if x is not None]


def _unwrap_rows(payload: Any, *wrapper_attrs: str) -> list[Any]:
    """Coerce a tool payload to a flat list of rows.

    Real tools return typed result objects like ``DocumentSearchResult``
    with a ``.chunks`` attribute, ``CollarDetailsResult`` with no
    iterable (the whole object IS the row), ``CoverageGapResult`` with
    ``.attribute_coverage`` etc. The legacy extractor signature was
    "give me a list of dicts" — this helper bridges the two:

    1. If ``payload`` is already list / tuple → return as a list.
    2. Else, try each of ``wrapper_attrs`` in order; the first one that
       resolves to a list-like is unwrapped.
    3. Else, if ``payload`` is a non-list object with the dict-row
       attributes the caller needs, wrap it in a one-element list so
       the per-row extractor can treat it uniformly (e.g.
       ``CollarDetailsResult`` is one collar).
    4. Else, return [] — converter gracefully produces an empty packet
       slice rather than crashing.

    Pass the empty positional list when no wrapper unwrap should be
    attempted; the helper then only does step (1) and (4).
    """
    if isinstance(payload, (list, tuple)):
        return list(payload)
    if payload is None:
        return []
    for attr in wrapper_attrs:
        val = getattr(payload, attr, None)
        if isinstance(val, (list, tuple)):
            return list(val)
    # Single-row wrapper: the object itself is the row. Caller's extractor
    # uses _field() which handles attribute access fine.
    if hasattr(payload, "__dict__") or hasattr(payload, "model_fields"):
        return [payload]
    return []


def estimate_evidence_tokens(evidence: EvidenceUnion) -> int:
    """Plan §0b cheap proxy — chars/4 over the textual surface of
    each evidence kind. Returns at least 1 so empty evidence still
    counts as a non-zero entry."""
    if isinstance(evidence, DocumentEvidence):
        chars = len(evidence.text) + len(evidence.section) + len(evidence.document_title)
    elif isinstance(evidence, TableEvidence):
        cells_chars = sum(
            len(str(v)) for row in evidence.cell_values for v in row.values()
        )
        chars = cells_chars + sum(len(c) for c in evidence.column_names)
        chars += len(evidence.caption or "") + len(evidence.section_heading or "")
    elif isinstance(evidence, AssayEvidence):
        # Numeric rows are small; rough estimate.
        chars = len(evidence.hole_id) + len(evidence.commodity) + len(evidence.unit) + 40
    elif isinstance(evidence, CollarEvidence):
        chars = len(evidence.hole_id) + len(evidence.crs) + 40
    elif isinstance(evidence, SpatialEvidence):
        chars = len(evidence.geometry_type) + len(evidence.crs) + 60
        chars += sum(len(e) for e in evidence.intersecting_entities)
    elif isinstance(evidence, GraphEvidence):
        chars = len(evidence.path) + sum(len(n) for n in evidence.node_ids)
    else:  # pragma: no cover — discriminated union shouldn't permit this
        chars = 0
    return max(1, chars // 4)


# ---------------------------------------------------------------------------
# Per-tool extractors
# ---------------------------------------------------------------------------


def extract_document_evidence(payload: Any) -> list[DocumentEvidence]:
    """``search_documents`` → list[DocumentEvidence].

    Expected per-entry fields (dict or object):
      chunk_id, text, document_id, document_title, document_type,
      page, section, char_start, char_end, score / relevance_score,
      parent_chunk_id, vocab_tags

    Accepts either a raw list of rows OR a typed wrapper like
    ``DocumentSearchResult`` (rows live on ``.chunks``). See
    :func:`_unwrap_rows`.
    """
    rows = _unwrap_rows(payload, "chunks", "results", "items")
    if not rows:
        return []
    out: list[DocumentEvidence] = []
    for entry in rows:
        try:
            text = _as_str(_field(entry, "text", "content", "chunk_text"))
            if not text:
                continue  # Citation Layer 2 invariant: text required
            char_start = _as_int(_field(entry, "char_start", "start", default=0)) or 0
            char_end = _as_int(_field(entry, "char_end", "end", default=0)) or 0
            if char_end < char_start:
                # Sanitise; trust char_start as the authoritative anchor.
                char_end = char_start + len(text)
            confidence = _as_float(
                _field(entry, "relevance_score", "score", "confidence"),
                default=1.0,
            )
            confidence = max(0.0, min(1.0, confidence or 1.0))

            out.append(
                DocumentEvidence(
                    document_id=_as_str(
                        _field(entry, "document_id", "report_id", "doc_id"),
                        default="unknown",
                    ),
                    document_title=_as_str(
                        _field(entry, "document_title", "title"),
                        default="(untitled)",
                    ),
                    document_type=_as_str(
                        _field(entry, "document_type", "doc_type"),
                        default="unknown",
                    ),
                    page=_as_int(_field(entry, "page", "page_number"), default=0) or 0,
                    section=_as_str(_field(entry, "section", "section_title")),
                    chunk_id=_as_str(
                        _field(entry, "chunk_id", "id", "passage_id"),
                        default="unknown",
                    ),
                    parent_chunk_id=(
                        _as_str(_field(entry, "parent_chunk_id"))
                        or None
                    ),
                    text=text,
                    char_start=char_start,
                    char_end=char_end,
                    extracted_entities=_as_list_of_str(
                        _field(entry, "extracted_entities", "entities"),
                    ),
                    vocab_tags=_as_list_of_str(_field(entry, "vocab_tags")),
                    confidence=confidence,
                    source_uri=_as_str(_field(entry, "source_uri", "uri")),
                )
            )
        except Exception:
            logger.warning(
                "extract_document_evidence: skipping malformed entry",
                exc_info=True,
            )
            continue
    return out


def extract_assay_evidence(payload: Any) -> list[AssayEvidence]:
    """``query_assay_data`` → list[AssayEvidence].

    Accepts a raw list OR a typed wrapper exposing ``.rows`` /
    ``.assays`` / ``.results``. See :func:`_unwrap_rows`.
    """
    rows = _unwrap_rows(payload, "rows", "assays", "results", "items")
    if not rows:
        return []
    out: list[AssayEvidence] = []
    for entry in rows:
        try:
            depth_from = _as_float(_field(entry, "depth_from_m", "from_m", "depth_from"))
            depth_to = _as_float(_field(entry, "depth_to_m", "to_m", "depth_to"))
            value = _as_float(_field(entry, "value", "assay_value", "grade"))
            commodity = _as_str(_field(entry, "commodity", "element"))
            if (
                depth_from is None or depth_to is None
                or value is None or not commodity
            ):
                # Insufficient to make an AssayEvidence — log + skip.
                logger.debug(
                    "extract_assay_evidence: skipping row missing required fields",
                )
                continue
            if depth_to < depth_from:
                depth_to, depth_from = depth_from, depth_to
            interval = _as_float(
                _field(entry, "interval_length_m", "length_m", "width_m"),
                default=max(0.0, depth_to - depth_from),
            )

            out.append(
                AssayEvidence(
                    project_id=_as_str(
                        _field(entry, "project_id"),
                        default="unknown",
                    ),
                    property_id=(
                        _as_str(_field(entry, "property_id"))
                        or None
                    ),
                    hole_id=_as_str(
                        _field(entry, "hole_id", "drillhole_id"),
                        default="unknown",
                    ),
                    sample_id=(
                        _as_str(_field(entry, "sample_id"))
                        or None
                    ),
                    depth_from_m=depth_from,
                    depth_to_m=depth_to,
                    interval_length_m=interval or 0.0,
                    commodity=commodity,
                    commodity_uri=(
                        _as_str(_field(entry, "commodity_uri"))
                        or None
                    ),
                    value=value,
                    unit=_as_str(
                        _field(entry, "unit", "unit_of_measure"),
                        default="g/t",
                    ),
                    lab=(_as_str(_field(entry, "lab", "lab_name")) or None),
                    method=(_as_str(_field(entry, "method", "assay_method")) or None),
                    is_composite=bool(
                        _field(entry, "is_composite", default=False)
                    ),
                    qaqc_flags=_as_list_of_str(_field(entry, "qaqc_flags")),
                    database_row_id=_as_int(_field(entry, "row_id", "id")),
                    source_document_id=(
                        _as_str(_field(entry, "source_document_id"))
                        or None
                    ),
                )
            )
        except Exception:
            logger.warning(
                "extract_assay_evidence: skipping malformed row",
                exc_info=True,
            )
            continue
    return out


def extract_collar_evidence(payload: Any) -> list[CollarEvidence]:
    """``query_spatial_collars`` (non-spatial-op rows) → list[CollarEvidence].

    Accepts a raw list OR a typed wrapper exposing ``.collars`` /
    ``.rows`` / ``.results``, OR a single collar object (e.g.
    ``CollarDetailsResult``). See :func:`_unwrap_rows`.
    """
    rows = _unwrap_rows(payload, "collars", "rows", "results", "items")
    if not rows:
        return []
    out: list[CollarEvidence] = []
    for entry in rows:
        try:
            easting = _as_float(_field(entry, "easting", "x"))
            northing = _as_float(_field(entry, "northing", "y"))
            crs = _as_str(_field(entry, "crs", "crs_epsg", "spatial_crs"))
            hole_id = _as_str(_field(entry, "hole_id", "drillhole_id"))
            if (
                easting is None or northing is None
                or not crs or not hole_id
            ):
                logger.debug(
                    "extract_collar_evidence: skipping row missing required fields",
                )
                continue
            out.append(
                CollarEvidence(
                    hole_id=hole_id,
                    easting=easting,
                    northing=northing,
                    elevation=_as_float(_field(entry, "elevation", "z")),
                    crs=crs,
                    azimuth=_as_float(_field(entry, "azimuth")),
                    dip=_as_float(_field(entry, "dip")),
                    total_depth=_as_float(
                        _field(entry, "total_depth", "max_depth"),
                    ),
                    drill_program=(
                        _as_str(_field(entry, "drill_program", "program"))
                        or None
                    ),
                    source=_as_str(_field(entry, "source"), default="silver.collars"),
                )
            )
        except Exception:
            logger.warning(
                "extract_collar_evidence: skipping malformed row",
                exc_info=True,
            )
            continue
    return out


def extract_spatial_evidence(payload: Any) -> list[SpatialEvidence]:
    """``query_spatial_collars`` results carrying ``spatial_operation`` →
    list[SpatialEvidence]. Distinct from CollarEvidence: a spatial-op
    row has done geometry work (ST_DWithin / etc.) and the result is
    the relationship, not the collar itself.

    Accepts a raw list OR a typed wrapper exposing ``.rows`` /
    ``.results`` / ``.features``. See :func:`_unwrap_rows`.
    """
    rows = _unwrap_rows(payload, "rows", "results", "features", "items")
    if not rows:
        return []
    out: list[SpatialEvidence] = []
    for entry in rows:
        try:
            op = _as_str(_field(entry, "spatial_operation", "st_op"))
            if not op:
                continue  # Not a spatial-op row; CollarEvidence path
                          # handles it instead.
            crs = _as_str(_field(entry, "crs", "crs_epsg"), default="EPSG:4326")
            geom_type = _as_str(
                _field(entry, "geometry_type", "geom_type"),
                default="point",
            ).lower()
            if geom_type not in {
                "point", "polygon", "polyline",
                "multipoint", "multipolygon", "multipolyline",
            }:
                geom_type = "point"
            if op not in {"within", "intersects", "contains", "distance", "buffer"}:
                op = "within"

            out.append(
                SpatialEvidence(
                    geometry_type=geom_type,
                    crs=crs,
                    spatial_operation=op,
                    result_value=_as_float(
                        _field(entry, "result_value", "distance_m"),
                    ),
                    intersecting_entities=_as_list_of_str(
                        _field(entry, "intersecting_entities", "hole_ids"),
                    ),
                    source_layer=_as_str(_field(entry, "source_layer")),
                    source_document_id=(
                        _as_str(_field(entry, "source_document_id"))
                        or None
                    ),
                )
            )
        except Exception:
            logger.warning(
                "extract_spatial_evidence: skipping malformed row",
                exc_info=True,
            )
            continue
    return out


def extract_graph_evidence(payload: Any) -> list[GraphEvidence]:
    """``traverse_knowledge_graph`` → list[GraphEvidence].

    Accepts a raw list OR a typed wrapper exposing ``.paths`` /
    ``.rows`` / ``.results``. See :func:`_unwrap_rows`.
    """
    rows = _unwrap_rows(payload, "paths", "rows", "results", "items")
    if not rows:
        return []
    out: list[GraphEvidence] = []
    for entry in rows:
        try:
            out.append(
                GraphEvidence(
                    node_ids=_as_list_of_str(
                        _field(entry, "node_ids", "nodes"),
                    ),
                    relationship_ids=_as_list_of_str(
                        _field(entry, "relationship_ids", "relationships"),
                    ),
                    path=_as_str(_field(entry, "path", "path_str")),
                    relationship_types=_as_list_of_str(
                        _field(entry, "relationship_types", "rel_types"),
                    ),
                    entities=(
                        _field(entry, "entities", default=[])
                        if isinstance(_field(entry, "entities"), list)
                        else []
                    ),
                    vocab_concept_uris=_as_list_of_str(
                        _field(entry, "vocab_concept_uris"),
                    ),
                    source=_as_str(_field(entry, "source"), default="neo4j"),
                )
            )
        except Exception:
            logger.warning(
                "extract_graph_evidence: skipping malformed row",
                exc_info=True,
            )
            continue
    return out


# ---------------------------------------------------------------------------
# Dispatcher + packet assembly
# ---------------------------------------------------------------------------


_TOOL_DISPATCH = {
    "search_documents": ("document", extract_document_evidence),
    "query_assay_data": ("assay", extract_assay_evidence),
    "query_downhole_logs": ("document", extract_document_evidence),
    "query_spatial_collars": ("collar", extract_collar_evidence),
    "traverse_knowledge_graph": ("graph", extract_graph_evidence),
    "query_project_overview": ("document", extract_document_evidence),
}


def build_evidence_packet(
    *,
    query_id: str,
    query_text: str,
    tool_results: list[tuple[str, Any]],
    system_prompt_tokens: int = 0,
    max_context_tokens: int = 6500,
) -> EvidencePacket:
    """Convert a ``tool_results`` list into a typed :class:`EvidencePacket`.

    Args:
        query_id: Stable UUID for this query (use the same one carried
            into ``silver.query_traces.query_id``).
        query_text: The user's raw query.
        tool_results: ``state.tool_results`` shape — list of
            ``(tool_name, payload)`` tuples.
        system_prompt_tokens: From the §0b runtime counter wired in
            ``assemble_node``. Used to compute ``remaining_budget``.
        max_context_tokens: From CLAUDE.md / plan ceiling. Default
            6500.

    Returns:
        An EvidencePacket. ``total_tokens`` + ``remaining_budget`` are
        computed; the caller can use ``remaining_budget`` to decide
        whether to truncate evidence before the LLM call.
    """
    evidence: list[EvidenceUnion] = []
    tool_names: list[str] = []

    for tool_name, payload in tool_results or []:
        tool_names.append(tool_name)
        dispatch = _TOOL_DISPATCH.get(tool_name)
        if dispatch is None:
            logger.info(
                "build_evidence_packet: unknown tool '%s' — wrapping payload "
                "in document_type='unknown' DocumentEvidence",
                tool_name,
            )
            # Wrap as a synthetic DocumentEvidence so the data isn't
            # lost; document_type='unknown' surfaces it in the trace.
            try:
                if isinstance(payload, str) and payload:
                    evidence.append(
                        DocumentEvidence(
                            document_id="unknown",
                            document_title=tool_name,
                            document_type="unknown",
                            page=0,
                            chunk_id=f"unknown-{tool_name}",
                            text=payload[:8000],
                            char_start=0,
                            char_end=min(len(payload), 8000),
                        )
                    )
            except Exception:
                pass
            continue

        kind, extractor = dispatch
        # query_spatial_collars produces BOTH CollarEvidence and
        # SpatialEvidence depending on row shape — call both extractors
        # and merge.
        if tool_name == "query_spatial_collars":
            evidence.extend(extract_collar_evidence(payload))
            evidence.extend(extract_spatial_evidence(payload))
        else:
            evidence.extend(extractor(payload))

    total_tokens = sum(estimate_evidence_tokens(e) for e in evidence)
    remaining = max_context_tokens - system_prompt_tokens - total_tokens

    return EvidencePacket(
        query_id=query_id,
        query_text=query_text,
        tool_plan=", ".join(tool_names),
        evidence=evidence,
        total_tokens=total_tokens,
        system_prompt_tokens=system_prompt_tokens,
        remaining_budget=remaining,
    )
