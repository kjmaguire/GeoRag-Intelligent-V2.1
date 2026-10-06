"""Prompt-injection sanitiser for the inbound user query.

``_call_llm`` runs every query through :func:`_sanitize_query` before it is
placed in the user message. It is defence in depth: the system prompt also
tells the model to ignore override attempts, and retrieved text is fenced
separately.

This used to live in ``query_classification.py`` beside the keyword
classifier of the retired deterministic orchestrator; that classifier had no
caller after the 2026-08-04 trim and was deleted in the 2026 full code review.
"""

from __future__ import annotations

import logging
import re

from prometheus_client import Counter

logger = logging.getLogger(__name__)

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|rules?|prompts?)",
    r"(system|admin|root)\s*:\s*",
    r"you\s+are\s+now\s+",
    r"forget\s+(everything|all)",
    r"override\s+(mode|safety|rules?)",
    r"<\s*/?\s*system\s*>",
    r"\[\s*INST\s*\]",
    r"```\s*(system|prompt)",
    # Eval 13 R3 additions — Markdown image / link exfiltration and
    # role-redirection attempts that the previous list missed.
    r"!\[[^\]]*\]\([^)]*\)",
    r"jailbreak",
    r"DAN\s+mode",
    r"pretend\s+(you|to\s+be)",
    r"act\s+as\s+(a\s+)?(system|developer|admin)",
]

#: Fires when ANY injection pattern matched, so the OPS dashboard surfaces the
#: attempt rate. Bucketed so cardinality is bounded: 1, 2-4, 5+.
_PROMPT_INJECTION_ATTEMPTS = Counter(
    "georag_prompt_injection_attempts_total",
    "Count of queries where the sanitiser stripped at "
    "least one known prompt-injection pattern. High "
    "rate from one workspace = either user education "
    "issue or active probing.",
    labelnames=("count_bucket",),
)


def _sanitize_query(query: str) -> str:
    """Sanitize user query to mitigate prompt injection attacks.

    Strips known injection patterns while preserving legitimate geological
    questions, caps the length, and counts the attempt on a Prometheus
    counter.
    """
    cleaned = query
    matches = 0
    for pattern in _INJECTION_PATTERNS:
        cleaned, n = re.subn(pattern, "", cleaned, flags=re.IGNORECASE)
        matches += n

    # Cap query length (geological questions rarely exceed 500 chars)
    cleaned = cleaned[:1000].strip()

    if matches:
        bucket = "1" if matches == 1 else "2-4" if matches < 5 else "5+"
        _PROMPT_INJECTION_ATTEMPTS.labels(count_bucket=bucket).inc()
        logger.warning(
            "_sanitize_query: stripped %d injection pattern(s) from inbound query (length=%d)",
            matches,
            len(query),
        )

    return cleaned if cleaned else query[:500]
