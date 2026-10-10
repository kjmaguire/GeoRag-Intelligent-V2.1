"""Workflow-level tests for ``ingest_well_logs``.

WHY THIS FILE EXISTS
    The audit's ask for the three restored geology workflows was "one
    workflow-level test per workflow asserting an ingest_progress row
    reaches a terminal state for both a good and a bad fixture". This is
    that test for the LAS path, and it runs the whole task body -- the
    same code the worker runs -- with the storage client, the LAS parser,
    asyncpg and the progress module faked at their real boundaries.

    A terminal progress row is not a nicety. When one is missing the
    upload shows as in-progress in the Ingestion Runs UI until the
    15-minute stale sweep times it out with no explanation, which is
    exactly what the Cameco incident looked like from the outside: 529
    runs silently CANCELLED. Two separate bugs in these workflows had that
    same signature --

      * ``input.run_id or start_run(...)`` short-circuited, so the INSERT
        never fired and every later UPDATE matched zero rows;
      * ``mark_failed_by_run(error_text=...)`` raised TypeError INSIDE the
        except block, replacing the real error and leaving the row open.

    Both are on the failure path, which is why the happy-path-only tests
    that existed never saw them. Every test here asserts on the progress
    calls, not just the return value.

WHAT THIS FILE DOES NOT COVER
    Real LAS parsing (``src/georag_geoparsers/tests/`` owns that) and the
    SQL's validity against the live schema (integration bucket).
"""
from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.hatchet_workflows import _progress
from app.hatchet_workflows.ingest_well_logs import (
    _MIN_SAMPLES,
    IngestWellLogsInput,
    run_ingest_well_logs,
)

#: ``run_ingest_well_logs`` imports the LAS parser lazily from the
#: georag_geoparsers path dependency. See the note in
#: test_ingest_tabular_persist.py for how to run these locally.
needs_geoparsers = pytest.mark.skipif(
    importlib.util.find_spec("georag_geoparsers") is None,
    reason=(
        "georag_geoparsers path dependency not importable; run with "
        'PYTHONPATH=".;../georag_geoparsers"'
    ),
)

WS = "a0000000-0000-0000-0000-00000000feed"
PROJECT = "b1000000-0000-0000-0000-0000000000a0"
RUN = "c2000000-0000-0000-0000-000000000007"
COLLAR = "d3000000-0000-0000-0000-000000000011"

pytestmark = [pytest.mark.asyncio, needs_geoparsers]


