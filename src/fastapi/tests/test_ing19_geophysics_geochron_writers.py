"""ING-19 writers: parser -> writer, against a recording fake connection.

What is pinned here (no database, no Hatchet):

* every column the writers INSERT exists in the migrations that create the
  table — the contract a live Postgres would otherwise be the first to check;
* every ``$n`` placeholder is bound (a missing argument is a runtime error
  on the first real upload, not a lint failure);
* XYZ: one parent survey upserted, its children cleared BEFORE any line is
  written (replace, not append), one line row per Geosoft line marker, one
  channel row per (line, channel), and the CRS decision surfaced as a warning
  when it was assumed;
* DCIP2D: readings carry chainages and their file row numbers, models carry
  the air mask, and "not georeferenced" is said out loud;
* geochronology: replace-per-file, per-row refusal, duplicate reporting;
* the routing rules that decide which files reach these writers.

The real-database half is tests/test_ing19_real_postgres.py.
"""

from __future__ import annotations

import re
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app.services.ingest import dcip_bundle, geochronology_writer, geophysics_writer

FIXTURES = Path(__file__).parent / "fixtures" / "geophysics"
REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO_ROOT / "database" / "migrations"
WS = "11111111-1111-1111-1111-111111111111"
PJ = "22222222-2222-2222-2222-222222222222"
SURVEY_ID = "33333333-3333-3333-3333-333333333333"



# ---------------------------------------------------------------------------
# A recording fake
# ---------------------------------------------------------------------------

class FakeConn:
    """Records statements; answers the few reads the writers make."""

    def __init__(self, *, replaced: bool = False, duplicate_rows: set[int] | None = None):
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.replaced = replaced
        self.duplicate_rows = duplicate_rows or set()
        self._line = 0

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append(("fetchrow", sql, args))
        if "INSERT INTO silver.geophysics_surveys" in sql:
            return {"survey_id": SURVEY_ID, "replaced": self.replaced}
        return None     # project_reference: no boundary, no collars

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append(("fetchval", sql, args))
        if "INSERT INTO silver.geophysics_lines" in sql:
            self._line += 1
            return f"44444444-4444-4444-4444-{self._line:012d}"
        if "INSERT INTO silver.geochronology_samples" in sql:
            return None if args[20] in self.duplicate_rows else "55555555-5555-5555-5555-555555555555"
        if "SELECT source_file FROM silver.geochronology_samples" in sql:
            return "20260101_000000_older.csv"
        return 0

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql, args))
        return "OK"

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        for row in rows:
            self.calls.append(("executemany", sql, row))

    def matching(self, needle: str) -> list[tuple[str, str, tuple[Any, ...]]]:
        return [c for c in self.calls if needle in c[1]]


# ---------------------------------------------------------------------------
# Contract: INSERT columns vs the migrations; placeholders vs arguments
# ---------------------------------------------------------------------------

_CREATE = re.compile(
    r"CREATE TABLE IF NOT EXISTS (silver\.[a-z_]+) \((.*?)\n\s*\)\n\s*SQL", re.S,
)
_ALTER = re.compile(
    r"ALTER TABLE (silver\.[a-z_]+)\s+((?:ADD COLUMN IF NOT EXISTS [^\n]+\s*)+)", re.S,
)
_COLUMN_LINE = re.compile(r"^\s{12,}([a-z_0-9]+)\s+[a-z]", re.M)
_ADDED = re.compile(r"ADD COLUMN IF NOT EXISTS ([a-z_0-9]+)")


def _migration_columns() -> dict[str, set[str]]:
    columns: dict[str, set[str]] = {}
    for name in (
        "2026_05_21_030000_create_silver_geophysics_surveys.php",
        "2026_05_24_010000_create_silver_geochronology_samples.php",
        "2026_09_29_231500_wire_geophysics_and_geochronology_ingest_tables.php",
    ):
        text = (MIGRATIONS / name).read_text(encoding="utf-8")
        for table, body in _CREATE.findall(text):
            found = {
                c for c in _COLUMN_LINE.findall(body)
                if c not in {"constraint", "check", "foreign", "references", "on", "or", "and"}
            }
            columns.setdefault(table, set()).update(found)
        for table, adds in _ALTER.findall(text):
            columns.setdefault(table, set()).update(_ADDED.findall(adds))
    return columns


def _insert_columns(sql: str) -> tuple[str, list[str]]:
    m = re.search(r"INSERT INTO (silver\.[a-z_]+) \((.*?)\)", sql, re.S)
    assert m, sql
    return m.group(1), [c.strip() for c in m.group(2).split(",") if c.strip()]


