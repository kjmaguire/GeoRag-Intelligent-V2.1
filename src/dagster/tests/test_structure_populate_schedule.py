"""Schedule registration tests — silver_structure_populate side.

Covers the silver_structure_populate side of the chat-cards backfill
schedule (Part B of the 2026-05-25 chat-cards backfill scope). The
schedule materialises both PR-2 (structure_populate) and PR-3
(entity_ner_backfill) at the same cadence — see
test_ner_backfill_schedule.py for the other side.

Run with:
    pytest src/dagster/tests/test_structure_populate_schedule.py -v
"""

from __future__ import annotations

from dagster import DefaultScheduleStatus

from georag_dagster.definitions import defs, silver_chat_cards_backfill_schedule


class TestSilverStructurePopulateScheduleRegistration:
    def test_schedule_targets_structure_populate_asset(self):
        sel = silver_chat_cards_backfill_schedule.target.job_def.selection
        keys = sel.resolve(defs.resolve_asset_graph())
        key_names = {k.to_user_string() for k in keys}
        assert "silver_structure_populate" in key_names

    def test_schedule_runs_every_30_minutes(self):
        # Same cadence assertion as the NER-side test — duplicated on
        # purpose so either test passes / fails independently when the
        # cadence is tuned.
        assert silver_chat_cards_backfill_schedule.cron_schedule == "*/30 * * * *"

    def test_schedule_default_status_is_running(self):
        assert (
            silver_chat_cards_backfill_schedule.default_status
            == DefaultScheduleStatus.RUNNING
        )

    def test_schedule_covers_both_assets(self):
        """One schedule, both PR-2 + PR-3 asset selections."""
        sel = silver_chat_cards_backfill_schedule.target.job_def.selection
        keys = sel.resolve(defs.resolve_asset_graph())
        key_names = {k.to_user_string() for k in keys}
        assert "silver_structure_populate" in key_names
        assert "silver_entity_ner_backfill" in key_names
