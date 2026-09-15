"""Cohere's own API chat backend — ``LLM_BACKEND=cohere`` (ADR-0023).

Cohere Command A+ over ``POST /v2/chat`` at ``api.cohere.com``, with an API
key. A sibling of ``llm_bedrock.call_bedrock_llm`` and
``_call_anthropic_llm``, dispatched from ``llm_calls._call_llm`` on backend.

Why this exists
---------------
ADR-0022 routed all four Cohere capabilities through Bedrock. On 2026-09-15
that was found not to hold: Command A+ and Parse 5 are **AWS Marketplace
SageMaker packages**, not Bedrock models, and their instance classes (A100 /
H100 for Command A+, ~$2.50/h for Parse) bill continuously because a
Marketplace endpoint has no idle state. ADR-0023 moves chat and parse to
Cohere's own API and leaves embeddings on Bedrock.

Async-native (hard rule 2): httpx's ``AsyncClient``, already a direct
dependency, so no new package. The streaming path yields to the event loop
between SSE frames, which is what keeps first-token latency an honest measure
— the same property the Bedrock and vLLM paths have.

----------------------------------------------------------------------------
WIRE SHAPE — [UNVERIFIED]
----------------------------------------------------------------------------
Written to Cohere's documented v2 contract and NOT yet confirmed against a
live call from this codebase. That is a materially better position than the
Bedrock adapter was in — Cohere's API is its own published, stable,
first-party contract rather than a Marketplace passthrough nobody had
exercised — but "better" is not "verified", and this module does not pretend
otherwise.

What is assumed:

  1. ``POST {base}/v2/chat`` with ``Authorization: Bearer``.
  2. ``messages`` uses OpenAI-ish roles, and **system is a message** with
     ``role: "system"`` — unlike Bedrock Converse, where system is a
     top-level parameter. Getting this backwards fails silently: a system
     prompt delivered as a user turn still produces plausible output.
  3. Non-streaming replies carry ``message.content`` as a LIST of typed
     blocks (``{"type": "text", "text": ...}``).
  4. Streaming is SSE with ``type``-tagged events, text arriving as
     ``content-delta`` and usage on ``message-end``.
  5. ``response_format: {"type": "json_object"}`` is honoured. Every
     typed-output guard in ``orchestrator_validators.py`` depends on this
     (hard rule 4), so it is the single most important thing to confirm.
  6. The ``<|START_TEXT|>`` / ``<|END_TEXT|>`` sentinels are a property of
     the MODEL, so they are stripped here exactly as on every other host —
     see ``llm_common.clean_model_text``.

Because (3) and (4) are assumptions, ``_extract_content`` and the stream
reader are deliberately TOLERANT: they accept several plausible spellings and
fall back rather than raising. A tolerant reader that returns the text is
worth more than a strict one that is right about the schema and returns
nothing. Where tolerance runs out, the failure is LOUD — see
``CohereResponseShapeError`` — because the alternative is the silent-blank-
page class of bug that ``cohere_parse_client`` shipped with until 2026-09-15.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.agent.llm_common import (
    BUDGET_EXHAUSTED_FALLBACK,
    cap_output_tokens,
    clean_model_text,
    record_llm_metrics,
)
from app.config import settings

logger = logging.getLogger(__name__)

#: HTTP statuses worth retrying BEFORE anything has been streamed. Mirrors
#: `llm_calls._PRE_STREAM_RETRYABLE_STATUS` rather than inventing a second
#: vocabulary for the same idea.
_PRE_STREAM_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_PRE_STREAM_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


class CoherePreStreamError(RuntimeError):
    """A Cohere failure that happened before any token reached the user.

    Mirrors ``BedrockPreStreamError`` and ``llm_calls._PreStreamTransientError``:
    retrying is safe only while nothing has been streamed. Once a delta has
    been forwarded, a failure propagates as itself so the caller degrades
    rather than emitting a second partial answer on top of the first.
    """


class CohereResponseShapeError(RuntimeError):
    """Cohere answered 200 with a body this adapter cannot read.

    Raised rather than returning "" on purpose. An empty string is
    indistinguishable from a model that legitimately had nothing to say, and
    that ambiguity is exactly the bug ``cohere_parse_client`` carried until
    2026-09-15: an unrecognised response shape produced a silently blank page
    that no metric could see. Since the wire shape here is [UNVERIFIED], this
    is the most likely way this module is wrong, and it fails loudly so the
    orchestrator's failover ladder can act on it.
    """


def _base_url() -> str:
    return (settings.COHERE_BASE_URL or "https://api.cohere.com").rstrip("/")


def _headers() -> dict[str, str]:
    key = (settings.COHERE_API_KEY or "").strip()
    if not key:
        raise RuntimeError(
            "COHERE_API_KEY is empty but LLM_BACKEND=cohere. It is written to "
            "Secrets Manager out of band (deploy/aws/README.md Step 3); ECS "
            "will not start a task referencing a key that does not exist, so "
            "reaching this line means the value is present but blank."
        )
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _build_request(
    *,
    user_message: str,
    system_content: str,
    temperature: float,
    max_output: int,
    response_format: str | None,
    stream: bool,
) -> dict[str, Any]:
    """Assemble the v2 chat body.

    System is a MESSAGE here, not a top-level field. That is the opposite of
    Bedrock Converse, and the difference does not announce itself: a system
    prompt sent as a user turn still returns fluent text, just without the
    grounding rules applied.
    """
    messages: list[dict[str, Any]] = []
    if system_content:
        messages.append({"role": "system", "content": system_content})
    messages.append({"role": "user", "content": user_message})

    body: dict[str, Any] = {
        "model": settings.effective_llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_output,
        "stream": stream,
    }
    if response_format == "json_object":
        # [UNVERIFIED] on this host. Hard rule 4 depends on it: every
        # citation and numeric guard in orchestrator_validators.py assumes
        # the model actually returns JSON when asked.
        body["response_format"] = {"type": "json_object"}
    return body


def _extract_content(payload: Any) -> str:
    """Pull the assistant text out of a non-streaming v2 reply.

    Tolerant by design — the shape is [UNVERIFIED], so this accepts the
    documented form and the plausible neighbours rather than betting on one.
    Raises rather than returning "" when nothing matches: see
    ``CohereResponseShapeError``.
    """
    if not isinstance(payload, dict):
        raise CohereResponseShapeError(f"expected a JSON object, got {type(payload).__name__}")

    message = payload.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        # Documented shape: a list of typed blocks.
        if isinstance(content, list):
            parts = [
                block["text"] for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            if parts:
                return "".join(parts)
        # Plausible neighbour: content as a bare string.
        if isinstance(content, str) and content:
            return content

    # Pre-v2 spelling, still worth accepting rather than failing a live call
    # over a field name.
    legacy_text = payload.get("text")
    if isinstance(legacy_text, str) and legacy_text:
        return legacy_text

    keys = sorted(payload)[:10]
    raise CohereResponseShapeError(
        f"no assistant text found in the Cohere reply (top-level keys={keys}). "
        "The wire shape is UNVERIFIED — run the probe and correct "
        "_extract_content from its report."
    )


def _extract_usage(payload: Any) -> tuple[int, int]:
    """Return ``(input_tokens, output_tokens)``, zero when absent.

    Usage is bookkeeping, not the answer, so an unreadable usage block
    degrades to zeros rather than failing the call — the same posture
    ``record_llm_metrics`` takes. Zeros are visible in the cost counters as a
    run that recorded nothing, which is the honest signal.
    """
    if not isinstance(payload, dict):
        return 0, 0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    # v2 nests the real counts under `tokens`; `billed_units` is the billing
    # view and can differ. Prefer tokens, fall back to the flat spelling.
    tokens = usage.get("tokens")
    source = tokens if isinstance(tokens, dict) else usage
    try:
        return (
            int(source.get("input_tokens", 0) or 0),
            int(source.get("output_tokens", 0) or 0),
        )
    except (TypeError, ValueError):
        # Degrading to zeros is right — usage is bookkeeping, not the answer,
        # and failing a generated answer over its accounting would be the
        # wrong trade. Saying nothing is not: `cost_burn_watcher` reads these
        # counters, and a workspace whose spend silently stops being recorded
        # looks exactly like a workspace that stopped asking questions.
        logger.debug(
            "cohere: usage block present but unreadable (keys=%s) — recording zero tokens for this call",
            sorted(source) if isinstance(source, dict) else type(source).__name__,
            exc_info=True,
        )
        return 0, 0


def _sse_events(line: str) -> dict[str, Any] | None:
    """Parse one SSE ``data:`` line into a dict, or None if it is not one."""
    if not line.startswith("data:"):
        return None
    raw = line[5:].strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        event = json.loads(raw)
    except ValueError:
        # A malformed frame is not worth failing a stream over; the text that
        # already arrived is still the answer.
        logger.debug("cohere: skipped an unparseable SSE frame")
        return None
    return event if isinstance(event, dict) else None


def _delta_text(event: dict[str, Any]) -> str | None:
    """Text carried by one streaming event, if any.

    Tolerant across the documented nesting
    (``delta.message.content.text``) and the flatter spellings, because the
    shape is [UNVERIFIED] and a missed delta is a silently truncated answer.
    """
    delta = event.get("delta")
    if isinstance(delta, dict):
        message = delta.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, dict):
                nested = content.get("text")
                if isinstance(nested, str):
                    return nested
            if isinstance(content, str):
                return content
        flat = delta.get("text")
        if isinstance(flat, str):
            return flat
    top_level = event.get("text")
    if isinstance(top_level, str):
        return top_level
    return None


async def call_cohere_llm(
    user_message: str,
    temperature: float,
    *,
    system_prompt: str | None = None,
    project_preamble: str | None = None,
    project_facts: str | None = None,
    user_id: str | None = None,
    token_callback: Callable[[str], Awaitable[None]] | None = None,
    response_format: str | None = None,
    max_retries: int = 2,
) -> str:
    """Call Cohere Command A+ on Cohere's API and return the answer text.

    Contract matches ``call_bedrock_llm``, ``_call_openai_compatible_llm``
    and ``_call_anthropic_llm``: returns the cleaned answer string, forwards
    every streamed delta to ``token_callback`` when one is supplied, and
    records token usage through ``llm_calls.add_token_usage`` so the run's
    cost accounting stays backend-independent.
    """
    from app.agent.llm_calls import add_token_usage  # noqa: PLC0415

    system_content = "\n\n".join(part for part in (system_prompt, project_preamble, project_facts) if part)
    requested_output = settings.COHERE_CHAT_MAX_TOKENS
    prompt_chars = len(system_content) + len(user_message)
    max_output = cap_output_tokens(
        max_output=requested_output,
        max_model_len=settings.COHERE_CHAT_MAX_MODEL_LEN,
        prompt_chars=prompt_chars,
    )
    if max_output < requested_output:
        logger.info(
            "call_cohere_llm: capping max_tokens %d -> %d (prompt_chars=%d max_model_len=%d)",
            requested_output,
            max_output,
            prompt_chars,
            settings.COHERE_CHAT_MAX_MODEL_LEN,
        )

    streaming = token_callback is not None
    body = _build_request(
        user_message=user_message,
        system_content=system_content,
        temperature=temperature,
        max_output=max_output,
        response_format=response_format,
        stream=streaming,
    )
    url = f"{_base_url()}/v2/chat"
    timeout = httpx.Timeout(settings.COHERE_CHAT_TIMEOUT_S, connect=10.0, read=settings.COHERE_CHAT_TIMEOUT_S)

    attempt = 0
    while True:
        sent_any_token = False
        content = ""
        input_tokens = output_tokens = 0
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if not streaming:
                    response = await client.post(url, headers=_headers(), json=body)
                    if response.status_code in _PRE_STREAM_RETRYABLE_STATUS:
                        raise CoherePreStreamError(f"HTTP {response.status_code} from Cohere before any output")
                    response.raise_for_status()
                    payload = response.json()
                    content = _extract_content(payload)
                    input_tokens, output_tokens = _extract_usage(payload)
                else:
                    chunks: list[str] = []
                    async with client.stream("POST", url, headers=_headers(), json=body) as response:
                        if response.status_code in _PRE_STREAM_RETRYABLE_STATUS:
                            raise CoherePreStreamError(f"HTTP {response.status_code} from Cohere before any output")
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            event = _sse_events(line)
                            if event is None:
                                continue
                            piece = _delta_text(event)
                            if piece:
                                chunks.append(piece)
                                # Forward BEFORE recording, so a callback that
                                # raises cannot leave the buffer ahead of what
                                # the user actually saw.
                                await token_callback(piece)  # type: ignore[misc]
                                sent_any_token = True
                                continue
                            if event.get("type") == "message-end" or "usage" in event:
                                got_in, got_out = _extract_usage(
                                    event.get("delta") if isinstance(event.get("delta"), dict) else event
                                )
                                input_tokens = got_in or input_tokens
                                output_tokens = got_out or output_tokens
                    content = "".join(chunks)
                    if not content:
                        # A stream that yielded no text at all is the
                        # streaming face of an unrecognised shape. Loud, for
                        # the same reason _extract_content is.
                        raise CohereResponseShapeError(
                            "the Cohere stream produced no text. The event shape is "
                            "UNVERIFIED — run the probe and correct _delta_text."
                        )
            break
        except Exception as exc:  # noqa: BLE001 — re-raised unless retryable
            retryable = isinstance(exc, (CoherePreStreamError, *_PRE_STREAM_RETRYABLE_EXCEPTIONS))
            if sent_any_token or not retryable or attempt >= max_retries:
                raise
            attempt += 1
            logger.warning(
                "cohere chat: transient %s (attempt %d/%d) — retrying before any token reached the user",
                type(exc).__name__,
                attempt,
                max_retries,
            )

    add_token_usage(input_tokens, output_tokens)
    record_llm_metrics(
        backend="cohere",
        model=settings.effective_llm_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        # Cohere's API reports no prompt-cache read counter, so this is zero
        # rather than assumed. The Bedrock path passes a real value when the
        # host supplies one; here there is nothing to pass.
        cache_read=0,
        user_id=user_id,
    )

    cleaned = clean_model_text(content, backend="cohere").strip()
    if not cleaned:
        logger.warning(
            "budget_exhausted_by_thinking: empty content after cleaning. "
            "backend=cohere model=%s max_tokens=%d input_tokens=%d.",
            settings.effective_llm_model,
            max_output,
            input_tokens,
        )
        return BUDGET_EXHAUSTED_FALLBACK
    return cleaned


__all__ = ["CoherePreStreamError", "CohereResponseShapeError", "call_cohere_llm"]
