"""idempotency_keys_cleanup: `older_than_days` cannot delete live keys.

`older_than_days` was an unbounded int. 0 made the cutoff `now()`, so
`created_at < now()` matched every row; a negative number put the cutoff in the
future, same result. Each row is an agent's "already done" record (R2: 30-day TTL,
R3 and up: 90), so wiping them lets a retried agent run write twice. The bound is
the shortest TTL any row is given.
"""
from __future__ import annotations

import inspect
import re

import pytest
from pydantic import ValidationError

from app.agents import wrapper
from app.hatchet_workflows import idempotency_keys_cleanup as ikc


def test_the_cron_path_sends_nothing_and_is_valid() -> None:
    assert ikc.CleanupInput().older_than_days is None


@pytest.mark.parametrize("days", [0, 1, 29, -1, -365])
def test_an_age_that_would_delete_live_keys_is_refused(days: int) -> None:
    with pytest.raises(ValidationError):
        ikc.CleanupInput(older_than_days=days)


@pytest.mark.parametrize("days", [30, 90, 365, 3650])
def test_an_age_at_or_past_the_shortest_ttl_is_accepted(days: int) -> None:
    assert ikc.CleanupInput(older_than_days=days).older_than_days == days


def test_an_absurd_age_is_refused() -> None:
    with pytest.raises(ValidationError):
        ikc.CleanupInput(older_than_days=3651)


def test_the_bound_is_the_shortest_ttl_the_wrapper_assigns() -> None:
    """The two numbers live in different modules; keep them from drifting."""
    source = inspect.getsource(wrapper._idempotency_store)
    ttls = [int(n) for n in re.findall(r'"R\d"\s*:\s*(\d+)', source)]
    assert ttls, "could not find the per-tier TTL table in _idempotency_store"
    assert min(ttls) == ikc.SHORTEST_KEY_TTL_DAYS
