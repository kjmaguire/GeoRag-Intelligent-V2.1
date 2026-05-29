"""Presentation Coach Agent (§7.6 / §15.4).

Applies the workspace's chosen tone (technical / executive / regulator)
to drafted section bodies. Cross-cutting — invoked after
``attach_citations`` to rewrite section prose without altering claims,
evidence, or citations.

Per §11 RAG and chat experience: the agent applies geological-
narrative tone — "Report Builder mode where the report reads like a
geologist wrote it" (§11 reference).

Phase H4 graduation — deterministic tone-template prefixes + suffixes.
LLM-driven rewriting replaces the templates when §25.4 prompt locks
land; the output contract is preserved across both implementations.
The key invariant — every claim_id from the input set MUST appear in
the rewritten markdown — is checked + raised as ValueError if violated.

Output contract — see module docstring.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Literal
from uuid import UUID

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


Tone = Literal["technical", "executive", "regulator"]


# Tone-specific prologue / epilogue patterns. The body itself is
# preserved verbatim under deterministic mode so claim citations and
# inline `[chunk_id]` references stay intact. LLM mode (gated on §25.4)
# would rewrite the body but must respect the same claim-id invariant.
_TONE_PROLOGUE: dict[str, str] = {
    "technical": (
        "**Audience:** Technical geologists. Numbers + uncertainty "
        "ranges + citations are first-class throughout this section.\n\n"
    ),
    "executive": (
        "**Audience:** Executive — investor / board reader. The section "
        "leads with the conclusion and surfaces detail on demand.\n\n"
    ),
    "regulator": (
        "**Audience:** Regulator (NI 43-101 / CSA 11-348). Forward-"
        "looking statements are explicitly flagged; QP credentials and "
        "data provenance are cited inline.\n\n"
    ),
}

_TONE_EPILOGUE: dict[str, str] = {
    "technical": (
        "\n\n*— Citations refer to the §29.13 appendix index. "
        "Uncertainty values are 1-sigma unless otherwise stated.*"
    ),
    "executive": (
        "\n\n*— Backing detail in the technical sections + §29.13 "
        "appendix. Confidence indicators flagged inline.*"
    ),
    "regulator": (
        "\n\n*— This section is subject to the §29.15 forward-looking "
        "disclaimer and the QP sign-off block.*"
    ),
}


def _verify_claim_preservation(rewritten: str, claim_ids: list[str]) -> list[str]:
    """Returns the list of claim_ids that DON'T appear in the rewritten
    markdown. A non-empty result means the rewrite would have lost
    claims — that's a §15.4 invariant violation."""
    return [cid for cid in claim_ids if cid not in rewritten]


@georag_agent(
    name="Presentation Coach Agent",
    risk_tier="R1",  # Rewrites prose; claims + citations preserved
    version="1.0.0",  # graduated Phase H4
)
async def presentation_coach(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    section_id: str,
    body_markdown: str,
    tone: Tone,
    claim_ids: list[str],
) -> dict[str, Any]:
    """Apply workspace tone to a drafted section.

    Args:
        workspace_id: workspace context (RLS scope; tone config gate).
        section_id: section under coaching.
        body_markdown: drafted prose to rewrite. Must already contain
            inline ``[claim_id]`` markers for every claim_id.
        tone: which tone to apply.
        claim_ids: claims that must survive the rewrite (invariant).

    Returns:
        Rewritten markdown + tone marker. Claims preserved.

    Raises:
        ValueError if a claim_id from the input set isn't present in
        the body_markdown (caller misuse — the §7 graph guarantees
        claims are inlined before the coach runs).
    """
    if tone not in _TONE_PROLOGUE:
        raise ValueError(
            f"unknown tone={tone!r}; expected technical|executive|regulator"
        )

    # Pre-check: every claim_id must be in the input body. If a claim
    # isn't here, we can't possibly preserve it.
    missing_before = _verify_claim_preservation(body_markdown, claim_ids)
    if missing_before:
        raise ValueError(
            f"claim_ids missing from input body: {missing_before}. "
            f"The §7 graph must inline claim markers before "
            f"presentation_coach runs."
        )

    rewritten = (
        _TONE_PROLOGUE[tone]
        + body_markdown.strip()
        + _TONE_EPILOGUE[tone]
    )

    # Post-check: every claim_id must still be there.
    missing_after = _verify_claim_preservation(rewritten, claim_ids)
    if missing_after:  # pragma: no cover — deterministic mode preserves body
        raise RuntimeError(
            f"presentation_coach dropped claim_ids during rewrite: "
            f"{missing_after}. This is a §15.4 invariant violation."
        )

    summary = (
        f"section={section_id} tone={tone} "
        f"claims_preserved={len(claim_ids)} "
        f"input_chars={len(body_markdown)} output_chars={len(rewritten)}"
    )
    logger.info("presentation_coach: %s", summary)

    return {
        "section_id":          section_id,
        "rewritten_markdown":  rewritten,
        "preserved_claim_ids": list(claim_ids),
        "tone_applied":        tone,
        "summary":             summary,
    }


__all__ = ["presentation_coach", "Tone"]
