"""Helpers shared by every LLM backend adapter (ADR-0023).

Extracted from ``llm_bedrock.py`` on 2026-09-15, when ADR-0023 added a second
adapter that speaks to the same *model* over a different *host*. Nothing here
is new; it is the subset of that module which was never about Bedrock.

Why extract rather than copy
---------------------------
``cap_output_tokens``'s own docstring records the answer: the guard "has been
load-bearing on the OpenAI-compatible path since the vLLM era; it did not
survive the first draft of the Bedrock port". Copying this logic between
backends has already cost one real bug. A third backend copying it again is
how that bug comes back.

The same applies to ``clean_model_text``. The sentinel tokens it strips are a
property of the Cohere *model*, not of Bedrock — llm_bedrock.py said so in a
comment long before there was a second Cohere transport to prove it. Command
A+ emits them on Bedrock and it emits them on api.cohere.com, so the stripping
belongs to neither host.

What is deliberately NOT here: anything that knows a wire format. Request
assembly, response extraction, and each host's own transient-error vocabulary
stay with their adapters, because those are exactly the parts that differ.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import re
import time
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output-token budgeting
# ---------------------------------------------------------------------------

# Pessimistic chars-per-token for the GeoRAG prompt mix, and the margin on
# top. Production samples ran 2.39-2.77 chars/token (system prompt is dense
# English, JSON-ish context blocks tokenise short, PLSS Township-Range syntax
# finer still), so 2.2 over-estimates input cleanly on the finest content.
# Same constants as `_call_openai_compatible_llm`, deliberately.
_CHARS_PER_TOKEN = 2.2
_SAFETY_MARGIN_TOKENS = 512
_MIN_OUTPUT_TOKENS = 64


def cap_output_tokens(*, max_output: int, max_model_len: int, prompt_chars: int) -> int:
    """Shrink the output request so it cannot overflow the context window.

    Without this, a long retrieval context plus a full-size output request
    pushes ``prompt_tokens + max_tokens`` past the model's window and the
    provider answers 400 instead of truncating — so the run fails AFTER
    paying to build the prompt. The guard has been load-bearing on the
    OpenAI-compatible path since the vLLM era; it did not survive the first
    draft of the Bedrock port, which is why it is a named, tested function
    rather than four lines inline in each adapter.

    Never returns 0: an over-long prompt still asks for
    ``_MIN_OUTPUT_TOKENS`` so the failure surfaces as a clean provider error
    on the orchestrator's failover ladder.
    """
    room = max_model_len - int(prompt_chars / _CHARS_PER_TOKEN) - _SAFETY_MARGIN_TOKENS
    return min(max_output, max(_MIN_OUTPUT_TOKENS, room))


# ---------------------------------------------------------------------------
# Model output cleaning
# ---------------------------------------------------------------------------

#: Cohere wraps JSON-mode output in sentinel tokens. A property of the MODEL,
#: not of any host — which is why this lives here rather than beside one
#: transport. Confirmed on Azure AI Foundry 2026-07-30; assumed, not verified,
#: on both Bedrock and api.cohere.com.
_SENTINEL_RE = re.compile(r"<\|START_TEXT\|>|<\|END_TEXT\|>")
_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def clean_model_text(content: str, *, backend: str = "llm") -> str:
    """Strip ``<think>`` blocks and Cohere sentinel tokens, never emptying.

    "Never emptying" is the load-bearing part: each strip is applied only if
    what remains is non-empty. A model that returns nothing BUT reasoning, or
    nothing but sentinels, keeps its raw content so the caller can see what
    actually came back instead of an empty string that looks like a different
    failure entirely.
    """
    if "<think>" in content:
        stripped = _THINK_RE.sub("", content).strip()
        if stripped:
            content = stripped
    if "<|START_TEXT|>" in content or "<|END_TEXT|>" in content:
        stripped = _SENTINEL_RE.sub("", content).strip()
        if stripped:
            logger.debug("%s: stripped Cohere sentinel tokens", backend)
            content = stripped
    return content


BUDGET_EXHAUSTED_FALLBACK = (
    "The model returned no content for this query due to token budget "
    "exhaustion during its internal reasoning pass. This typically happens "
    "on very large projects. Please retry, or raise the configured max "
    "output/context budget if the problem persists."
)


# ---------------------------------------------------------------------------
# Cost and token bookkeeping
# ---------------------------------------------------------------------------


def record_llm_metrics(
    *,
    backend: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    user_id: str | None,
) -> None:
    """Mirror the OpenAI path's cost and token bookkeeping.

    Kept separate so a metrics import failure cannot take out an answer that
    has already been generated — the same posture the OpenAI path takes with
    its ``except ImportError: pass``. It logs at debug rather than swallowing
    silently: an answer is worth more than its bookkeeping, but a run whose
    cost was never recorded should still leave a trace, because
    `cost_burn_watcher` reads those counters and would otherwise just see a
    quiet workspace (Ch 12 §1.3).

    ``backend`` is a parameter rather than a constant because the same
    counters are fed by every adapter, and a hardcoded label would have made
    the second one report as the first.
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

        PROMPT_TOTAL_TOKENS.labels(backend=backend).inc(input_tokens)
        if cache_read > 0:
            PROMPT_CACHE_TOKENS.labels(backend=backend).inc(cache_read)
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
        logger.debug(
            "%s: token/cost accounting skipped — metrics or pricing module unavailable (model=%s, input_tokens=%d)",
            backend,
            model,
            input_tokens,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Pre-stream retry pacing (VEN-2 / AGT-8, 2026-09-29)
# ---------------------------------------------------------------------------
#
# The vLLM path has paced its pre-stream retries through
# `llm_calls._retry_pre_stream_call` since the Foundry era: 2/4/8 s backoff,
# each retry charged to the per-query call counter, and no retry once the
# remaining TIMEOUT_GATHER_S could not fit another wait. The Cohere and
# Bedrock adapters -- the DEFAULT production backend among them -- re-sent
# immediately, ignored Retry-After and charged nothing, so a 429 burst became
# three back-to-back 429s inside a second and then a user-facing failure.
#
# This is the pacing both of those adapters now share. It does not know any
# wire format: each adapter still decides WHAT is retryable (its own status
# and error vocabulary); this only decides WHETHER and WHEN.

#: First backoff step and the ceiling of the exponential ladder (2, 4, 8 s).
PRE_STREAM_BACKOFF_BASE_S = 2.0
PRE_STREAM_BACKOFF_CAP_S = 8.0
#: A server asking for more than this is not worth waiting on inside a user
#: query; the retry is skipped and the original error raised instead.
PRE_STREAM_RETRY_AFTER_CAP_S = 20.0

#: Indirection so tests can run the pacing without real sleeps.
_sleep = asyncio.sleep


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP-date).

    None when absent or unreadable -- an unreadable header falls back to the
    exponential ladder rather than failing the retry decision.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def pre_stream_backoff_s(attempt: int, *, retry_after_s: float | None = None) -> float:
    """Delay before retry number ``attempt`` (1-based).

    Exponential (2, 4, 8 s, capped) with up to 25 % added jitter so a burst
    of concurrent queries does not retry in lock-step. A server-supplied
    ``Retry-After`` wins when it is LONGER than the ladder -- waiting less
    than the host asked for just earns another 429.
    """
    ladder = min(PRE_STREAM_BACKOFF_BASE_S * float(2 ** max(attempt - 1, 0)), PRE_STREAM_BACKOFF_CAP_S)
    delay: float = ladder * (1.0 + 0.25 * random.random())  # noqa: S311 -- jitter, not crypto
    if retry_after_s is not None and retry_after_s > delay:
        delay = retry_after_s
    return delay


