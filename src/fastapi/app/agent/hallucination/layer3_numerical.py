"""Layer 3 — Numerical Claim Verification.

Architecture reference: Section 04i, Layer 3.

Purpose
-------
Catch the hallucination we observed: the agent called PostGIS and got 10 drill
holes back, but then stated "There are 2459 drill holes" in its response.
The LLM invented a number that was not present in any tool call result.

This validator:
  1. Parses every integer and float out of the response text using a regex.
  2. Walks ctx.messages to collect every number that appeared in a tool call
     result (counts, depths, grades, coordinates, IDs with embedded digits, etc.)
  3. For each number found in the response text, checks whether it can be
     traced back to at least one tool result value.
  4. If a number cannot be grounded, raises ModelRetry with a correction hint
     that names the ungrounded number and points the agent to the correct source.

Design decisions
----------------
- We check against ALL numbers extracted from ALL tool results, not just
  perfectly matched values.  A number is "grounded" if it appears literally
  in any tool result — as an integer, float, count, or within a string
  (e.g. a depth "350.0" would match the float 350.0).

- Very small numbers (0, 1) and numbers that appear only in citation markers
  like "[DATA-1]" are excluded from verification — they are formatting
  artefacts, not factual claims.

- The validator is disabled when settings.NUMERICAL_VERIFICATION_ENABLED is
  False, allowing temporary bypass in development without a code change.

Pydantic AI output_validator
-----------------------------
The function signature must be:

    async def verify_numerical_claims(
        ctx: RunContext[AgentDeps],
        output: GeoRAGResponse,
    ) -> GeoRAGResponse:

Registered in geo_agent.py with ``@geo_agent.output_validator``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass

from pydantic_ai import ModelRetry, RunContext

from app.agent.deps import AgentDeps
from app.config import settings
from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)

# Matches integers and decimals. Citation markers like [DATA-1] are stripped
# upstream by _CITATION_MARKER_RE before this regex is applied, so no lookbehind
# is needed here (Python's re module doesn't support variable-width lookbehinds).
_NUMBER_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")

# Citation marker pattern — numbers that are part of [DATA-X] should not be
# verified because they are reference indices, not factual claims.
# Audit 2026-06-27 (T3): accept BOTH the colon form ([DATA:1]) that the prompts
# instruct the model to emit (canonical, Kyle 2026-04-22) and the dash form
# ([DATA-1]) the assembler appends, plus the PGEO prefix. Previously only dash
# matched, so colon citation indices leaked through as "ungrounded numbers" and
# inflated the Layer 3 false-positive / retry rate.
_CITATION_MARKER_RE = re.compile(r"\[(?:DATA|NI43|PUB|PGEO)[:-]\d+\]")

# Numbers so small they're almost certainly formatting rather than facts.
# 0 and 1 appear in too many innocuous contexts (list indices, boolean-like
# fields, "0 results") to make useful verification targets.
_SKIP_VALUES: frozenset[float] = frozenset({0.0, 1.0})


def _extract_numbers_from_text(text: str) -> list[float]:
    """Extract all standalone numeric values from response text.

    Excludes numbers embedded in citation markers like [DATA-1].
    Excludes 0 and 1 as too common to verify usefully.
    Returns deduplicated list preserving first-occurrence order.
    """
    # Blank out citation markers so their digits are not extracted.
    clean = _CITATION_MARKER_RE.sub("", text)

    seen: set[float] = set()
    result: list[float] = []
    for m in _NUMBER_RE.finditer(clean):
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        if val in _SKIP_VALUES:
            continue
        if val not in seen:
            seen.add(val)
            result.append(val)
    return result


def _flatten_tool_result_to_numbers(obj: object, _depth: int = 0) -> set[float]:
    """Recursively walk a tool result object and collect all numeric values.

    Handles:
    - Primitive int / float
    - str that can be parsed as a number (e.g. "350.0")
    - list / tuple — recurse into each element
    - dict — recurse into values
    - dataclass — recurse into field values
    - Pydantic BaseModel — recurse into model_dump() values
    - Pydantic AI message / tool-result objects (access .content or .__dict__)

    Depth limit prevents infinite recursion on pathological objects.
    """
    numbers: set[float] = set()

    if _depth > 10:
        return numbers

    if isinstance(obj, bool):
        # bool is a subclass of int; skip it — True/False are not factual claims.
        return numbers

    if isinstance(obj, (int, float)):
        numbers.add(float(obj))
        return numbers

    if isinstance(obj, str):
        # Try parsing the whole string as a number (e.g. "350.0", "10").
        stripped = obj.strip()
        try:
            numbers.add(float(stripped))
        except ValueError:
            # Also scan the string for embedded numbers (e.g. collar IDs,
            # SQL query strings that contain the verified value).
            for m in _NUMBER_RE.finditer(stripped):
                with contextlib.suppress(ValueError):
                    numbers.add(float(m.group(1)))
        return numbers

    if isinstance(obj, (list, tuple)):
        for item in obj:
            numbers |= _flatten_tool_result_to_numbers(item, _depth + 1)
        return numbers

    if isinstance(obj, dict):
        for v in obj.values():
            numbers |= _flatten_tool_result_to_numbers(v, _depth + 1)
        return numbers

    if is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclass_fields(obj):  # type: ignore[arg-type]
            numbers |= _flatten_tool_result_to_numbers(getattr(obj, f.name), _depth + 1)
        return numbers

    # Pydantic BaseModel
    try:
        numbers |= _flatten_tool_result_to_numbers(obj.model_dump(), _depth + 1)  # type: ignore[union-attr]
        return numbers
    except AttributeError:
        pass

    # Fallback: try __dict__
    with contextlib.suppress(TypeError):
        numbers |= _flatten_tool_result_to_numbers(vars(obj), _depth + 1)

    return numbers


def _collect_grounded_numbers(ctx: RunContext[AgentDeps]) -> set[float]:
    """Walk all messages in ctx to collect every number from tool call results.

    Pydantic AI stores the full message history in ctx.messages.  Each message
    is a ModelMessage subtype.  Tool return values appear as ToolReturnPart
    objects whose ``content`` is a JSON string of the serialised result.

    We walk every message part whose kind is "tool-return" and deserialise its
    content to extract numbers.  We also include the count field from
    SpatialQueryResult explicitly since that is the most important value for
    count-style queries.
    """
    grounded: set[float] = set()

    for message in ctx.messages:
        # Pydantic AI messages expose .parts as a list of message part objects.
        parts = getattr(message, "parts", None)
        if parts is None:
            continue
        for part in parts:
            kind = getattr(part, "part_kind", None) or getattr(part, "kind", None)
            if kind != "tool-return":
                continue
            content = getattr(part, "content", None)
            if content is None:
                continue
            # content is a JSON string when the tool returns a dataclass/dict.
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    grounded |= _flatten_tool_result_to_numbers(parsed)
                except (json.JSONDecodeError, TypeError):
                    # Not JSON — scan the raw string for numbers.
                    grounded |= _flatten_tool_result_to_numbers(content)
            else:
                grounded |= _flatten_tool_result_to_numbers(content)

    logger.debug(
        "layer3_numerical: collected %d grounded numbers from tool results: %s",
        len(grounded),
        sorted(grounded)[:30],  # log at most 30 to avoid flooding
    )

    return grounded


def _is_grounded(value: float, grounded: set[float], tolerance: float = 0.01) -> bool:
    """Return True if value appears (within tolerance) in the grounded number set."""
    if value in grounded:
        return True
    return any(abs(value - g) <= tolerance for g in grounded)


async def verify_numerical_claims(
    ctx: RunContext[AgentDeps],
    output: GeoRAGResponse,
) -> GeoRAGResponse:
    """Output validator: verify every number in the response text against tool results.

    This is hallucination prevention Layer 3. It catches the exact failure mode
    we observed: the agent reported 2459 drill holes when the PostGIS tool
    returned 10.

    Registration: @geo_agent.output_validator in geo_agent.py.

    Raises:
        ModelRetry: if any number in output.text cannot be traced to a tool
            result.  The retry message names the ungrounded number and
            instructs the agent to use only tool-sourced values.

    Returns:
        The unchanged output if all numbers are grounded.
    """
    if not settings.NUMERICAL_VERIFICATION_ENABLED:
        logger.debug("layer3_numerical: disabled via settings — skipping")
        return output

    numbers_in_text = _extract_numbers_from_text(output.text)

    if not numbers_in_text:
        logger.debug("layer3_numerical: no verifiable numbers found in response text")
        return output

    grounded = _collect_grounded_numbers(ctx)

    ungrounded: list[float] = []
    for num in numbers_in_text:
        verified = _is_grounded(num, grounded)
        logger.debug(
            "layer3_numerical: %.4f — %s",
            num,
            "VERIFIED" if verified else "UNGROUNDED",
        )
        if not verified:
            ungrounded.append(num)

    if ungrounded:
        ungrounded_str = ", ".join(
            str(int(n)) if n == int(n) else str(n) for n in ungrounded
        )
        logger.warning(
            "layer3_numerical: %d ungrounded number(s) in response: %s — "
            "raising ModelRetry",
            len(ungrounded),
            ungrounded_str,
        )
        # Build a targeted correction message.  For count-style queries we
        # explicitly name the 'count' field of SpatialQueryResult so the LLM
        # knows where to look.
        raise ModelRetry(
            f"The following number(s) in your response could not be verified "
            f"against any tool call result: {ungrounded_str}.\n\n"
            f"You MUST only state numbers that were returned by a tool call. "
            f"Rules:\n"
            f"- For drill-hole counts, use the 'count' field from the "
            f"tool_query_spatial_collars result (not a number you generate "
            f"yourself).\n"
            f"- For depths, grades, or coordinates, use the exact values "
            f"from the CollarRecord or sample records.\n"
            f"- If the tool returned no data, say 'insufficient information' "
            f"rather than guessing.\n\n"
            f"Re-read the tool results and rewrite your response using ONLY "
            f"the numbers present in those results."
        )

    logger.debug(
        "layer3_numerical: all %d number(s) verified against tool results",
        len(numbers_in_text),
    )
    return output
