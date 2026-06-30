"""Unit tests for LLM cost estimation + user bucket assignment."""

from __future__ import annotations

from app.agent.pricing import estimate_cost_usd, user_bucket


class TestEstimateCost:
    def test_opus_cost_computed_from_published_rates(self):
        # Opus 4.8: $15/1M input, $75/1M output. 1000 input + 500 output =
        # (0.001 * 15) + (0.0005 * 75) = 0.015 + 0.0375 = 0.0525
        cost = estimate_cost_usd("claude-opus-4-8", input_tokens=1000, output_tokens=500)
        assert abs(cost - 0.0525) < 1e-6

    def test_cached_input_billed_at_cache_rate(self):
        # Sonnet: $3/1M uncached, $0.30/1M cached, $15/1M output.
        # 1000 uncached + 4000 cached + 500 output:
        #   (0.001 * 3) + (0.004 * 0.30) + (0.0005 * 15) = 0.003 + 0.0012 + 0.0075 = 0.0117
        cost = estimate_cost_usd(
            "claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
            cached_input_tokens=4000,
        )
        assert abs(cost - 0.0117) < 1e-6

    def test_local_models_are_free(self):
        # vLLM-served models have zero API-side cost — infra is tracked elsewhere.
        # 2026-06-23: swapped from Ollama-era `qwen2.5:14b` to the current
        # vLLM model `Qwen/Qwen3-14B-AWQ` post-Ollama cutover (see project
        # memory `project_qwen_ecosystem_swap_2026_06_03`). DeepSeek-V3
        # assertion dropped — we don't serve it and the second assertion
        # was just demonstrating the same property.
        assert estimate_cost_usd("Qwen/Qwen3-14B-AWQ", 1000, 500) == 0.0

    def test_unknown_model_uses_standard_fallback(self):
        # Sonnet-equivalent rates: 1000 * 3e-6 + 500 * 15e-6 = 0.003 + 0.0075 = 0.0105
        cost = estimate_cost_usd("some-future-model", 1000, 500)
        assert abs(cost - 0.0105) < 1e-6

    def test_zero_tokens_returns_zero(self):
        assert estimate_cost_usd("claude-opus-4-8", 0, 0) == 0.0

    def test_cache_creation_billed_at_creation_rate(self):
        """P1 #30 — cache_creation_input_tokens is now billed at the
        published 1.25× input rate, was previously dropped silently."""
        # Sonnet: input $3, cache_read $0.30, cache_creation $3.75, output $15
        # 1000 input + 4000 cached + 2000 cache_creation + 500 output:
        #   (0.001 * 3) + (0.004 * 0.30) + (0.002 * 3.75) + (0.0005 * 15)
        # = 0.003 + 0.0012 + 0.0075 + 0.0075
        # = 0.0192
        cost = estimate_cost_usd(
            "claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
            cached_input_tokens=4000,
            cache_creation_tokens=2000,
        )
        assert abs(cost - 0.0192) < 1e-6

    def test_cache_creation_default_zero_preserves_old_callers(self):
        """Backward-compat — callers that don't pass cache_creation_tokens
        get exactly the same cost as before the field was introduced."""
        legacy = estimate_cost_usd(
            "claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
            cached_input_tokens=4000,
        )
        with_explicit_zero = estimate_cost_usd(
            "claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
            cached_input_tokens=4000,
            cache_creation_tokens=0,
        )
        assert legacy == with_explicit_zero


class TestUserBucket:
    def test_none_maps_to_unknown(self):
        assert user_bucket(None) == "unknown"

    def test_empty_string_maps_to_unknown(self):
        assert user_bucket("") == "unknown"

    def test_literal_unknown_stays_unknown(self):
        assert user_bucket("unknown") == "unknown"

    def test_same_user_maps_to_same_bucket(self):
        assert user_bucket("user-abc") == user_bucket("user-abc")

    def test_buckets_are_low_cardinality(self):
        # 16 possible buckets (first hex char) + "unknown" = 17 max labels
        # regardless of user count. Verify spread across a large set.
        buckets = {user_bucket(f"user-{i}") for i in range(1000)}
        # Should hit most of the 16 hex buckets.
        assert "unknown" not in buckets  # all inputs were valid
        assert len(buckets) <= 16
        assert len(buckets) >= 10  # typically all 16; 10 is a soft lower bound

    def test_bucket_format(self):
        bucket = user_bucket("kyle@example.com")
        assert bucket.startswith("bucket_")
        assert len(bucket) == len("bucket_") + 1  # one hex char
