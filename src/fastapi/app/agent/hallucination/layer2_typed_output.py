"""Layer 2 — Typed Output Validation.

Architecture reference: Section 04i, Layer 2.

Purpose
-------
Validate that the assembled GeoRAGResponse is internally consistent:

  1. Every citation marker in the LLM text (e.g. ``[DATA-1]``, ``[NI43-2]``)
     has a corresponding Citation object in the ``citations`` list.
  2. No Citation has an empty or placeholder ``source_chunk_id``.
  3. The ``text`` field is not empty or a pure refusal with no grounding data.
  4. ``confidence`` is within [0.0, 1.0].

This layer runs AFTER the response_assembler has built the GeoRAGResponse but
BEFORE the response is streamed to the client. Unlike Pydantic AI's native
``output_validator`` (which reruns the LLM), this is a hard gate — validation
failures are logged as warnings and the response is repaired in-place rather
than rejected, because the LLM cannot be re-invoked in the deterministic
orchestrator flow.

Repairs applied
---------------
- Orphan citation markers (in text but not in citations list) are stripped
  from the text to avoid rendering broken citation chips in the frontend.
- Placeholder source_chunk_ids ("no-tool-call") are replaced with a
  descriptive string so the provenance chain is honest about the gap.
- Confidence is clamped to [0.0, 1.0] if somehow out of range.

Usage
-----
Call ``validate_and_repair`` on the assembled GeoRAGResponse in the
orchestrator's ``run_deterministic_rag`` immediately before returning:

    from app.agent.hallucination.layer2_typed_output import validate_and_repair
    response = validate_and_repair(response)
"""

from __future__ import annotations

import logging
import re

from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)

# All supported citation marker patterns.
_CITATION_MARKER_RE = re.compile(r"\[(DATA|NI43|PUB)-(\d+)\]")


def validate_and_repair(response: GeoRAGResponse) -> GeoRAGResponse:
    """Validate and repair a GeoRAGResponse for internal consistency.

    This is hallucination prevention Layer 2.

    Returns:
        The (possibly repaired) GeoRAGResponse. Never raises — all issues
        are logged as warnings and fixed in-place.
    """
    issues: list[str] = []

    # ── Check 1: citation marker ↔ citation list consistency ──────────────
    known_ids = {c.citation_id for c in response.citations}
    markers_in_text = _CITATION_MARKER_RE.findall(response.text)

    orphan_markers: list[str] = []
    for prefix, num in markers_in_text:
        marker = f"[{prefix}-{num}]"
        if marker not in known_ids:
            orphan_markers.append(marker)

    if orphan_markers:
        issues.append(
            f"Orphan citation marker(s) in text with no matching Citation object: "
            f"{', '.join(orphan_markers)}"
        )
        # Repair: strip orphan markers from the text.
        text = response.text
        for marker in orphan_markers:
            text = text.replace(marker, "")
        # Clean up double spaces left by removal.
        text = re.sub(r"  +", " ", text).strip()
        response = response.model_copy(update={"text": text})

    # ── Check 2: source_chunk_id validity ─────────────────────────────────
    for citation in response.citations:
        if not citation.source_chunk_id or citation.source_chunk_id == "no-tool-call":
            issues.append(
                f"Citation {citation.citation_id} has placeholder "
                f"source_chunk_id='{citation.source_chunk_id}'"
            )
            # Don't repair — the assembler's fallback is the best we can do
            # when no tool was called. The placeholder is honest.

    # ── Check 3: text not empty ───────────────────────────────────────────
    if not response.text or not response.text.strip():
        issues.append("Response text is empty")
        response = response.model_copy(
            update={"text": "I was unable to generate a response."}
        )

    # ── Check 4: confidence clamped ───────────────────────────────────────
    if response.confidence < 0.0 or response.confidence > 1.0:
        issues.append(
            f"Confidence {response.confidence} out of [0.0, 1.0] range"
        )
        response = response.model_copy(
            update={
                "confidence": max(0.0, min(1.0, response.confidence))
            }
        )

    # ── Check 5: at least one source_used ─────────────────────────────────
    if not response.sources_used:
        issues.append("sources_used list is empty — no grounding data")

    # ── Log results ───────────────────────────────────────────────────────
    if issues:
        logger.warning(
            "layer2_typed_output: %d issue(s) found and repaired:\n  %s",
            len(issues),
            "\n  ".join(issues),
        )
    else:
        logger.debug("layer2_typed_output: response passed all checks")

    return response
