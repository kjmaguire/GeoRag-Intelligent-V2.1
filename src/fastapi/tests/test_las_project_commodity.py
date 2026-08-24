"""The LAS ingester stamped "uranium" on every project it created.

`las_ingester` was written for the Wyoming Cameco / WSGS uranium archive
and two places carried that origin as a literal:

  - `_get_or_create_project(commodity: str = "uranium")` — the parameter
    default, reached only when `ingest_las_file` is called WITHOUT
    `project_id_override`.
  - `cluster_runner.ingest_cluster`'s stub-project INSERT, which hardcoded
    `'uranium'` directly in the VALUES list with no way for a caller to
    override it. This is the one that actually fired.

`silver.projects.commodity` is nullable (varchar 50, see
2026_04_09_180000_create_projects_table.php) and LAS 2.0 has no commodity
field in its ~W section, so NULL — "the file does not say" — is the only
honest value. Same fix, same reasoning, as the `silver.reports.commodity`
hardcode removed from `xlsx_ingester` on 2026-08-21.
"""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    import lasio as _lasio_probe  # noqa: F401
    _LASIO_AVAILABLE = True
except ImportError:
    _LASIO_AVAILABLE = False

_requires_lasio = pytest.mark.skipif(
    not _LASIO_AVAILABLE,
    reason="lasio not installed in this image — rebuild fastapi after pin lands",
)

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_NEW_PJ = "c2000000-0000-0000-0000-0000000000b0"

# Bind order of the silver.projects INSERT in both modules under test:
# project_name, slug, company, region, commodity, workspace_id
_COMMODITY_ARG = 4


class _NullTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingConn:
    """Records the bind args of every INSERT INTO silver.projects.

    Permissive everywhere else — this suite only cares about what lands in
    the project row's commodity column.
    """

    def __init__(self, *, existing_project: str | None = None) -> None:
        self.existing_project = existing_project
        self.project_args: tuple | None = None
        self.project_inserts = 0

    def is_in_transaction(self) -> bool:
        # See the note in test_entity_resolver.py: bind_workspace_scope
        # now refuses SET LOCAL outside a transaction.
        return getattr(self, "_in_tx", False)

    def transaction(self):
        conn = self

        class _TrackedTx:
            async def __aenter__(self):
                conn._in_tx = True
                return None

            async def __aexit__(self, *exc):
                conn._in_tx = False
                return False

        return _TrackedTx()

    async def execute(self, sql: str, *args):
        return "SET"

    async def fetchval(self, sql: str, *args):
        return self.existing_project or _NEW_PJ

    async def fetchrow(self, sql: str, *args):
        flat = " ".join(sql.split())
        if "INSERT INTO silver.projects" in flat:
            self.project_inserts += 1
            self.project_args = args
            return {"project_id": _NEW_PJ}
        if flat.startswith("SELECT project_id"):
            if self.existing_project:
                return {"project_id": self.existing_project}
            return None
        if "INSERT INTO silver.collars" in flat:
            return {"collar_id": "d3000000-0000-0000-0000-0000000000c0"}
        if flat.startswith("SELECT collar_id"):
            return None
        if "INSERT INTO" in flat and "RETURNING" in flat:
            return {"id": "e4000000-0000-0000-0000-0000000000d0"}
        return None


def _minimal_las(path: Path, *, well: str = "GOLD-001", comp: str = "ACME GOLD") -> None:
    """A LAS 2.0 file for a gold hole. Note there is nowhere to say "gold"."""
    path.write_text(
        "~VERSION INFORMATION\n"
        " VERS.                 2.0 : CWLS LOG ASCII STANDARD - VERSION 2.0\n"
        " WRAP.                  NO : ONE LINE PER DEPTH STEP\n"
        "~WELL INFORMATION\n"
        "STRT .F          0.0 : START DEPTH\n"
        "STOP .F        100.0 : STOP DEPTH\n"
        "STEP .F          0.5 : STEP\n"
        "NULL .       -999.25 : NULL VALUE\n"
        f"COMP .  {comp} : COMPANY\n"
        f"WELL .  {well} : WELL\n"
        "FLD  .  Red Lake : FIELD\n"
        "CNTY .  KENORA : COUNTY\n"
        "STAT .  ON : STATE\n"
        "LOC  .  36 28 79 : LOCATION\n"
        "DATE .  08/13/2012 : DATE\n"
        "~CURVE INFORMATION\n"
        "DEPT .F  : DEPTH\n"
        "GR   .API : GAMMA RAY\n"
        "~ASCII\n"
        "0.0   12.0\n"
        "0.5   14.0\n",
        encoding="ascii",
    )


