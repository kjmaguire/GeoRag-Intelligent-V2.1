"""Bounded escalation: LLM-powered query rephrasing when the keyword
classifier falls through to empty results (R9).

Design
------
When `classifier_fallback AND all_tools_empty` fires — the signature of a
query the keyword classifier can't route — we ask the LLM to propose a
handful of alternative phrasings and retry the deterministic tool dispatch
against each one. First non-empty result wins.

This is a bounded stepping-stone toward the full agentic Pydantic AI path
the plan mentions for when telemetry justifies it. It's also the smallest
useful thing: a single LLM rephrasing round-trip, deterministic tool
dispatch on each candidate, latency bounded by
MAX_REPHRASINGS * parallel tool fan-out.

Deliberately NOT in scope here
------------------------------
- Multi-turn tool-calling agent (Pydantic AI agent) — that's R9-full.
- Cross-project retrieval — scope stays per-project.
- Learning from escalation outcomes — we emit a structured log line so a
  future harvester can use it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

# Phase 12 Step 2 (R-P11-prompts-migrate) — the rephrase system prompt
# was previously a module-level constant inline below. It now lives at
# the canonical Phase 11 Step 3 path so the pre-commit
# `system-prompt-version-bump` hook can enforce version bookkeeping.
from app.agent.prompts.rephrase_system import (
    SYSTEM_PROMPT as _REPHRASE_SYSTEM_PROMPT,
)
from app.config import settings

logger = logging.getLogger(__name__)


def _parse_rephrasings_json(text: str, max_count: int) -> list[str]:
    """Extract the `rephrasings` list from an LLM JSON response.

    Tolerates leading chatter, trailing prose, and code-fence wrappers —
    small models sometimes ignore "nothing else" instructions.
    """
    text = text.strip()
    # Strip code fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    # Find the first JSON object in the output.
    match = re.search(r"\{[^{}]*\"rephrasings\"[^{}]*\}", text, re.DOTALL)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    items = payload.get("rephrasings") or []
    if not isinstance(items, list):
        return []
    cleaned = [str(s).strip() for s in items if isinstance(s, str) and s.strip()]
    return cleaned[:max_count]


async def rephrase_query(
    query: str,
    *,
    attempted_tools: list[str] | None = None,
    anthropic_client: Any = None,
    model: str | None = None,
    max_rephrasings: int | None = None,
) -> list[str]:
    """Ask the LLM for up to `max_rephrasings` alternative phrasings.

    Returns an empty list when:
      - escalation is disabled by settings
      - the anthropic_client is absent (non-anthropic deploys — we
        deliberately don't open a second OpenAI-compat client here to
        keep the blast radius small; OpenAI-compat can call this via
        `anthropic_client=None`+ `base_url=...` in a follow-up)
      - the LLM response can't be parsed

    On any failure the caller falls through to the original empty-tool
    path — rephrasing is strictly additive, never blocking.
    """
    if not getattr(settings, "AGENTIC_ESCALATION_ENABLED", True):
        return []
    if anthropic_client is None:
        return []

    cap = max_rephrasings if max_rephrasings is not None else getattr(
        settings, "AGENTIC_ESCALATION_MAX_REPHRASINGS", 2
    )
    if cap <= 0:
        return []

    attempted_desc = (
        f"The keyword classifier already tried these tools and got no results: "
        f"{', '.join(attempted_tools or ['(none)'])}."
    )
    user_content = (
        f"Original query: {query}\n\n{attempted_desc}\n\n"
        f"Propose up to {cap} alternative phrasings likely to match keywords "
        f"in a geological database or technical-report index."
    )

    try:
        msg = await anthropic_client.messages.create(
            model=model or settings.MODEL_TIER_FAST,
            max_tokens=400,
            temperature=0.4,  # a little variation to diversify phrasings
            system=[{"type": "text", "text": _REPHRASE_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": user_content}],
            timeout=min(6.0, float(settings.TIMEOUT_GATHER_S)),
        )
    except httpx.TimeoutException:
        logger.info("rephrase_query: anthropic timeout — returning no rephrasings")
        return []
    except Exception as exc:
        logger.warning(
            "rephrase_query: anthropic error (non-fatal): %s", exc.__class__.__name__
        )
        return []

    text_parts: list[str] = []
    for block in getattr(msg, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text_parts.append(getattr(block, "text", ""))
    raw = "".join(text_parts).strip()
    rephrasings = _parse_rephrasings_json(raw, cap)
    # Defensive: drop anything that's the literal original query.
    rephrasings = [r for r in rephrasings if r.strip().lower() != query.strip().lower()]
    from app.agent.log_safe import query_hash  # noqa: PLC0415
    logger.info(
        "rephrase_query: produced %d rephrasings for query_hash=%s",
        len(rephrasings),
        query_hash(query),
    )
    return rephrasings
