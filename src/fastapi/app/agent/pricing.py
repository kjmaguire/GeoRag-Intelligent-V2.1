"""LLM pricing table + per-query cost estimation.

Closes the C+ cost-accountability gap. Every completed query emits its
estimated cost to Prometheus so the Grafana dashboard can show $/hour
and $/user. Pricing is per-1M-token and pulled from each provider's
published sheet as of 2026-04-17; tweak the dict when rates change.

Per-user cost labelling uses a deliberately low-cardinality bucketing
scheme (first-letter of user_id hash) so a 100-user deployment doesn't
explode Prometheus time-series count. When we need true per-user
drilldown we'll swap to structured logs + Loki.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Pricing:
    """USD per 1M tokens.

    Three input rates per Anthropic's pricing model:
      - input_per_million              normal uncached input tokens
      - cached_input_per_million       cache HIT (read) — 10 % of normal
      - cache_creation_per_million     cache WRITE (first-time storage)
                                        — 125 % of normal on Anthropic
    """
    input_per_million: float
    cached_input_per_million: float       # Anthropic cache READ rate (≈ 0.1×)
    cache_creation_per_million: float     # Anthropic cache WRITE rate (≈ 1.25×)
    output_per_million: float


# 2026-04-17 published rates. Keys are the model names the orchestrator
# uses (settings.MODEL_TIER_FAST/STANDARD/DEEP + the OpenAI-compatible
# targets). Fallback for unknown models is STANDARD-tier Sonnet pricing.
#
# P1 #30 — cache_creation_per_million was NOT being billed prior to this
# change. The orchestrator counted `cache_write` tokens but passed only
# `cache_read` to estimate_cost_usd, silently understating monthly spend
# whenever the ephemeral cache rotated (every 5 min for the default TTL).
_PRICE_TABLE: dict[str, Pricing] = {
    # Anthropic — cache_creation is 1.25× the input rate (published).
    "claude-haiku-4-5":   Pricing(1.00,  0.10,  1.25,  5.00),
    "claude-sonnet-4-5":  Pricing(3.00,  0.30,  3.75, 15.00),
    "claude-sonnet-4-6":  Pricing(3.00,  0.30,  3.75, 15.00),
    "claude-opus-4-7":    Pricing(15.00, 1.50, 18.75, 75.00),

    # Local / OpenAI-compatible — vLLM is "free" from an API-spend
    # perspective (infra cost handled elsewhere). We still track token
    # counts so the dashboard shows throughput.
    "Qwen/Qwen3-14B-AWQ": Pricing(0.0, 0.0, 0.0, 0.0),
}


_FALLBACK = Pricing(3.00, 0.30, 3.75, 15.00)


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Compute USD cost for a single LLM call.

    `input_tokens` is the NON-cached portion. Anthropic's usage object
    reports these as:
      - input_tokens             — uncached portion of the prompt
      - cache_read_input_tokens  — billed at cached_input_per_million
      - cache_creation_input_tokens — billed at cache_creation_per_million
                                        (P1 #30 — previously missing)

    Returns 0.0 for unknown models with a log so operators notice a new
    model appearing in traffic before it distorts the dashboard.
    """
    pricing = _PRICE_TABLE.get(model)
    if pricing is None:
        # Log once per unseen model — dedup via logging.lru_cache isn't
        # worth the complexity here; Prometheus itself dedups labels.
        logger.info(
            "estimate_cost_usd: no pricing for model=%s; using STANDARD-tier fallback",
            model,
        )
        pricing = _FALLBACK

    uncached_cost = (input_tokens / 1_000_000) * pricing.input_per_million
    cached_cost = (cached_input_tokens / 1_000_000) * pricing.cached_input_per_million
    cache_create_cost = (cache_creation_tokens / 1_000_000) * pricing.cache_creation_per_million
    output_cost = (output_tokens / 1_000_000) * pricing.output_per_million
    return round(
        uncached_cost + cached_cost + cache_create_cost + output_cost, 6
    )


def user_bucket(user_id: str | None) -> str:
    """Low-cardinality label for per-user cost tracking.

    Goals:
      - Never explode cardinality: cap at 16 buckets regardless of user count.
      - Deterministic: same user_id always maps to the same bucket.
      - Opaque: the bucket reveals nothing about the user_id.

    Uses the first hex character of sha256(user_id) — gives 16 buckets
    with uniform distribution. "unknown" routes to its own dedicated
    bucket so unauthenticated traffic is distinguishable.
    """
    if not user_id or user_id == "unknown":
        return "unknown"
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return f"bucket_{digest[0]}"
