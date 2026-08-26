"""A gold promotion that cannot be dispatched must cost a delay, not a run.

WHY THIS FILE EXISTS
    ``ingest_tabular`` and ``nightly_ingestion_integrity`` both dispatch
    ``promote_silver_to_gold`` with ``aio_run_no_wait``. The name reads as
    fire-and-forget and the comment above the call said so, but ``no_wait``
    only declines to wait for the promotion's RESULT — the dispatch RPC is
    still awaited, and against an unreachable Hatchet the SDK retries for
    ~17 seconds before raising.

    Both call sites swallowed that exception by design, so nothing ever
    failed and nothing pointed at it. What it actually bought:

      - production: a Hatchet outage held an ingest worker slot open for
        ~17s per file, after the user-visible completion had already
        broadcast, for a promotion the nightly sweep re-runs anyway;
      - the nightly sweep: the per-workspace loop sat inside ONE try, so
        the first unreachable workspace aborted promotion for every
        workspace after it — and ``promotions_dispatched`` was never set,
        so the report did not say so;
      - CI: ~17s on every unit test that ran one of these workflows to
        completion. One file paid it 30 times — 393 seconds — which pushed
        the pytest job past its own ``timeout-minutes: 10``. The job was
        cancelled three times and read as a mysterious external failure.

WHAT IS UNDER TEST
    ``dispatch_promotion`` — the one place the bound and the swallow live.
    It exists precisely so the two call sites cannot drift: the bound was
    added to both by hand first, which is the arrangement that produces a
    fix on one and not the other.

WHAT THIS FILE DOES NOT COVER
    That the promotion itself is correct. That is
    test_promote_silver_to_gold.py. Here the promotion never runs — only
    the handing-off of it does.
"""
from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / "app" / "hatchet_workflows"

WS = "a0000000-0000-0000-0000-00000000feed"
PROJECT = "b1000000-0000-0000-0000-0000000000a0"

#: Outer guard for the two timing tests, as a multiple of the bound each
#: one puts under test. Derived rather than configured so the guard cannot
#: be tightened past the thing it is guarding; wide enough that a loaded CI
#: runner will not trip it, and only ever waited out on the failure path.
_OUTER_GUARD_FACTOR = 20


@pytest.fixture
def promo():
    from app.hatchet_workflows import promote_silver_to_gold as mod
    return mod


# ---------------------------------------------------------------------------
# The bound
# ---------------------------------------------------------------------------

class TestAHangingDispatchIsAbandoned:
    """The failure that motivated this: a dispatch that never answers."""

    async def test_it_returns_instead_of_hanging(self, promo, monkeypatch):
        """The whole point. Unbounded, this never returns."""
        bound = 0.05
        monkeypatch.setattr(promo, "PROMOTION_DISPATCH_TIMEOUT_S", bound)

        async def _never_answers(_payload):
            await asyncio.sleep(3600)

        monkeypatch.setattr(
            promo.promote_silver_to_gold, "aio_run_no_wait", _never_answers,
        )

        started = time.monotonic()
        try:
            # Derived from the bound under test, never configured
            # separately: if dispatch_promotion stops applying its
            # timeout this FAILS here instead of hanging the suite —
            # which is how the original bug stayed invisible.
            ok = await asyncio.wait_for(
                promo.dispatch_promotion(workspace_id=WS),
                timeout=bound * _OUTER_GUARD_FACTOR,
            )
        except TimeoutError:
            pytest.fail(
                f"dispatch_promotion did not return within "
                f"{bound * _OUTER_GUARD_FACTOR:.1f}s "
                f"against a {bound}s bound — it is not applying its timeout"
            )
        elapsed = time.monotonic() - started

        assert ok is False, "a dispatch that never answered was reported OK"
        assert elapsed < bound * _OUTER_GUARD_FACTOR

    async def test_the_timeout_is_the_module_constant_not_a_literal(
        self, promo, monkeypatch,
    ):
        """Raising the constant must actually raise the wait.

        Pins that the value is read at call time. A constant that is
        imported once and captured would make this test pass while the
        bound silently ignored any change to it.
        """
        bound = 0.30
        monkeypatch.setattr(promo, "PROMOTION_DISPATCH_TIMEOUT_S", bound)

        async def _never_answers(_payload):
            await asyncio.sleep(3600)

        monkeypatch.setattr(
            promo.promote_silver_to_gold, "aio_run_no_wait", _never_answers,
        )

        started = time.monotonic()
        try:
            await asyncio.wait_for(
                promo.dispatch_promotion(workspace_id=WS),
                # Derived: a lost bound fails here rather than hanging.
                timeout=bound * _OUTER_GUARD_FACTOR,
            )
        except TimeoutError:
            pytest.fail("dispatch_promotion is not applying any timeout")
        elapsed = time.monotonic() - started

        assert elapsed >= bound * 0.8, (
            f"waited only {elapsed:.2f}s for a {bound}s bound — the constant "
            f"is not the value in force"
        )

    def test_the_default_bound_is_short_enough_to_matter(self, promo):
        """A bound above the ~17s it exists to cut would be decorative."""
        assert 0 < promo.PROMOTION_DISPATCH_TIMEOUT_S < 17.0