def curve(name: str = "GR", samples: int = 100, **overrides: Any) -> SimpleNamespace:
    base = dict(
        name=name, unit="API", description="Gamma ray",
        min_depth=0.0, max_depth=300.0, step=0.15, null_value=-999.25,
        sample_count=samples,
        depths=[0.0, 0.15], values=[12.0, 14.0],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def las_result(**overrides: Any) -> SimpleNamespace:
    base = dict(
        curves=[curve()], skipped_curves=0, well_name="EAGLE PT #1",
        depth_curve_name="DEPT", las_version="2.0",
        total_curves_in_file=1, parse_quality_pct=100.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeConn:
    def __init__(self, replaced: int = 0, stored: list[dict] | None = None) -> None:
        self.executed: list[tuple] = []
        #: (sql, args) of every fetchval — the workflow's only one is the
        #: replace-delete, and WHAT IT MATCHES is the thing worth pinning.
        self.fetchvals: list[tuple[str, tuple]] = []
        self.replaced = replaced
        #: Rows ``fetch`` answers with: the curves the hole already holds
        #: (las_curve_conflicts.fetch_stored_curves), none by default.
        self.stored = stored or []
        self.closed = False
        self.transactions = 0

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append(args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetchvals.append((sql, args))
        return self.replaced

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        return [r for r in self.stored if r["curve_name"] in args[1]]

    def transaction(self):  # noqa: ANN201 - mirrors asyncpg
        conn = self

        @asynccontextmanager
        async def _txn():
            conn.transactions += 1
            yield conn

        return _txn()

    async def close(self) -> None:
        self.closed = True


class Harness:
    """Everything ``run_ingest_well_logs`` reaches outside its own module."""

    def __init__(self, *, parse: Any, collar_index: dict | None = None,
                 conn: FakeConn | None = None) -> None:
        self.parse = parse
        self.collar_index = (
            {"EAGLE PT #1": COLLAR} if collar_index is None else collar_index)
        self.conn = conn or FakeConn()
        self.start_run = AsyncMock(return_value=RUN)
        self.stage = AsyncMock()
        self.completed = AsyncMock()
        self.failed = AsyncMock()

    @property
    def stages(self) -> list[str]:
        return [c.kwargs["stage"] for c in self.stage.call_args_list]

    def patches(self):  # noqa: ANN201
        module = "app.hatchet_workflows.ingest_well_logs"
        store = MagicMock()
        store.get_file = MagicMock(return_value=None)
        parse = (
            MagicMock(side_effect=self.parse)
            if isinstance(self.parse, Exception) or callable(self.parse)
            else MagicMock(return_value=self.parse)
        )
        return (
            patch(f"{module}.get_storage_client", return_value=store),
            patch("georag_geoparsers.las_parser.parse_las_file", parse),
            patch(f"{module}.asyncpg.connect", AsyncMock(return_value=self.conn)),
            patch(f"{module}.bind_workspace_scope", AsyncMock()),
            patch(f"{module}._collar_index",
                  AsyncMock(return_value=self.collar_index)),
            patch.object(_progress, "start_run", self.start_run),
            patch.object(_progress, "mark_stage_started", self.stage),
            patch.object(_progress, "mark_completed_by_run", self.completed),
            patch.object(_progress, "mark_failed_by_run", self.failed),
        )


async def run(harness: Harness, **input_overrides: Any):
    payload = dict(
        workspace_id=WS, project_id=PROJECT,
        minio_key="uploads/eagle/EAGLE_PT_1.las", run_id=RUN,
    )
    payload.update(input_overrides)
    contexts = harness.patches()
    for ctx_manager in contexts:
        ctx_manager.start()
    try:
        # ``aio_mock_run`` is hatchet_sdk's own testing entry point: it
        # builds the Context the worker would build and calls through the
        # real path. The obvious alternative, ``run_ingest_well_logs.fn``,
        # warns that ``fn`` is internal and goes away in SDK v2.
        return await run_ingest_well_logs.aio_mock_run(
            IngestWellLogsInput(**payload),
        )
    finally:
        for ctx_manager in reversed(contexts):
            ctx_manager.stop()


# ---------------------------------------------------------------------------
# Good fixture
# ---------------------------------------------------------------------------

class TestGoodFixture:
    async def test_curves_are_written_and_the_row_is_completed(self) -> None:
        harness = Harness(parse=las_result())

        out = await run(harness)

        assert out.curves_written == 1
        assert out.orphaned is False
        assert harness.completed.await_count == 1
        assert harness.completed.await_args.kwargs["rows_written"] == 1
        assert harness.failed.await_count == 0

    async def test_the_callers_run_id_is_used_not_a_fresh_one(self) -> None:
        """The `input.run_id or start_run(...)` bug, pinned.

        Laravel stamps a UUID on every upload; when start_run was skipped
        no row existed and every later UPDATE matched zero rows, so the
        upload was invisible in the UI whether it succeeded or failed.
        """
        harness = Harness(parse=las_result())

        await run(harness)

        assert harness.start_run.await_count == 1, (
            "start_run must be called unconditionally -- it is an upsert")
        assert harness.start_run.await_args.kwargs["run_id"] == RUN

    async def test_every_stage_is_reported_in_order(self) -> None:
        harness = Harness(parse=las_result())
        await run(harness)
        assert harness.stages == ["preflight", "parse", "persist"]

    async def test_the_depth_curve_is_not_stored_as_a_curve(self) -> None:
        """DEPT is the index every other curve is sampled against.

        Storing it as a channel would put a curve called DEPT in the
        picker whose values are its own depths.
        """
        harness = Harness(parse=las_result(
            curves=[curve("DEPT"), curve("GR")], total_curves_in_file=2))

        out = await run(harness)

        assert out.curves_written == 1
        assert harness.conn.executed[0][2] == "GR"

    async def test_an_empty_curve_is_not_written(self) -> None:
        """sample_count/min_depth/max_depth are NOT NULL, and a curve with
        no samples carries no information to begin with."""
        harness = Harness(parse=las_result(
            curves=[curve("GR", samples=_MIN_SAMPLES - 1), curve("RES")],
            total_curves_in_file=2))

        out = await run(harness)

        assert out.curves_written == 1
        assert harness.conn.executed[0][2] == "RES"

    async def test_reingest_replaces_and_reports_the_count(self) -> None:
        """well_log_curves has no natural key, so appending would silently
        double every curve -- and a doubled curve corrupts exactly what a
        geologist reads off the log."""
        harness = Harness(parse=las_result(), conn=FakeConn(replaced=12))

        out = await run(harness)

        assert out.curves_replaced == 12
        assert harness.conn.transactions == 1, (
            "delete and insert must share a transaction, or a failed insert "
            "leaves the hole with no curves at all")

    async def test_the_replace_only_touches_this_files_curves(self) -> None:
        """A hole has curves from more than one LAS file.

        A gamma probe run and a density/resistivity run are different tool
        strings, usually different days, always different files. The delete
        used to be `WHERE collar_id = $1` with no curve filter, so ingesting
        the second file destroyed the first file's curves — and the schema
        never asked for that: the unique constraint is on
        (collar_id, curve_name), so GAMMA from one file and RES from another
        coexist.
        """
        harness = Harness(parse=las_result(
            curves=[curve("GR"), curve("RES")], total_curves_in_file=2))

        await run(harness)

        sql, args = harness.conn.fetchvals[0]
        assert "DELETE FROM silver.well_log_curves" in sql
        assert "curve_name = ANY(" in sql, (
            "the delete must be scoped to the curves this file writes")
        assert sorted(args[1]) == ["GR", "RES"]

    async def test_a_file_with_nothing_usable_deletes_nothing(self) -> None:
        """The sharp edge of the unscoped delete.

        Every curve below _MIN_SAMPLES means `usable` is empty. The old
        delete still ran, so the hole lost every curve it had and got
        nothing back — and the workflow reported success.
        """
        harness = Harness(parse=las_result(
            curves=[curve("GR", samples=_MIN_SAMPLES - 1)],
            total_curves_in_file=1))

        out = await run(harness)

        assert out.curves_written == 0
        _, args = harness.conn.fetchvals[0]
        assert args[1] == [], "an empty curve list must match no rows"

    async def test_reingesting_the_same_file_is_still_idempotent(self) -> None:
        """Scoping the delete must not reintroduce duplicate curves.

        The names removed are exactly the names about to be written, so a
        second run of the same file replaces its own rows rather than
        appending a second copy of each.
        """
        harness = Harness(parse=las_result(
            curves=[curve("GR"), curve("RES")], total_curves_in_file=2))

        await run(harness)

        _, args = harness.conn.fetchvals[0]
        written = sorted(call[2] for call in harness.conn.executed)
        assert sorted(args[1]) == written

    async def test_the_callers_hole_id_beats_the_las_well_name(self) -> None:
        """LAS ~W well names are free text ("EAGLE PT #1") and rarely match
        the collar file's identifier, so an explicit hole_id wins."""
        harness = Harness(
            parse=las_result(well_name="EAGLE PT #1"),
            collar_index={"EL-001": COLLAR},
        )

        out = await run(harness, hole_id="EL-001")

        assert out.hole_id == "EL-001"
        assert out.curves_written == 1
        assert out.orphaned is False

    async def test_the_connection_is_closed(self) -> None:
        harness = Harness(parse=las_result())
        await run(harness)
        assert harness.conn.closed is True

    async def test_parse_metadata_reaches_the_output(self) -> None:
        harness = Harness(parse=las_result(
            las_version="3.0", parse_quality_pct=91.5, skipped_curves=2))

        out = await run(harness)

        assert out.las_version == "3.0"
        assert out.parse_quality_pct == 91.5
        assert out.curves_skipped == 2


def stored_curve(name: str, source_file: str | None, lo: float, hi: float,
                 unit: str | None = "m") -> dict:
    return {
        "curve_name": name, "source_file": source_file,
        "min_depth": lo, "max_depth": hi, "depth_unit": unit,
    }


class TestSameNamedCurveFromAnotherFile:
    """Audit finding 5: one hole holds one curve per name, and two tool runs
    both carry GAMMA. The second used to replace the first, silently."""

    async def test_an_overlapping_curve_replaces_and_says_so(self) -> None:
        conn = FakeConn(replaced=1, stored=[
            stored_curve("GR", "20260901_100000_run1.las", 0.0, 250.0)])
        harness = Harness(parse=las_result(), conn=conn)   # GR 0-300 m

        out = await run(harness)

        assert out.curves_written == 1 and out.curves_replaced == 1
        (note,) = [w for w in out.warnings if w["code"] == "curve_replaced_from_other_file"]
        assert "'run1.las'" in note["detail"] and "'EAGLE_PT_1.las'" in note["detail"]
        assert "0-250 m" in note["detail"] and "0-300 m" in note["detail"]
        assert note["curves"] == ["GR"]
        assert harness.completed.await_args.kwargs["warnings"] == out.warnings

    async def test_a_shorter_replacement_states_the_coverage_it_gave_up(self) -> None:
        conn = FakeConn(replaced=1, stored=[
            stored_curve("GR", "run1.las", 0.0, 500.0)])
        harness = Harness(parse=las_result(), conn=conn)   # GR 0-300 m

        out = await run(harness)

        note = next(w for w in out.warnings if w["code"] == "curve_replaced_from_other_file")
        assert "covers less depth" in note["detail"]

    async def test_a_complementary_run_is_refused_and_the_stored_curve_kept(self) -> None:
        conn = FakeConn(stored=[stored_curve("GR", "run1.las", 400.0, 700.0)])
        harness = Harness(
            parse=las_result(curves=[curve("GR"), curve("RES")], total_curves_in_file=2),
            conn=conn,
        )                                                   # GR 0-300 m: no overlap

        out = await run(harness)

        assert out.curves_written == 1 and out.curves_skipped == 1
        assert [c[2] for c in conn.executed] == ["RES"]      # GR never inserted
        assert conn.fetchvals[0][1][1] == ["RES"]            # ... and never deleted
        (note,) = [w for w in out.warnings if w["code"] == "curve_replacement_refused"]
        assert "GR" in note["detail"] and "'run1.las'" in note["detail"]
        assert "400-700 m" in note["detail"] and "complementary" in note["detail"]
        assert not [w for w in out.warnings if w["code"] == "curve_replaced_from_other_file"]

    async def test_a_reupload_of_the_same_file_stays_silent(self) -> None:
        """The upload timestamp differs on every upload; the file is the same."""
        conn = FakeConn(replaced=1, stored=[
            stored_curve("GR", "20260901_100000_EAGLE_PT_1.las", 0.0, 300.0)])
        harness = Harness(parse=las_result(), conn=conn)

        out = await run(harness, minio_key="uploads/eagle/20260929_081500_EAGLE_PT_1.las")

        assert out.curves_written == 1 and out.curves_replaced == 1
        assert out.warnings == []

    async def test_a_legacy_row_with_no_recorded_unit_is_replaced_with_a_warning(self) -> None:
        conn = FakeConn(replaced=1, stored=[stored_curve("GR", "old.las", 0.0, 900.0, unit=None)])
        harness = Harness(parse=las_result(), conn=conn)

        out = await run(harness)

        note = next(w for w in out.warnings if w["code"] == "curve_replaced_from_other_file")
        assert "unrecorded depth range" in note["detail"]
        assert out.curves_written == 1

    async def test_a_feet_row_is_compared_in_metres(self) -> None:
        # 1000-1500 ft is 304.8-457.2 m: disjoint from a 0-300 m curve.
        conn = FakeConn(stored=[stored_curve("GR", "run1.las", 1000.0, 1500.0, unit="ft")])
        harness = Harness(parse=las_result(), conn=conn)

        out = await run(harness)

        assert out.curves_written == 0
        assert [w["code"] for w in out.warnings] == ["curve_replacement_refused"]


# ---------------------------------------------------------------------------
# Bad fixtures
# ---------------------------------------------------------------------------

class TestOrphanedFixture:
    """A LAS file for a hole nobody uploaded. Reported, not dropped."""

    async def test_no_collar_still_reaches_a_terminal_state(self) -> None:
        harness = Harness(parse=las_result(), collar_index={})

        out = await run(harness)

        assert out.orphaned is True
        assert out.curves_written == 0
        assert harness.completed.await_count == 1, (
            "an orphaned upload is finished, not stuck -- the row must close")
        assert harness.completed.await_args.kwargs["rows_written"] == 0

    async def test_the_warning_says_what_to_do_about_it(self) -> None:
        """"No collar matched" is only useful with the fix attached: the
        LAS well name is free text, so the answer is usually "pass hole_id"
        rather than "your file is broken"."""
        harness = Harness(parse=las_result(), collar_index={})

        out = await run(harness)

        codes = [w["code"] for w in out.warnings]
        assert codes == ["no_matching_collar"]
        detail = out.warnings[0]["detail"]
        assert "hole_id" in detail
        assert "KEPT" in detail

    async def test_the_warning_is_persisted_not_just_returned(self) -> None:
        """Warnings returned only in the Hatchet run object never reach the
        Ingestion Runs page, which is where a geologist would see them."""
        harness = Harness(parse=las_result(), collar_index={})

        await run(harness)

        assert harness.completed.await_args.kwargs["warnings"][0]["code"] == (
            "no_matching_collar")

    async def test_nothing_is_deleted_when_no_collar_matched(self) -> None:
        """The replace happens inside the resolved-collar branch. An
        orphaned upload must not empty some other hole."""
        harness = Harness(parse=las_result(), collar_index={},
                          conn=FakeConn(replaced=99))

        out = await run(harness)

        assert out.curves_replaced == 0
        assert harness.conn.transactions == 0


class TestFailingFixture:
    async def test_a_parse_failure_marks_the_row_failed_and_reraises(self) -> None:
        harness = Harness(parse=ValueError("malformed ~A section at line 88"))

        with pytest.raises(ValueError, match="malformed"):
            await run(harness)

        assert harness.failed.await_count == 1
        assert harness.completed.await_count == 0

    async def test_the_real_error_survives_the_handler(self) -> None:
        """The ``error_text=`` bug: the wrong kwarg raised TypeError inside
        the except block, so the row recorded a TypeError -- if it recorded
        anything -- instead of what actually went wrong."""
        harness = Harness(parse=ValueError("malformed ~A section at line 88"))

        with pytest.raises(ValueError):
            await run(harness)

        recorded = harness.failed.await_args.kwargs["error"]
        assert "malformed ~A section at line 88" in recorded
        assert "TypeError" not in recorded

    async def test_the_recorded_error_is_bounded(self) -> None:
        """A parser can raise a megabyte of context; the column is not that
        wide and Log Analytics ingest is billed by the byte."""
        harness = Harness(parse=ValueError("x" * 5000))

        with pytest.raises(ValueError):
            await run(harness)

        assert len(harness.failed.await_args.kwargs["error"]) <= 1000

    async def test_a_database_failure_also_closes_the_row(self) -> None:
        class Exploding(FakeConn):
            async def execute(self, sql: str, *args: Any) -> None:
                raise RuntimeError("deadlock detected")

        harness = Harness(parse=las_result(), conn=Exploding())

        with pytest.raises(RuntimeError, match="deadlock"):
            await run(harness)

        assert harness.failed.await_count == 1

    async def test_an_unsupported_extension_is_refused_before_any_work(
        self,
    ) -> None:
        """Refused at the top, so no download and no parse.

        Note what this means for observability: the check fires BEFORE
        start_run, so a .csv routed here by mistake leaves NO
        ingest_progress row at all rather than a failed one. That is the
        right call for a misrouted file -- the row belongs to the workflow
        that should have had it -- but it is a deliberate asymmetry with
        every other failure path in this file, so it is pinned rather
        than left to be rediscovered.
        """
        harness = Harness(parse=las_result())

        with pytest.raises(ValueError, match="cannot handle"):
            await run(harness, minio_key="uploads/eagle/assays.csv")

        assert harness.start_run.await_count == 0
        assert harness.failed.await_count == 0

    async def test_a_malformed_workspace_id_is_rejected_at_the_boundary(
        self,
    ) -> None:
        """The field_validator runs on the input model, before the worker
        picks the task up at all."""
        with pytest.raises(ValueError):
            IngestWellLogsInput(
                workspace_id="not-a-uuid", project_id=PROJECT,
                minio_key="uploads/x.las",
            )


# ---------------------------------------------------------------------------
# Skipped curves are reported, not just counted
# ---------------------------------------------------------------------------

class TestSkippedCurvesAreReported:
    """las_parser records {curve, reason} for every curve it drops (length
    mismatch, lasio read error). The workflow kept only the count and the
    run ended 'completed', so a missing gamma curve was invisible."""

    DETAILS = [
        {"curve": "GR", "reason": "length mismatch: curve has 90 samples, depth has 100 samples"},
        {"curve": "RHOB", "reason": "lasio read error: boom"},
    ]

    def _harness(self) -> Harness:
        return Harness(parse=las_result(
            curves=[curve("NPHI")], skipped_curves=2, total_curves_in_file=3,
            skipped_details=list(self.DETAILS),
        ))

    async def test_a_curves_skipped_warning_names_each_curve_and_reason(self) -> None:
        out = await run(self._harness())

        (w,) = [w for w in out.warnings if w["code"] == "curves_skipped"]
        for name in ("GR", "RHOB"):
            assert name in w["detail"]
        assert "length mismatch" in w["detail"]
        assert "lasio read error" in w["detail"]
        assert "2 of 3" in w["detail"]
        assert w["curves"] == ["GR", "RHOB"]
        assert out.curves_skipped == 2
        assert out.curves_written == 1

    async def test_the_warning_is_persisted_and_the_run_is_partial(self) -> None:
        harness = self._harness()
        broadcast = AsyncMock()
        with patch.object(_progress, "broadcast_terminal", broadcast):
            await run(harness)

        persisted = harness.completed.await_args.kwargs
        assert persisted["rows_written"] == 1
        assert [w["code"] for w in persisted["warnings"]] == ["curves_skipped"]
        # What mark_completed_by_run computes from these exact arguments.
        assert _progress.terminal_status(
            rows_written=persisted["rows_written"], warnings=persisted["warnings"],
        ) == "partial"
        assert broadcast.await_args.kwargs["status"] == "partial"

    async def test_no_skips_stays_completed(self) -> None:
        harness = Harness(parse=las_result())
        broadcast = AsyncMock()
        with patch.object(_progress, "broadcast_terminal", broadcast):
            out = await run(harness)

        assert out.warnings == []
        assert harness.completed.await_args.kwargs["warnings"] == []
        assert broadcast.await_args.kwargs["status"] == "completed"

    async def test_a_long_skip_list_is_counted_not_dumped(self) -> None:
        details = [{"curve": f"C{i}", "reason": "lasio read error: x"} for i in range(30)]
        harness = Harness(parse=las_result(
            curves=[], skipped_curves=30, total_curves_in_file=30,
            skipped_details=details,
        ))

        out = await run(harness)

        w = next(w for w in out.warnings if w["code"] == "curves_skipped")
        assert "C0" in w["detail"] and "C29" not in w["detail"]
        assert "+22 more" in w["detail"]
        assert len(w["curves"]) == 30  # structured list stays complete

    async def test_a_count_without_details_still_warns(self) -> None:
        harness = Harness(parse=las_result(
            skipped_curves=1, total_curves_in_file=2,
        ))

        out = await run(harness)

        assert [w["code"] for w in out.warnings] == ["curves_skipped"]
