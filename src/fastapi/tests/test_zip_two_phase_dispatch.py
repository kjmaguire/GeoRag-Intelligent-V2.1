"""ZIP members that need a collar must be dispatched after the collars exist.

WHY THIS FILE EXISTS
    ``ingest_zip_archive`` fired every member with ``aio_run_no_wait`` in
    ``rglob`` order. An interval or survey table that ran before its collar
    table resolved no holes; ingest_tabular counted its rows ``orphaned`` and
    warned "upload the collar file, then re-run this one" -- and nothing ever
    re-ran it. LAS files, which run in-process, had the same problem.

    Phase 1 (collar producers) is dispatched, the archive WAITS for those
    runs to reach a terminal state, then phase 2 (interval tables, LAS) goes
    out. Which phase a table belongs to is decided by the header classifier
    ingest_tabular itself uses.

Two layers here: the pure pieces (phase split, the bounded wait, the warning
summaries) and the whole task body run end to end against fakes that record
the ORDER of events -- storage, asyncpg, the progress modules and the child
workflows are faked at their real boundaries.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.hatchet_workflows import _archive_progress
from app.hatchet_workflows import ingest_zip_archive as module
from app.services.ingest.las_ingester import LASIngestResult

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_RUN = "c2000000-0000-0000-0000-000000000007"

COLLAR_CSV = "hole_id,easting,northing,elevation,total_depth\nH1,471000,4657000,2000,120\n"
LITHO_CSV = "hole_id,from_depth,to_depth,lithology_code,description\nH1,0,5,SST,sand\n"
SURVEY_CSV = "hole_id,depth,azimuth,dip\nH1,10,0,-90\n"
JUNK_CSV = "foo,bar,baz\n1,2,3\n"
LAS_TEXT = "~VERSION INFORMATION\n VERS. 2.0 : X\n"


def _write(dir_: Path, name: str, text: str = "x") -> Path:
    path = dir_ / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Phase split
# ---------------------------------------------------------------------------


async def test_collar_tables_and_everything_that_needs_no_collar_are_phase_one(tmp_path: Path) -> None:
    files = [
        _write(tmp_path, "collars.csv", COLLAR_CSV),
        _write(tmp_path, "notes.pdf"),
        _write(tmp_path, "scan.tif"),
        _write(tmp_path, "veins.geojson"),
        _write(tmp_path, "hole.log"),
        _write(tmp_path, "mystery.csv", JUNK_CSV),   # unclassifiable -> today's behaviour
    ]

    producers, dependents = await module._split_into_phases(files)

    assert producers == files
    assert dependents == []


async def test_interval_tables_and_las_are_phase_two(tmp_path: Path) -> None:
    collars = _write(tmp_path, "collars.csv", COLLAR_CSV)
    litho = _write(tmp_path, "lithology.csv", LITHO_CSV)
    survey = _write(tmp_path, "sub/survey.tsv", SURVEY_CSV.replace(",", "\t"))
    las = _write(tmp_path, "H1.LAS", LAS_TEXT)
    pdf = _write(tmp_path, "report.pdf")

    producers, dependents = await module._split_into_phases([litho, las, collars, survey, pdf])

    assert producers == [collars, pdf]
    assert dependents == [litho, las, survey]  # original order kept within each phase


async def test_a_workbook_is_deferred_only_when_none_of_its_sheets_is_a_collar_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    openpyxl = pytest.importorskip("openpyxl")

    def workbook(name: str, sheets: dict[str, list[list[Any]]]) -> Path:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for title, rows in sheets.items():
            ws = wb.create_sheet(title)
            for row in rows:
                ws.append(row)
        path = tmp_path / name
        wb.save(path)
        return path

    intervals_only = workbook("intervals.xlsx", {
        "Litho": [["hole_id", "from_depth", "to_depth", "lithology_code"], ["H1", 0, 5, "SST"]],
        "Survey": [["hole_id", "depth", "azimuth", "dip"], ["H1", 10, 0, -90]],
    })
    with_collars = workbook("everything.xlsx", {
        "Collars": [["hole_id", "easting", "northing", "elevation", "total_depth"], ["H1", 1, 2, 3, 4]],
        "Litho": [["hole_id", "from_depth", "to_depth", "lithology_code"], ["H1", 0, 5, "SST"]],
    })
    unreadable = _write(tmp_path, "broken.xlsx", "not a workbook")

    producers, dependents = await module._split_into_phases([intervals_only, with_collars, unreadable])

    assert dependents == [intervals_only]
    assert producers == [with_collars, unreadable]  # a failed sniff stays in phase 1


async def test_a_sniff_that_raises_leaves_the_member_in_phase_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr("georag_geoparsers._sheet_classifier.classify_sheet_type", boom)
    litho = _write(tmp_path, "lithology.csv", LITHO_CSV)

    producers, dependents = await module._split_into_phases([litho])

    assert producers == [litho] and dependents == []


# ---------------------------------------------------------------------------
# The bounded wait
# ---------------------------------------------------------------------------


def _runs(*names: str) -> list[module._MemberRun]:
    return [module._MemberRun(name=n, run_id=f"run-{n}") for n in names]


async def test_wait_returns_once_every_run_is_terminal() -> None:
    polls: dict[str, int] = {}

    async def status(run_id: str) -> str | None:
        polls[run_id] = polls.get(run_id, 0) + 1
        # 'a' finishes on its 3rd poll, 'b' on its 1st.
        if run_id == "run-a":
            return "COMPLETED" if polls[run_id] >= 3 else "RUNNING"
        return "COMPLETED"

    outcome = await module._await_runs(
        _runs("a", "b"), timeout_s=30, poll_s=0, status_fn=status,
    )

    assert [r.name for r in outcome.finished] == ["b", "a"]
    assert outcome.pending == [] and outcome.not_completed == []
    assert polls == {"run-a": 3, "run-b": 1}, "finished runs must not be polled again"


async def test_wait_gives_up_at_the_deadline_and_reports_what_is_pending() -> None:
    async def status(run_id: str) -> str | None:
        return "RUNNING"

    outcome = await module._await_runs(
        _runs("a"), timeout_s=0.05, poll_s=0.01, status_fn=status,
    )

    assert [r.name for r in outcome.pending] == ["a"]
    assert outcome.finished == []
    assert outcome.waited_s >= 0.05


async def test_an_unreadable_status_is_not_mistaken_for_done() -> None:
    async def status(run_id: str) -> str | None:
        return None  # engine unreachable

    outcome = await module._await_runs(
        _runs("a"), timeout_s=0.03, poll_s=0.01, status_fn=status,
    )

    assert [r.name for r in outcome.pending] == ["a"]


async def test_failed_and_cancelled_runs_are_terminal_but_not_completed() -> None:
    states = {"run-a": "FAILED", "run-b": "CANCELLED", "run-c": "COMPLETED"}

    async def status(run_id: str) -> str | None:
        return states[run_id]

    outcome = await module._await_runs(
        _runs("a", "b", "c"), timeout_s=5, poll_s=0, status_fn=status,
    )

    assert sorted((r.name, s) for r, s in outcome.not_completed) == [("a", "FAILED"), ("b", "CANCELLED")]
    assert [r.name for r in outcome.finished] == ["c"]
    warnings = module._wait_warnings(
        outcome, waiting_for="collar-bearing member run(s)", consequence="the interval tables",
    )
    assert [w["code"] for w in warnings] == ["archive_member_run_not_completed"]
    assert "a" in warnings[0]["detail"] and "FAILED" in warnings[0]["detail"]


async def test_wait_uses_hatchets_status_call_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def aio_get_status(run_id: str) -> Any:
        seen.append(run_id)
        return SimpleNamespace(value="COMPLETED")

    monkeypatch.setattr(module.hatchet.runs, "aio_get_status", aio_get_status)

    outcome = await module._await_runs(_runs("a"), timeout_s=5, poll_s=0)

    assert seen == ["run-a"] and [r.name for r in outcome.finished] == ["a"]


def test_the_timeout_is_configurable_and_a_bad_value_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEORAG_ZIP_PHASE_WAIT_TIMEOUT_S", raising=False)
    assert module._wait_timeout_s() == module._DEFAULT_WAIT_TIMEOUT_S
    monkeypatch.setenv("GEORAG_ZIP_PHASE_WAIT_TIMEOUT_S", "42")
    assert module._wait_timeout_s() == 42.0
    monkeypatch.setenv("GEORAG_ZIP_PHASE_WAIT_TIMEOUT_S", "soon")
    assert module._wait_timeout_s() == module._DEFAULT_WAIT_TIMEOUT_S


# ---------------------------------------------------------------------------
# The whole task body, against fakes that record the order of events
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, zip_path: Path, events: list[tuple[str, str]]) -> None:
        self.zip_path = zip_path
        self.events = events
        self.objects: dict[str, bytes] = {}

    def get_file(self, bucket: Any, key: str, dest: str) -> None:
        Path(dest).write_bytes(self.zip_path.read_bytes())

    def put_bytes(self, bucket: Any, key: str, data: bytes) -> None:
        self.objects[key] = data


class _Conn:
    def is_in_transaction(self) -> bool:
        return True

    def transaction(self) -> Any:
        return contextlib.nullcontext()

    async def execute(self, *_: Any, **__: Any) -> str:
        return "OK"

    async def fetch(self, *_: Any, **__: Any) -> list[Any]:
        return []  # no LAS waiting for a collar

    async def close(self) -> None:
        return None


class _Harness:
    """Patches every boundary run_zip_ingest crosses and records what happens."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, members: dict[str, str]) -> None:
        self.events: list[tuple[str, str]] = []
        #: member file name -> the statuses its child run reports, in order.
        self.status_script: dict[str, list[str]] = {}
        #: member file name -> the workflow_run_id its dispatch returned.
        self.run_ids: dict[str, str] = {}
        self.completed: dict[str, Any] = {}
        self.las_result = lambda name: LASIngestResult(  # noqa: E731
            file_path=name, hole_id="H1", project_id=_PJ, collar_id="c", curves_inserted=1,
        )
        self.derive_summary: dict[str, Any] = {
            "skipped": False, "skipped_reason": None, "collars_skipped_logged_lithology": 0,
        }
        self.derive_calls = 0

        zip_path = tmp_path / "in.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, text in members.items():
                zf.writestr(name, text)
        self.store = _Store(zip_path, self.events)

        events = self.events
        harness = self
        counter = {"n": 0}

        async def aio_run_no_wait(payload: Any) -> Any:
            counter["n"] += 1
            key = payload.minio_key
            # bronze key: <prefix>/<project>/<YYYYmmdd_HHMMSS_ffffff>_<file name>
            name = re.sub(r"^.*/\d{8}_\d{6}_\d+_", "", key)
            events.append(("dispatch", name))
            harness.run_ids[name] = f"wf-{counter['n']}"
            return SimpleNamespace(workflow_run_id=f"wf-{counter['n']}")

        async def run_status(run_id: str) -> str | None:
            name = next(n for n, rid in harness.run_ids.items() if rid == run_id)
            events.append(("poll", name))
            script = harness.status_script.get(name)
            if script:
                return script.pop(0)
            return "COMPLETED"

        async def fake_ingest_las(conn: Any, path: str, **kw: Any) -> LASIngestResult:
            events.append(("las", Path(path).name))
            return harness.las_result(Path(path).name)

        async def fake_derive(project_id: str) -> dict[str, Any]:
            events.append(("derive", project_id))
            harness.derive_calls += 1
            return harness.derive_summary

        async def fake_connect(*_: Any, **__: Any) -> _Conn:
            return _Conn()

        async def noop(*_: Any, **__: Any) -> None:
            return None

        async def true(*_: Any, **__: Any) -> bool:
            return True

        async def mark_completed(**kw: Any) -> bool:
            harness.completed = kw
            return True

        @contextlib.asynccontextmanager
        async def lifecycle(**_: Any) -> Any:
            yield "archive-run-1"

        @contextlib.asynccontextmanager
        async def heartbeat(**_: Any) -> Any:
            yield

        real_sleep = asyncio.sleep

        # Force the WORST arrival order: the workflow walks the extracted tree
        # with rglob, whose order is the filesystem's. Sorting by name puts the
        # interval tables and the LAS ahead of the collar table, which is the
        # order that orphaned their rows before the two-phase dispatch.
        real_rglob = Path.rglob
        monkeypatch.setattr(
            Path, "rglob",
            lambda self, pattern: iter(sorted(real_rglob(self, pattern), key=lambda p: p.name)),
        )

        async def fast_sleep(delay: float, *a: Any, **k: Any) -> None:
            await real_sleep(0)

        monkeypatch.setattr(module, "get_storage_client", lambda: self.store)
        monkeypatch.setattr(module.asyncpg, "connect", fake_connect)
        monkeypatch.setattr(module, "bind_workspace_scope", noop)
        monkeypatch.setattr(module.ingest_tabular, "aio_run_no_wait", aio_run_no_wait)
        monkeypatch.setattr(module.ingest_pdf, "aio_run_no_wait", aio_run_no_wait)
        monkeypatch.setattr(module.ingest_spatial, "aio_run_no_wait", aio_run_no_wait)
        monkeypatch.setattr(module, "_run_status", run_status)
        monkeypatch.setattr("app.services.ingest.las_ingester.ingest_las_file", fake_ingest_las)
        monkeypatch.setattr("app.services.ingest.derive_intervals.derive_project", fake_derive)
        monkeypatch.setattr(module.asyncio, "sleep", fast_sleep)
        for name in ("mark_extracting", "mark_fanning_out", "increment_counts", "mark_terminal"):
            monkeypatch.setattr(_archive_progress, name, noop)
        monkeypatch.setattr(_archive_progress, "archive_lifecycle", lifecycle)
        ip = module.ingest_progress
        monkeypatch.setattr(ip, "start_run", _return("progress-1"))
        for name in ("mark_stage_started", "mark_stage_progress", "broadcast_terminal"):
            monkeypatch.setattr(ip, name, noop)
        monkeypatch.setattr(ip, "heartbeat_loop", heartbeat)
        monkeypatch.setattr(ip, "mark_completed_by_run", mark_completed)
        monkeypatch.setattr(ip, "mark_failed_by_run", true)

    async def run(self) -> dict[str, Any]:
        payload = module.IngestZipArchiveInput(
            minio_key="zips/a.zip", workspace_id=_WS, project_id=_PJ, run_id=_RUN,
        )
        return await module.run_zip_ingest.fn(payload, SimpleNamespace(workflow_run_id="wf-archive"))

    def positions(self, kind: str) -> list[int]:
        return [i for i, (k, _) in enumerate(self.events) if k == kind]

    def index_of(self, kind: str, name: str) -> int:
        return next(i for i, e in enumerate(self.events) if e == (kind, name))


