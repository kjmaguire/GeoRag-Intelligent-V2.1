"""Where a cost-burn alert actually goes.

This file used to be the PagerDuty dispatcher's test suite. The dispatcher
was deleted on 2026-08-28: `create_pagerduty_incident` was a complete and
correct Events v2 client -- dedup keys, severity mapping, retry handling --
with no caller outside its own package re-export, `PAGERDUTY_INTEGRATION_KEY`
empty and set on no container app, and no PagerDuty account behind it. A full
passing test suite for it made CI read as "PagerDuty alerting verified" while
nothing dispatched.

What survives here is the part that was never about PagerDuty: the tests
pinning the escalation route that DOES reach a human. A detector logs a
distinctive marker, a Log Analytics scheduled query rule matches it, and
`georag-alerts-ag` emails a real address. These pin the cost-burn detector
onto it, because until 2026-08-22 its "high" severity alert -- the one that
precedes suspending a workspace's LLM activity -- terminated in a database
row that reached nobody.
"""
from __future__ import annotations

import importlib.util


def test_retired_dispatchers_are_not_importable() -> None:
    """Both retired dispatchers stay retired.

    `kestra` was removed earlier; `pagerduty` went on 2026-08-28. If either
    comes back, it needs a caller and a configured key in the same change --
    otherwise it is a passing test suite for a capability the platform does
    not have, which is exactly why both were deleted.
    """
    assert importlib.util.find_spec("app.services.dispatchers") is None


class TestTheEscalationPathThatExists:
    """The log-marker route, end to end.

    Each test pins one link in the chain. Break any one and the alert is
    written and read by nobody, with nothing failing to say so.
    """

    def test_the_cost_burn_alert_leaves_the_database(self) -> None:
        import inspect

        from app.hatchet_workflows import cost_burn_watcher

        assert cost_burn_watcher.COST_BURN_ALERT_MARKER

        source = inspect.getsource(cost_burn_watcher)
        assert "COST_BURN_ALERT_MARKER," in source, (
            "the marker is defined but never logged, so the alert still "
            "ends in audit.audit_ledger and reaches nobody"
        )

    def test_the_marker_is_distinctive_enough_to_match_on(self) -> None:
        """It is matched with `Log_s has '<marker>'`. A short or
        lower-case marker would match ordinary log prose and stack
        traces, and the alert would fire on nothing."""
        from app.hatchet_workflows.cost_burn_watcher import (
            COST_BURN_ALERT_MARKER,
        )

        assert COST_BURN_ALERT_MARKER.isupper()
        assert "_" in COST_BURN_ALERT_MARKER
        assert len(COST_BURN_ALERT_MARKER) > 12

    def test_an_alert_rule_matches_the_marker(self) -> None:
        """The marker and the rule are in different repos-worth of file
        and drift silently: the log line keeps being written and nothing
        is listening."""
        from pathlib import Path

        from app.hatchet_workflows.cost_burn_watcher import (
            COST_BURN_ALERT_MARKER,
        )

        repo = Path(__file__).resolve().parents[3]
        script = (repo / "deploy" / "azure" / "alerts" / "create-alerts.sh").read_text(
            encoding="utf-8"
        )

        assert COST_BURN_ALERT_MARKER in script, (
            "no scheduled query rule matches the cost-burn marker, so the "
            "log line goes nowhere"
        )

    def test_the_alert_does_not_carry_query_text(self) -> None:
        """A 30-day log store is not where customer questions belong —
        the same rule sparse_encoder was fixed for."""
        import inspect

        from app.hatchet_workflows import cost_burn_watcher

        source = inspect.getsource(cost_burn_watcher)
        marker_call = source[source.index("COST_BURN_ALERT_MARKER,"):][:400]

        for leaky in ("query_text", "question", "prompt"):
            assert leaky not in marker_call, leaky
