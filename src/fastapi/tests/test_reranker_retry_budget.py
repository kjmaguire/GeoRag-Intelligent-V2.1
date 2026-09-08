"""The reranker's retry budget must fit inside its caller's timeout (2026-08-20).

`search_documents` wraps `reranker.predict` in
``asyncio.wait_for(..., TIMEOUT_RERANKER_S)``. The hosted backend's retry path
was originally written for ingestion, where spending 70s recovering from a 429
is fine. Reused on the interactive query path under an 8s wait_for — with an 8s
per-call timeout of its own — the retry was unreachable: a single 429 meant the
wait_for fired first, the branch degraded to raw Qdrant cosine ordering, and the
executor thread kept retrying in the background against a result nobody would
read.

Rewritten 2026-09-08 for ADR-0022. The mechanism changed — botocore's adaptive
retry replaced `_foundry_retry`'s hand-rolled backoff ladder, and that module
is gone — but every invariant this file was written to pin is unchanged, because
they are properties of the *caller's* budget rather than of the transport:

  1. The budgets are DERIVED from one setting, not configured twice. The
     original bug was two independently-set 8.0s values that looked
     compatible and were not.
  2. A single call must not be allowed to consume the whole budget, or there
     is by definition no room for a retry.
  3. N query groups share one budget rather than claiming it each.
  4. The interactive retry profile stays tighter than the ingestion one.

What is NOT pinned here any more: the exact backoff delays. botocore owns
those now and honours the service's own throttling signals, which is strictly
better than a fixed 2s/4s ladder and not something a unit test should freeze.
"""

from __future__ import annotations

import pytest

from app.services import _bedrock
from app.services import reranker as reranker_mod


@pytest.fixture(autouse=True)
def _clear_client_cache():
    """_bedrock caches clients per (service, region, attempts, timeout)."""
    _bedrock.reset_client_cache()
    yield
    _bedrock.reset_client_cache()


class TestBudgetDerivation:
    def test_budget_comes_from_the_caller_timeout_setting(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "TIMEOUT_RERANKER_S", 20.0)
        assert reranker_mod._caller_budget_s() == pytest.approx(19.0)

    def test_budget_never_goes_non_positive(self, monkeypatch) -> None:
        """A misconfigured tiny timeout must not produce a zero/negative
        budget, which would make every deadline instantly expired."""
        from app.config import settings

        monkeypatch.setattr(settings, "TIMEOUT_RERANKER_S", 0.2)
        assert reranker_mod._caller_budget_s() >= 1.0

    def test_config_import_failure_degrades_instead_of_killing_rerank(
        self, monkeypatch
    ) -> None:
        """This module is also imported by the sidecar and eval scripts."""
        import builtins

        real_import = builtins.__import__

        def boom(name, *args, **kwargs):
            if name == "app.config":
                raise ImportError("no config here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", boom)
        assert reranker_mod._caller_budget_s() == pytest.approx(7.0)

    def test_bedrock_per_call_timeout_leaves_room_for_a_retry(
        self, monkeypatch
    ) -> None:
        """The original bug in one assertion.

        An 8.0s timeout under an 8.0s wait_for meant one slow call consumed
        the entire budget. Whatever the configured per-call timeout, it must
        not exceed half the total budget.
        """
        from app.config import settings

        monkeypatch.setattr(settings, "TIMEOUT_RERANKER_S", 20.0)
        monkeypatch.setattr(reranker_mod, "RERANKER_BACKEND", "bedrock")
        monkeypatch.setattr(
            reranker_mod, "BEDROCK_RERANK_MODEL_ID", "cohere.rerank-v3-5:0"
        )

        client = reranker_mod.get_reranker_or_none()

        assert isinstance(client, reranker_mod._BedrockReranker)
        assert client._total_budget_s == pytest.approx(19.0)
        assert client._timeout_s <= client._total_budget_s / 2.0

    def test_sidecar_timeout_is_clamped_to_the_budget(self, monkeypatch) -> None:
        """Same drift on the RERANKER_SERVICE_URL path: a 10s sidecar
        timeout above an 8s wait_for is always cancelled, never answered."""
        from app.config import settings

        monkeypatch.setattr(settings, "TIMEOUT_RERANKER_S", 8.0)
        monkeypatch.setattr(reranker_mod, "RERANKER_BACKEND", "cross_encoder")
        monkeypatch.setenv("RERANKER_SERVICE_URL", "http://reranker:8000")
        monkeypatch.setenv("RERANKER_SERVICE_TIMEOUT_S", "10")

        client = reranker_mod.get_reranker_or_none()

        assert isinstance(client, reranker_mod._RemoteReranker)
        assert client._timeout_s == pytest.approx(7.0)


class TestAttemptsWithinBudget:
    """botocore takes an attempt count, not a wall-clock deadline.

    `attempts_within_budget` is the translation, and it is the only place the
    caller's clock still reaches the retry layer — so it carries the whole
    invariant that used to live in `with_foundry_retry`'s `deadline` argument.
    """

    def test_no_budget_gets_the_ceiling(self) -> None:
        """Ingestion callers with no wait_for above them keep their patience."""
        assert _bedrock.attempts_within_budget(None) == 4

    def test_a_generous_budget_gets_the_ceiling(self) -> None:
        assert _bedrock.attempts_within_budget(19.0) == 4

    def test_a_tight_budget_is_cut_down(self) -> None:
        """3 seconds does not pay for four attempts and their backoffs."""
        assert _bedrock.attempts_within_budget(3.0) == 1

    def test_never_returns_zero_attempts(self) -> None:
        """Zero attempts would mean the call never happens at all — the
        deadline bounds retries, not the attempt the caller asked for. That
        was `test_an_expired_deadline_still_makes_the_first_call` before the
        mechanism changed."""
        assert _bedrock.attempts_within_budget(0.0) == 1
        assert _bedrock.attempts_within_budget(-10.0) == 1

    def test_ceiling_is_respected(self) -> None:
        assert _bedrock.attempts_within_budget(1000.0, ceiling=2) == 2


