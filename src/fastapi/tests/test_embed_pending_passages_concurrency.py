"""Locks the concurrency contract on embed_pending_passages_wf.

The workflow is a per-workspace singleton: same-workspace runs queue
(GROUP_ROUND_ROBIN), different-workspace runs proceed in parallel. This
prevents the every-10-min safety-net cron from racing the daily bulk
sync — and prevents a Hatchet retry from clobbering an in-flight run.

If anyone changes the expression, max_runs, or limit strategy, this
test wakes up.
"""
from __future__ import annotations

from hatchet_sdk import ConcurrencyLimitStrategy


def test_embed_pending_passages_has_per_workspace_singleton_concurrency():
    from app.hatchet_workflows.embed_pending_passages import (
        embed_pending_passages_wf,
    )

    cfg = embed_pending_passages_wf.config
    assert cfg.concurrency is not None, (
        "embed_pending_passages_wf must declare concurrency — "
        "without it the every-10-min cron can race a long-running "
        "daily bulk run."
    )

    expr = cfg.concurrency
    assert expr.expression == "input.workspace_id", (
        f"Expected per-workspace grouping, got {expr.expression!r}"
    )
    assert expr.max_runs == 1, (
        f"Expected max_runs=1 (singleton per workspace), got {expr.max_runs}"
    )
    assert expr.limit_strategy == ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN, (
        f"Expected GROUP_ROUND_ROBIN (queue, do not cancel), "
        f"got {expr.limit_strategy}"
    )
