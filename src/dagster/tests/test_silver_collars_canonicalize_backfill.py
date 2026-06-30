"""Unit tests for silver_collars_canonicalize_backfill.

The asset's SQL side is exercised via a MagicMock harness mirroring
``test_silver_entity_ner_backfill.py``. Pure-function coverage of the
canonicalize() rule lives in test_csv_collar_parser.py / hole-ID tests; we
focus here on the row-shaping + batching + workspace-pinning logic.

Run with:
    pytest src/dagster/tests/test_silver_collars_canonicalize_backfill.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from georag_dagster.assets.silver_collars_canonicalize_backfill import (
    BATCH_SIZE,
    SilverCollarsCanonicalizeBackfillConfig,
    silver_collars_canonicalize_backfill,
)

# Bypass the @asset decorator's Dagster wrapper so we can call the raw fn.
_RAW_ASSET_FN = silver_collars_canonicalize_backfill.op.compute_fn.decorated_fn


def _build_mock_postgres(null_rows: list[dict]):
    """MagicMock PostgresResource whose first fetchall returns ``null_rows``."""
    cursor = MagicMock()
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.return_value = null_rows
    cursor.rowcount = 1

    conn = MagicMock()
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    conn.commit = MagicMock()

    postgres = MagicMock()
    postgres.get_connection.return_value = conn
    return postgres, cursor, conn


class TestSilverCollarsCanonicalizeBackfillAsset:
    @patch(
        "georag_dagster.assets.silver_collars_canonicalize_backfill.psycopg2.extras.execute_batch"
    )
    def test_no_rows_no_writes(self, mock_execute_batch):
        """Empty SELECT result → execute_batch never called, commit still runs."""
        postgres, _cursor, conn = _build_mock_postgres(null_rows=[])
        ctx = MagicMock()
        cfg = SilverCollarsCanonicalizeBackfillConfig(workspace_id="")
        result = _RAW_ASSET_FN(ctx, cfg, postgres)

        assert mock_execute_batch.call_count == 0
        conn.commit.assert_called_once()
        meta = result.metadata
        assert meta["rows_scanned"].value == 0
        assert meta["rows_updated"].value == 0
        assert meta["workspace_filter"].value == "(all)"

    @patch(
        "georag_dagster.assets.silver_collars_canonicalize_backfill.psycopg2.extras.execute_batch"
    )
    def test_three_rows_one_batch(self, mock_execute_batch):
        ws = "a0000000-0000-0000-0000-000000000001"
        rows = [
            {"collar_id": "c1", "workspace_id": ws, "hole_id": "PLS-22-08"},
            {"collar_id": "c2", "workspace_id": ws, "hole_id": "leb_23_001"},
            {"collar_id": "c3", "workspace_id": ws, "hole_id": "36-1085"},
        ]
        postgres, _cursor, conn = _build_mock_postgres(null_rows=rows)
        ctx = MagicMock()
        cfg = SilverCollarsCanonicalizeBackfillConfig(workspace_id="")
        result = _RAW_ASSET_FN(ctx, cfg, postgres)

        # One batch call, three payload rows, each canonical filled.
        assert mock_execute_batch.call_count == 1
        _cursor_arg, _sql, payload = mock_execute_batch.call_args[0][:3]
        canonical_by_collar = {p["collar_id"]: p["hole_id_canonical"] for p in payload}
        assert canonical_by_collar["c1"] == "PLS2208"
        assert canonical_by_collar["c2"] == "LEB23001"
        assert canonical_by_collar["c3"] == "361085"
        # Workspace pinned on every row → no cross-tenant risk.
        assert all(p["workspace_id"] == ws for p in payload)

        conn.commit.assert_called_once()
        assert result.metadata["rows_scanned"].value == 3
        assert result.metadata["rows_updated"].value == 3
        assert result.metadata["workspaces_touched"].value == 1

    @patch(
        "georag_dagster.assets.silver_collars_canonicalize_backfill.psycopg2.extras.execute_batch"
    )
    def test_blank_canonical_skipped(self, mock_execute_batch):
        """hole_id that canonicalizes to None (separators only) is skipped."""
        ws = "a0000000-0000-0000-0000-000000000001"
        rows = [
            {"collar_id": "c1", "workspace_id": ws, "hole_id": "PLS-22-08"},
            {"collar_id": "c2", "workspace_id": ws, "hole_id": "  ---  "},
        ]
        postgres, _cursor, _conn = _build_mock_postgres(null_rows=rows)
        ctx = MagicMock()
        cfg = SilverCollarsCanonicalizeBackfillConfig(workspace_id="")
        result = _RAW_ASSET_FN(ctx, cfg, postgres)

        # Only one row makes it into the batch payload.
        payload = mock_execute_batch.call_args[0][2]
        assert len(payload) == 1
        assert payload[0]["collar_id"] == "c1"
        assert result.metadata["rows_skipped_blank_canonical"].value == 1
        assert result.metadata["rows_updated"].value == 1

    @patch(
        "georag_dagster.assets.silver_collars_canonicalize_backfill.psycopg2.extras.execute_batch"
    )
    def test_batches_chunk_at_batch_size(self, mock_execute_batch):
        """More than BATCH_SIZE rows → multiple execute_batch calls."""
        ws = "a0000000-0000-0000-0000-000000000001"
        rows = [
            {"collar_id": f"c{i}", "workspace_id": ws, "hole_id": f"HOLE-{i:04d}"}
            for i in range(BATCH_SIZE + 5)
        ]
        postgres, _cursor, _conn = _build_mock_postgres(null_rows=rows)
        ctx = MagicMock()
        cfg = SilverCollarsCanonicalizeBackfillConfig(workspace_id="")
        result = _RAW_ASSET_FN(ctx, cfg, postgres)

        # 100 + 5 = 2 batches.
        assert mock_execute_batch.call_count == 2
        # First batch = BATCH_SIZE rows.
        first_payload = mock_execute_batch.call_args_list[0][0][2]
        second_payload = mock_execute_batch.call_args_list[1][0][2]
        assert len(first_payload) == BATCH_SIZE
        assert len(second_payload) == 5
        assert result.metadata["rows_updated"].value == BATCH_SIZE + 5

    @patch(
        "georag_dagster.assets.silver_collars_canonicalize_backfill.psycopg2.extras.execute_batch"
    )
    def test_workspace_filter_changes_select(self, mock_execute_batch):
        """workspace_id config narrows the SELECT to that workspace."""
        ws = "b0000000-0000-0000-0000-000000000099"
        postgres, cursor, _conn = _build_mock_postgres(null_rows=[])
        ctx = MagicMock()
        cfg = SilverCollarsCanonicalizeBackfillConfig(workspace_id=ws)
        _RAW_ASSET_FN(ctx, cfg, postgres)

        # The first cursor.execute call carries the workspace_id param.
        first_call = cursor.execute.call_args_list[0]
        sql, params = first_call[0]
        assert "workspace_id = %(workspace_id)s::uuid" in sql
        assert params == {"workspace_id": ws}


class TestRowShape:
    """Verify the canonicalize logic produces the expected DB values."""

    def test_hole_36_1085_canonicalizes_to_361085(self):
        """The smoke-test target hole from the chat-cards spec."""
        from georag_dagster.parsers._hole_id import canonicalize
        assert canonicalize("36-1085") == "361085"

    def test_idempotency_filter_present_in_update_sql(self):
        """Re-running the asset must be a no-op — verify the SQL filter."""
        from georag_dagster.assets.silver_collars_canonicalize_backfill import (
            UPDATE_CANONICAL_SQL,
        )
        assert "hole_id_canonical IS NULL" in UPDATE_CANONICAL_SQL
        assert "workspace_id = %(workspace_id)s::uuid" in UPDATE_CANONICAL_SQL
