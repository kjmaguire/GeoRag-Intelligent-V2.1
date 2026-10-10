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
Marketplace endpoint has no idle state. ADR-0023 moved chat and parse to
Cohere's own API (embeddings followed under ADR-0025).

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
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.agent.llm_common import (
    BUDGET_EXHAUSTED_FALLBACK,
    cap_output_tokens,
    clean_model_text,
    is_truncated_finish_reason,
    note_truncated_generation,
    parse_retry_after,
    record_llm_metrics,
    wait_before_pre_stream_retry,
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
#: The same list minus ``ReadTimeout``, for the NON-streaming call. A read
#: timeout there means Cohere accepted the request and was still generating
#: when the client gave up -- up to COHERE_CHAT_MAX_TOKENS of output that is
#: billed whether or not anyone reads it. Re-issuing blindly paid for the
#: same answer up to three times (VEN-2, 2026-09-29). On the streaming call a
#: read timeout before the first event is still retried: nothing arrived at
#: all, which is far more likely a stalled connection than a slow answer.
_NON_STREAM_RETRYABLE_EXCEPTIONS = tuple(
    exc for exc in _PRE_STREAM_RETRYABLE_EXCEPTIONS if exc is not httpx.ReadTimeout
)


class CoherePreStreamError(RuntimeError):
    """A Cohere failure that happened before any token reached the user.

    Mirrors ``llm_calls._PreStreamTransientError``:
    retrying is safe only while nothing has been streamed. Once a delta has
    been forwarded, a failure propagates as itself so the caller degrades
    rather than emitting a second partial answer on top of the first.

    ``retry_after_s`` carries the host's ``Retry-After`` when it sent one,
    so the retry waits at least that long instead of earning another 429.
    """

    def __init__(self, message: str, *, status: int | None = None, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


def _pre_stream_error(response: httpx.Response) -> CoherePreStreamError:
    return CoherePreStreamError(
        f"HTTP {response.status_code} from Cohere before any output",
        status=response.status_code,
        retry_after_s=parse_retry_after(response.headers.get("retry-after")),
    )


class _ThinkingRejectedError(RuntimeError):
    """Cohere returned 400/422 naming the ``thinking`` field."""


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


def _headers(*, stream: bool = False) -> dict[str, str]:
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
        # A stream asks for an event stream. The first live run (2026-09-23)
        # sent `application/json` on the streaming call too and parsed ZERO
        # `data:` frames from a 200; Cohere's own SDK sends no Accept header
        # and reads SSE. The reader below also takes the other framings, so
        # this is the likely cause rather than the only defence.
        "Accept": "text/event-stream" if stream else "application/json",
    }


def _build_request(
    *,
    user_message: str,
    system_content: str,
    temperature: float,
    max_output: int,
    response_format: str | None,
    stream: bool,
    thinking_budget: int | None = None,
) -> dict[str, Any]:
    """Assemble the v2 chat body.

    System is a MESSAGE here, not a top-level field. That is the opposite of
    Bedrock Converse, and the difference does not announce itself: a system
    prompt sent as a user turn still returns fluent text, just without the
    grounding rules applied.

    ``thinking_budget``: None sends no ``thinking`` field (model default),
    0 disables reasoning, a positive value caps it. [UNVERIFIED] on this
    model -- see ``_thinking_rejected`` for what happens if it is refused.
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
    if thinking_budget is not None:
        body["thinking"] = (
            {"type": "enabled", "token_budget": thinking_budget} if thinking_budget > 0 else {"type": "disabled"}
        )
    return body


def _thinking_budget(max_output: int) -> int | None:
    """The reasoning cap to send, from ``COHERE_CHAT_THINKING_BUDGET``.

    Never more than half of ``max_output``: reasoning spends from the same
    budget first, so a cap at or above it is no cap on the failure it exists
    to prevent -- an answer-less reply.
    """
    configured = settings.COHERE_CHAT_THINKING_BUDGET
    if configured < 0:
        return None
    if configured == 0:
        return 0
    return max(1, min(configured, max_output // 2))


def _thinking_rejected(status: int, body: dict[str, Any], detail: str) -> bool:
    """True when Cohere refused the request over its ``thinking`` field.

    The field is [UNVERIFIED] on Command A+. A 400/422 that names it is
    retried once without it, loudly, rather than failing every chat: an
    uncapped-reasoning answer is worse than a capped one but much better
    than none.
    """
    return status in (400, 422) and "thinking" in body and "thinking" in detail.lower()


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
        # Reasoning blocks and no text: the model spent max_tokens thinking.
        # That is a budget outcome with its own handling in call_cohere_llm
        # (BUDGET_EXHAUSTED_FALLBACK), not an unrecognised shape, so it must
        # not raise the error that tells an operator to re-probe the wire.
        if isinstance(content, list) and any(
            isinstance(block, dict) and isinstance(block.get("thinking"), str) for block in content
        ):
            return ""

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


def _extract_finish_reason(payload: Any) -> str | None:
    """The v2 ``finish_reason`` of a non-streaming reply (COMPLETE, MAX_TOKENS, ERROR ...).

    None when absent or not a string: a reply that does not say why it stopped
    is not evidence that it was cut off.
    """
    if not isinstance(payload, dict):
        return None
    reason = payload.get("finish_reason")
    return reason if isinstance(reason, str) else None


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
    """Parse one streamed line into an event dict, or None if it is not one.

    An SSE ``data:`` line is the documented framing. A line that is itself a
    JSON object is newline-delimited JSON — how Cohere's v1 API streamed, and
    one candidate for what the 2026-09-23 run received when it parsed no
    ``data:`` frame at all. ``event:``/``id:``/comment lines carry nothing
    this reader needs (the type is inside the JSON) and return None.
    """
    if line.startswith("data:"):
        raw = line[5:].strip()
    elif line.lstrip().startswith("{"):
        raw = line.strip()
    else:
        return None
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


def _shape_fingerprint(value: Any, *, _depth: int = 0) -> Any:
    """Describe a value's STRUCTURE — keys and types, never contents.

    Exists so that "the stream produced no text" carries the evidence needed
    to fix it. The 2026-09-18 rehearsal hit exactly that error against the
    live deployment and the adapter threw away the events it had just seen,
    so the only way forward was a separate credentialed probe run — on an API
    this sandbox cannot reach. One self-describing failure beats a second
    round trip.

    VALUES ARE NEVER INCLUDED. The deltas carry workspace text, and an
    operator reading CloudWatch to debug a wire shape must not thereby read a
    tenant's geology. Strings collapse to ``str(len)``, numbers to their type
    name. That is enough to tell ``delta.message.content.text`` from
    ``delta.message.content[].text`` while disclosing nothing.
    """
    if _depth > 6:
        return "..."
    if isinstance(value, dict):
        return {k: _shape_fingerprint(v, _depth=_depth + 1) for k, v in list(value.items())[:12]}
    if isinstance(value, list):
        # One representative element: a 400-delta stream must not print 400
        # identical fingerprints.
        head = _shape_fingerprint(value[0], _depth=_depth + 1) if value else None
        return [head, f"...x{len(value)}"] if len(value) > 1 else [head]
    if isinstance(value, str):
        return f"str({len(value)})"
    return type(value).__name__


#: How much unframed body the stream reader keeps for the whole-body
#: fallback and the error message. A real reply to one question is far
#: below this; anything larger is not a reply this path should buffer.
_UNFRAMED_LIMIT = 2_000_000


def _is_whole_reply(event: dict[str, Any]) -> bool:
    """A complete non-streaming reply, rather than a stream event.

    Stream events carry a ``type`` and put their payload under ``delta``; a
    v2 reply has neither and carries ``message`` at the top level.
    """
    return "type" not in event and "delta" not in event and isinstance(event.get("message"), dict)


def _whole_body_reply(text: str) -> dict[str, Any] | None:
    """The body as one JSON reply, if that is what it is."""
    try:
        payload = json.loads(text)
    except ValueError:
        # Expected when the body is not one JSON document; the caller then
        # raises with the framing evidence, which is the useful message.
        logger.debug("cohere: unframed stream body is not a single JSON reply", exc_info=True)
        return None
    return payload if isinstance(payload, dict) and _is_whole_reply(payload) else None


def _line_shape(line: str) -> str:
    """Name a line's framing without quoting it — it may carry answer text.

    An SSE field name (``event``, ``id``, ``retry``) is safe to print and is
    exactly the evidence needed; anything else is described by its first
    character's class and its length.
    """
    field, sep, _ = line.partition(":")
    if sep and field.isidentifier() and len(field) <= 16:
        return f"{field}:…({len(line)})"
    head = line[:1]
    kind = "brace" if head in "{[" else "alnum" if head.isalnum() else "punct" if head else "empty"
    return f"{kind}…({len(line)})"


def _delta_text(event: dict[str, Any]) -> str | None:
    """Text carried by one streaming event, if any.

    Tolerant across the documented nesting
    (``delta.message.content.text``) and the flatter spellings, because the
    shape is [UNVERIFIED] and a missed delta is a silently truncated answer.

    ``content`` as a LIST of typed blocks is handled here because
    ``_extract_content`` — the non-streaming sibling twenty lines down —
    already handles exactly that, and has since it was written. The two
    parsers disagreeing about the same field on the same API was an
    asymmetry, not a decision: whichever shape the host actually sends, only
    one of the two paths would have worked.
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
            if isinstance(content, list):
                parts = [
                    block["text"] for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)
                ]
                if parts:
                    return "".join(parts)
            if isinstance(content, str):
                return content
            # Some hosts put the assistant turn under `text` beside `content`.
            message_text = message.get("text")
            if isinstance(message_text, str):
                return message_text
        flat = delta.get("text")
        if isinstance(flat, str):
            return flat
    top_level = event.get("text")
    if isinstance(top_level, str):
        return top_level
    return None


