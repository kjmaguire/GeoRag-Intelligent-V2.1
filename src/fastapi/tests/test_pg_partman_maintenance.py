"""Registration contract for the pg_partman maintenance cron."""

from __future__ import annotations

from app.hatchet_workflows.pg_partman_maintenance import pg_partman_maintenance
from app.hatchet_workflows.worker import POOLS


def test_pg_partman_maintenance_is_registered_with_expected_cron() -> None:
    assert pg_partman_maintenance in POOLS["ai"]
    assert pg_partman_maintenance in POOLS["all"]

    crons = (
        getattr(pg_partman_maintenance.config, "on_crons", None)
        or getattr(pg_partman_maintenance, "on_crons", None)
    )
    # 19:15 UTC. Moved from 04:15 on 2026-09-16, when the shutdown window
    # shrank to 09:00-17:00 Pacific and closed 00:00-17:00 UTC. The literal
    # is fine here -- unlike repair_shadow_aggregate this slot encodes no
    # relationship -- but the constraint that actually matters (that it is
    # not inside the window) is enforced by
    # tests/test_crons_avoid_the_shutdown_window.py, which derives the
    # window from the Terraform rather than restating it.
    assert crons == ["15 19 * * *"]
