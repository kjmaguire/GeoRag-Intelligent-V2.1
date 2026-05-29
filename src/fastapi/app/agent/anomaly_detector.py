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
#: text.  Consumed by ``strip_proactive_insights`` so §04i validators (Layers
#: 3, 4, 6 + completeness_guard) skip the block — the numbers, entities, and
#: sentences inside it are deterministically computed from raw tool_results
#: data and don't need adversarial grounding.
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


def strip_proactive_insights(text: str) -> str:
    """Return *text* with the proactive-insights block removed.

    The orchestrator appends a deterministic "Proactive Insights" block to
    the LLM answer text after synthesis (see ``orchestrator.run_deterministic_rag``
    step 4b).  Numbers and entities inside the block come from
    ``anomaly_detector._depth_anomalies`` / ``_assay_anomalies`` /
    ``_lithology_anomalies`` — derived statistics (mean, sigma) computed
    deterministically from real tool_results rows, not LLM output.

    The §04i validators (numeric grounding, entity resolution, completeness)
    are designed to catch *LLM hallucinations*.  Running them over the
    insights block produces noise: Layer 3 flags σ-derived stats that don't
    appear verbatim in tool_results, Layer 4 flags common-word
    TitleCase tokens like "Depth" / "Consider", and Layer 6 flags every
    insight bullet as uncited.  Phase F.5 strips the block before each
    validator runs so the layers grade only what the LLM actually wrote.

    Strip semantics: cut from the marker header to the end of the string.
    The insights block is always the *last* thing the orchestrator appends
    (assemble_response may add citation markers after, but those are
    intentionally part of the LLM-answer surface for completeness_guard to
    see).  We therefore conservatively strip from the header onward and
    re-append any trailing citation markers if present.
    """
    if PROACTIVE_INSIGHTS_HEADER not in text:
        return text

    head, _, tail = text.partition(PROACTIVE_INSIGHTS_HEADER)

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
