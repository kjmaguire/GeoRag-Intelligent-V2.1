"""Stage 1 — Pre-generation evidence binding (Module 6 Phase B Chunk 2).

Architecture reference: georag-architecture-addendum-v1.10.html §04j;
module spec 06-citation-hallucination-guards.md §6 B1.

Purpose
-------
Before the LLM is called, ``bind_evidence`` converts raw tool results (and
optionally evidence_items rows from B8.5+) into a ``BoundEvidenceSet``.  Each
binding carries:

  * The exact marker string the prompt will show the model   e.g. ``[DATA:1]``
  * Enough metadata (store, FK targets) for the span resolver to write
    ``silver.answer_citation_items`` rows after synthesis.

The bound set is the single source of truth that flows from prompt-assembly
through to span resolution — there is no second pass that re-derives marker
assignments.

Marker format
-------------
All markers are *colon-form* per Option A (Kyle's decision 2026-04-22).
  ``[DATA:N]``    — structured / spatial tool result, N = tool-slot index
  ``[NI43:N]``   — NI 43-101 document chunk
  ``[PUB:N]``    — publication chunk
  ``[PGEO:N]``   — Public Geoscience record (one per record, same counter)
  ``[ev:<id>]``  — evidence_items-based binding (B8.5+); id = first 8 chars of
                   evidence_id UUID (hex, no hyphens)

Feature flag
------------
This module is imported unconditionally but ``bind_evidence`` is *called* by
the orchestrator only when ``settings.CITATION_SPAN_RESOLVER_ENABLED=true``.
When the flag is false the orchestrator uses the legacy ``assign_citation_ids``
path in ``response_assembler.py`` — that path is entirely unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

MarkerKind = Literal["DATA", "NI43", "PUB", "PGEO", "ev"]

_TOOL_NAME_TO_KIND: dict[str, MarkerKind] = {
    "query_spatial_collars": "DATA",
    "query_assay_data": "DATA",
    "query_downhole_logs": "DATA",
    "query_project_overview": "DATA",   # Phase F.9
    "query_graph_by_label": "DATA",
    "traverse_knowledge_graph": "DATA",
    "search_documents": "NI43",      # refined per-chunk below
    "search_public_geoscience": "PGEO",
}

_TOOL_NAME_TO_STORE: dict[str, str] = {
    "query_spatial_collars": "postgis",
    "query_assay_data": "postgis",
    "query_downhole_logs": "postgis",
    "query_project_overview": "postgis",  # Phase F.9
    "query_graph_by_label": "neo4j",
    "traverse_knowledge_graph": "neo4j",
    "search_documents": "qdrant",
    "search_public_geoscience": "hybrid",
}


@dataclass(frozen=True)
class BoundEvidence:
    """One row in the bound evidence set.

    ``marker_text`` is the colon-form string the model should use to cite
    this item, e.g. ``[DATA:1]`` for tool-slot or ``[ev:019d74a7]`` for an
    evidence_items-based binding.

    For tool-slot bindings (kind in DATA/NI43/PUB/PGEO) both ``evidence_id``
    and ``passage_id`` are None until the span resolver resolves them post-gen.
    For ``[ev:*]`` bindings (kind='ev') ``evidence_id`` is populated at
    bind time.
    """

    marker_text: str            # e.g. '[DATA:1]' or '[ev:019d74a7]'
    kind: MarkerKind
    index_or_id: str            # 'N' for tool-slot, short evidence_id for [ev:*]
    source_store: str           # 'qdrant' | 'neo4j' | 'postgis' | 'hybrid'

    # Target FKs — at most one populated per binding.  Both None means this is
    # a tool-slot whose target resolves at span-resolution time (normal case).
    evidence_id: UUID | None = None
    passage_id: UUID | None = None

    # Opaque payload used by the prompt block + evidence inspector.
    display_ref: dict | None = None

    # Preview text for the prompt's Evidence Set block (first 200 chars).
    preview_text: str = ""


@dataclass
class BoundEvidenceSet:
    """Full bound set for a single answer run.

    ``bindings``  — ordered list, one entry per unique marker.
    ``by_marker`` — fast lookup: ``marker_text`` → index into ``bindings``.
    """

    bindings: list[BoundEvidence] = field(default_factory=list)
    by_marker: dict[str, int] = field(default_factory=dict)

    def get(self, marker_text: str) -> BoundEvidence | None:
        """Return the binding for *marker_text*, or None if not found."""
        idx = self.by_marker.get(marker_text)
        if idx is None:
            return None
        return self.bindings[idx]

    def add(self, binding: BoundEvidence) -> None:
        """Append a binding and update the fast-lookup dict."""
        idx = len(self.bindings)
        self.bindings.append(binding)
        self.by_marker[binding.marker_text] = idx


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _kind_for_document_chunk(chunk: Any) -> MarkerKind:
    """Determine NI43 vs PUB from chunk.document_type."""
    doc_type = getattr(chunk, "document_type", "") or ""
    doc_type_upper = doc_type.upper()
    if "NI43" in doc_type_upper or "NI 43" in doc_type_upper:
        return "NI43"
    if "PUB" in doc_type_upper:
        return "PUB"
    # Default to NI43 (most common in our corpus)
    return "NI43"


def _short_ev_id(evidence_id: UUID) -> str:
    """Return the first 8 hex chars of a UUID (no hyphens).

    Collision risk within a single answer run is effectively zero
    (8 hex chars = 4 billion unique values; runs have ≤ 50 evidence items).
    See design doc open-for-review item 2.
    """
    return evidence_id.hex[:8]


def _preview(result: Any, tool_name: str) -> str:
    """Extract a short preview text from a tool result."""
    try:
        # DocumentSearchResult — first chunk text
        chunks = getattr(result, "chunks", None)
        if chunks:
            return str(getattr(chunks[0], "text", ""))[:200]
        # SpatialQueryResult / AssayDataResult — repr of first collar
        collars = getattr(result, "collars", None)
        if collars:
            return str(collars[0])[:200]
        # GraphTraversalResult
        entities = getattr(result, "entities", None)
        if entities:
            return str(entities[0])[:200]
        # PublicGeoscienceRecord (single record passed in)
        text = getattr(result, "text", None) or getattr(result, "abstract", None)
        if text:
            return str(text)[:200]
        return str(result)[:200]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def bind_evidence(
    *,
    workspace_id: UUID,
    tool_results: list[tuple[str, Any]],
    evidence_items: list[Any] | None = None,
) -> BoundEvidenceSet:
    """Build a bound evidence set for prompt assembly + later span resolution.

    For each tool result, emit one ``[<KIND>:<N>]`` binding (colon form).
    ``PublicGeoscienceSearchResult`` is special: it may contain multiple
    records, each assigned its own ``[PGEO:N]`` binding (matching the legacy
    ``assign_citation_ids`` behaviour so the prompt slot numbering is
    identical).

    For any ``evidence_items`` rows (B8.5 future), emit ``[ev:<short>]``
    bindings keyed by evidence_id.

    Args:
        workspace_id:    Current workspace UUID (for future per-workspace
                         telemetry; currently stored as metadata only).
        tool_results:    List of (tool_name, result_object) from orchestrator
                         fan-out.  Same shape as the existing
                         ``assign_citation_ids`` input.
        evidence_items:  Optional list of evidence_items ORM/dataclass objects
                         carrying ``.evidence_id``, ``.evidence_type``, and
                         optional ``.passage_id``.  Pass None (default) until
                         B8.5 behavioral enable.

    Returns:
        BoundEvidenceSet with one BoundEvidence per unique marker.
    """
    bound_set = BoundEvidenceSet()
    counter = 0

    for tool_name, result in tool_results:
        store = _TOOL_NAME_TO_STORE.get(tool_name, "hybrid")

        # PublicGeoscienceSearchResult — one binding per record
        records = getattr(result, "records", None)
        if records is not None and tool_name in ("search_public_geoscience",):
            for rec in records:
                counter += 1
                marker = f"[PGEO:{counter}]"
                binding = BoundEvidence(
                    marker_text=marker,
                    kind="PGEO",
                    index_or_id=str(counter),
                    source_store=store,
                    display_ref={"tool": tool_name, "slot": counter},
                    preview_text=_preview(rec, tool_name),
                )
                bound_set.add(binding)
            logger.debug(
                "bind_evidence: PGEO %d record(s) from %s → markers [PGEO:%d]..[PGEO:%d]",
                len(records),
                tool_name,
                counter - len(records) + 1,
                counter,
            )
            continue

        # DocumentSearchResult — KIND depends on first chunk's document_type.
        # Carry chunk_id from the first chunk in display_ref so Stage 2 can
        # look up passage_id via document_passages.embedding_id = chunk_id.
        # This is the passage_id lookup path for Chunk 3 (C1 of reviewer conditions).
        chunks = getattr(result, "chunks", None)
        if chunks is not None:
            # Determine kind from first chunk
            kind: MarkerKind = _kind_for_document_chunk(chunks[0]) if chunks else "NI43"
            counter += 1
            marker = f"[{kind}:{counter}]"
            # Carry the Qdrant point UUID (chunk_id) from the first chunk so
            # the Stage 2 span resolver can look up passage_id via
            # silver.document_passages WHERE embedding_id = chunk_id.
            first_chunk_id: str | None = None
            if chunks:
                first_chunk_id = getattr(chunks[0], "chunk_id", None)
            binding = BoundEvidence(
                marker_text=marker,
                kind=kind,
                index_or_id=str(counter),
                source_store=store,
                display_ref={
                    "tool": tool_name,
                    "slot": counter,
                    "chunk_id": first_chunk_id,  # Qdrant point UUID for passage lookup
                },
                preview_text=_preview(result, tool_name),
            )
            bound_set.add(binding)
            logger.debug(
                "bind_evidence: %s → %s (slot %d, chunk_id=%s)",
                tool_name, marker, counter, first_chunk_id,
            )
            continue

        # All other tool types (spatial, graph, assay, downhole) → DATA
        counter += 1
        marker = f"[DATA:{counter}]"
        binding = BoundEvidence(
            marker_text=marker,
            kind="DATA",
            index_or_id=str(counter),
            source_store=store,
            display_ref={"tool": tool_name, "slot": counter},
            preview_text=_preview(result, tool_name),
        )
        bound_set.add(binding)
        logger.debug(
            "bind_evidence: %s → %s (slot %d)", tool_name, marker, counter
        )

    # B8.5 — evidence_items-based bindings (placeholder path; no rows yet)
    if evidence_items:
        for ev in evidence_items:
            try:
                ev_id: UUID = ev.evidence_id
                short = _short_ev_id(ev_id)
                marker = f"[ev:{short}]"
                if marker in bound_set.by_marker:
                    # Collision (8-char truncation) — extend to 12 chars
                    short = ev_id.hex[:12]
                    marker = f"[ev:{short}]"
                passage_id: UUID | None = getattr(ev, "passage_id", None)
                ev_store = {
                    "passage": "qdrant",
                    "structured_record": "postgis",
                    "graph_edge": "neo4j",
                    "map_feature": "hybrid",
                }.get(getattr(ev, "evidence_type", ""), "hybrid")
                binding = BoundEvidence(
                    marker_text=marker,
                    kind="ev",
                    index_or_id=short,
                    source_store=ev_store,
                    evidence_id=ev_id,
                    passage_id=passage_id,
                    display_ref={"evidence_type": getattr(ev, "evidence_type", None)},
                    preview_text=str(getattr(ev, "preview_text", ""))[:200],
                )
                bound_set.add(binding)
                logger.debug("bind_evidence: evidence_item %s → %s", ev_id, marker)
            except Exception:
                logger.warning(
                    "bind_evidence: failed to bind evidence_item %s (skipped)",
                    getattr(ev, "evidence_id", "?"),
                    exc_info=True,
                )

    logger.info(
        "bind_evidence: %d binding(s) total (%d tool-slot, %d ev-id)",
        len(bound_set.bindings),
        counter,
        len(bound_set.bindings) - counter,
    )
    return bound_set


def render_evidence_block(bound_set: BoundEvidenceSet) -> str:
    """Render the Evidence Set section of the LLM prompt.

    Produces a compact block the model reads before writing its answer:

        Evidence Set (cite each fact with the marker shown):
        [DATA:1] source_store=postgis preview="1 drill hole in PLS project"
        [NI43:2] source_store=qdrant preview="NI 43-101 section 14 — Resource..."

    Module 6 Chunk 3.5 trim (2026-04-22):
      - Preview cap: 160 → 80 chars. Saves ~500-1000 prompt tokens per query
        on typical multi-tool results, reducing LLM decode time ~5-10s on 30B.
      - Removed verbose [STORE / tool_name] dual-label — only source_store kept.
        The tool_name was diagnostic metadata the model doesn't need for citing.

    When the bound set is empty (no tools ran), returns an empty string so the
    caller can omit the block entirely.
    """
    if not bound_set.bindings:
        return ""

    lines: list[str] = ["Evidence Set (cite each fact with the marker shown):"]
    for b in bound_set.bindings:
        # 80-char preview cap (was 160).
        preview = b.preview_text[:80].replace("\n", " ") if b.preview_text else ""
        store_label = b.source_store or "unknown"
        lines.append(
            f"  {b.marker_text} source_store={store_label}"
            + (f' preview="{preview}"' if preview else "")
        )

    return "\n".join(lines)