def _delta_thinking(event: dict[str, Any]) -> str | None:
    """Reasoning text carried by one streaming event, if any.

    The Cohere SDK types ``content-delta`` as
    ``delta.message.content = {"text": str | None, "thinking": str | None}``,
    and reasoning is on by default for models that support it — so a
    reasoning model streams ``thinking`` deltas before any ``text``. They are
    never forwarded to the user; they are counted so that a stream made of
    nothing else is recognised as the budget running out mid-thought rather
    than misreported as an unrecognised wire shape.
    """
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None
    message = delta.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, dict):
        thinking = content.get("thinking")
        if isinstance(thinking, str):
            return thinking
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
        thinking_budget=_thinking_budget(max_output),
    )
    url = f"{_base_url()}/v2/chat"
    timeout = httpx.Timeout(settings.COHERE_CHAT_TIMEOUT_S, connect=10.0, read=settings.COHERE_CHAT_TIMEOUT_S)

    attempt = 0
    started = time.monotonic()
    retryable_exceptions = _PRE_STREAM_RETRYABLE_EXCEPTIONS if streaming else _NON_STREAM_RETRYABLE_EXCEPTIONS
    while True:
        sent_any_token = False
        content = ""
        input_tokens = output_tokens = 0
        finish_reason: str | None = None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if not streaming:
                    response = await client.post(url, headers=_headers(), json=body)
                    if response.status_code in _PRE_STREAM_RETRYABLE_STATUS:
                        raise _pre_stream_error(response)
                    if _thinking_rejected(response.status_code, body, response.text):
                        raise _ThinkingRejectedError(response.text[:300])
                    if response.status_code >= 400:
                        logger.error(
                            "cohere chat: refused with HTTP %d: %s",
                            response.status_code,
                            response.text[:300],
                        )
                        raise httpx.HTTPStatusError(
                            f"HTTP {response.status_code} from Cohere /v2/chat: {response.text[:300]}",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    payload = response.json()
                    content = _extract_content(payload)
                    input_tokens, output_tokens = _extract_usage(payload)
                    finish_reason = _extract_finish_reason(payload)
                else:
                    chunks: list[str] = []
                    # Kept for the error path only — see _shape_fingerprint.
                    seen_types: list[str] = []
                    seen_shapes: list[Any] = []
                    saw_any_event = False
                    thinking_chars = 0
                    # Lines that were not events, kept (bounded) in case the
                    # whole body is one pretty-printed JSON reply — a host
                    # that ignored `stream`. Also the evidence if nothing
                    # was readable at all.
                    unframed: list[str] = []
                    unframed_chars = 0
                    content_type = ""
                    async with client.stream("POST", url, headers=_headers(stream=True), json=body) as response:
                        if response.status_code in _PRE_STREAM_RETRYABLE_STATUS:
                            raise _pre_stream_error(response)
                        if response.status_code >= 400:
                            # Read the body before raising: Cohere's error
                            # text is the only thing that says WHY (a context
                            # overflow answers 400 rather than truncating),
                            # and raise_for_status() on a stream discards it
                            # (VEN-17). It carries no key; at most it echoes
                            # what Cohere says about the request.
                            detail = (await response.aread()).decode(errors="replace")
                            if _thinking_rejected(response.status_code, body, detail):
                                raise _ThinkingRejectedError(detail[:300])
                            logger.error(
                                "cohere chat: stream refused with HTTP %d: %s",
                                response.status_code,
                                detail[:300],
                            )
                            raise httpx.HTTPStatusError(
                                f"HTTP {response.status_code} from Cohere /v2/chat (stream): {detail[:300]}",
                                request=response.request,
                                response=response,
                            )
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "")
                        async for line in response.aiter_lines():
                            event = _sse_events(line)
                            if event is None:
                                if line.strip() and unframed_chars < _UNFRAMED_LIMIT:
                                    unframed.append(line)
                                    unframed_chars += len(line)
                                continue
                            saw_any_event = True
                            if len(seen_shapes) < 3:
                                seen_shapes.append(_shape_fingerprint(event))
                            event_type = event.get("type")
                            if isinstance(event_type, str) and event_type not in seen_types:
                                seen_types.append(event_type)
                            piece = _delta_text(event)
                            if piece is None and _is_whole_reply(event):
                                # A complete non-streaming reply on one line.
                                piece = _extract_content(event)
                                input_tokens, output_tokens = _extract_usage(event)
                                finish_reason = _extract_finish_reason(event) or finish_reason
                            if piece:
                                chunks.append(piece)
                                # Forward BEFORE recording, so a callback that
                                # raises cannot leave the buffer ahead of what
                                # the user actually saw.
                                await token_callback(piece)  # type: ignore[misc]
                                sent_any_token = True
                                continue
                            thought = _delta_thinking(event)
                            if thought:
                                thinking_chars += len(thought)
                                continue
                            if event.get("type") == "message-end" or "usage" in event:
                                end_delta = event.get("delta")
                                if isinstance(end_delta, dict) and isinstance(end_delta.get("finish_reason"), str):
                                    finish_reason = end_delta["finish_reason"]
                                got_in, got_out = _extract_usage(
                                    event.get("delta") if isinstance(event.get("delta"), dict) else event
                                )
                                input_tokens = got_in or input_tokens
                                output_tokens = got_out or output_tokens
                    if not saw_any_event and unframed and unframed_chars < _UNFRAMED_LIMIT:
                        whole = _whole_body_reply("\n".join(unframed))
                        if whole is not None:
                            saw_any_event = True
                            piece = _extract_content(whole)
                            input_tokens, output_tokens = _extract_usage(whole)
                            finish_reason = _extract_finish_reason(whole) or finish_reason
                            if piece:
                                chunks.append(piece)
                                await token_callback(piece)  # type: ignore[misc]
                                sent_any_token = True
                    content = "".join(chunks)
                    if not content and thinking_chars:
                        # Not a shape problem: the model reasoned and never
                        # reached its answer. Fall through to the
                        # budget_exhausted_by_thinking handling below.
                        logger.warning(
                            "cohere chat: stream carried %d chars of reasoning and no answer text "
                            "(finish_reason=%s, max_tokens=%d)",
                            thinking_chars,
                            finish_reason,
                            max_output,
                        )
                    elif not content:
                        # A stream that yielded no text at all is the
                        # streaming face of an unrecognised shape. Loud, for
                        # the same reason _extract_content is — but loud WITH
                        # the evidence, so the shape can be corrected from
                        # this one failure instead of a second probe run.
                        if not saw_any_event:
                            detail = (
                                "no event was parsed at all (content-type "
                                f"{content_type or '(none)'!r}; {len(unframed)} non-empty "
                                f"unparsed line(s), first shaped {_line_shape(unframed[0]) if unframed else '-'}). "
                                "Either the response was not an event stream (check that "
                                "`stream` survived into the request body) or it frames "
                                "events differently."
                            )
                        else:
                            detail = (
                                f"event types seen: {seen_types or '(none had a `type`)'}; "
                                f"structure of the first {len(seen_shapes)} "
                                f"(keys and types only, no content): {seen_shapes}"
                            )
                        logger.error(
                            "cohere chat: stream produced no text — %s",
                            detail,
                            extra={"cohere_event_types": seen_types, "cohere_event_shapes": seen_shapes},
                        )
                        raise CohereResponseShapeError(
                            "the Cohere stream produced no text. The event shape is "
                            f"UNVERIFIED — correct _delta_text to match. {detail}"
                        )
            break
        except _ThinkingRejectedError as exc:
            # Once only: the field is gone from `body` after this, so a
            # second rejection cannot match and falls through to raise.
            body.pop("thinking", None)
            logger.error(
                "cohere chat: the API rejected the `thinking` field (%s) -- retrying "
                "without it, so reasoning is UNCAPPED and answers can come back empty. "
                "Set COHERE_CHAT_THINKING_BUDGET=-1 to stop sending it.",
                exc,
            )
        except Exception as exc:  # noqa: BLE001 — re-raised unless retryable
            retryable = isinstance(exc, (CoherePreStreamError, *retryable_exceptions))
            if sent_any_token or not retryable:
                raise
            attempt += 1
            # Backoff with jitter, Retry-After honoured, each retry charged to
            # the per-query call budget and bounded by TIMEOUT_GATHER_S --
            # the same pacing the vLLM path has had all along (VEN-2/AGT-8).
            if not await wait_before_pre_stream_retry(
                label="cohere chat",
                attempt=attempt,
                max_retries=max_retries,
                started_monotonic=started,
                retry_after_s=getattr(exc, "retry_after_s", None),
                error=exc,
            ):
                raise

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
    if is_truncated_finish_reason(finish_reason):
        # Cut off at the output cap (or errored) with text already delivered.
        # Not raised: see llm_common "Cut-off generations".
        note_truncated_generation(
            backend="cohere",
            model=settings.effective_llm_model,
            reason=finish_reason,
            answer_chars=len(cleaned),
        )
    return cleaned


__all__ = ["CoherePreStreamError", "CohereResponseShapeError", "call_cohere_llm"]
