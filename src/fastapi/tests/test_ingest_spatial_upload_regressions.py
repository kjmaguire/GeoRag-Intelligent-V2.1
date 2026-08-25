"""Three defects a real customer upload found on 2026-08-23, after deploy.

The RedStar delivery went through the live intake and produced, among the
expected CRS refusals, three failures that were nothing to do with the data:

  * five zipped MapInfo tables reported "archive contains no readable vector
    file ... must include the .shp" — the parser had learned .tab/.mif but the
    workflow's own member list had not;
  * a DXF failed the INSERT outright with "Geometry has Z dimension but column
    does not", taking every feature in the file with it;
  * a legacy .xls reported "produced no searchable text" although xlrd was
    installed and the typed-drill parser had been reading .xls for months.

Each test below fails on the code as deployed.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

TAB = chr(9)
NL = chr(10)


class TestMapInfoIsAnArchiveMember:
    """A zipped .tab must be something _extract_archive will open."""

    def test_tab_and_mif_are_vector_extensions(self):
        from app.hatchet_workflows.ingest_spatial import VECTOR_EXTENSIONS

        assert ".tab" in VECTOR_EXTENSIONS
        assert ".mif" in VECTOR_EXTENSIONS

    @pytest.mark.parametrize("sidecar", [".dat", ".map", ".id", ".ind", ".mid"])
    def test_mapinfo_sidecars_are_not(self, sidecar):
        """Listing them would open the same table twice.

        A .mid opens directly, so a MIF/MID pair would be ingested as two
        layers -- the same reason .dbf and .shx are kept out of this set.
        """
        from app.hatchet_workflows.ingest_spatial import VECTOR_EXTENSIONS

        assert sidecar not in VECTOR_EXTENSIONS

    def test_a_zipped_tab_yields_a_member(self, tmp_path: Path):
        import zipfile

        from app.hatchet_workflows.ingest_spatial import _extract_archive

        archive = tmp_path / "table.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Sitka_trA.tab", '!table\n!version 300\nDefinition Table\n')
            z.writestr("Sitka_trA.dat", "binary-ish")
            z.writestr("Sitka_trA.map", "binary-ish")
        out = _extract_archive(archive, tmp_path / "un")
        names = sorted(p.name for p in out.members)
        assert names == ["Sitka_trA.tab"], (
            "the .tab is the entry point and the .dat/.map are read through it"
        )


class TestZGeometryDoesNotKillTheLayer:
    """silver.spatial_features.geom is coord_dimension 2."""

    def test_the_insert_forces_2d(self):
        from app.hatchet_workflows import ingest_spatial as m

        # Read the constant, not the file: a source grep would also match the
        # comment that explains the fix.
        sql = m._INSERT_SQL
        geom_expr = next(
            line for line in sql.splitlines() if "ST_GeomFromText" in line
        )
        assert "ST_Force2D" in geom_expr, (
            "a 3D WKT into a 2D column raises 'Geometry has Z dimension but "
            "column does not' and rolls back every feature in the file"
        )

    @pytest.mark.parametrize(
        "wkt,expected",
        [
            ("POINT Z (1 2 3)", True),
            ("point z (1 2 3)", True),
            ("LINESTRING ZM (1 2 3 4)", True),
            ("MULTIPOLYGON Z (((0 0 1,1 1 1,1 0 1,0 0 1)))", True),
            ("POINT (1 2)", False),
            ("POLYGON ((0 0,1 1,1 0,0 0))", False),
        ],
    )
    def test_z_detection(self, wkt, expected):
        from app.hatchet_workflows.ingest_spatial import _layer_drops_z

        result = SimpleNamespace(features=[SimpleNamespace(geometry_wkt=wkt)])
        assert _layer_drops_z(result) is expected

    def test_a_layer_with_one_z_feature_is_reported(self):
        from app.hatchet_workflows.ingest_spatial import _layer_drops_z

        result = SimpleNamespace(features=[
            SimpleNamespace(geometry_wkt="POINT (1 2)"),
            SimpleNamespace(geometry_wkt="POINT Z (1 2 3)"),
        ])
        assert _layer_drops_z(result) is True

    def test_no_features_is_not_a_z_layer(self):
        from app.hatchet_workflows.ingest_spatial import _layer_drops_z

        assert _layer_drops_z(SimpleNamespace(features=[])) is False
        assert _layer_drops_z(SimpleNamespace()) is False


class TestLegacyXlsReachesTheTextFallback:
    """openpyxl reads OOXML zips; .xls is an OLE2 binary."""

    def test_xls_suffixes_are_recognised(self):
        from app.services.ingest.xlsx_ingester import _XLS_SUFFIXES

        assert ".xls" in _XLS_SUFFIXES

    def test_xlrd_still_supports_xls(self):
        """xlrd must READ a .xls — asserted by reading one, not by version.

        This used to assert `major < 2`, on the premise that "xlrd 2.x
        dropped .xls". That is backwards, and the pin it defended had the
        same inverted comment. **xlrd 2.0 dropped .xlsx**, keeping .xls as
        the only format it reads.

        Measured inside georag-fastapi on RedStar's export_UTM.xls, calling
        xlrd directly at both versions:

            xlrd 1.2.0: open_workbook OK, rows=25 cols=12
            xlrd 2.0.2: open_workbook OK, rows=25 cols=12

        Identical. What actually broke at 1.2.0 was **pandas**, which
        refuses xlrd below 2.0.1 outright:

            ImportError: Pandas requires version '2.0.1' or newer of 'xlrd'
                         (version '1.2.0' currently installed)

        So the old assertion pinned the codebase to the one version range in
        which pandas cannot open a legacy workbook at all — in a product
        built for decades-old drill archives. Both of RedStar's .xls files
        were unreadable because of it.

        Asserting the BEHAVIOUR rather than the version is what keeps this
        honest: if a future xlrd genuinely drops .xls, open_workbook fails
        and so does this test, without anyone having to predict which
        version number does it.
        """
        xlrd = pytest.importorskip("xlrd")
        xlwt = pytest.importorskip("xlwt")

        book = xlwt.Workbook()
        sheet = book.add_sheet("collars")
        for col, name in enumerate(("hole_id", "easting", "northing")):
            sheet.write(0, col, name)
        sheet.write(1, 0, "TR002")
        sheet.write(1, 1, 400807.0)
        sheet.write(1, 2, 6117291.0)

        target = Path(tempfile.mkdtemp()) / "legacy.xls"
        book.save(str(target))

        opened = xlrd.open_workbook(str(target))
        read = opened.sheet_by_index(0)
        assert read.nrows == 2, f"xlrd {xlrd.__version__} did not read the rows"
        assert read.cell_value(0, 0) == "hole_id"
        assert read.cell_value(1, 1) == 400807.0

    def test_xlrd_satisfies_the_floor_pandas_demands(self):
        """The version fact that IS real, and the one that bit.

        xlrd reads .xls at any version; pandas refuses to USE it below
        2.0.1. Several ingest paths go through pandas.read_excel, so the
        floor is a genuine requirement even though the reader itself is not
        the limitation.
        """
        xlrd = pytest.importorskip("xlrd")
        major, minor, patch = (int(p) for p in xlrd.__version__.split(".")[:3])
        assert (major, minor, patch) >= (2, 0, 1), (
            f"xlrd {xlrd.__version__} is below the 2.0.1 floor pandas requires; "
            "pandas.read_excel will refuse every .xls with an ImportError"
        )

    def test_an_xls_never_reaches_openpyxl(self, tmp_path: Path):
        """The routing test, and it does not need a valid .xls to prove it.

        Feed the ingester bytes that are not a workbook at all. Before the fix
        the failure came back as openpyxl_failed:InvalidFileException, because
        every path went through load_workbook. After it, a .xls is handed to
        xlrd and fails as xlrd_failed:* — different reader, which is the whole
        point. Deliberately not guarded by importorskip: a skip here would let
        the regression back in silently.
        """
        import asyncio

        from app.services.ingest.xlsx_ingester import ingest_xlsx_file

        path = tmp_path / "not-really.xls"
        path.write_bytes(b"this is not an OLE2 compound document")

        # conn is untouched: both readers fail long before any SQL runs.
        result = asyncio.run(
            ingest_xlsx_file(None, str(path), workspace_id="w")  # type: ignore[arg-type]
        )
        assert result.skipped is True
        assert result.skipped_reason is not None
        assert result.skipped_reason.startswith("xlrd_failed:"), (
            f"a .xls must be read by xlrd, not openpyxl; got {result.skipped_reason}"
        )

    def test_it_reads_a_real_xls(self, tmp_path: Path):
        """Full round trip when xlwt is available to build a fixture."""
        pytest.importorskip("xlrd")
        xlwt = pytest.importorskip("xlwt")
        from app.services.ingest.xlsx_ingester import _xls_sheet_texts

        book = xlwt.Workbook()
        sheet = book.add_sheet("Ages")
        for c, v in enumerate(["Sample", "Age Ma", "method"]):
            sheet.write(0, c, v)
        for c, v in enumerate(["82ASh014", 37.1, "K-Ar"]):
            sheet.write(1, c, v)
        book.add_sheet("Empty")
        path = tmp_path / "ages.xls"
        book.save(str(path))

        out = _xls_sheet_texts(str(path))
        assert [name for name, _ in out] == ["Ages"], "an empty sheet adds nothing"
        text = out[0][1]
        assert "Sample" in text and "82ASh014" in text
        assert re.search("Sample" + TAB + ".*" + NL + ".*82ASh014" + TAB, text), "tab separated, header first"
