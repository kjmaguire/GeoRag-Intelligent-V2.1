"""The binary .log format states no CRS, so it never places a collar unasked.

`cameco_log_ingester` transformed every .log's E=/N= pair from NAD83 /
Wyoming East (EPSG:32155, US survey feet) to UTM 13N no matter which project
the file landed in: a .log in an archive from anywhere else was placed in
Wyoming, silently. The format's CRS is now used ONLY when the operator declared
it for the upload (``source_epsg == LOG_COORD_EPSG``); otherwise the member is
refused with a named warning (ZIP) or an entry in the summary (cluster runner).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.hatchet_workflows import ingest_zip_archive as zip_module
from app.services.ingest import cameco_log_ingester as cli
from app.services.ingest.cluster_runner import ingest_cluster

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_RUN = "c2000000-0000-0000-0000-000000000007"

_LOG_NAME = "36-1042_08-13-12_10-08_9057C_.10_0.70_1257.50_ORE.log"
_LOG_BYTES = (
    b"PROCESSED9057C 3.60K   1       F.597923 .10    0.60  340.00UE"
    + b"\x00" * 50 + b"CAMECO RESOURCES" + b"\x00" * 50 + b"SHIRLEY BASIN"
    + b"\x00" * 30 + b"E=791126 N=617244" + b"\x00" * 100
)


def _log(dir_: Path) -> Path:
    path = dir_ / _LOG_NAME
    path.write_bytes(_LOG_BYTES)
    return path


class _Conn:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self._in_tx = False

    def is_in_transaction(self) -> bool:
        return self._in_tx

    def transaction(self) -> Any:
        conn = self

        class _Tx:
            async def __aenter__(self) -> None:
                conn._in_tx = True

            async def __aexit__(self, *exc: Any) -> bool:
                conn._in_tx = False
                return False

        return _Tx()

    async def execute(self, sql: str, *args: Any) -> str:
        if "silver.collars" in sql:
            self.writes.append(" ".join(sql.split())[:60])
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return _PJ

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "silver.collars" in sql:
            self.writes.append(" ".join(sql.split())[:60])
            return {"collar_id": "d3000000-0000-0000-0000-0000000000c0"}
        if "INSERT INTO silver.projects" in sql:
            return {"project_id": _PJ}
        return None


def _parsed(tmp_path: Path) -> Any:
    parsed = cli.parse_cameco_log_header(str(_log(tmp_path)))
    assert not parsed.skipped and parsed.state_plane_easting == 791126.0
    return parsed


def test_the_format_epsg_is_the_only_declaration_that_counts() -> None:
    assert cli.LOG_COORD_EPSG == 32155
    assert cli.log_crs_declared(32155)
    for other in (None, 32613, 26913, 4326):
        assert not cli.log_crs_declared(other)


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [None, 32613, 26904])
async def test_without_the_declaration_nothing_is_written(tmp_path: Path, declared: int | None) -> None:
    parsed = _parsed(tmp_path)
    conn = _Conn()

    updated = await cli.update_collar_with_log_coords(
        conn, project_id=_PJ, parsed=parsed, source_epsg=declared,  # type: ignore[arg-type]
    )
    created = await cli.upsert_collar_from_log(
        conn, project_id=_PJ, workspace_id=_WS, parsed=parsed, source_epsg=declared,  # type: ignore[arg-type]
    )

    assert updated is False and created is None
    assert conn.writes == []


@pytest.mark.asyncio
async def test_with_the_declaration_the_collar_is_written_as_declared(tmp_path: Path) -> None:
    parsed = _parsed(tmp_path)
    conn = _Conn()

    created = await cli.upsert_collar_from_log(
        conn, project_id=_PJ, workspace_id=_WS, parsed=parsed, source_epsg=32155,  # type: ignore[arg-type]
    )

    assert created is not None and len(conn.writes) == 1


def _zip_input(source_epsg: int | None) -> Any:
    return zip_module.IngestZipArchiveInput(
        minio_key="zips/a.zip", workspace_id=_WS, project_id=_PJ, run_id=_RUN,
        source_epsg=source_epsg,
    )


@pytest.mark.asyncio
async def test_a_zip_log_member_is_refused_with_a_named_warning_when_undeclared(tmp_path: Path) -> None:
    counts = dict.fromkeys(zip_module._COUNT_KEYS, 0)
    warnings: list[dict[str, str]] = []
    conn = _Conn()

    await zip_module._ingest_one(
        file_path=_log(tmp_path), ext="log", conn=conn, store=SimpleNamespace(),  # type: ignore[arg-type]
        input=_zip_input(None), counts=counts, member_warnings=warnings,
    )

    assert counts["log"] == 0 and counts["skipped"] == 1
    assert [w["code"] for w in warnings] == ["log_collar_crs_undeclared"]
    assert warnings[0]["file"] == _LOG_NAME
    assert conn.writes == []
    (summary,) = zip_module._member_warning_summaries(warnings)
    assert "EPSG:32155" in summary["detail"] and _LOG_NAME in summary["detail"]


@pytest.mark.asyncio
async def test_a_zip_log_member_lands_when_the_format_crs_is_declared(tmp_path: Path) -> None:
    counts = dict.fromkeys(zip_module._COUNT_KEYS, 0)
    warnings: list[dict[str, str]] = []
    conn = _Conn()

    await _run_with_async_null_tx(conn, tmp_path, counts, warnings)

    assert counts["log"] == 1 and counts["skipped"] == 0
    assert warnings == []
    assert len(conn.writes) == 1


async def _run_with_async_null_tx(
    conn: _Conn, tmp_path: Path, counts: dict[str, int], warnings: list[dict[str, str]],
) -> None:
    class _Tx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    conn.transaction = lambda: _Tx()  # type: ignore[method-assign]
    await zip_module._ingest_one(
        file_path=_log(tmp_path), ext="log", conn=conn, store=SimpleNamespace(),  # type: ignore[arg-type]
        input=_zip_input(32155), counts=counts, member_warnings=warnings,
    )


@pytest.mark.asyncio
async def test_the_cluster_runner_skips_the_log_pass_and_says_so_when_undeclared(tmp_path: Path) -> None:
    _log(tmp_path)
    conn = _Conn()

    summary = await ingest_cluster(
        str(tmp_path), workspace_id=_WS, conn=conn,  # type: ignore[arg-type]
        project_name="Some Project", project_slug="some-project", project_company="SOME CO",
    )

    assert summary.log_files == 1 and summary.log_collars_updated == 0
    assert any("EPSG:32155 was not declared" in e["err"] for e in summary.errors)
    assert conn.writes == []


def test_the_cluster_runner_has_no_dataset_defaults() -> None:
    import inspect

    params = inspect.signature(ingest_cluster).parameters
    for name in ("project_name", "project_slug", "project_company"):
        assert params[name].default is inspect.Parameter.empty, name
    assert "plss_section_key" not in params
    assert params["project_region"].default is None
