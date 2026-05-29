"""Schedule registration tests — silver_dq_daily_schedule (§6a).

Pins the daily 04:00 UTC sweep that re-materialises the four §6a DQ
rule families so the DataQualityFlagsBadge tracks live data drift
without manual triggers.

Run with:
    pytest src/dagster/tests/test_silver_dq_daily_schedule.py -v
"""

from __future__ import annotations

from dagster import DefaultScheduleStatus

from georag_dagster.definitions import defs, silver_dq_daily_schedule


class TestSilverDqDailySchedule:
    def test_schedule_is_registered_in_definitions(self):
        """Schedule appears in the live Definitions object."""
        registered_names = {s.name for s in defs.schedules}
        assert "silver_dq_daily_schedule" in registered_names

    def test_schedule_runs_daily_at_0400_utc(self):
        """Cron 04:00 UTC fires 2h after the global full_ingest_schedule
        (02:00 UTC) so the DQ assets see freshly-loaded silver data."""
        assert silver_dq_daily_schedule.cron_schedule == "0 4 * * *"

    def test_schedule_default_status_is_stopped(self):
        """Per the silver_dq_daily_schedule comment + matching the
        full_ingest_schedule convention — operator enables in Dagster
        UI once they're ready for the daily badge cadence."""
        assert (
            silver_dq_daily_schedule.default_status
            == DefaultScheduleStatus.STOPPED
        )

    def test_schedule_targets_all_four_dq_assets(self):
        """All four §6a rule families must be in the asset selection.
        Missing one would silently drop a domain (e.g. assay or CRS)
        from the daily badge refresh."""
        sel = silver_dq_daily_schedule.target.job_def.selection
        keys = sel.resolve(defs.resolve_asset_graph())
        key_names = {k.to_user_string() for k in keys}
        for asset_name in (
            "silver_collar_dq",
            "silver_assay_dq",
            "silver_crs_dq",
            "silver_unit_consistency_dq",
        ):
            assert asset_name in key_names, (
                f"{asset_name} missing from silver_dq_daily_schedule selection"
            )

    def test_schedule_does_not_pull_in_unrelated_assets(self):
        """The selection should be precisely the four DQ rule-family
        assets — no transitive deps that would slow the sweep or
        accidentally re-materialise upstream silver assets."""
        sel = silver_dq_daily_schedule.target.job_def.selection
        keys = sel.resolve(defs.resolve_asset_graph())
        key_names = {k.to_user_string() for k in keys}
        # Exactly the four assets, no more.
        assert key_names == {
            "silver_collar_dq",
            "silver_assay_dq",
            "silver_crs_dq",
            "silver_unit_consistency_dq",
        }
