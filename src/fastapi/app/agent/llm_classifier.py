"""LLM-based classifier fallback (→ A grade).

When the keyword classifier (`_classify_query` in orchestrator.py) falls
through to its generic "spatial + documents" fallback, we now ask a
FAST-tier LLM (Haiku by default) to re-classify the query into the same
category dict. The idea: keyword classifiers are expressive enough for
90% of queries but will always miss the long tail. A cheap LLM pass
recovers most of that tail before any retrieval runs.

Cost envelope
-------------
- Runs ONLY when the keyword classifier already hit `classifier_fallback`.
- One Haiku call, capped at 200 output tokens.
- Typical latency ~300ms; cost ~$0.00002 per call at Haiku rates.
- Disable with `LLM_CLASSIFIER_FALLBACK_ENABLED=false` to revert to
  keyword-only behaviour.

Output contract
---------------
Returns the same category dict shape the keyword classifier produces,
or None when the LLM is unavailable / parsing fails. The caller merges
the LLM's True flags into the existing categories dict — we never
DOWNGRADE a True back to False.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# Phase 13 Step 1 (R-P12-more-prompts) — the classifier system prompt
# was previously a module-level triple-quoted string here. It now lives
# at the canonical Phase 11 Step 3 path; see prompts/classifier_system.py.
from app.agent.prompts.classifier_system import (  # noqa: E402
    SYSTEM_PROMPT as _CLASSIFIER_SYSTEM_PROMPT,
)

# Default-all-false shape the caller expects.
_EMPTY_CATEGORIES: dict[str, bool] = {
    "spatial": False,
    "documents": False,
    "graph": False,
    "assay": False,
    "downhole": False,
    "targeting": False,
    "public_geo": False,
}


def _parse_classifier_json(text: str) -> dict[str, bool]:
    """Extract the category dict from an LLM JSON response.

    Returns _EMPTY_CATEGORIES on any parse failure — safer than raising
    because the caller falls through to keyword-classifier behaviour.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    match = re.search(r"\{[^{}]+\}", text, re.DOTALL)
    if not match:
        return dict(_EMPTY_CATEGORIES)
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return dict(_EMPTY_CATEGORIES)

    out = dict(_EMPTY_CATEGORIES)
    for key in out:
        value = payload.get(key)
        if isinstance(value, bool):
            out[key] = value
    return out


async def classify_via_llm(
    query: str,
    *,
    anthropic_client: Any = None,
    model: str | None = None,
) -> dict[str, bool] | None:
    """Ask a FAST-tier LLM to classify a keyword-fallback query.

    Returns None when:
      - escalation disabled by settings
      - no anthropic_client available
      - the LLM call timed out or errored

    Returns an empty dict (all False) when the LLM responded but no
    buckets matched. The caller treats "no buckets matched" the same as
    the keyword fallback behaviour: route to spatial+documents.
    """
    if not getattr(settings, "LLM_CLASSIFIER_FALLBACK_ENABLED", True):
        return None
    if anthropic_client is None:
        return None

    try:
        msg = await anthropic_client.messages.create(
            model=model or settings.MODEL_TIER_FAST,
            max_tokens=200,
            temperature=0.0,   # deterministic routing
            system=[{"type": "text", "text": _CLASSIFIER_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": query}],
            timeout=min(5.0, float(settings.TIMEOUT_GATHER_S)),
        )
    except httpx.TimeoutException:
        logger.info("classify_via_llm: anthropic timeout — keyword fallback preserved")
        return None
    except Exception as exc:
        logger.warning(
            "classify_via_llm: anthropic error (non-fatal): %s", exc.__class__.__name__
        )
        return None

    text_parts: list[str] = []
    for block in getattr(msg, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text_parts.append(getattr(block, "text", ""))
    raw = "".join(text_parts)
    parsed = _parse_classifier_json(raw)

    matched = [k for k, v in parsed.items() if v]
    from app.agent.log_safe import query_hash  # noqa: PLC0415
    logger.info(
        "classify_via_llm: query_hash=%s llm_categories=%s",
        query_hash(query),
        matched,
    )
    return parsed
