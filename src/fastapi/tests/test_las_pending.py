"""A LAS with no collar is kept and attaches when its collar arrives.

A LAS whose well has no collar and no header coordinates cannot be placed
(silver.collars.easting / northing are NOT NULL; a location is never invented).
It used to be refused and its curves lost. Now the file is kept in bronze,
recorded in silver.las_pending_collar, and ``attach_pending_las`` -- called
after collars are written -- ingests it once a collar with that hole id
(exact, then canonical) exists, exactly once.

Recording fakes; SQL against a live schema is the integration bucket's job.
"""
from __future__ import annotations

import contextlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("lasio")

from app.hatchet_workflows import ingest_zip_archive as zip_module  # noqa: E402
from app.services.ingest import las_pending  # noqa: E402
from app.services.ingest.las_ingester import LASIngestResult  # noqa: E402

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_COLLAR = "d3000000-0000-0000-0000-0000000000c0"


class _Conn:
    """Holds pending rows in memory and honours the conditional claim."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.recorded: list[tuple[Any, ...]] = []
        self.attached_calls: list[tuple[str, str]] = []

    def transaction(self) -> Any:
        return contextlib.nullcontext()

    async def execute(self, sql: str, *args: Any) -> str:
        flat = " ".join(sql.split())
        if "INSERT INTO silver.las_pending_collar" in flat:
            self.recorded.append(args)
        elif "SET status = 'attached'" in flat:
            pending_id, collar_id = args
            self.attached_calls.append((pending_id, collar_id))
            self._row(pending_id)["status"] = "attached"
        elif "SET status = 'pending'" in flat:
            self._row(args[0])["status"] = "pending"
            self._row(args[0])["last_error"] = args[1]
        return "OK"

    def _row(self, pending_id: str) -> dict[str, Any]:
        return next(r for r in self.rows if r["pending_id"] == pending_id)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        # The real query also joins silver.collars; the fake's rows are the
        # ones whose collar exists (`has_collar`).
        return [r for r in self.rows if r["status"] == "pending" and r.get("has_collar", True)]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = self._row(args[0])
        if row["status"] != "pending":
            return None  # someone else holds it, or it is attached
        row["status"] = "attaching"
        return 1


class _Store:
    def __init__(self) -> None:
        self.puts: dict[str, bytes] = {}
        self.gets: list[str] = []

    def get_file(self, bucket: Any, key: str, dest: str) -> None:
        self.gets.append(key)
        Path(dest).write_text("las", encoding="utf-8")

    def put_bytes(self, bucket: Any, key: str, data: bytes) -> None:
        self.puts[key] = data


def _row(pending_id: str = "p1", **kw: Any) -> dict[str, Any]:
    base = {
        "pending_id": pending_id, "hole_id": "SIT-007", "bronze_key": f"las/{_PJ}/{pending_id}.las",
        "source_name": f"{pending_id}.las", "status": "pending",
    }
    return base | kw


@pytest.fixture
def ingest(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the LAS ingester; record every call it gets."""
    calls: list[dict[str, Any]] = []

    async def fake_ingest(conn: Any, path: str, **kw: Any) -> LASIngestResult:
        calls.append({"path": path, **kw})
        return LASIngestResult(
            file_path=path, hole_id=kw["hole_id_override"], project_id=_PJ,
            collar_id=_COLLAR, curves_inserted=3,
        )

    monkeypatch.setattr("app.services.ingest.las_ingester.ingest_las_file", fake_ingest)
    return calls


def test_the_canonical_form_matches_the_ingesters_rule() -> None:
    assert las_pending.canonical_hole_id("SRE09-6") == las_pending.canonical_hole_id("sre09_6") == "SRE096"
    assert las_pending.canonical_hole_id("  ") is None


@pytest.mark.asyncio
async def test_record_is_an_idempotent_upsert_that_never_reopens_an_attached_row() -> None:
    conn = _Conn()

    await las_pending.record_pending(
        conn, workspace_id=_WS, project_id=_PJ, hole_id="SRE09_6",
        bronze_key="las/k.las", source_name="k.las",
    )

    (args,) = conn.recorded
    assert args[2] == "SRE09_6" and args[3] == "SRE096"  # hole id + canonical stored
    text = " ".join(inspect.getsource(las_pending.record_pending).split())
    assert "ON CONFLICT (project_id, bronze_key)" in text
    assert "status <> 'attached'" in text


@pytest.mark.asyncio
async def test_a_pending_file_attaches_once_its_collar_exists(ingest: list[dict[str, Any]]) -> None:
    conn = _Conn([_row("p1")])
    store = _Store()

    summary = await las_pending.attach_pending_las(conn, store=store, workspace_id=_WS, project_id=_PJ)

    assert summary.attached == ["p1.las"] and summary.errors == []
    assert store.gets == [f"las/{_PJ}/p1.las"]
    # Ingested under the recorded hole id, not whatever the header says.
    assert ingest[0]["hole_id_override"] == "SIT-007" and ingest[0]["project_id_override"] == _PJ
    assert conn.attached_calls == [("p1", _COLLAR)]


@pytest.mark.asyncio
async def test_attaching_twice_ingests_it_once(ingest: list[dict[str, Any]]) -> None:
    conn = _Conn([_row("p1")])
    store = _Store()

    first = await las_pending.attach_pending_las(conn, store=store, workspace_id=_WS, project_id=_PJ)
    second = await las_pending.attach_pending_las(conn, store=store, workspace_id=_WS, project_id=_PJ)

    assert first.attached == ["p1.las"] and second.attached == []
    assert len(ingest) == 1, "an attached row must never be ingested again"


