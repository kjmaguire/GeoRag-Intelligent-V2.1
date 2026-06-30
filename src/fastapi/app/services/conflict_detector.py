"""Conflict detection for bound evidence items (Module 6 Phase B Chunk 4b).

Architecture reference: module spec 06-citation-hallucination-guards.md §6 B7.

Purpose
-------
After Stage 2 span resolution, ``detect_conflicts`` scans the full
``BoundEvidenceSet`` for structured-record and graph-edge evidence items that
carry contradictory scalar properties for the same entity.

V1 coverage
-----------
  * ``structured_record`` evidence: two bindings with matching entity keys AND
    differing scalar property values → conflict.  Entity key is derived from
    the ``structured_ref`` JSONB shape (schema.table + primary key tuple).
  * ``graph_edge`` evidence: two bindings with the same (start_node_id,
    end_node_id) pair but differing ``rel_type`` OR same triple with differing
    relationship properties → conflict.
  * ``passage`` evidence: skip (V1 — LLM-hard; the completeness guard's refusal
    path catches flagrant contradictions at the sentence level).
  * ``map_feature`` evidence: defer (V1 — rare, no structured key to normalize).

Safety invariant (Global Invariant 7)
--------------------------------------
This function NEVER picks a winner or merges values.  It surfaces BOTH sides
side-by-side so the Module 7 UI can render conflict cards.  Detection failure
(unexpected JSONB shape, key error, etc.) always returns an empty list and
logs a WARNING — never raises.

Time budget
-----------
The fuzzy/substring search over 5-10 bindings is O(N²) on the binding count.
With typical answer runs producing ≤15 bindings this is well within the 500ms
budget reserved for post-span-resolution work before TIMEOUT_GATHER_S fires.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from app.agent.citation_binding import BoundEvidence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class ConflictingEvidence:
    """A detected conflict between two or more bound evidence items.

    ``entity_key`` is a normalized string that identifies the entity being
    compared, e.g.:
      - ``"silver.collars:collar_id=019d74a7-..."`` for structured_record
      - ``"neo4j:edge(123,456)"`` for graph_edge

    ``property_name`` is the scalar field that differs, e.g. ``"total_depth"``
    for a collar or ``"rel_type"`` for a graph relationship.

    ``evidence_ids`` and ``values`` are parallel lists — ``evidence_ids[i]``
    is the evidence_id of the binding that carries ``values[i]``.  For
    tool-slot bindings (where evidence_id is None), a synthetic placeholder
    string is used so the UI can still render distinct cards.
    """

    entity_key: str
    property_name: str
    evidence_ids: list[UUID | str]
    values: list[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _structured_entity_key(display_ref: dict) -> str | None:
    """Derive a normalized entity key from a structured_record display_ref.

    Expects keys: ``schema``, ``table``, ``pk`` (dict of column→value pairs).
    Returns None when the ref is missing required keys.

    Example output: ``"silver.collars:collar_id=019d74a7-..."``
    """
    try:
        schema = display_ref.get("schema") or display_ref.get("table_schema")
        table = display_ref.get("table")
        pk: dict = display_ref.get("pk") or {}
        if not table or not pk:
            return None
        prefix = f"{schema}.{table}" if schema else table
        pk_str = ",".join(f"{k}={v}" for k, v in sorted(pk.items()))
        return f"{prefix}:{pk_str}"
    except Exception:
        return None


def _graph_edge_key(display_ref: dict) -> str | None:
    """Derive a normalized entity key from a graph_edge display_ref.

    Expects keys: ``start_node_id``, ``end_node_id``.
    Returns None when required keys are absent.

    Example output: ``"neo4j:edge(123,456)"``
    """
    try:
        start = display_ref.get("start_node_id")
        end = display_ref.get("end_node_id")
        if start is None or end is None:
            return None
        return f"neo4j:edge({start},{end})"
    except Exception:
        return None


def _scalar_properties(display_ref: dict) -> dict[str, str]:
    """Extract scalar string properties from a display_ref for comparison.

    Returns a flat dict of {property_name: stringified_value}.
    Only includes scalar values (str, int, float, bool) — ignores nested dicts
    and lists which cannot be compared as atomic values.

    Reserved keys that are structural (not domain properties) are excluded:
    schema, table, table_schema, pk, start_node_id, end_node_id, rel_type,
    tool, slot, chunk_id.
    """
    _STRUCTURAL_KEYS = frozenset({
        "schema", "table", "table_schema", "pk",
        "start_node_id", "end_node_id",
        "tool", "slot", "chunk_id",
        "evidence_type",
    })
    result: dict[str, str] = {}
    try:
        for k, v in display_ref.items():
            if k in _STRUCTURAL_KEYS:
                continue
            if isinstance(v, (str, int, float, bool)):
                result[k] = str(v)
    except Exception:
        pass
    return result


def _binding_id_label(b: BoundEvidence) -> UUID | str:
    """Return a display-friendly identifier for the binding.

    Uses evidence_id when present; falls back to the marker_text string so
    tool-slot bindings (where evidence_id is None) are still distinguishable.
    """
    if b.evidence_id is not None:
        return b.evidence_id
    return b.marker_text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_conflicts(
    bindings: list[BoundEvidence],
) -> list[ConflictingEvidence]:
    """Scan bound evidence for structured-record + graph-edge contradictions.

    Scans all bindings twice:
      Pass 1 — group ``structured_record``-type bindings by normalized entity key.
               Within each group, compare scalar properties.  Any property with
               ≥2 distinct values generates one ``ConflictingEvidence`` entry.

      Pass 2 — group ``graph_edge``-type bindings by (start_node_id, end_node_id).
               Within each group, detect differing ``rel_type`` OR differing
               scalar properties.

    Passage evidence is explicitly skipped (V1 scope constraint: contradiction
    detection in free text is LLM-hard; the completeness guard catches flagrant
    cases at the sentence level).

    Returns an empty list when:
      - No conflicts are found.
      - Any detection step fails (exception caught, logged, silently skipped).

    This function is NON-MUTATING — the input ``bindings`` list is not modified.

    Args:
        bindings: List of BoundEvidence from BoundEvidenceSet.bindings.

    Returns:
        List of ConflictingEvidence, possibly empty.
    """
    conflicts: list[ConflictingEvidence] = []

    # ── Pass 1: structured_record bindings ──────────────────────────────────
    # Group by entity key, then compare scalar properties within each group.
    structured_groups: dict[str, list[BoundEvidence]] = {}
    for b in bindings:
        if b.display_ref is None:
            continue
        # Identify structured_record bindings: those whose display_ref carries
        # a 'pk' key (primary key dict) — the canonical marker per citation_binding.
        # evidence_items [ev:*] structured bindings carry evidence_type='structured_record'.
        ev_type = (b.display_ref or {}).get("evidence_type")
        has_pk = "pk" in (b.display_ref or {})
        if not (has_pk or ev_type == "structured_record"):
            continue
        key = _structured_entity_key(b.display_ref)
        if key is None:
            continue
        structured_groups.setdefault(key, []).append(b)

    for entity_key, group in structured_groups.items():
        if len(group) < 2:
            continue
        try:
            # Collect per-property values across all bindings in this group.
            # prop_values: {property_name: [(value_str, binding), ...]}
            prop_values: dict[str, list[tuple[str, BoundEvidence]]] = {}
            for b in group:
                for prop, val in _scalar_properties(b.display_ref or {}).items():
                    prop_values.setdefault(prop, []).append((val, b))

            for prop, val_bindings in prop_values.items():
                unique_vals = {v for v, _ in val_bindings}
                if len(unique_vals) < 2:
                    continue
                # Conflict found — collect one entry per distinct value (dedup by value).
                seen_vals: dict[str, UUID | str] = {}
                for val, b in val_bindings:
                    if val not in seen_vals:
                        seen_vals[val] = _binding_id_label(b)
                conflicts.append(ConflictingEvidence(
                    entity_key=entity_key,
                    property_name=prop,
                    evidence_ids=list(seen_vals.values()),
                    values=list(seen_vals.keys()),
                ))
                logger.info(
                    "conflict_detector: structured_record conflict entity_key=%s "
                    "property=%s values=%s",
                    entity_key, prop, list(seen_vals.keys()),
                )
        except Exception:
            logger.warning(
                "conflict_detector: error scanning structured group entity_key=%s "
                "(skipped)",
                entity_key,
                exc_info=True,
            )

    # ── Pass 2: graph_edge bindings ─────────────────────────────────────────
    # Group by (start_node_id, end_node_id), then compare rel_type + properties.
    graph_groups: dict[str, list[BoundEvidence]] = {}
    for b in bindings:
        if b.display_ref is None:
            continue
        ev_type = (b.display_ref or {}).get("evidence_type")
        has_nodes = (
            "start_node_id" in (b.display_ref or {})
            and "end_node_id" in (b.display_ref or {})
        )
        if not (has_nodes or ev_type == "graph_edge"):
            continue
        key = _graph_edge_key(b.display_ref)
        if key is None:
            continue
        graph_groups.setdefault(key, []).append(b)

    for edge_key, group in graph_groups.items():
        if len(group) < 2:
            continue
        try:
            # rel_type conflict: same node pair, different relationship type.
            rel_type_vals: dict[str, UUID | str] = {}
            for b in group:
                rt = (b.display_ref or {}).get("rel_type")
                if rt is not None:
                    rt_str = str(rt)
                    if rt_str not in rel_type_vals:
                        rel_type_vals[rt_str] = _binding_id_label(b)
            if len(rel_type_vals) >= 2:
                conflicts.append(ConflictingEvidence(
                    entity_key=edge_key,
                    property_name="rel_type",
                    evidence_ids=list(rel_type_vals.values()),
                    values=list(rel_type_vals.keys()),
                ))
                logger.info(
                    "conflict_detector: graph_edge rel_type conflict edge=%s values=%s",
                    edge_key, list(rel_type_vals.keys()),
                )

            # Scalar property conflicts within same node pair.
            prop_values_g: dict[str, list[tuple[str, BoundEvidence]]] = {}
            for b in group:
                for prop, val in _scalar_properties(b.display_ref or {}).items():
                    prop_values_g.setdefault(prop, []).append((val, b))

            for prop, val_bindings in prop_values_g.items():
                unique_vals = {v for v, _ in val_bindings}
                if len(unique_vals) < 2:
                    continue
                seen_vals_g: dict[str, UUID | str] = {}
                for val, b in val_bindings:
                    if val not in seen_vals_g:
                        seen_vals_g[val] = _binding_id_label(b)
                conflicts.append(ConflictingEvidence(
                    entity_key=edge_key,
                    property_name=prop,
                    evidence_ids=list(seen_vals_g.values()),
                    values=list(seen_vals_g.keys()),
                ))
                logger.info(
                    "conflict_detector: graph_edge property conflict edge=%s "
                    "property=%s values=%s",
                    edge_key, prop, list(seen_vals_g.keys()),
                )
        except Exception:
            logger.warning(
                "conflict_detector: error scanning graph group edge_key=%s (skipped)",
                edge_key,
                exc_info=True,
            )

    if conflicts:
        logger.info(
            "conflict_detector: %d conflict(s) detected across %d bindings",
            len(conflicts),
            len(bindings),
        )
    else:
        logger.debug(
            "conflict_detector: no conflicts in %d binding(s)",
            len(bindings),
        )

    return conflicts


# ---------------------------------------------------------------------------
# Public answer-text formatter (Eval 01 L5 follow-up, 2026-05-20).
#
# When detect_conflicts() returns a non-empty list, the orchestrator currently
# attaches the structured ConflictingEvidence objects to
# response.conflicting_evidence — visible to the chat UI's evidence
# inspector, but NOT visible in the answer prose. The §04i guard spec calls
# for explicit surfacing in the user-facing answer text: "Records conflict:
# X shows Y; Z shows W". This helper produces that sentence.
#
# Conservative wording: we DO NOT pick a winner. We surface BOTH sides.
# The geologist decides. This preserves Global Invariant 7 (never merge or
# pick — surface).
# ---------------------------------------------------------------------------


def format_conflict_notice(conflicts: list[ConflictingEvidence]) -> str:
    """Produce an answer-text-ready notice describing detected conflicts.

    Returns an empty string when there are no conflicts (caller may then
    skip the injection cleanly).

    Output shape: one Markdown-italicised note line per conflict, e.g.

        *Conflicting evidence:* silver.collars total_depth — 145.0 vs 168.5
        (records: a3b…, c7d…).

    The orchestrator prepends this notice to response.text so a geologist
    reading the answer sees the conflict before the synthesis.
    """
    if not conflicts:
        return ""

    lines: list[str] = []
    for c in conflicts:
        # entity_key already contains the table/key info; trim the long
        # PK suffix to keep the notice readable.
        ek = c.entity_key
        if len(ek) > 70:
            ek = ek[:67] + "…"

        # Format value list. Truncate each value to 40 chars so a runaway
        # JSONB blob doesn't dominate the answer.
        vals = ", ".join(
            (v if len(str(v)) <= 40 else str(v)[:37] + "…") for v in c.values
        )

        # Trim evidence ids similarly for readability.
        eids = ", ".join(
            str(e)[:8] + "…" if len(str(e)) > 10 else str(e)
            for e in c.evidence_ids
        )

        lines.append(
            f"*Conflicting evidence:* {ek} `{c.property_name}` — "
            f"values: {vals} (evidence: {eids})."
        )

    return "\n\n".join(lines) + "\n\n"