@pytest.mark.parametrize(
    "sql",
    [
        geophysics_writer._SURVEY_UPSERT_SQL,
        geophysics_writer.LINE_SQL,
        geophysics_writer.CHANNEL_SQL,
        geophysics_writer.OBSERVATION_SQL,
        geophysics_writer.MODEL_SQL,
        geochronology_writer.INSERT_SQL,
    ],
    ids=["surveys", "lines", "channels", "dcip_observations", "dcip_models", "geochron"],
)
async def test_every_inserted_column_is_created_by_a_migration(sql: str) -> None:
    table, columns = _insert_columns(sql)
    known = _migration_columns().get(table)
    assert known, f"no migration creates {table}"
    missing = [c for c in columns if c not in known]
    assert not missing, f"{table}: {missing} are not created by any migration"


def _placeholders(sql: str) -> int:
    return max(int(n) for n in re.findall(r"\$(\d+)", sql))


# ---------------------------------------------------------------------------
# Geosoft XYZ
# ---------------------------------------------------------------------------

async def _write_fixture_xyz(conn: FakeConn, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "path": str(FIXTURES / "sitka_mag.xyz"),
        "workspace_id": WS,
        "project_id": PJ,
        "survey_name": "sitka_mag.xyz",
        "source_file": "20260929_120000_sitka_mag.xyz",
        "source_file_sha256": "a" * 64,
        "source_object_key": f"xyz/{PJ}/20260929_120000_sitka_mag.xyz",
        "declared_epsg": None,
        "project_epsg": None,
        "default_epsg": 32613,
    }
    kwargs.update(overrides)
    return await geophysics_writer.write_xyz_survey(conn, **kwargs)


async def test_xyz_writes_one_survey_and_a_row_per_line() -> None:
    conn = FakeConn()
    result = await _write_fixture_xyz(conn, declared_epsg=26909)

    upserts = conn.matching("INSERT INTO silver.geophysics_surveys")
    assert len(upserts) == 1
    args = upserts[0][2]
    assert len(args) == _placeholders(geophysics_writer._SURVEY_UPSERT_SQL)
    assert args[2] == "magnetic"            # survey_type from MAG_* channels
    assert args[3] == "sitka_mag.xyz"       # survey_name: the upsert key
    assert args[4] == ["1010", "1020", "9010"]
    assert args[5] == 26909
    assert args[13] == "declared"

    lines = conn.matching("INSERT INTO silver.geophysics_lines")
    assert [(c[2][3], c[2][4]) for c in lines] == [
        ("1010", "line"), ("1020", "line"), ("9010", "tie"),
    ]
    for _kind, sql, line_args in lines:
        assert len(line_args) == _placeholders(sql)
    # Rows 12 (non-numeric Y) and 13 (short) skipped; 4 points remain on 1010.
    assert lines[0][2][8] == [9, 10, 11, 14]

    channels = conn.matching("INSERT INTO silver.geophysics_line_channels")
    assert len(channels) == 3 * 4            # 3 lines x (FID, TMI, DIURN, RESID)
    resid = next(c[2] for c in channels if c[2][4] == "MAG_RESID")
    assert resid[5] == [-12.3, None, -8.7, -6.1]
    assert resid[6] == 1                     # null_count
    assert len(resid) == _placeholders(geophysics_writer.CHANNEL_SQL)

    assert result.counts["points"] == 9 and result.counts["lines"] == 3
    assert result.rows_written == 9
    codes = {w["code"] for w in result.warnings}
    assert {
        "xyz_rows_skipped_coordinate_not_numeric",
        "xyz_rows_skipped_field_count_mismatch",
    } <= codes
    assert "geophysics_crs_assumed" not in codes


async def test_xyz_clears_the_previous_upload_before_writing_lines() -> None:
    conn = FakeConn(replaced=True)
    result = await _write_fixture_xyz(conn, declared_epsg=26909)
    order = [
        ("clear" if "DELETE FROM silver.geophysics_lines" in sql else
         "line" if "INSERT INTO silver.geophysics_lines" in sql else "")
        for _k, sql, _a in conn.calls
    ]
    order = [o for o in order if o]
    assert order[0] == "clear" and order.count("clear") == 1
    assert result.replaced is True


async def test_xyz_without_a_crs_is_placed_by_assumption_and_says_so() -> None:
    conn = FakeConn()
    result = await _write_fixture_xyz(conn)
    assert "geophysics_crs_assumed" in {w["code"] for w in result.warnings}
    assert conn.matching("INSERT INTO silver.geophysics_surveys")[0][2][13] == "assumed"


