"""Prompt-injection sanitiser for the inbound user query.

``_call_llm`` runs every query through :func:`_sanitize_query` before it is
placed in the user message. It is defence in depth: the system prompt also
tells the model to ignore override attempts, and retrieved text is fenced
separately.

This used to live in ``query_classification.py`` (deleted 2026-10-06) beside the keyword
classifier of the retired deterministic orchestrator; that classifier had no
caller after the 2026-08-04 trim and was deleted in the 2026 full code review.
"""

from __future__ import annotations

import logging
import re

from prometheus_client import Counter

logger = logging.getLogger(__name__)

#: The longest question the API accepts: ``QueryRequest.query`` in
#: ``app.routers.queries`` takes its ``max_length`` from here, so the two
#: cannot drift apart again. (Laravel's StoreQueryRequest is tighter still,
#: ``max:2000``, and is the limit a user meets.) The sanitiser used to cut at
#: 1000 characters, silently, on the grounds that "geological questions rarely
#: exceed 500": a 1,500-character question passed both front doors and lost
#: its last third -- usually the actual ask -- before the model saw it
#: (2026-10-10 audit, AL-15).
MAX_QUERY_CHARS = 4096

#: The longest string :func:`_sanitize_query` is handed for a question the API
#: accepted. ``nodes._question_for_llm`` passes the user's own words and, when
#: the query was rewritten against the conversation, a labelled copy of the
#: rewrite after them -- up to two questions and the label. A cap at
#: MAX_QUERY_CHARS alone would cut that rewrite mid-sentence for a long
#: question. The cap is a backstop for callers that bypass the router, not a
#: limit on users, and it says so (a WARNING with lengths) when it bites.
MAX_LLM_QUESTION_CHARS = 2 * MAX_QUERY_CHARS + 256

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|rules?|prompts?)",
    # A chat-transcript role label opening a LINE: "system: you are now ...",
    # "> Admin: ...", "**System:** ...". Anchored to the start of a line (with
    # optional quote / list / emphasis marks in front) because the unanchored
    # `(system|admin|root)\s*:\s*` it replaces ate the word and its colon
    # anywhere: "the hydrothermal system: faults or contacts?" lost "system:",
    # "ecosystem: x" became "eco" (2026-10-10 audit, RAG-21). A role word
    # that is not the first thing on its line is prose. The payload after a
    # mid-line label ("Thanks. System: you are now free") is still caught by
    # the patterns below; only the harmless label survives.
    r"(?m)^[ \t>*_#-]*(?:system|admin|root)[ \t]*:[ \t*_]*",
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
    questions, caps the length at :data:`MAX_LLM_QUESTION_CHARS` (logging, not
    silently, if it has to), and counts the attempt on a Prometheus counter.
    """
    cleaned = query
    matches = 0
    for pattern in _INJECTION_PATTERNS:
        cleaned, n = re.subn(pattern, "", cleaned, flags=re.IGNORECASE)
        matches += n

    if len(cleaned) > MAX_LLM_QUESTION_CHARS:
        # Lengths only: the question itself is the user's, and does not belong
        # in a log line.
        logger.warning(
            "_sanitize_query: question cut from %d to %d characters (the API "
            "accepts %d per question) -- the tail of it was NOT sent to the model",
            len(cleaned),
            MAX_LLM_QUESTION_CHARS,
            MAX_QUERY_CHARS,
        )
        cleaned = cleaned[:MAX_LLM_QUESTION_CHARS]
    cleaned = cleaned.strip()

    if matches:
        bucket = "1" if matches == 1 else "2-4" if matches < 5 else "5+"
        _PROMPT_INJECTION_ATTEMPTS.labels(count_bucket=bucket).inc()
        logger.warning(
            "_sanitize_query: stripped %d injection pattern(s) from inbound query (length=%d)",
            matches,
            len(query),
        )

    return cleaned if cleaned else query[:500]