class TestBedrockRerankerHonoursItsBudget:
    def _client(self, budget: float | None):
        return reranker_mod._BedrockReranker(
            "cohere.rerank-v3-5:0",
            timeout_s=2.0,
            total_budget_s=budget,
        )

    @staticmethod
    def _fake_client(captured: dict):
        class _Client:
            @staticmethod
            def rerank(**kwargs):
                captured.setdefault("calls", []).append(kwargs)
                return {"results": [{"index": 0, "relevanceScore": 0.5}]}

        return _Client()

    def test_budget_is_shared_across_query_groups(self, monkeypatch) -> None:
        """Two groups must split one budget, not claim it each.

        `predict` groups pairs by query. Deriving the attempt count from the
        full budget per group would let N queries spend N x the retries the
        caller allowed. The division happens once, before the client is
        built, which is why there is exactly one client for the whole predict.
        """
        captured: dict = {}
        seen_attempts: list[int] = []

        def fake_get_client(service, *, max_attempts=4, read_timeout_s=30.0):
            seen_attempts.append(max_attempts)
            return self._fake_client(captured)

        monkeypatch.setattr(_bedrock, "get_client", fake_get_client)

        self._client(19.0).predict([("q1", "doc a"), ("q2", "doc b")])

        assert len(seen_attempts) == 1, "one client for the whole predict"
        # 19s across two groups is 9.5s each -> 4 attempts, capped by the
        # interactive ceiling of 3.
        assert seen_attempts[0] == 3
        assert len(captured["calls"]) == 2, "one rerank call per query group"

    def test_more_groups_means_fewer_attempts_each(self, monkeypatch) -> None:
        """The point of sharing: eight groups on one budget must not each
        retry as if they owned it."""
        captured: dict = {}
        seen_attempts: list[int] = []

        def fake_get_client(service, *, max_attempts=4, read_timeout_s=30.0):
            seen_attempts.append(max_attempts)
            return self._fake_client(captured)

        monkeypatch.setattr(_bedrock, "get_client", fake_get_client)

        pairs = [(f"q{i}", f"doc {i}") for i in range(8)]
        self._client(8.0).predict(pairs)

        assert seen_attempts[0] == 1, "1s per group pays for one attempt"

    def test_no_budget_gets_the_interactive_ceiling(self, monkeypatch) -> None:
        """Eval/script callers with no wait_for above them keep old behavior."""
        captured: dict = {}
        seen_attempts: list[int] = []

        def fake_get_client(service, *, max_attempts=4, read_timeout_s=30.0):
            seen_attempts.append(max_attempts)
            return self._fake_client(captured)

        monkeypatch.setattr(_bedrock, "get_client", fake_get_client)

        self._client(None).predict([("q1", "doc a")])

        assert seen_attempts[0] == reranker_mod.BEDROCK_RERANK_MAX_ATTEMPTS

    def test_interactive_retry_profile_is_tighter_than_ingestion(self) -> None:
        """A geologist waiting on a chat answer should not wait 70s for a
        perfect ordering. Ingestion's ceiling is 4."""
        assert reranker_mod.BEDROCK_RERANK_MAX_ATTEMPTS < 4

    def test_scores_are_remapped_to_the_callers_pair_order(self, monkeypatch) -> None:
        """Bedrock returns results keyed by its own index. Getting this wrong
        silently attaches each score to the wrong passage — an ordering that
        looks real and is not."""
        def fake_get_client(service, *, max_attempts=4, read_timeout_s=30.0):
            class _Client:
                @staticmethod
                def rerank(**_kwargs):
                    # Deliberately out of order, and note the camelCase field
                    # name — Bedrock's Rerank API, not Cohere's /v2/rerank.
                    return {
                        "results": [
                            {"index": 2, "relevanceScore": 0.9},
                            {"index": 0, "relevanceScore": 0.1},
                            {"index": 1, "relevanceScore": 0.5},
                        ]
                    }

            return _Client()

        monkeypatch.setattr(_bedrock, "get_client", fake_get_client)

        scores = self._client(None).predict(
            [("q", "doc a"), ("q", "doc b"), ("q", "doc c")]
        )

        assert scores == [0.1, 0.5, 0.9]


class TestModelArnResolution:
    def test_a_bare_model_id_becomes_a_foundation_model_arn(self, monkeypatch) -> None:
        monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
        client = reranker_mod._BedrockReranker("cohere.rerank-v3-5:0", timeout_s=2.0)
        assert client._model_arn() == (
            "arn:aws:bedrock:us-east-1::foundation-model/cohere.rerank-v3-5:0"
        )

    def test_an_arn_is_passed_through(self) -> None:
        """A Marketplace deployment is addressed by endpoint ARN, and building
        a foundation-model ARN around one would produce nonsense."""
        arn = "arn:aws:sagemaker:us-east-1:123456789012:endpoint/cohere-rerank"
        client = reranker_mod._BedrockReranker(arn, timeout_s=2.0)
        assert client._model_arn() == arn