def _return(value: Any) -> Any:
    async def fn(*_: Any, **__: Any) -> Any:
        return value

    return fn


ALL_MEMBERS = {
    # Deliberately listed dependents-first: rglob order is whatever the
    # filesystem gives, so nothing here may rely on it.
    "a_lithology.csv": LITHO_CSV,
    "b_survey.csv": SURVEY_CSV,
    "c_H1.las": LAS_TEXT,
    "d_collars.csv": COLLAR_CSV,
    "e_report.pdf": "%PDF-1.4",
}


async def test_interval_tables_and_las_are_dispatched_only_after_the_collar_run_finished(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    h = _Harness(monkeypatch, tmp_path, ALL_MEMBERS)
    h.status_script["d_collars.csv"] = ["RUNNING", "RUNNING", "COMPLETED"]

    result = await h.run()

    collar_dispatch = h.index_of("dispatch", "d_collars.csv")
    polls = [i for i, e in enumerate(h.events) if e == ("poll", "d_collars.csv")]
    assert polls, "the archive never waited for the collar run"
    assert len(polls) == 3, "the collar run is polled until terminal, then never again"
    litho_dispatch = h.index_of("dispatch", "a_lithology.csv")
    survey_dispatch = h.index_of("dispatch", "b_survey.csv")
    las_call = h.index_of("las", "c_H1.las")

    # collar dispatched -> polled to COMPLETED -> only then the dependents.
    assert collar_dispatch < polls[0]
    for dependent in (litho_dispatch, survey_dispatch, las_call):
        assert dependent > polls[-1], h.events
    # The PDF needs no collar: it is phase 1 and goes out before the wait.
    assert h.index_of("dispatch", "e_report.pdf") < polls[0]
    assert result["dispatch_plan"] == {"phase1": 2, "phase2": 3}
    assert result["counts"]["errors"] == 0


async def test_the_derive_step_waits_for_the_interval_runs_it_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    h = _Harness(monkeypatch, tmp_path, ALL_MEMBERS)

    await h.run()

    derive = h.index_of("derive", _PJ)
    # The lithology and survey runs are polled (phase 2 wait) before derive
    # reads silver.lithology_logs.
    for name in ("a_lithology.csv", "b_survey.csv"):
        assert ("poll", name) in h.events
        assert h.index_of("poll", name) < derive
    assert h.positions("poll")[-1] < derive
    assert h.index_of("las", "c_H1.las") < derive
    assert h.derive_calls == 1


async def test_a_collar_run_that_never_finishes_does_not_hang_the_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GEORAG_ZIP_PHASE_WAIT_TIMEOUT_S", "0")
    h = _Harness(monkeypatch, tmp_path, ALL_MEMBERS)
    h.status_script["d_collars.csv"] = ["RUNNING"] * 50

    result = await h.run()

    # Dispatched anyway...
    assert ("las", "c_H1.las") in h.events
    assert ("dispatch", "a_lithology.csv") in h.events
    # ...and the archive says so, once, naming the run.
    codes = [w["code"] for w in result["warnings"]]
    assert "archive_dependency_wait_timeout" in codes
    timeout_warning = next(w for w in result["warnings"] if w["code"] == "archive_dependency_wait_timeout")
    assert "d_collars.csv" in timeout_warning["detail"]
    reported = [w["code"] for w in h.completed["warnings"]]
    assert "archive_dependency_wait_timeout" in reported


async def test_an_archive_with_no_dependents_never_waits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    h = _Harness(monkeypatch, tmp_path, {"collars.csv": COLLAR_CSV, "r.pdf": "%PDF-1.4"})

    result = await h.run()

    assert h.positions("poll") == []
    assert result["dispatch_plan"] == {"phase1": 2, "phase2": 0}
    assert h.derive_calls == 0  # no LAS landed


async def test_las_warnings_reach_the_archive_row_as_one_line_per_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    members = {f"w{i}.las": LAS_TEXT for i in range(4)} | {"z_bad.las": LAS_TEXT}
    h = _Harness(monkeypatch, tmp_path, members)

    def result_for(name: str) -> LASIngestResult:
        if name == "z_bad.las":
            return LASIngestResult(
                file_path=name, hole_id="Z", project_id=_PJ, collar_id=None,
                curves_inserted=0, skipped=True, skipped_reason="collar_unlocated",
                warnings=[{"code": "las_collar_unlocated", "detail": "no location"}],
            )
        return LASIngestResult(
            file_path=name, hole_id=name, project_id=_PJ, collar_id="c", curves_inserted=1,
            warnings=[{"code": "las_collar_crs_assumed", "detail": "approx"}],
            georef_method="assumed",
        )

    h.las_result = result_for

    await h.run()

    by_code = {w["code"]: w["detail"] for w in h.completed["warnings"]}
    assert "4 LAS well(s)" in by_code["las_collar_crs_assumed"]
    assert "no stated CRS" in by_code["las_collar_crs_assumed"]
    assert "1 LAS file(s) have no collar" in by_code["las_collar_unlocated"]
    assert "KEPT" in by_code["las_collar_unlocated"]
    assert "z_bad.las" in by_code["las_collar_unlocated"]
    # One entry per code, however many files raised it.
    assert [w["code"] for w in h.completed["warnings"]].count("las_collar_crs_assumed") == 1


async def test_a_skipped_derive_is_one_warning_not_one_per_hole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    h = _Harness(monkeypatch, tmp_path, {"H1.las": LAS_TEXT, "H2.las": LAS_TEXT})
    h.derive_summary = {
        "skipped": True, "skipped_reason": "commodity_not_uranium", "commodity": "gold",
    }

    result = await h.run()

    derive_warnings = [w for w in h.completed["warnings"] if w["code"].startswith("derive_intervals")]
    assert len(derive_warnings) == 1
    assert derive_warnings[0]["code"] == "derive_intervals_skipped"
    assert "'gold'" in derive_warnings[0]["detail"]
    assert result["derive_intervals"]["skipped_reason"] == "commodity_not_uranium"


def test_the_logged_lithology_skip_is_summarised_once() -> None:
    (warning,) = module._derive_warnings({
        "skipped": False, "skipped_reason": None, "collars_skipped_logged_lithology": 37,
    })
    assert warning["code"] == "derive_intervals_skipped"
    assert warning["detail"].startswith("37 hole(s) already have logged lithology")
    assert module._derive_warnings({"skipped": False, "collars_skipped_logged_lithology": 0}) == []
    assert module._derive_warnings(None) == []
    (failed,) = module._derive_warnings({"error": "boom"})
    assert failed["code"] == "derive_intervals_failed"


async def test_nested_zips_gdb_folders_and_txt_all_reach_an_ingester(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import io

    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("collars.csv", COLLAR_CSV)
        zf.writestr("more/readme.txt", "field notes")
    members: dict[str, Any] = {
        "delivery/part1.zip": inner.getvalue(),
        "Site.gdb/a00000001.gdbtable": b"table",
        "Site.gdb/gdb": b"x",
        "._junk.csv": b"\x00",
    }
    h = _Harness(monkeypatch, tmp_path, members)

    result = await h.run()

    dispatched = [name for kind, name in h.events if kind == "dispatch"]
    assert sorted(dispatched) == ["Site.zip", "collars.csv", "readme.txt"]
    assert result["counts"]["unknown"] == 0 and result["counts"]["errors"] == 0
    assert result["counts"]["spatial"] == 1 and result["counts"]["csv"] == 2
    assert not [w for w in result["warnings"] if w["code"] == "archive_member_unhandled"]
