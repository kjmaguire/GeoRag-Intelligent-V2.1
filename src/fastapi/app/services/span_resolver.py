"""Stage 2 — Post-generation span resolver (Module 6 Phase B Chunk 2).

Architecture reference: georag-architecture-addendum-v1.10.html §04j;
module spec 06-citation-hallucination-guards.md §6 B1 / B5.

Purpose
-------
After the LLM returns its answer text, ``resolve_spans`` walks the text to
find colon-form citation markers (``[DATA:N]``, ``[NI43:N]``, ``[PUB:N]``,
``[PGEO:N]``, ``[ev:<id>]``), computes their character offsets, and binds
each marker to the FK targets recorded in the ``BoundEvidenceSet`` produced
by Stage 1 (``citation_binding.bind_evidence``).

The resolver produces:
  * A list of ``AnswerCitationItemCreate`` — one per *unique* marker that
    successfully resolved to a binding with at least one FK target.
  * A list of ``AnswerCitationSpanCreate`` — one per *occurrence* of any
    resolvable marker in the answer text (including duplicates).
  * A ``telemetry`` dict with partial-resolution metrics.

Defensive rewrite
-----------------
The model may still emit legacy dash-form markers (``[DATA-N]``) during the
feature-flag rollout window.  ``_normalize_markers`` rewrites any dash-form
markers to colon-form before the primary regex runs.  The rewrite count is
captured in ``telemetry['legacy_dash_rewrites']``.

Partial failure
---------------
Markers that appear in the answer text but have no entry in the bound set are
treated as *unresolved*.  They are NOT inserted as citation_items (that would
violate the ``has_target`` DB CHECK).  Their count lands in
``telemetry['markers_unresolved']``.  The ``partial_resolution_rate`` metric
is designed to feed Chunk 4's ``hybrid_delayed_attachment`` decision — Chunk 2
only writes ``posthoc_span_resolution`` rows.

Feature flag
------------
This module is imported unconditionally by the orchestrator but ``resolve_spans``
is *called* only when ``settings.CITATION_SPAN_RESOLVER_ENABLED=true``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from uuid import UUID
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from app.agent.citation_binding import BoundEvidence, BoundEvidenceSet
from app.models.answer_run import AnswerCitationItemCreate, AnswerCitationSpanCreate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Primary pattern — colon-form markers only.
# Groups: (kind, index_or_id)
# Matches: [DATA:1], [NI43:2], [PUB:3], [PGEO:4], [ev:a1b2c3d4]
_MARKER_RE = re.compile(r"\[(DATA|NI43|PUB|PGEO|ev):([A-Za-z0-9-]+)\]")

# Legacy dash-form — used by the defensive normalizer only.
# Matches: [DATA-1], [NI43-2], [PUB-3], [PGEO-4]
# (ev markers never had a dash-form; ev: is new in Chunk 2)
_LEGACY_DASH_RE = re.compile(r"\[(DATA|NI43|PUB|PGEO)-(\d+)\]")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize_markers(answer_text: str) -> tuple[str, int]:
    """Defensive rewrite: convert dash-form markers to colon form.

    The model may emit ``[DATA-N]`` during the flag-rollout window if an
    older system prompt version is still cached or the flag was recently
    flipped.  This function rewrites *only* dash-form markers so the primary
    regex sees a consistent colon-form input.

    Args:
        answer_text: Raw LLM output string.

    Returns:
        (normalized_text, count_rewritten) — count is 0 when no rewrites
        were needed.
    """
    count = 0

    def _repl(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f"[{m.group(1)}:{m.group(2)}]"

    normalized = _LEGACY_DASH_RE.sub(_repl, answer_text)
    if count:
        logger.info(
            "span_resolver._normalize_markers: rewrote %d legacy dash-form marker(s)",
            count,
        )
    return normalized, count


# ---------------------------------------------------------------------------
# Span resolution
# ---------------------------------------------------------------------------

async def _lookup_passage_id(
    chunk_id: str,
    pg_pool: object,
    timeout_s: float = 2.0,
) -> UUID | None:
    """Look up passage_id in silver.document_passages WHERE embedding_id = chunk_id.

    ``embedding_id`` is the Qdrant point UUID (TEXT column).  This is the FK
    bridge between a Qdrant search result and the passage audit table.

    Returns None on any failure (timeout, no row, pool unavailable) — fail open.
    """
    if pg_pool is None or not chunk_id:
        return None
    sql = (
        "SELECT passage_id FROM silver.document_passages "
        "WHERE embedding_id = $1 LIMIT 1"
    )
    try:
        async def _run() -> UUID | None:
            async with pg_pool.acquire() as conn:  # type: ignore[union-attr]
                row = await conn.fetchrow(sql, chunk_id)
            if row:
                return UUID(str(row["passage_id"]))
            return None

        return await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.debug(
            "span_resolver._lookup_passage_id: timed out for chunk_id=%s (fail-open)",
            chunk_id,
        )
    except Exception:
        logger.debug(
            "span_resolver._lookup_passage_id: lookup failed for chunk_id=%s (fail-open)",
            chunk_id,
            exc_info=True,
        )
    return None


async def resolve_spans(
    *,
    answer_text: str,
    bound_set: BoundEvidenceSet,
    answer_run_id: UUID,
    workspace_id: UUID,
    pg_pool: object = None,
) -> tuple[list[AnswerCitationItemCreate], list[list[AnswerCitationSpanCreate]], dict]:
    """Parse answer text for colon-form markers, compute spans, bind to evidence.

    This is Stage 2 of the two-stage citation pipeline (Module 6 B1 spec).

    Algorithm:
      1. Defensive normalise: dash-form → colon-form.
      2. Find all marker occurrences via ``_MARKER_RE`` with character offsets.
      3. Group occurrences by marker_text (same marker can appear N times).
      4. For each unique marker, look it up in ``bound_set``.
         - Hit with FK target → create ``AnswerCitationItemCreate``.
         - Hit without FK target (tool-slot binding whose passage_id isn't
           resolved yet) → skip INSERT (both FKs None → violates has_target).
           Treated as unresolved + logged.
         - Miss (not in bound_set at all) → unresolved + telemetry only.
      5. For each resolved unique marker, create one ``AnswerCitationSpanCreate``
         per occurrence in the text.

    Span offsets are into *normalized_text* (after dash→colon rewrite).  The
    orchestrator should use ``normalized_text`` (returned via telemetry key
    ``normalized_text``) as the canonical answer text when the resolver runs
    so that stored offsets are valid against the stored string.

    Args:
        answer_text:    Raw LLM output (may contain dash-form markers).
        bound_set:      BoundEvidenceSet from Stage 1 ``bind_evidence``.
        answer_run_id:  FK for all rows written.
        workspace_id:   FK for all rows written.

    Returns:
        (items, spans_per_item, telemetry)

        items          — AnswerCitationItemCreate list (one per resolved unique
                         marker), in the order markers were first encountered.
        spans_per_item — Parallel list: ``spans_per_item[i]`` is the list of
                         AnswerCitationSpanCreate objects for ``items[i]``.
                         Each span has ``answer_citation_item_id`` set to the
                         nil UUID (00000000-…); the orchestrator back-fills the
                         real UUID after INSERT RETURNING.
        telemetry      — dict with keys:
            total_markers_found     int   — raw count of all marker occurrences
            unique_markers          int   — count of distinct marker_text values
            markers_resolved        int   — unique markers with a valid FK binding
            markers_unresolved      int   — unique markers that could not be bound
            legacy_dash_rewrites    int   — occurrences rewritten from dash-form
            fully_resolved          bool  — True iff markers_unresolved == 0
                                            and total_markers_found > 0
            partial_resolution_rate float — markers_resolved / unique_markers
                                            (0.0 when unique_markers == 0)
            normalized_text         str   — answer text after dash→colon rewrite
    """
    _NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")

    # Step 1 — normalize
    normalized_text, rewrite_count = _normalize_markers(answer_text)

    # Step 2 — find all occurrences with character offsets
    # occurrences: list of (marker_text, span_start, span_end)
    occurrences: list[tuple[str, int, int]] = []
    for m in _MARKER_RE.finditer(normalized_text):
        kind = m.group(1)
        idx_or_id = m.group(2)
        marker_text = f"[{kind}:{idx_or_id}]"
        occurrences.append((marker_text, m.start(), m.end()))

    total_markers_found = len(occurrences)

    # Step 3 — group occurrences by marker_text (preserving insertion order)
    # Use an ordered dict (Python 3.7+ dicts are insertion-ordered).
    # marker_text → list of (span_start, span_end)
    by_marker: dict[str, list[tuple[int, int]]] = {}
    for marker_text, span_start, span_end in occurrences:
        by_marker.setdefault(marker_text, []).append((span_start, span_end))

    unique_markers = len(by_marker)

    # Step 4 — resolve each unique marker against the bound set.
    # Chunk 3 addition: for tool-slot bindings with no FK target, attempt a
    # passage_id lookup via document_passages.embedding_id = chunk_id.
    items: list[AnswerCitationItemCreate] = []
    spans_per_item: list[list[AnswerCitationSpanCreate]] = []
    markers_resolved = 0
    markers_unresolved = 0
    tool_slot_passage_resolved = 0
    tool_slot_unresolvable = 0

    for marker_text, span_positions in by_marker.items():
        binding: BoundEvidence | None = bound_set.get(marker_text)

        if binding is None:
            markers_unresolved += 1
            logger.warning(
                "span_resolver: marker %s not in bound_set (answer_run_id=%s) — "
                "unresolved; skipping INSERT",
                marker_text,
                answer_run_id,
            )
            continue

        # Determine effective FK targets (may be augmented by passage_id lookup).
        effective_evidence_id: UUID | None = binding.evidence_id
        effective_passage_id: UUID | None = binding.passage_id

        # Chunk 3: passage_id lookup for tool-slot bindings with no FK target.
        # Applies to NI43/PUB/PGEO markers that came from search_documents
        # (Qdrant) and carry a chunk_id in display_ref.
        # DATA markers (PostGIS) never have a passage_id — skipped below.
        if effective_evidence_id is None and effective_passage_id is None:
            chunk_id: str | None = (binding.display_ref or {}).get("chunk_id")
            can_lookup = (
                chunk_id is not None
                and binding.kind in ("NI43", "PUB", "PGEO")
                and pg_pool is not None
            )
            if can_lookup:
                effective_passage_id = await _lookup_passage_id(
                    chunk_id, pg_pool, timeout_s=2.0  # type: ignore[arg-type]
                )
                if effective_passage_id is not None:
                    tool_slot_passage_resolved += 1
                    logger.debug(
                        "span_resolver: %s → passage_id=%s (via embedding_id lookup)",
                        marker_text,
                        effective_passage_id,
                    )
                else:
                    tool_slot_unresolvable += 1
                    logger.debug(
                        "span_resolver: %s chunk_id=%s not found in document_passages "
                        "(no passage yet ingested for this chunk)",
                        marker_text,
                        chunk_id,
                    )
            else:
                # DATA bindings (PostGIS/Neo4j) have no passage_id by design.
                # PGEO without pg_pool also lands here.
                tool_slot_unresolvable += 1
                logger.debug(
                    "span_resolver: %s has no FK target and no chunk_id for lookup "
                    "(kind=%s) — unresolvable (expected for structured-data tools)",
                    marker_text,
                    binding.kind,
                )

        # Final check: if still no FK target, skip INSERT (has_target CHECK).
        if effective_evidence_id is None and effective_passage_id is None:
            markers_unresolved += 1
            continue

        # Validate source_store against the DB CHECK constraint values.
        valid_stores = {"qdrant", "neo4j", "postgis", "hybrid"}
        source_store = binding.source_store if binding.source_store in valid_stores else None

        try:
            item = AnswerCitationItemCreate(
                answer_run_id=answer_run_id,
                workspace_id=workspace_id,
                evidence_id=effective_evidence_id,
                passage_id=effective_passage_id,
                marker_text=marker_text,
                source_store=source_store,  # type: ignore[arg-type]
                confidence=None,
                rejection_reason=None,
            )
        except Exception as exc:
            markers_unresolved += 1
            logger.warning(
                "span_resolver: AnswerCitationItemCreate validation failed for %s: %s",
                marker_text,
                exc,
            )
            continue

        markers_resolved += 1
        items.append(item)

        # Step 5 — build AnswerCitationSpanCreate for each occurrence.
        # answer_citation_item_id is set to the nil UUID as a placeholder;
        # the orchestrator back-fills the real UUID via INSERT RETURNING.
        item_spans: list[AnswerCitationSpanCreate] = []
        for span_start, span_end in span_positions:
            try:
                span = AnswerCitationSpanCreate(
                    answer_run_id=answer_run_id,
                    answer_citation_item_id=_NIL_UUID,
                    workspace_id=workspace_id,
                    span_start=span_start,
                    span_end=span_end,
                )
                item_spans.append(span)
            except Exception as exc:
                logger.warning(
                    "span_resolver: AnswerCitationSpanCreate validation failed "
                    "for %s offset (%d,%d): %s",
                    marker_text,
                    span_start,
                    span_end,
                    exc,
                )

        spans_per_item.append(item_spans)

    # Telemetry
    partial_resolution_rate = (
        markers_resolved / unique_markers if unique_markers > 0 else 0.0
    )
    fully_resolved = (markers_unresolved == 0) and (total_markers_found > 0)

    telemetry: dict = {
        "total_markers_found": total_markers_found,
        "unique_markers": unique_markers,
        "markers_resolved": markers_resolved,
        "markers_unresolved": markers_unresolved,
        "legacy_dash_rewrites": rewrite_count,
        "fully_resolved": fully_resolved,
        "partial_resolution_rate": partial_resolution_rate,
        "normalized_text": normalized_text,
        # Chunk 3 additions
        "tool_slot_passage_resolved": tool_slot_passage_resolved,
        "tool_slot_unresolvable": tool_slot_unresolvable,
    }

    logger.info(
        "span_resolver: resolved %d/%d unique markers "
        "(%d occurrences) dash_rewrites=%d fully=%s "
        "slot_passage_resolved=%d slot_unresolvable=%d",
        markers_resolved,
        unique_markers,
        total_markers_found,
        rewrite_count,
        fully_resolved,
        tool_slot_passage_resolved,
        tool_slot_unresolvable,
    )

    return items, spans_per_item, telemetry


# ---------------------------------------------------------------------------
# Delayed-attachment fallback (Module 6 Phase B Chunk 4b — B8)
# ---------------------------------------------------------------------------

# Fuzzy marker pattern: allow optional whitespace around the colon separator.
# Matches: [DATA : 1], [NI43:2], [DATA:1], [PUB :3]
_FUZZY_MARKER_RE = re.compile(
    r"\[(DATA|NI43|PUB|PGEO|ev)\s*:\s*([A-Za-z0-9-]+)\]"
)


def resolve_spans_delayed(
    *,
    answer_text: str,
    bound_set: BoundEvidenceSet,
    answer_run_id: UUID,
    workspace_id: UUID,
    unresolved_marker_texts: set[str],
) -> tuple[list[AnswerCitationItemCreate], list[list[AnswerCitationSpanCreate]], dict]:
    """Fallback pass for markers that did not resolve in ``resolve_spans``.

    Module 6 Phase B spec B8: if the primary span resolution pass leaves M-of-N
    markers unresolved (M > 0, M < N), this function attempts two looser
    matching strategies for the unresolved markers only.

    Strategy (a) — fuzzy regex:
        Allow optional whitespace around the colon separator, e.g.
        ``[DATA : 1]`` instead of ``[DATA:1]``.  The LLM occasionally emits
        this variant when system-prompt formatting discipline is slightly
        relaxed.

    Strategy (b) — substring search against preview_text:
        If a binding's ``preview_text`` (≥12 chars) is a substring of the
        answer text, treat that binding as the target for any unresolved
        marker of the same kind.  Minimum length guard avoids false positives
        on trivially short previews.

    This function is SYNCHRONOUS (no I/O) so it cannot block the event loop.
    Time budget: ≤500ms — O(N²) over ≤15 bindings is well within budget.

    Args:
        answer_text:              Normalized answer text (after primary pass
                                  dash→colon rewrite).
        bound_set:                BoundEvidenceSet from Stage 1.
        answer_run_id:            FK for any rows created.
        workspace_id:             FK for any rows created.
        unresolved_marker_texts:  Set of marker strings that resolve_spans
                                  could not bind (e.g. ``{"[DATA:3]"}``).

    Returns:
        (items, spans_per_item, telemetry)

        items           — AnswerCitationItemCreate list for markers that
                          this fallback successfully resolved.
        spans_per_item  — Parallel list of AnswerCitationSpanCreate lists.
        telemetry       — dict with keys:
            fallback_resolved_count   int
            fallback_failed_count     int
            citation_mode_used        str  — 'hybrid_delayed_attachment' when
                                             fallback_resolved_count > 0,
                                             else 'posthoc_span_resolution'
    """
    _NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")
    _MIN_PREVIEW_LEN = 12   # min chars to treat preview as a distinct anchor

    items: list[AnswerCitationItemCreate] = []
    spans_per_item: list[list[AnswerCitationSpanCreate]] = []
    fallback_resolved = 0
    fallback_failed = 0

    valid_stores = {"qdrant", "neo4j", "postgis", "hybrid"}

    for marker_text in unresolved_marker_texts:
        binding: BoundEvidence | None = bound_set.get(marker_text)
        if binding is None:
            fallback_failed += 1
            logger.debug(
                "resolve_spans_delayed: %s not in bound_set — cannot resolve",
                marker_text,
            )
            continue

        resolved_spans: list[tuple[int, int]] = []

        # ── Strategy (a): fuzzy regex ────────────────────────────────────────
        try:
            m0 = _MARKER_RE.match(marker_text)
            if m0:
                kind_str = m0.group(1)
                idx_str = m0.group(2)
                for fm in _FUZZY_MARKER_RE.finditer(answer_text):
                    if fm.group(1) == kind_str and fm.group(2) == idx_str:
                        resolved_spans.append((fm.start(), fm.end()))
        except Exception:
            logger.debug(
                "resolve_spans_delayed: fuzzy regex failed for %s (skipped)",
                marker_text,
                exc_info=True,
            )

        # ── Strategy (b): preview_text substring search ──────────────────────
        if not resolved_spans and binding.preview_text:
            preview = binding.preview_text.strip()
            if len(preview) >= _MIN_PREVIEW_LEN:
                idx = answer_text.find(preview)
                if idx != -1:
                    span_end = idx + len(preview)
                    resolved_spans.append((idx, span_end))
                    logger.debug(
                        "resolve_spans_delayed: %s resolved via preview_text "
                        "substring at offset %d",
                        marker_text, idx,
                    )

        if not resolved_spans:
            fallback_failed += 1
            logger.info(
                "resolve_spans_delayed: %s — both fuzzy + substring failed "
                "(unresolvable)",
                marker_text,
            )
            continue

        # Still no FK target → skip INSERT (has_target CHECK would fail).
        if binding.evidence_id is None and binding.passage_id is None:
            fallback_failed += 1
            logger.info(
                "resolve_spans_delayed: %s — no FK target on binding even after "
                "match (has_target would fail; skipping)",
                marker_text,
            )
            continue

        source_store = (
            binding.source_store if binding.source_store in valid_stores else None
        )
        try:
            item = AnswerCitationItemCreate(
                answer_run_id=answer_run_id,
                workspace_id=workspace_id,
                evidence_id=binding.evidence_id,
                passage_id=binding.passage_id,
                marker_text=marker_text,
                source_store=source_store,  # type: ignore[arg-type]
                confidence=None,
                rejection_reason=None,
            )
        except Exception as exc:
            fallback_failed += 1
            logger.warning(
                "resolve_spans_delayed: AnswerCitationItemCreate failed for %s: %s",
                marker_text, exc,
            )
            continue

        fallback_resolved += 1
        items.append(item)

        item_spans: list[AnswerCitationSpanCreate] = []
        for span_start, span_end in resolved_spans:
            try:
                span = AnswerCitationSpanCreate(
                    answer_run_id=answer_run_id,
                    answer_citation_item_id=_NIL_UUID,
                    workspace_id=workspace_id,
                    span_start=span_start,
                    span_end=span_end,
                )
                item_spans.append(span)
            except Exception as exc:
                logger.warning(
                    "resolve_spans_delayed: AnswerCitationSpanCreate failed for "
                    "%s offset (%d,%d): %s",
                    marker_text, span_start, span_end, exc,
                )
        spans_per_item.append(item_spans)

    citation_mode_used = (
        "hybrid_delayed_attachment" if fallback_resolved > 0
        else "posthoc_span_resolution"
    )

    telemetry = {
        "fallback_resolved_count": fallback_resolved,
        "fallback_failed_count": fallback_failed,
        "citation_mode_used": citation_mode_used,
    }

    logger.info(
        "resolve_spans_delayed: fallback_resolved=%d fallback_failed=%d "
        "citation_mode=%s",
        fallback_resolved, fallback_failed, citation_mode_used,
    )

    return items, spans_per_item, telemetry
