"""Amazon Bedrock chat backend — ``LLM_BACKEND=bedrock`` (ADR-0022).

Cohere Command A+ through Bedrock's **Converse** API. This is a sibling of
``_call_anthropic_llm`` rather than a branch inside
``_call_openai_compatible_llm``: Converse is its own wire shape, not an
OpenAI-compatible one, and folding it into the httpx client would have meant
two protocols sharing one 600-line function. ``llm_calls._call_llm``
dispatches here on backend, exactly as it does for Anthropic.

Async-native throughout (hard rule 2). ``aioboto3`` wraps aiobotocore, so
``converse``/``converse_stream`` are real coroutines and the streaming path
yields to the event loop between deltas — the same property the httpx SSE
path had and the reason first-token latency is an honest measure. A blocking
boto3 client here would stall the loop for the whole generation.

Why a client per call rather than a pooled one: aioboto3 sessions are not
safe to share across concurrent tasks, which is the same constraint
``georag_object_storage.async_client`` documents and solves the same way.

----------------------------------------------------------------------------
WIRE SHAPE — [UNVERIFIED]
----------------------------------------------------------------------------
Everything below is written to Bedrock's documented Converse contract and has
NOT been confirmed against a live endpoint. That matters more here than
anywhere else in this migration, because the path it replaces had three
behaviours confirmed by a real test call on 2026-07-30 that documentation
alone would have got wrong (the Foundry catalog table and Cohere's own docs
disagreed, and only a live call settled it):

  1. JSON ``response_format`` was supported.
  2. Reasoning arrived in a separate ``reasoning_content`` message field.
  3. JSON output was wrapped in ``<|START_TEXT|>``/``<|END_TEXT|>`` sentinels.

None of those is assumed to carry over. What this module does instead:

  - (1) becomes ``additionalModelRequestFields``, which Bedrock forwards to
    the model verbatim. Whether Command A+ honours it through Converse is
    exactly what the probe has to answer.
  - (2) is handled as a ``reasoningContent`` content block, which is
    Converse's own representation — and ALSO still checked for the old
    sibling-field shape, because a Marketplace endpoint may pass the
    provider response through more literally than a first-party model does.
  - (3) is stripped unconditionally, as it already was on the way out of the
    OpenAI path: the sentinel is a property of the Cohere model, not of the
    transport, so it survives a host change by default. That widening was
    itself a 2026-08-10 fix, after a wrapped answer reached the UI verbatim
    through a path reporting a different backend.

Run ``ops/validation/bedrock_probe.py`` and commit its report before trusting
this module in production (ADR-0022 "Verification").

The one behaviour that IS assumed to carry over is the failure mode: an empty
answer with a partial reasoning trace and ``stopReason: "max_tokens"`` is the
model exhausting its budget on thinking, and it reproduces on any host. It
gets the same structured fallback the OpenAI path returns.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

#: Cohere wraps JSON-mode output in sentinel tokens. Property of the model,
#: not the host — see the module docstring.
_SENTINEL_RE = re.compile(r"<\|START_TEXT\|>|<\|END_TEXT\|>")
_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

#: Bedrock exception names that mean "try again" rather than "this is wrong".
#: botocore's adaptive retry already handles these internally; this set is for
#: the pre-stream retry above it, which exists because a failure once tokens
#: have reached the user is NOT retryable — resending would duplicate output.
_TRANSIENT_ERROR_CODES = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
    }
)


class BedrockPreStreamError(RuntimeError):
    """A Bedrock failure that happened before any token reached the user.

    Mirrors ``llm_calls._PreStreamTransientError``: retrying is safe only
    while nothing has been streamed. Once ``sent_any_token`` flips, a failure
    propagates as itself so the caller degrades rather than emitting a second
    partial answer on top of the first.
    """


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    return response.get("Error", {}).get("Code")


def _is_transient(exc: BaseException) -> bool:
    return _error_code(exc) in _TRANSIENT_ERROR_CODES


def _clean(content: str) -> str:
    """Strip <think> blocks and Cohere sentinel tokens, never emptying."""
    if "<think>" in content:
        stripped = _THINK_RE.sub("", content).strip()
        if stripped:
            content = stripped
    if "<|START_TEXT|>" in content or "<|END_TEXT|>" in content:
        stripped = _SENTINEL_RE.sub("", content).strip()
        if stripped:
            logger.debug("bedrock: stripped Cohere sentinel tokens")
            content = stripped
    return content


def _build_request(
    *,
    user_message: str,
    system_content: str,
    temperature: float,
    max_output: int,
    response_format: str | None,
) -> dict[str, Any]:
    """Assemble the Converse request.

    ``system`` is a top-level parameter in Converse, not a message with
    ``role: "system"`` — a difference from the OpenAI shape that is easy to
    get wrong silently, because a system message sent as a user turn still
    produces plausible output.
    """
    request: dict[str, Any] = {
        "modelId": settings.effective_llm_model,
        "messages": [{"role": "user", "content": [{"text": user_message}]}],
        "inferenceConfig": {
            "maxTokens": max_output,
            "temperature": temperature,
        },
    }
    if system_content:
        request["system"] = [{"text": system_content}]

    if response_format == "json_object":
        # Converse has no first-class JSON mode; model-specific request
        # fields are forwarded to the provider untouched. [UNVERIFIED] that
        # Command A+ honours this through a Marketplace endpoint — it is the
        # single most important thing for the probe to answer, because every
        # typed-output guard in orchestrator_validators.py depends on the
        # model actually returning JSON (hard rule 4).
        request["additionalModelRequestFields"] = {
            "response_format": {"type": "json_object"}
        }
    return request


def _extract_from_message(message: dict[str, Any]) -> tuple[str, str]:
    """Return ``(content, reasoning)`` from a Converse output message."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    for block in message.get("content") or []:
        if "text" in block:
            content_parts.append(block["text"])
        elif "reasoningContent" in block:
            reasoning = block["reasoningContent"]
            text = (reasoning.get("reasoningText") or {}).get("text") or reasoning.get("text")
            if text:
                reasoning_parts.append(text)
    # Also accept the Foundry-era sibling-field shape. A Marketplace endpoint
    # forwards the provider's own response more literally than a first-party
    # Bedrock model does, so `reasoning_content` may still appear here — and
    # missing it would only show up as a confusing empty answer.
    sibling = message.get("reasoning_content") or message.get("reasoning")
    if sibling and not reasoning_parts:
        reasoning_parts.append(str(sibling))
    return "".join(content_parts), "".join(reasoning_parts)


