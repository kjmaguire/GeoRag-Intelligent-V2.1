"""Schedule registration tests — silver_chat_cards_backfill_schedule.

Covers the silver_entity_ner_backfill side of the chat-cards backfill
schedule (Part B of the 2026-05-25 chat-cards backfill scope). The schedule
itself materialises BOTH silver_structure_populate AND
silver_entity_ner_backfill — see test_structure_populate_schedule.py for
the structure_populate side and the shared cadence assertion.

Run with:
    pytest src/dagster/tests/test_ner_backfill_schedule.py -v
"""

from __future__ import annotations

from dagster import DefaultScheduleStatus

from georag_dagster.definitions import defs, silver_chat_cards_backfill_schedule


class TestSilverChatCardsBackfillScheduleRegistration:
    def test_schedule_is_registered_in_definitions(self):
        """The schedule appears in the live Definitions object."""
        registered_names = {s.name for s in defs.schedules}
        assert "silver_chat_cards_backfill_schedule" in registered_names

    def test_schedule_runs_every_30_minutes(self):
        assert silver_chat_cards_backfill_schedule.cron_schedule == "*/30 * * * *"

    def test_schedule_targets_ner_backfill_asset(self):
        """silver_entity_ner_backfill is in the schedule's asset selection."""
        # The target wraps an UnresolvedAssetJobDefinition whose ``selection``
        # is the original KeysAssetSelection. Resolve against the live
        # asset graph to confirm the asset key is present.
        sel = silver_chat_cards_backfill_schedule.target.job_def.selection
        keys = sel.resolve(defs.resolve_asset_graph())
        key_names = {k.to_user_string() for k in keys}
        assert "silver_entity_ner_backfill" in key_names

    def test_schedule_default_status_is_running(self):
        """Schedule is enabled by default — new projects auto-populate."""
        assert (
            silver_chat_cards_backfill_schedule.default_status
            == DefaultScheduleStatus.RUNNING
        )

    def test_schedule_description_calls_out_chat_cards(self):
        desc = silver_chat_cards_backfill_schedule.description or ""
        assert "chat-card" in desc.lower() or "chat cards" in desc.lower()
