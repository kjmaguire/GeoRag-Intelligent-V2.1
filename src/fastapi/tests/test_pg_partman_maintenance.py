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
    assert crons == ["15 4 * * *"]
