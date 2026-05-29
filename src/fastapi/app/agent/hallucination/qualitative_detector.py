"""Qualitative claim detection — catches vague geological assertions.

Complements Layer 3 (numerical verification) by detecting qualitative
claims that bypass numeric checks:

  - "significant mineralization" → what threshold defines "significant"?
  - "high-grade intersection" → how high?
  - "extensive alteration" → how extensive?

These claims are not rejected (they may be paraphrasing the NI 43-101
text), but they trigger a confidence penalty and are logged for review.
The penalty reduces the response confidence by 0.05 per detected
qualifier, capped at 0.2 total deduction.

Usage:
    from app.agent.hallucination.qualitative_detector import detect_qualitative_claims
    claims = detect_qualitative_claims(response_text)
    # Each claim: {"phrase": str, "severity": "high"|"medium"|"low"}
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Qualitative geological terms that imply interpretation without data.
# Ordered by severity — "significant" is more dangerous than "some".
_QUALITATIVE_PATTERNS: list[tuple[str, str]] = [
    # High severity — strong quantitative implication without a number
    (r"\bsignificant\s+(?:mineral|grade|resource|deposit)", "high"),
    (r"\bhigh[\s-]grade\b", "high"),
    (r"\blow[\s-]grade\b", "high"),
    (r"\beconomic\s+(?:mineral|grade|resource|deposit|potential)", "high"),
    (r"\bsubstantial\s+(?:mineral|resource|tonnage|grade)", "high"),
    (r"\bmajor\s+(?:deposit|discovery|resource|intersection)", "high"),

    # Medium severity — interpretive qualifiers
    (r"\bextensive\s+(?:alteration|mineral|zone)", "medium"),
    (r"\bmoderate\s+(?:grade|alteration|mineral)", "medium"),
    (r"\bpromising\s+(?:result|intersection|target)", "medium"),
    (r"\bfavourable\s+(?:geology|host|result|indication)", "medium"),
    (r"\banomalous\s+(?:value|result|zone|response)", "medium"),

    # Low severity — common but imprecise language
    (r"\belevated\s+(?:grade|value|response)", "low"),
    (r"\benriched\b", "low"),
    (r"\bdepleted\b", "low"),
    (r"\bwidespread\b", "low"),
]


def detect_qualitative_claims(text: str) -> list[dict]:
    """Detect qualitative geological claims in response text.

    Returns a list of dicts:
        {"phrase": str, "severity": "high"|"medium"|"low", "position": int}
    """
    claims: list[dict] = []
    seen_phrases: set[str] = set()

    for pattern, severity in _QUALITATIVE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            phrase = match.group(0).strip()
            if phrase.lower() not in seen_phrases:
                seen_phrases.add(phrase.lower())
                claims.append({
                    "phrase": phrase,
                    "severity": severity,
                    "position": match.start(),
                })

    if claims:
        logger.info(
            "qualitative_detector: %d qualitative claim(s) detected: %s",
            len(claims),
            ", ".join(f'"{c["phrase"]}" ({c["severity"]})' for c in claims),
        )

    return claims


def confidence_penalty(claims: list[dict]) -> float:
    """Calculate a confidence penalty for qualitative claims.

    Returns a float to subtract from the confidence score (0.0–0.2).
    """
    if not claims:
        return 0.0

    penalty = 0.0
    for claim in claims:
        if claim["severity"] == "high":
            penalty += 0.08
        elif claim["severity"] == "medium":
            penalty += 0.05
        else:
            penalty += 0.02

    return min(penalty, 0.2)  # cap at 0.2 deduction
