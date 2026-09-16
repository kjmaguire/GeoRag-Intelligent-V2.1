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

import logging
import re

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


__all__ = [
    "BUDGET_EXHAUSTED_FALLBACK",
    "cap_output_tokens",
    "clean_model_text",
    "record_llm_metrics",
]