_BUDGET_EXHAUSTED_FALLBACK = (
    "The model returned no content for this query due to token budget "
    "exhaustion during its internal reasoning pass. This typically happens "
    "on very large projects. Please retry, or raise the configured max "
    "output/context budget if the problem persists."
)


async def call_bedrock_llm(
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
    """Call Cohere Command A+ on Bedrock and return the answer text.

    Contract matches ``_call_openai_compatible_llm`` and
    ``_call_anthropic_llm``: returns the cleaned answer string, forwards
    every streamed delta to ``token_callback`` when one is supplied, and
    records token usage through ``llm_calls.add_token_usage`` so the run's
    cost accounting is backend-independent.
    """
    import aioboto3  # noqa: PLC0415

    from app.agent.llm_calls import add_token_usage  # noqa: PLC0415
    from app.services._bedrock import bedrock_region  # noqa: PLC0415

    system_content = "\n\n".join(
        part for part in (system_prompt, project_preamble, project_facts) if part
    )
    max_output = settings.BEDROCK_CHAT_MAX_TOKENS
    request = _build_request(
        user_message=user_message,
        system_content=system_content,
        temperature=temperature,
        max_output=max_output,
        response_format=response_format,
    )

    from botocore.config import Config  # noqa: PLC0415

    # A Marketplace endpoint that the nightly sweep has just recreated can be
    # genuinely slow on its first call — the model is cold, not broken — so
    # the read timeout is the cold-start ceiling rather than the request
    # budget. Retries are left to the pre-stream loop below, which knows
    # whether resending is safe; botocore's own retries do not.
    config = Config(
        region_name=bedrock_region(),
        retries={"mode": "adaptive", "max_attempts": 1},
        read_timeout=settings.BEDROCK_CHAT_COLD_START_TIMEOUT_S,
        connect_timeout=10.0,
        tcp_keepalive=True,
    )
    session = aioboto3.Session()

    attempt = 0
    while True:
        sent_any_token = False
        try:
            async with session.client("bedrock-runtime", config=config) as client:
                if token_callback is None:
                    response = await client.converse(**request)
                    message = (response.get("output") or {}).get("message") or {}
                    content, reasoning = _extract_from_message(message)
                    usage = response.get("usage") or {}
                    stop_reason = response.get("stopReason")
                else:
                    response = await client.converse_stream(**request)
                    chunks: list[str] = []
                    reasoning_chunks: list[str] = []
                    usage = {}
                    stop_reason = None
                    async for event in response["stream"]:
                        if "contentBlockDelta" in event:
                            delta = event["contentBlockDelta"].get("delta") or {}
                            if "text" in delta:
                                piece = delta["text"]
                                chunks.append(piece)
                                # Forward before recording, so a callback that
                                # raises cannot leave the buffer ahead of what
                                # the user actually saw.
                                await token_callback(piece)
                                sent_any_token = True
                            elif "reasoningContent" in delta:
                                rc = delta["reasoningContent"]
                                text = rc.get("text") or (rc.get("reasoningText") or {}).get("text")
                                if text:
                                    # Deliberately NOT forwarded to the user:
                                    # reasoning is not the answer, and the
                                    # OpenAI path never streamed it either.
                                    reasoning_chunks.append(text)
                        elif "messageStop" in event:
                            stop_reason = event["messageStop"].get("stopReason")
                        elif "metadata" in event:
                            usage = event["metadata"].get("usage") or {}
                    content = "".join(chunks)
                    reasoning = "".join(reasoning_chunks)
            break
        except Exception as exc:  # noqa: BLE001 — re-raised unless retryable
            if sent_any_token or not _is_transient(exc) or attempt >= max_retries:
                raise
            attempt += 1
            logger.warning(
                "bedrock chat: transient %s (attempt %d/%d) — retrying before "
                "any token reached the user",
                _error_code(exc), attempt, max_retries,
            )

    input_tokens = int(usage.get("inputTokens", 0) or 0)
    output_tokens = int(usage.get("outputTokens", 0) or 0)
    # Bedrock reports cache reads separately when the model supports prompt
    # caching. Command A+ through a Marketplace endpoint may report neither
    # key; both default to zero rather than being assumed present.
    cache_read = int(usage.get("cacheReadInputTokens", 0) or 0)
    add_token_usage(input_tokens, output_tokens)

    _record_metrics(
        model=settings.effective_llm_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read=cache_read,
        user_id=user_id,
    )

    content = _clean(content).strip()
    if not content and reasoning.strip():
        logger.warning(
            "budget_exhausted_by_thinking: empty content with %d-char reasoning. "
            "backend=bedrock model=%s max_tokens=%d stop_reason=%s "
            "input_tokens=%d. Consider raising BEDROCK_CHAT_MAX_TOKENS or "
            "disabling reasoning on this call site.",
            len(reasoning), settings.effective_llm_model, max_output,
            stop_reason, input_tokens,
        )
        return _BUDGET_EXHAUSTED_FALLBACK
    return content


def _record_metrics(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    user_id: str | None,
) -> None:
    """Mirror the OpenAI path's cost and token bookkeeping.

    Kept separate so a metrics import failure cannot take out an answer that
    has already been generated — the same posture the OpenAI path takes with
    its ``except ImportError: pass``.
    """
    if input_tokens <= 0:
        return
    try:
        from app.agent.pricing import (  # noqa: PLC0415
            estimate_cost_usd,
            has_pricing,
            user_bucket,
        )
        from app.metrics import (  # noqa: PLC0415
            LLM_COST_USD,
            LLM_TOKENS_OUTPUT,
            PROMPT_CACHE_TOKENS,
            PROMPT_TOTAL_TOKENS,
        )

        PROMPT_TOTAL_TOKENS.labels(backend="bedrock").inc(input_tokens)
        if cache_read > 0:
            PROMPT_CACHE_TOKENS.labels(backend="bedrock").inc(cache_read)
        if output_tokens > 0:
            LLM_TOKENS_OUTPUT.labels(model=model).inc(output_tokens)
        if has_pricing(model):
            cost_usd = estimate_cost_usd(
                model=model,
                input_tokens=max(input_tokens - cache_read, 0),
                output_tokens=output_tokens,
                cached_input_tokens=cache_read,
            )
            if cost_usd > 0:
                LLM_COST_USD.labels(model=model, user_bucket=user_bucket(user_id)).inc(cost_usd)
    except ImportError:
        pass


__all__ = ["BedrockPreStreamError", "call_bedrock_llm"]