@pytest.mark.asyncio
async def test_a_row_claimed_by_another_worker_is_skipped(ingest: list[dict[str, Any]]) -> None:
    conn = _Conn([_row("p1", status="pending")])
    orig = conn.fetchval

    async def lost_race(sql: str, *args: Any) -> Any:
        conn.rows[0]["status"] = "attaching"  # the other worker won first
        return await orig(sql, *args)

    conn.fetchval = lost_race  # type: ignore[method-assign]

    summary = await las_pending.attach_pending_las(conn, store=_Store(), workspace_id=_WS, project_id=_PJ)

    assert summary.attached == [] and ingest == []


@pytest.mark.asyncio
async def test_a_file_whose_collar_still_is_not_there_goes_back_to_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(conn: Any, path: str, **kw: Any) -> LASIngestResult:
        return LASIngestResult(
            file_path=path, hole_id="SIT-007", project_id=_PJ, collar_id=None,
            curves_inserted=0, skipped=True, skipped_reason="collar_unlocated",
        )

    monkeypatch.setattr("app.services.ingest.las_ingester.ingest_las_file", refuse)
    conn = _Conn([_row("p1")])

    summary = await las_pending.attach_pending_las(conn, store=_Store(), workspace_id=_WS, project_id=_PJ)

    assert summary.attached == [] and summary.still_pending == ["p1.las"]
    assert conn.rows[0]["status"] == "pending" and conn.rows[0]["last_error"] == "collar_unlocated"


@pytest.mark.asyncio
async def test_one_failing_file_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    async def flaky(conn: Any, path: str, **kw: Any) -> LASIngestResult:
        if Path(path).name == "bad.las":
            raise RuntimeError("corrupt")
        return LASIngestResult(
            file_path=path, hole_id=kw["hole_id_override"], project_id=_PJ,
            collar_id=_COLLAR, curves_inserted=1,
        )

    monkeypatch.setattr("app.services.ingest.las_ingester.ingest_las_file", flaky)
    conn = _Conn([_row("p1", source_name="bad.las"), _row("p2", source_name="good.las")])

    summary = await las_pending.attach_pending_las(conn, store=_Store(), workspace_id=_WS, project_id=_PJ)

    assert summary.attached == ["good.las"]
    assert len(summary.errors) == 1 and "bad.las" in summary.errors[0]
    assert conn.rows[0]["status"] == "pending", "the failed one is retried by the next attach"


@pytest.mark.asyncio
async def test_a_lookup_failure_never_raises() -> None:
    class Broken(_Conn):
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            raise RuntimeError("relation silver.las_pending_collar does not exist")

    summary = await las_pending.attach_pending_las(Broken(), store=_Store(), workspace_id=_WS, project_id=_PJ)

    assert summary.attached == [] and summary.errors


def test_the_candidate_query_matches_exact_then_canonical_and_reclaims_stale_claims() -> None:
    sql = " ".join(las_pending._CANDIDATES_SQL.split())
    assert "c.hole_id = p.hole_id" in sql
    assert "c.hole_id_canonical = p.hole_id_canonical" in sql
    assert "status = 'attaching'" in sql and "interval '30 minutes'" in sql


@pytest.mark.asyncio
async def test_a_zip_las_with_no_collar_is_kept_in_bronze_and_recorded(tmp_path: Path) -> None:
    las = tmp_path / "SIT_7.las"
    las.write_text("~VERSION INFORMATION\n VERS. 2.0 : X\n", encoding="utf-8")
    conn = _Conn()
    store = _Store()
    input_ = zip_module.IngestZipArchiveInput(
        minio_key="zips/a.zip", workspace_id=_WS, project_id=_PJ,
        run_id="c2000000-0000-0000-0000-000000000007",
    )

    kept = await zip_module._keep_las_for_later(
        file_path=las, hole_id="SIT-007", conn=conn, store=store, input=input_,  # type: ignore[arg-type]
    )

    assert kept is True
    (key,) = store.puts
    assert key.startswith(f"las/{_PJ}/") and key.endswith("_SIT_7.las")
    (args,) = conn.recorded
    assert args[2] == "SIT-007" and args[4] == key and args[5] == "SIT_7.las"


@pytest.mark.asyncio
async def test_a_zip_las_that_cannot_be_kept_says_so(tmp_path: Path) -> None:
    las = tmp_path / "SIT_7.las"
    las.write_text("x", encoding="utf-8")

    class NoStore(_Store):
        def put_bytes(self, bucket: Any, key: str, data: bytes) -> None:
            raise OSError("bucket unavailable")

    kept = await zip_module._keep_las_for_later(
        file_path=las, hole_id="SIT-007", conn=_Conn(), store=NoStore(),  # type: ignore[arg-type]
        input=SimpleNamespace(project_id=_PJ, workspace_id=_WS),  # type: ignore[arg-type]
    )

    assert kept is False
    (summary,) = zip_module._member_warning_summaries(
        [{"code": "las_pending_not_kept", "detail": "x", "file": "SIT_7.las"}],
    )
    assert "could NOT be kept" in summary["detail"]


def _source(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "app" / "hatchet_workflows" / name).read_text(encoding="utf-8")


def test_ingest_tabular_calls_the_attach_hook_only_after_collars_were_written() -> None:
    src = _source("ingest_tabular.py")
    hook = src.index("attach_pending_las(")
    guard = src.rindex('written.get("collar", {}).get("written")', 0, hook)
    assert hook - guard < 400, "the hook must sit directly under the 'collars were written' guard"


def test_ingest_well_logs_records_an_orphan_instead_of_dropping_it() -> None:
    src = _source("ingest_well_logs.py")
    assert "record_pending(" in src and "_keep_for_later(" in src