# ---------------------------------------------------------------------------
# The swallow
# ---------------------------------------------------------------------------

class TestItNeverRaisesAtTheCaller:
    """Callers dispatch AFTER committing to silver. A raise there would
    relabel a landed ingest as failed."""

    async def test_a_raising_dispatch_is_reported_not_raised(
        self, promo, monkeypatch,
    ):
        async def _boom(_payload):
            raise ConnectionRefusedError("no hatchet here")

        monkeypatch.setattr(
            promo.promote_silver_to_gold, "aio_run_no_wait", _boom,
        )

        assert await promo.dispatch_promotion(workspace_id=WS) is False

    async def test_a_successful_dispatch_says_so(self, promo, monkeypatch):
        seen = []

        async def _ok(payload):
            seen.append(payload)

        monkeypatch.setattr(
            promo.promote_silver_to_gold, "aio_run_no_wait", _ok,
        )

        assert await promo.dispatch_promotion(
            workspace_id=WS, project_id=PROJECT,
        ) is True
        assert len(seen) == 1
        assert str(seen[0].workspace_id) == WS
        assert str(seen[0].project_id) == PROJECT

    async def test_the_workspace_reaches_the_payload(self, promo, monkeypatch):
        """The tables promoted are fail-CLOSED under RLS: a dispatch that
        lost the workspace would promote nothing and report success."""
        seen = []

        async def _ok(payload):
            seen.append(payload)

        monkeypatch.setattr(
            promo.promote_silver_to_gold, "aio_run_no_wait", _ok,
        )

        await promo.dispatch_promotion(workspace_id=WS)

        assert str(seen[0].workspace_id) == WS
        assert seen[0].project_id is None, (
            "a workspace-wide sweep must not be narrowed to a project"
        )


# ---------------------------------------------------------------------------
# The nightly loop
# ---------------------------------------------------------------------------

class TestOneBadWorkspaceDoesNotStopTheSweep:
    """The second defect: the per-workspace loop inside a single try."""

    def _tier3_source(self) -> str:
        return (WORKFLOWS / "nightly_ingestion_integrity.py").read_text(
            encoding="utf-8",
        )

    def test_the_loop_counts_successes_not_the_workspace_total(self):
        """``promotions_dispatched`` must be the count that was accepted.

        Reporting ``len(workspace_ids)`` would say every workspace was
        promoted on a night when none were.
        """
        src = self._tier3_source()
        assert 'report.extras["promotions_dispatched"] = dispatched' in src, (
            "promotions_dispatched is not the accepted count"
        )
        assert 'report.extras["promotions_dispatched"] = len(' not in src, (
            "promotions_dispatched reports the workspace total, so a night "
            "where every dispatch failed still reads as fully promoted"
        )

    def test_the_failures_are_reported_too(self):
        src = self._tier3_source()
        assert 'report.extras["promotions_failed"]' in src

    def test_the_loop_does_not_reimplement_the_dispatch(self):
        """Both call sites must go through dispatch_promotion.

        A call site that reaches past it to ``aio_run_no_wait`` gets the
        unbounded await back, which is the bug this file is about.
        """
        for name in ("nightly_ingestion_integrity.py", "ingest_tabular.py"):
            src = (WORKFLOWS / name).read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                if node.attr != "aio_run_no_wait":
                    continue
                target = getattr(node.value, "id", None) or getattr(
                    node.value, "attr", None,
                )
                assert target != "promote_silver_to_gold", (
                    f"{name} dispatches promote_silver_to_gold directly "
                    f"instead of via dispatch_promotion, so it does not get "
                    f"the timeout"
                )