class TestTheProjectRowIsHonest:
    @pytest.mark.asyncio
    @_requires_lasio
    async def test_the_commodity_is_not_stamped_uranium(self) -> None:
        """A gold hole's project row must not claim uranium."""
        from app.services.ingest.las_ingester import _get_or_create_project

        conn = _RecordingConn()
        await _get_or_create_project(
            conn,
            project_name="ACME GOLD — Red Lake",
            company="ACME GOLD",
            region="KENORA, ON",
            workspace_id=_WS,
        )

        assert conn.project_args is not None
        assert "uranium" not in [str(a).lower() for a in conn.project_args]

    @pytest.mark.asyncio
    @_requires_lasio
    async def test_an_unstated_commodity_lands_as_null_not_a_guess(self) -> None:
        """The column is nullable; NULL is how "the LAS did not say" is spelled."""
        from app.services.ingest.las_ingester import _get_or_create_project

        conn = _RecordingConn()
        await _get_or_create_project(
            conn,
            project_name="ACME GOLD — Red Lake",
            company="ACME GOLD",
            region="KENORA, ON",
            workspace_id=_WS,
        )

        assert conn.project_args[_COMMODITY_ARG] is None

    @pytest.mark.asyncio
    @_requires_lasio
    async def test_a_caller_that_knows_the_commodity_is_still_honoured(self) -> None:
        """Dropping the default must not remove the ability to state one."""
        from app.services.ingest.las_ingester import _get_or_create_project

        conn = _RecordingConn()
        await _get_or_create_project(
            conn,
            project_name="ACME GOLD — Red Lake",
            company="ACME GOLD",
            region="KENORA, ON",
            workspace_id=_WS,
            commodity="gold",
        )

        assert conn.project_args[_COMMODITY_ARG] == "gold"


class TestWhichCallPathActuallyReachedTheDefault:
    @pytest.mark.asyncio
    @_requires_lasio
    async def test_project_id_override_creates_no_project_row_at_all(
        self, tmp_path,
    ) -> None:
        """Both production callers pass project_id_override, so the
        `_get_or_create_project` default was latent, not live.

        `ingest_zip_archive` passes `input.project_id` (a REQUIRED,
        UUID-validated field on IngestZipArchiveInput) and `cluster_runner`
        passes its own stub id. Pinning this keeps a future caller from
        quietly reopening the path without noticing it creates projects.
        """
        from app.services.ingest.las_ingester import ingest_las_file

        path = tmp_path / "hole.las"
        _minimal_las(path)
        conn = _RecordingConn()

        result = await ingest_las_file(
            conn, str(path), workspace_id=_WS, project_id_override=_PJ,
        )

        assert not result.skipped, result.skipped_reason
        assert result.project_id == _PJ
        assert conn.project_inserts == 0

    @pytest.mark.asyncio
    @_requires_lasio
    async def test_without_an_override_the_project_row_is_created_clean(
        self, tmp_path,
    ) -> None:
        """The latent path, exercised: it must not reintroduce uranium."""
        from app.services.ingest.las_ingester import ingest_las_file

        path = tmp_path / "hole.las"
        _minimal_las(path)
        conn = _RecordingConn()

        result = await ingest_las_file(conn, str(path), workspace_id=_WS)

        assert not result.skipped, result.skipped_reason
        assert conn.project_inserts == 1
        assert "uranium" not in [str(a).lower() for a in conn.project_args]


class TestTheClusterRunnerStubProject:
    @pytest.mark.asyncio
    async def test_the_stub_project_is_not_stamped_uranium(self, tmp_path) -> None:
        """This is the hardcode that actually fired on every cluster ingest.

        An empty cluster_dir exercises the stub-project INSERT and nothing
        else — every later pass rglobs for files that aren't there.
        """
        from app.services.ingest.cluster_runner import ingest_cluster

        conn = _RecordingConn()
        await ingest_cluster(
            str(tmp_path),
            workspace_id=_WS,
            conn=conn,
            project_name="ACME GOLD Red Lake",
            project_slug="acme-gold-red-lake",
            project_company="ACME GOLD",
            project_region="KENORA, ON",
        )

        assert conn.project_args is not None
        assert "uranium" not in [str(a).lower() for a in conn.project_args]
        assert conn.project_args[_COMMODITY_ARG] is None

    @pytest.mark.asyncio
    async def test_a_caller_that_knows_the_commodity_is_still_honoured(
        self, tmp_path,
    ) -> None:
        from app.services.ingest.cluster_runner import ingest_cluster

        conn = _RecordingConn()
        await ingest_cluster(
            str(tmp_path),
            workspace_id=_WS,
            conn=conn,
            project_name="Cameco Shirley Basin Uranium",
            project_slug="cameco-shirley-basin",
            project_company="CAMECO RESOURCES",
            project_region="CARBON, WY",
            project_commodity="uranium",
        )

        assert conn.project_args[_COMMODITY_ARG] == "uranium"
