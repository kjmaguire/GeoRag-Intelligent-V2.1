"""Proactive anomaly detection — surfaces insights the geologist didn't ask for.

Runs as a post-query enrichment step. After the orchestrator assembles the
GeoRAGResponse, the anomaly detector scans the tool results for statistical
outliers and appends "insight cards" to the response text.

Anomaly types detected:
  1. Grade outliers — assay values >2σ above the project mean
  2. Depth anomalies — holes significantly deeper/shallower than peers
  3. Lithology transitions — unusual formation contacts not seen elsewhere
  4. Grade-thickness products — high GT intervals worth highlighting

Insights are appended as a block at the end of the response text (not
inline) so they don't break citation flow. The frontend can later render
them as collapsible "Insight" cards below the answer.

Wiring status (2026-08-15): ``detect_anomalies`` / ``append_insights_block``
are not currently invoked by any live orchestrator path — the step that
used to call them lived in the deleted legacy orchestrator body. They're
kept here, tested, and ready to re-wire (the natural call site is
``app.agent.response_assembler.assemble_response``, before the
``GeoRAGResponse`` is constructed). ``strip_proactive_insights`` and the
§04i guards that call it are written defensively regardless of this: they
trust only an explicit ``proactive_insights_offset``, never a text search,
so re-wiring this feature later cannot silently reopen the guard-bypass
that a text-search boundary would allow (see ``strip_proactive_insights``).
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.agent.tools import (
    AssayDataResult,
    DownholeLogsResult,
    SpatialQueryResult,
)

logger = logging.getLogger(__name__)


def detect_anomalies(
    tool_results: list[tuple[str, Any]],
    query: str,
) -> list[str]:
    """Scan tool results for anomalies and return insight strings.

    Returns an empty list if no anomalies are detected. Each string is
    a self-contained insight suitable for appending to the LLM response.
    """
    insights: list[str] = []

    for _tool_name, result in tool_results:
        if isinstance(result, AssayDataResult) and result.count >= 3:
            insights.extend(_assay_anomalies(result))
        if isinstance(result, SpatialQueryResult) and result.count >= 3:
            insights.extend(_depth_anomalies(result))
        if isinstance(result, DownholeLogsResult) and result.count >= 2:
            insights.extend(_lithology_anomalies(result))

    return insights[:5]  # cap at 5 insights per response


def _assay_anomalies(result: AssayDataResult) -> list[str]:
    """Detect grade outliers (>2σ above mean)."""
    insights = []
    vals = [s.value for s in result.samples]
    if len(vals) < 3:
        return insights

    mean = sum(vals) / len(vals)
    variance = sum((v - mean) ** 2 for v in vals) / len(vals)
    std = math.sqrt(variance) if variance > 0 else 0

    if std == 0:
        return insights

    threshold = mean + 2 * std

    for sample in result.samples:
        if sample.value > threshold:
            sigma = (sample.value - mean) / std
            gt = sample.value * (sample.to_depth - sample.from_depth)
            insights.append(
                f"Grade anomaly: {sample.hole_id} returned "
                f"{sample.value:,.0f} {result.element} at "
                f"{sample.from_depth:.1f}–{sample.to_depth:.1f} m "
                f"({sigma:.1f}σ above project mean of {mean:,.0f}). "
                f"Grade-thickness product: {gt:,.0f}."
            )

    return insights


def _depth_anomalies(result: SpatialQueryResult) -> list[str]:
    """Detect holes significantly deeper/shallower than the project average."""
    insights = []
    depths = [c.total_depth for c in result.collars]
    if len(depths) < 3:
        return insights

    mean = sum(depths) / len(depths)
    variance = sum((d - mean) ** 2 for d in depths) / len(depths)
    std = math.sqrt(variance) if variance > 0 else 0

    if std == 0:
        return insights

    for collar in result.collars:
        sigma = abs(collar.total_depth - mean) / std
        if sigma > 2.0:
            direction = "deeper" if collar.total_depth > mean else "shallower"
            insights.append(
                f"Depth anomaly: {collar.hole_id} is {collar.total_depth:.0f} m TD — "
                f"{sigma:.1f}σ {direction} than the project average of {mean:.0f} m. "
                f"Consider whether this reflects geological targets at depth or "
                f"operational constraints."
            )

    return insights


def _lithology_anomalies(result: DownholeLogsResult) -> list[str]:
    """Flag unusual lithology sequences."""
    insights = []
    if not result.collar or not result.intervals:
        return insights

    # Check for very thick intervals (>100m of a single unit)
    for iv in result.intervals:
        thickness = iv.to_depth - iv.from_depth
        if thickness > 100:
            insights.append(
                f"Thick interval: {result.collar.hole_id} has "
                f"{thickness:.0f} m of {iv.lithology_code} "
                f"({iv.lithology_description or 'no description'}) "
                f"from {iv.from_depth:.0f}–{iv.to_depth:.0f} m. "
                f"This dominates the downhole column — consider whether "
                f"this represents a favourable host rock or barren cover."
            )

    # Check for very high RQD variance (fractured vs intact zones)
    rqd_vals = [iv.rqd for iv in result.intervals if iv.rqd is not None]
    if len(rqd_vals) >= 2:
        rqd_range = max(rqd_vals) - min(rqd_vals)
        if rqd_range > 20:
            low_rqd = min(result.intervals, key=lambda iv: iv.rqd or 100)
            insights.append(
                f"Rock quality variation: {result.collar.hole_id} shows "
                f"RQD ranging from {min(rqd_vals):.0f}% to {max(rqd_vals):.0f}%. "
                f"The lowest RQD is in the {low_rqd.lithology_code} interval "
                f"({low_rqd.from_depth:.0f}–{low_rqd.to_depth:.0f} m) — "
                f"this may indicate a structural zone worth investigating."
            )

    return insights


#: Header that marks the start of the proactive-insights block in response
#: text.  This is a human-readable label only — see the security note on
#: ``strip_proactive_insights`` for why it must never be used to *locate*
#: the block (i.e. never search the final response text for it).
PROACTIVE_INSIGHTS_HEADER = "--- Proactive Insights ---"


def format_insights_block(insights: list[str]) -> str:
    """Format a list of insight strings into a text block for the response."""
    if not insights:
        return ""

    lines = [
        "",
        PROACTIVE_INSIGHTS_HEADER,
    ]
    for i, insight in enumerate(insights, 1):
        lines.append(f"  {i}. {insight}")
    lines.append("")

    return "\n".join(lines)


def append_insights_block(
    llm_text: str, insights: list[str]
) -> tuple[str, int | None]:
    """Append the deterministic insights block to *llm_text* and return the
    combined text plus the character offset where the appended block
    begins.

    This is the ONLY sanctioned way to attach the anomaly-detector's
    insights to a response. Callers MUST propagate the returned offset onto
    ``GeoRAGResponse.proactive_insights_offset`` — the §04i guards (numeric
    grounding, entity resolution, constraint checking; see
    ``app.agent.hallucination.orchestrator_validators.run_post_assembly_validation``)
    read that field, not a search of the final text, to find the boundary
    between LLM-authored content and this deterministic block. See
    ``strip_proactive_insights`` for why locating the boundary via a text
    search is unsafe.

    Returns ``(llm_text, None)`` unchanged when there are no insights to
    append (the common case).
    """
    block = format_insights_block(insights)
    if not block:
        return llm_text, None

    # format_insights_block prefixes the header with a blank-line separator
    # ("\n" + header + ...) for readability, so the header itself starts
    # partway into `block`, not at its first character. The recorded offset
    # must point at the header exactly — strip_proactive_insights asserts
    # `text[offset:]` starts with the header as a defensive consistency
    # check — so locate it within `block` (trusted, just-generated text,
    # not a search over LLM output) rather than assuming offset == len(llm_text).
    offset = len(llm_text) + block.index(PROACTIVE_INSIGHTS_HEADER)
    return llm_text + block, offset


def strip_proactive_insights(text: str, proactive_insights_offset: int | None) -> str:
    """Return *text* with the proactive-insights block removed.

    ``proactive_insights_offset`` is ``GeoRAGResponse.proactive_insights_offset``
    — the character index, recorded at assembly time by
    ``append_insights_block``, where the deterministic insights block
    begins. Numbers and entities inside that block come from
    ``anomaly_detector._depth_anomalies`` / ``_assay_anomalies`` /
    ``_lithology_anomalies`` — derived statistics (mean, sigma) computed
    deterministically from real tool_results rows, not LLM output.

    The §04i validators (numeric grounding, entity resolution, completeness)
    are designed to catch *LLM hallucinations*.  Running them over the
    insights block produces noise: Layer 3 flags σ-derived stats that don't
    appear verbatim in tool_results, Layer 4 flags common-word
    TitleCase tokens like "Depth" / "Consider", and Layer 6 flags every
    insight bullet as uncited.  Each validator strips the block before it
    runs so the layers grade only what the LLM actually wrote.

    SECURITY: the boundary is taken exclusively from
    ``proactive_insights_offset``, an explicit, structurally-recorded value
    set by trusted assembly code — it is never re-derived by searching
    *text* for ``PROACTIVE_INSIGHTS_HEADER``. A previous version of this
    function did exactly that (``text.partition(PROACTIVE_INSIGHTS_HEADER)``),
    which is unsafe: *text* is the final, fully-concatenated response,
    including whatever the LLM itself generated. If the model's own output
    contains the literal header string — via prompt injection from an
    ingested document, or simply by imitating the header's phrasing — a
    text search finds that occurrence too, and everything the model wrote
    afterwards (fabricated numbers, entities, constraint-violating claims)
    would be silently excluded from every guard. A boundary discovered
    inside attacker-influenceable text is not a trustworthy boundary.

    ``proactive_insights_offset is None`` — true for essentially every
    response, since no live orchestrator path currently calls
    ``append_insights_block`` — means no insights block was appended, so
    the entire string is LLM-generated and nothing is stripped, regardless
    of what substrings it happens to contain.
    """
    if proactive_insights_offset is None:
        return text
    if proactive_insights_offset < 0 or proactive_insights_offset > len(text):
        # A recorded offset that no longer fits the text (e.g. text was
        # mutated after assembly in a way that invalidated the offset) is
        # treated as untrustworthy. Fail toward *not* stripping — the whole
        # string stays subject to guard verification — rather than guessing.
        return text

    head, tail = text[:proactive_insights_offset], text[proactive_insights_offset:]
    if not tail.startswith(PROACTIVE_INSIGHTS_HEADER):
        # Defensive consistency check only (not a search): the recorded
        # offset should always point exactly at the header, since
        # append_insights_block derives one from the other. A mismatch
        # means the offset is stale — don't strip.
        return text

    # The assembler may append a closing "[DATA-N] [NI43-M]." run after the
    # insights block.  Preserve those trailing markers so completeness_guard
    # still sees them when grading the head LLM text.
    import re as _re
    trailing = _re.search(
        r"\s+(?:\[(?:DATA|NI43|PUB|PGEO)[-:]\d+\]\s*)+\.?\s*\Z",
        tail,
    )
    suffix = trailing.group(0) if trailing else ""

    return (head.rstrip() + suffix).rstrip()
