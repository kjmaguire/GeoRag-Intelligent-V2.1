"""The reranker's retry budget must fit inside its caller's timeout (2026-08-20).

`search_documents` wraps `reranker.predict` in
``asyncio.wait_for(..., TIMEOUT_RERANKER_S)``. The foundry backend's retry
path (`_foundry_retry.with_foundry_retry`) was written for ingestion, where
spending 70s recovering from a 429 is fine. Reused on the interactive query
path under an 8s wait_for — with an 8s per-call HTTP timeout of its own —
the retry was unreachable: a single 429 meant the wait_for fired first, the
branch degraded to raw Qdrant cosine ordering, and the executor thread kept
retrying in the background against a result nobody would read.

Two invariants are pinned here:

  1. `with_foundry_retry` will not *start* a sleep that runs past a caller's
     deadline — it surfaces the failure instead of being cancelled.
  2. The budgets are DERIVED from one setting, not configured twice. The
     original bug was two independently-set 8.0s values that looked
     compatible and were not.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.services import _foundry_retry as retry_mod
from app.services import reranker as reranker_mod
from app.services._foundry_retry import with_foundry_retry


class _Resp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class TestDeadlineAwareRetry:
    def test_no_deadline_keeps_the_ingestion_behaviour(self, monkeypatch) -> None:
        """Ingestion callers must not lose their patience."""
        slept: list[float] = []
        monkeypatch.setattr(retry_mod.time, "sleep", lambda s: slept.append(s))
        responses = iter([_Resp(429), _Resp(429), _Resp(429), _Resp(429), _Resp(200)])

        resp = with_foundry_retry(lambda: next(responses), label="t", max_retries=4)

        assert resp.status_code == 200
        assert slept == [2.0, 4.0, 8.0, 16.0]

    def test_stops_when_the_next_backoff_would_pass_the_deadline(
        self, monkeypatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr(retry_mod.time, "sleep", lambda s: slept.append(s))
        calls: list[int] = []

        def do_post():
            calls.append(1)
            return _Resp(429)

        # 3s of budget: the first 2s backoff fits, the following 4s does not.
        with pytest.raises(RuntimeError, match="status 429"):
            with_foundry_retry(
                do_post,
                label="t",
                max_retries=4,
                deadline=time.monotonic() + 3.0,
            )

        assert slept == [2.0], "only the backoff that fit should have happened"
        assert len(calls) == 2, "one initial call plus one retry"

    def test_raises_rather_than_returning_a_bad_response(self, monkeypatch) -> None:
        """The caller must see a failure, not a 429 masquerading as a result.

        Returning the response would have `predict` call `.json()` on an
        error body and score every passage 0.0 — worse than degrading, it
        would be a *wrong* ordering presented as a real one.
        """
        monkeypatch.setattr(retry_mod.time, "sleep", lambda _s: None)

        with pytest.raises(RuntimeError, match="status 503"):
            with_foundry_retry(
                lambda: _Resp(503),
                label="t",
                max_retries=4,
                deadline=time.monotonic() - 1.0,  # already blown
            )

    def test_an_expired_deadline_still_makes_the_first_call(self, monkeypatch) -> None:
        """The deadline bounds retries, not the attempt the caller asked for."""
        monkeypatch.setattr(retry_mod.time, "sleep", lambda _s: None)
        calls: list[int] = []

        def do_post():
            calls.append(1)
            return _Resp(200)

        resp = with_foundry_retry(
            do_post, label="t", deadline=time.monotonic() - 10.0
        )

        assert resp.status_code == 200
        assert len(calls) == 1

    def test_retry_after_header_is_also_deadline_checked(self, monkeypatch) -> None:
        """A server-sent Retry-After can exceed the budget just as easily."""
        slept: list[float] = []
        monkeypatch.setattr(retry_mod.time, "sleep", lambda s: slept.append(s))

        with pytest.raises(RuntimeError, match="status 429"):
            with_foundry_retry(
                lambda: _Resp(429, {"retry-after": "25"}),
                label="t",
                max_retries=4,
                deadline=time.monotonic() + 5.0,
            )

        assert slept == [], "a 25s Retry-After must not be honoured on a 5s budget"


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

    def test_foundry_per_call_timeout_leaves_room_for_a_retry(
        self, monkeypatch
    ) -> None:
        """The original bug in one assertion.

        An 8.0s HTTP timeout under an 8.0s wait_for meant one slow call
        consumed the entire budget. Whatever the configured per-call
        timeout, it must not exceed half the total budget.
        """
        from app.config import settings

        monkeypatch.setattr(settings, "TIMEOUT_RERANKER_S", 20.0)
        monkeypatch.setattr(reranker_mod, "RERANKER_BACKEND", "foundry")
        monkeypatch.setattr(reranker_mod, "AZURE_FOUNDRY_RERANK_DEPLOYMENT", "rerank-v4")
        monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com")
        monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "fake-key")

        client = reranker_mod.get_reranker_or_none()

        assert isinstance(client, reranker_mod._FoundryReranker)
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


class TestFoundryRerankerHonoursItsDeadline:
    def _client(self, budget: float | None):
        return reranker_mod._FoundryReranker(
            "https://example.services.ai.azure.com",
            "fake-key",
            "rerank-v4",
            timeout_s=2.0,
            total_budget_s=budget,
        )

    def test_deadline_is_shared_across_query_groups(self, monkeypatch) -> None:
        """Two groups must split one budget, not claim it each.

        `predict` groups pairs by query. Computing the deadline per group
        would let N queries spend N x the budget the caller allowed.
        """
        seen: list[float | None] = []

        def fake_retry(_do, *, label, max_retries, max_backoff_s, deadline):
            seen.append(deadline)
            class _R:
                @staticmethod
                def json():
                    return {"results": [{"index": 0, "relevance_score": 0.5}]}
            return _R()

        with patch("app.services._foundry_retry.with_foundry_retry", fake_retry), \
             patch("httpx.Client"):
            self._client(19.0).predict([("q1", "doc a"), ("q2", "doc b")])

        assert len(seen) == 2
        assert seen[0] == seen[1], "both groups must share one deadline"

    def test_no_budget_passes_no_deadline(self) -> None:
        """Eval/script callers with no wait_for above them keep old behavior."""
        seen: list[float | None] = []

        def fake_retry(_do, *, label, max_retries, max_backoff_s, deadline):
            seen.append(deadline)
            class _R:
                @staticmethod
                def json():
                    return {"results": [{"index": 0, "relevance_score": 0.5}]}
            return _R()

        with patch("app.services._foundry_retry.with_foundry_retry", fake_retry), \
             patch("httpx.Client"):
            self._client(None).predict([("q1", "doc a")])

        assert seen == [None]

    def test_interactive_retry_profile_is_tighter_than_ingestion(self) -> None:
        """A geologist waiting on a chat answer should not wait 70s for a
        perfect ordering."""
        captured: dict[str, object] = {}

        def fake_retry(_do, *, label, max_retries, max_backoff_s, deadline):
            captured["max_retries"] = max_retries
            captured["max_backoff_s"] = max_backoff_s
            class _R:
                @staticmethod
                def json():
                    return {"results": [{"index": 0, "relevance_score": 0.5}]}
            return _R()

        with patch("app.services._foundry_retry.with_foundry_retry", fake_retry), \
             patch("httpx.Client"):
            self._client(19.0).predict([("q1", "doc a")])

        assert captured["max_retries"] < 4, "ingestion default is 4"
        assert captured["max_backoff_s"] < 30.0, "ingestion default is 30s"
        # Worst case must still fit: retries x max backoff + calls <= budget.
        worst_backoff = captured["max_retries"] * captured["max_backoff_s"]
        assert worst_backoff < 19.0