async def wait_before_pre_stream_retry(
    *,
    label: str,
    attempt: int,
    max_retries: int,
    started_monotonic: float,
    retry_after_s: float | None = None,
    error: BaseException | None = None,
) -> bool:
    """Sleep before a pre-stream retry, or return False if it must not happen.

    ``attempt`` is the retry about to be made (1 = first retry). Returns False
    -- and the caller re-raises the original error -- when any budget is
    spent:

    - ``max_retries`` for this call;
    - the per-query LLM call budget (``MAX_LLM_CALLS_PER_QUERY``): a retry
      is charged to ``llm_calls._llm_call_counter`` exactly like a distinct
      ``_call_llm`` call, the same accounting the vLLM path does;
    - the wall-clock budget: no retry once ``TIMEOUT_GATHER_S`` minus the
      time already spent in this call cannot fit the wait;
    - a ``Retry-After`` longer than ``PRE_STREAM_RETRY_AFTER_CAP_S``.
    """
    from app.agent.llm_calls import _llm_call_counter  # noqa: PLC0415 -- import cycle
    from app.config import settings  # noqa: PLC0415

    cap = int(getattr(settings, "MAX_LLM_CALLS_PER_QUERY", 8))
    n_so_far = _llm_call_counter.get()
    delay = pre_stream_backoff_s(attempt, retry_after_s=retry_after_s)
    budget = float(getattr(settings, "TIMEOUT_GATHER_S", 180.0) or 180.0)
    elapsed = time.monotonic() - started_monotonic
    remaining = budget - elapsed
    reason: str | None = None
    if attempt > max_retries:
        reason = "attempts"
    elif n_so_far >= cap:
        reason = "call_budget"
    elif retry_after_s is not None and retry_after_s > PRE_STREAM_RETRY_AFTER_CAP_S:
        reason = "retry_after_too_long"
    elif remaining <= delay:
        reason = "time_budget"
    err = f"{type(error).__name__}: {error}" if error is not None else "-"
    if reason is not None:
        logger.warning(
            "%s: pre-stream retry not attempted (%s; retry=%d/%d call_budget=%d/%d "
            "elapsed=%.1fs remaining=%.1fs retry_after=%s) -- raising %s",
            label,
            reason,
            attempt,
            max_retries,
            n_so_far,
            cap,
            elapsed,
            remaining,
            retry_after_s,
            err,
        )
        return False
    _llm_call_counter.set(n_so_far + 1)
    logger.warning(
        "%s: transient pre-stream failure -- retry %d/%d in %.1fs (retry_after=%s) err=%s",
        label,
        attempt,
        max_retries,
        delay,
        retry_after_s,
        err,
    )
    await _sleep(delay)
    return True


__all__ = [
    "BUDGET_EXHAUSTED_FALLBACK",
    "PRE_STREAM_RETRY_AFTER_CAP_S",
    "cap_output_tokens",
    "clean_model_text",
    "parse_retry_after",
    "pre_stream_backoff_s",
    "record_llm_metrics",
    "wait_before_pre_stream_retry",
]