async def test_a_file_with_no_coordinate_header_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.xyz"
    bad.write_text("/ FID MAG\n1 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        await _write_fixture_xyz(FakeConn(), path=str(bad))


def test_survey_type_label() -> None:
    assert geophysics_writer.classify_channels(["X", "Y", "FID", "MAG_TMI"])[0] == "magnetic"
    assert geophysics_writer.classify_channels(["GRAV_BOUGUER", "ELEV"])[0] == "gravity"
    assert geophysics_writer.classify_channels(["K_PCT", "U_PPM", "TH_PPM"])[0] == "radiometric"
    # Mag + radiometrics, the common airborne package: 'other', families kept.
    kind, families = geophysics_writer.classify_channels(["MAG_TMI", "K_PCT"])
    assert kind == "other" and set(families) == {"magnetic", "radiometric"}


# ---------------------------------------------------------------------------
# DCIP2D
# ---------------------------------------------------------------------------

def _dcip_export(root: Path) -> Path:
    export = root / "L1200N" / "export"
    export.mkdir(parents=True)
    (export / "SYN_Vp_XYZ.rdtmd").write_text(
        "Vp - Line 1200 N\nPole-Dipole\n  100.0  100.0  150.0  200.0  0.5\n"
        "  bad row\n  150.0  150.0  200.0  250.0  0.25\n",
        encoding="ascii",
    )
    (export / "SYN_Vp_XYZ.rdtmm").write_text("Vp - Line 1200 N\nPole-Dipole\n", encoding="ascii")
    (export / "dcinv2d.010").write_text("  2   2\n0.01 0.02 0.03 0.04\n", encoding="ascii")
    (export / "ipinv2d.chg").write_text("  2   2\n-1e30 5.0 6.0 7.0\n", encoding="ascii")
    (export / "IP.inp").write_text(
        "0 15 ! niter, irest\ndcinv2d.msh ! mesh\nL1200dz.txt ! topography\n",
        encoding="ascii",
    )
    (export / "L1200dz.txt").write_text("topo\n", encoding="ascii")
    return export


async def test_dcip_readings_and_models_land_under_one_survey(tmp_path: Path) -> None:
    from georag_geoparsers.dcip2d_survey import read_dcip2d_survey

    survey = read_dcip2d_survey(_dcip_export(tmp_path), skip_bad_rows=True)
    conn = FakeConn()
    result = await geophysics_writer.write_dcip_survey(
        conn, survey=survey, workspace_id=WS, project_id=PJ,
        survey_name="L1200N DCIP2D — ip.zip/L1200N/export",
        source_file="b.zip", source_file_sha256="b" * 64, source_object_key="geophysics/x",
    )
    upsert = conn.matching("INSERT INTO silver.geophysics_surveys")[0][2]
    assert upsert[2] == "IP" and upsert[4] == ["L1200N"]
    assert upsert[5] is None and upsert[13] is None     # no CRS, no georef method

    readings = conn.matching("INSERT INTO silver.geophysics_dcip_observations")
    assert [(r[2][4], r[2][5]) for r in readings] == [
        ("SYN_Vp_XYZ.rdtmd", 3), ("SYN_Vp_XYZ.rdtmd", 5),
    ]
    assert readings[0][2][8:13] == (100.0, 100.0, 150.0, 200.0, 0.5)
    assert all(len(r[2]) == _placeholders(geophysics_writer.OBSERVATION_SQL) for r in readings)

    models = {m[2][4]: m[2] for m in conn.matching("INSERT INTO silver.geophysics_dcip_models")}
    assert set(models) == {"dcinv2d", "ipinv2d"}
    ip = models["ipinv2d"]
    assert ip[8] == "mV/V" and ip[7] is True             # unit, is_final
    assert ip[12] == [True, False, False, False]         # the air cell
    assert (ip[13], ip[14]) == (5.0, 7.0)                # air excluded from range

    codes = {w["code"] for w in result.warnings}
    assert {"dcip_not_georeferenced", "dcip_rows_skipped"} <= codes
    assert result.counts == {
        "observations": 2, "models": 2, "skipped_rows": 1, "rejected_files": 0,
    }


def test_dcip_export_is_claimed_as_one_member(tmp_path: Path) -> None:
    root = tmp_path / "extracted"
    export = _dcip_export(root / "Geophysics" / "IP")
    report = export / "L1200N_interpretation.pdf"
    report.write_bytes(b"%PDF-1.4")
    other = root / "collars.csv"
    other.write_text("hole_id\n", encoding="utf-8")
    files = sorted(p for p in root.rglob("*") if p.is_file())

    exports, rest = dcip_bundle.find_dcip_exports(files)
    assert len(exports) == 1
    owned = {p.name for p in exports[0].members}
    # The .inp names the topography file, so it travels too; the PDF does not.
    assert owned == {
        "SYN_Vp_XYZ.rdtmd", "SYN_Vp_XYZ.rdtmm", "dcinv2d.010", "ipinv2d.chg",
        "IP.inp", "L1200dz.txt",
    }
    assert set(rest) == {report, other}

    bundle = tmp_path / "bundle.zip"
    dcip_bundle.write_bundle(exports[0], root, bundle)
    with zipfile.ZipFile(bundle) as zf:
        assert "Geophysics/IP/L1200N/export/IP.inp" in zf.namelist()
    dirs = dcip_bundle.extract_bundle(bundle, tmp_path / "unpacked")
    assert [d.relative_to(tmp_path / "unpacked").as_posix() for d in dirs] == [
        "Geophysics/IP/L1200N/export",
    ]
    assert dcip_bundle.survey_name_for(
        "ip.zip", dirs[0], tmp_path / "unpacked", "L1200N",
    ) == "L1200N DCIP2D — ip.zip/Geophysics/IP/L1200N/export"


def test_a_bundle_member_escaping_the_bundle_is_refused(tmp_path: Path) -> None:
    bundle = tmp_path / "evil.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("../escape.rdt", "x")
    with pytest.raises(ValueError, match="escapes"):
        dcip_bundle.extract_bundle(bundle, tmp_path / "out")


# ---------------------------------------------------------------------------
# Geochronology
# ---------------------------------------------------------------------------

def _parsed_ages() -> Any:
    from georag_geoparsers.csv_geochronology import parse_csv_geochronology

    return parse_csv_geochronology(FIXTURES / "ages_utm.csv", source_label="ages_utm.csv")


async def test_geochron_rows_are_written_with_lineage_and_crs() -> None:
    conn = FakeConn()
    stats = await geochronology_writer.write_geochronology(
        conn, workspace_id=WS, project_id=PJ, result=_parsed_ages(), label="ages_utm.csv",
        source_file="20260929_120000_ages_utm.csv", source_file_sha256="c" * 64,
        source_object_key="geochronology/x", declared_epsg=26909, project_epsg=None,
        default_epsg=32613,
    )
    replace = conn.matching("DELETE FROM silver.geochronology_samples")
    assert replace and replace[0][2][2] == "ages_utm.csv"    # stamp-stripped name
    inserts = conn.matching("INSERT INTO silver.geochronology_samples")
    assert len(inserts) == 3 and stats.written == 3
    first = inserts[0][2]
    assert len(first) == _placeholders(geochronology_writer.INSERT_SQL)
    assert (first[2], first[4], first[6]) == ("SK-01", "U-Pb", 1845.2)
    assert (first[12], first[13], first[16]) == (495000.0, 6220000.0, 26909)
    assert first[15] == "declared"
    assert first[20] == 2                                     # source_row
    # SK-03 has no coordinates: kept, unlocated.
    sk03 = next(c[2] for c in inserts if c[2][2] == "SK-03")
    assert sk03[12] is None and sk03[16] is None
    assert stats.located == 2


async def test_geochron_duplicates_are_reported_not_overwritten() -> None:
    conn = FakeConn(duplicate_rows={3})
    stats = await geochronology_writer.write_geochronology(
        conn, workspace_id=WS, project_id=PJ, result=_parsed_ages(), label="ages_utm.csv",
        source_file="20260929_120000_ages_utm.csv", source_file_sha256=None,
        source_object_key=None, declared_epsg=26909, project_epsg=None, default_epsg=32613,
    )
    assert (stats.written, stats.skipped) == (2, 1)
    warning = next(w for w in stats.warnings if w["code"] == "geochron_rows_not_written")
    assert "older.csv" in warning["detail"]


async def test_geochron_without_a_crs_warns_that_it_assumed_one() -> None:
    stats = await geochronology_writer.write_geochronology(
        FakeConn(), workspace_id=WS, project_id=PJ, result=_parsed_ages(),
        label="ages_utm.csv", source_file="ages_utm.csv", source_file_sha256=None,
        source_object_key=None, declared_epsg=None, project_epsg=None, default_epsg=32613,
    )
    assert "geochron_crs_assumed" in {w["code"] for w in stats.warnings}


def test_geochronology_routing() -> None:
    drill = ("collar", "survey", "lithology", "sample")
    route = geochronology_writer.routes_to_geochronology
    assert route(["Sample", "System", "Age_Ma"], "unknown", drill_types=drill)
    # Strong signal beats a drill classification.
    assert route(["HoleID", "From", "To", "Sample", "System", "Age_Ma"], "sample", drill_types=drill)
    # Weak only when nothing else claimed the table.
    assert route(["Sample", "Method", "Age"], "unknown", drill_types=drill)
    assert not route(["Sample", "Method", "Age"], "sample", drill_types=drill)
    assert not route(["HoleID", "From", "To", "Au_ppm"], "sample", drill_types=drill)
    assert route(["anything"], "unknown", drill_types=drill, hinted=True)
    assert geochronology_writer.logical_source_name("20260929_120000_123456_a.csv") == "a.csv"
