"""Classifier-driven model routing (B1).

Sends factoid queries to a cheap fast model (Haiku), narrative queries to a
balanced model (Sonnet), and multi-hop / uncertain queries to the premium
model (Opus). Saves Opus rate-limit headroom for queries that actually need
it.

Design
------
- A pure function `select_tier()` over the classifier output + retry_count.
  No network, no state — trivially unit-testable.
- A mapping `tier_to_model()` reads from settings so operators can override
  model names without redeploying the routing logic.
- Failover is NOT handled here — it lives in the orchestrator's error path
  where we have access to the exception object. This module only chooses
  the intended target.

Tier policy (see select_tier for exact logic)
---------------------------------------------
  FAST     — factoid lookups: pure `spatial` or pure `assay`, no narrative
             synthesis. Example: "How many drill holes in Lazy Edward Bay?"
  STANDARD — document-anchored narrative, or multi-tool fan-out where a
             single model can summarize without deep reasoning. Most queries
             land here.
  DEEP     — classifier_fallback (keyword miss), `graph` traversal (multi-hop
             entity reasoning), `targeting` (optimisation with geological
             constraints), or any retry after a validation failure.

Operator levers
---------------
  MODEL_ROUTING_ENABLED=False → always use DEEP tier (original behaviour).
  MODEL_TIER_FAST / _STANDARD / _DEEP — override model names per tier.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from app.config import settings


class ModelTier(str, Enum):
    """Cost/capability tiers that the classifier maps into."""

    FAST = "fast"          # Haiku-class
    STANDARD = "standard"  # Sonnet-class
    DEEP = "deep"          # Opus 4.7 — premium reasoning, saved for hard cases


def select_tier(categories: dict[str, Any], retry_count: int = 0) -> ModelTier:
    """Map classifier output + retry_count to a ModelTier.

    Rules, in priority order:
      1. Retries always escalate to DEEP — we're in a correction loop and
         need the stronger model to self-correct hallucinations.
      2. classifier_fallback → DEEP. The keyword classifier missed; we're
         routing to the generic spatial+documents fallback, so the query
         is likely subtle and deserves the premium model.
      3. `graph` OR `targeting` → DEEP. Multi-hop entity reasoning / drill
         target optimisation — cheaper models routinely fabricate
         relationships here.
      4. `documents` OR `public_geo` → STANDARD. Narrative synthesis
         with citation enforcement.
      5. `spatial` OR `assay` only → FAST. Pure factoid lookups that the
         deterministic tool dispatch has already structured; the LLM just
         has to narrate the numbers.
      6. Anything else → STANDARD (safe default).
    """
    if not getattr(settings, "MODEL_ROUTING_ENABLED", True):
        return ModelTier.DEEP

    if retry_count > 0:
        return ModelTier.DEEP

    if categories.get("classifier_fallback"):
        return ModelTier.DEEP

    if categories.get("graph") or categories.get("targeting"):
        return ModelTier.DEEP

    if categories.get("documents") or categories.get("public_geo"):
        return ModelTier.STANDARD

    # At this point only the structured tool buckets are active — pure
    # factoid lookup territory.
    structured_only = bool(categories.get("spatial") or categories.get("assay") or categories.get("downhole"))
    if structured_only:
        return ModelTier.FAST

    return ModelTier.STANDARD


def tier_to_model(tier: ModelTier) -> str:
    """Resolve a tier into a concrete Anthropic model name from settings."""
    if tier is ModelTier.FAST:
        return getattr(settings, "MODEL_TIER_FAST", "claude-haiku-4-5")
    if tier is ModelTier.STANDARD:
        return getattr(settings, "MODEL_TIER_STANDARD", "claude-sonnet-4-6")
    return getattr(settings, "MODEL_TIER_DEEP", settings.ANTHROPIC_MODEL)


def tier_to_model_for_backend(tier: ModelTier, backend: str) -> str:
    """Resolve a tier into the right model name for the active backend.

    Single entry point that knows about Anthropic + vLLM. vLLM is the
    only local-inference backend post-Ollama removal (2026-05-17).

    vLLM rationale: a vLLM process serves one model checkpoint chosen at
    `--model` startup time. Multi-tier on vLLM means multiple processes —
    not viable on the dev workstation A4500 (single AWQ-30B fills VRAM).
    Tier routing on vLLM therefore always returns `VLLM_MODEL`, and the
    cost/capability shaping happens entirely on the Anthropic side.
    Multi-instance vLLM tiering is left as future work (production-
    readiness doc scope).
    """
    if backend == "anthropic":
        if not getattr(settings, "MODEL_ROUTING_ENABLED", True):
            return getattr(settings, "ANTHROPIC_MODEL", "claude-opus-4-8")
        return tier_to_model(tier)
    if backend == "vllm":
        # Single-instance vLLM: always serve the configured checkpoint.
        # `tier` is accepted for signature parity with the Anthropic path
        # but doesn't influence the resolved model.
        return getattr(settings, "VLLM_MODEL", "")
    # Unknown backends: stay on the configured vLLM primary. Keeps the
    # function total and avoids a None / KeyError leak on misconfiguration.
    return getattr(settings, "VLLM_MODEL", getattr(settings, "LLM_PRIMARY_MODEL", ""))


def downshift(tier: ModelTier) -> ModelTier:
    """Return the tier one level BELOW `tier`, for cost-aware failover.

    DEEP → STANDARD → FAST → FAST (floor).
    Used by the orchestrator when Opus returns 429/529/overloaded and we
    want to retry once on a less-loaded model rather than hard-failing.
    """
    if tier is ModelTier.DEEP:
        return ModelTier.STANDARD
    if tier is ModelTier.STANDARD:
        return ModelTier.FAST
    return ModelTier.FAST


def is_retriable_via_failover(exc: BaseException) -> bool:
    """Does this exception warrant a one-shot failover to a different target?

    Retriable: Anthropic 429 (rate limit), 529 (overloaded), 500/502/503/504
    (server errors), and any timeout. Non-retriable: 400-class validation
    errors, auth errors, model-unsupported errors.
    """
    # httpx.TimeoutException and anthropic.APIStatusError are both importable
    # lazily — no need to require anthropic in non-anthropic deploys.
    import asyncio  # noqa: PLC0415

    if isinstance(exc, asyncio.TimeoutError):
        return True

    try:
        import httpx  # noqa: PLC0415

        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout)):
            return True
    except ImportError:
        pass

    # Inspect duck-typed .status_code (Anthropic SDK exceptions) without a
    # hard import of anthropic.
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {408, 429, 500, 502, 503, 504, 529}

    return False
