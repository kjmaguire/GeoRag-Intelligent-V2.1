"""Tests for QGIS project (.qgs / .qgz) parsing.

Fixtures are built here rather than committed as binaries so the expected
contents are readable in the test that asserts them.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from georag_geoparsers.qgis_parser import (
    QGIS_EXTENSIONS,
    _split_datasource,
    parse_qgis_project,
)

# A project with the four datasource shapes that actually occur: a GPKG
# sublayer reference, a second sublayer of the SAME file, a PostGIS
# connection string, and a relative path to data that was not shipped.
QGS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<qgis version="3.34.1-Prizren" projectname="Eagle Point">
  <title>Eagle Point</title>
  <projectCrs><spatialrefsys><authid>EPSG:4326</authid></spatialrefsys></projectCrs>
  <projectlayers>
    <maplayer geometry="Point">
      <layername>Collars</layername>
      <datasource>./eagle.gpkg|layername=collars</datasource>
      <provider>ogr</provider>
      <srs><spatialrefsys><authid>EPSG:4326</authid></spatialrefsys></srs>
    </maplayer>
    <maplayer geometry="Polygon">
      <layername>Target Zones</layername>
      <datasource>./eagle.gpkg|layername=zones</datasource>
      <provider>ogr</provider>
      <srs><spatialrefsys><authid>EPSG:4326</authid></spatialrefsys></srs>
    </maplayer>
    <maplayer geometry="Point">
      <layername>Corporate DB</layername>
      <datasource>dbname='corp' host=10.0.0.9 port=5432 table="public"."collars" (geom)</datasource>
      <provider>postgres</provider>
    </maplayer>
    <maplayer geometry="Line">
      <layername>Roads</layername>
      <datasource>../not_shipped/roads.shp</datasource>
      <provider>ogr</provider>
    </maplayer>
  </projectlayers>
</qgis>
"""


@pytest.fixture
def gpkg(tmp_path: Path) -> Path:
    """A two-layer GeoPackage, as QGIS's 'package layers' would write."""
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point, Polygon

    path = tmp_path / "eagle.gpkg"
    gpd.GeoDataFrame(
        {"hole_id": ["EL-001", "EL-002"], "au_gpt": [1.4, 0.2]},
        geometry=[Point(-105.66, 57.20), Point(-105.62, 57.24)],
        crs="EPSG:4326",
    ).to_file(path, layer="collars", driver="GPKG")
    gpd.GeoDataFrame(
        {"zone": ["Main"], "commodity": ["U"]},
        geometry=[Polygon([(-105.7, 57.1), (-105.5, 57.1), (-105.5, 57.3), (-105.7, 57.3)])],
        crs="EPSG:4326",
    ).to_file(path, layer="zones", driver="GPKG")
    return path


@pytest.fixture
def qgz(tmp_path: Path, gpkg: Path) -> Path:
    """A .qgz with its data packaged inside, the common real-world shape."""
    qgs = tmp_path / "eagle.qgs"
    qgs.write_text(QGS_XML, encoding="utf-8")
    archive = tmp_path / "eagle.qgz"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(qgs, "eagle.qgs")
        z.write(gpkg, "eagle.gpkg")
    return archive


class TestSplitDatasource:
    def test_gpkg_sublayer_reference(self):
        assert _split_datasource("./eagle.gpkg|layername=collars") == (
            "./eagle.gpkg", "collars",
        )

    def test_plain_file_path(self):
        assert _split_datasource("./collars.shp") == ("./collars.shp", None)

    def test_postgis_connection_string_is_not_a_path(self):
        """Treating this as a filename would produce a nonsense resolve."""
        assert _split_datasource(
            "dbname='corp' host=10.0.0.9 table=\"public\".\"collars\" (geom)"
        ) == (None, None)

    def test_extra_fragments_do_not_confuse_the_layer_name(self):
        path, sub = _split_datasource("./x.gpkg|layerid=0|layername=lith")
        assert (path, sub) == ("./x.gpkg", "lith")

    def test_empty(self):
        assert _split_datasource("") == (None, None)


class TestBareQgs:
    def test_manifest_is_returned_even_with_no_data(self, tmp_path: Path):
        """A .qgs points at the geologist's own disk. Zero features is the
        expected outcome, not a parse failure — the manifest is the value."""
        qgs = tmp_path / "bare.qgs"
        qgs.write_text(QGS_XML, encoding="utf-8")

        result = parse_qgis_project(str(qgs))

        assert result.source_format == "qgs"
        assert result.project_title == "Eagle Point"
        assert result.project_crs == "EPSG:4326"
        assert result.qgis_version == "3.34.1-Prizren"
        assert len(result.layers) == 4
        assert result.is_manifest_only is True
        assert result.resolved_layer_count == 0

    def test_says_why_nothing_resolved(self, tmp_path: Path):
        qgs = tmp_path / "bare.qgs"
        qgs.write_text(QGS_XML, encoding="utf-8")
        codes = {w["code"] for w in parse_qgis_project(str(qgs)).warnings}
        assert "data_not_bundled" in codes

    def test_does_not_search_the_containing_directory(self, tmp_path: Path):
        """A bare .qgs lands in a shared scratch dir. Walking it could attach
        an unrelated upload's collars.shp to this project's layer."""
        (tmp_path / "not_shipped").mkdir()
        decoy = tmp_path / "not_shipped" / "roads.shp"
        decoy.write_bytes(b"not really a shapefile")

        qgs = tmp_path / "bare.qgs"
        qgs.write_text(QGS_XML, encoding="utf-8")

        roads = next(
            lyr for lyr in parse_qgis_project(str(qgs)).layers if lyr.name == "Roads"
        )
        # ../not_shipped/roads.shp does not resolve from tmp_path, and the
        # basename fallback must not rescue it outside an archive.
        assert roads.resolved is False


class TestQgz:
    def test_resolves_bundled_data(self, qgz: Path):
        result = parse_qgis_project(str(qgz))
        assert result.source_format == "qgz"
        assert result.resolved_layer_count == 2
        assert result.is_manifest_only is False
        assert "eagle.gpkg" in result.bundled_files

    def test_each_layer_keeps_its_own_sublayer(self, qgz: Path):
        """Both layers are backed by the same .gpkg. Losing the sublayer makes
        them return identical features — which is what happened before
        parse_spatial_file grew a `layer` argument."""
        by_name = {lyr.name: lyr for lyr in parse_qgis_project(str(qgz)).layers}
        assert by_name["Collars"].sublayer == "collars"
        assert by_name["Target Zones"].sublayer == "zones"

    def test_resolved_layers_read_as_distinct_feature_sets(self, qgz: Path):
        from georag_geoparsers.spatial_parser import parse_spatial_file

        by_name = {lyr.name: lyr for lyr in parse_qgis_project(str(qgz)).layers}

        collars = parse_spatial_file(
            by_name["Collars"].resolved_path, layer=by_name["Collars"].sublayer,
        )
        zones = parse_spatial_file(
            by_name["Target Zones"].resolved_path, layer=by_name["Target Zones"].sublayer,
        )

        assert collars.feature_count == 2
        assert zones.feature_count == 1
        assert collars.features[0].geometry_type == "Point"
        assert zones.features[0].geometry_type == "Polygon"
        assert "hole_id" in collars.features[0].properties
        assert "commodity" in zones.features[0].properties

    def test_database_backed_layer_is_catalogued_not_resolved(self, qgz: Path):
        corp = next(
            lyr for lyr in parse_qgis_project(str(qgz)).layers
            if lyr.name == "Corporate DB"
        )
        assert corp.resolved is False
        assert corp.provider == "postgres"
        # Still recorded, so a reader can see the project referenced it.
        assert "dbname=" in corp.datasource

    def test_extract_dir_survives_for_the_caller(self, qgz: Path):
        """resolved_path must still be readable after parse returns — the
        TemporaryDirectory is held by the result, not scoped to the call."""
        result = parse_qgis_project(str(qgz))
        resolved = [lyr for lyr in result.layers if lyr.resolved]
        assert resolved
        assert Path(resolved[0].resolved_path).exists()


class TestSafety:
    def test_zip_slip_member_is_refused(self, tmp_path: Path):
        """A member named ../../evil.qgs must not be written outside the
        extraction root. Geology data arrives from third parties."""
        outside = tmp_path / "escaped.txt"
        archive = tmp_path / "evil.qgz"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("eagle.qgs", QGS_XML)
            z.writestr("../../escaped.txt", "pwned")

        parse_qgis_project(str(archive))

        assert not outside.exists()

    def test_archive_without_a_project_is_an_error_not_an_empty_result(
        self, tmp_path: Path,
    ):
        archive = tmp_path / "nothing.qgz"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("readme.txt", "no project here")

        with pytest.raises(ValueError, match="no .qgs"):
            parse_qgis_project(str(archive))

    def test_unparseable_xml_warns_rather_than_raising(self, tmp_path: Path):
        """One corrupt project must not take down a batch ingest."""
        qgs = tmp_path / "broken.qgs"
        qgs.write_text("<qgis><unclosed>", encoding="utf-8")

        result = parse_qgis_project(str(qgs))

        assert result.layers == []
        assert any(w["code"] == "project_xml_unparseable" for w in result.warnings)

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            parse_qgis_project(str(tmp_path / "nope.qgz"))

    def test_wrong_extension(self, tmp_path: Path):
        other = tmp_path / "data.shp"
        other.write_bytes(b"x")
        with pytest.raises(ValueError, match="not a QGIS project"):
            parse_qgis_project(str(other))

    def test_extension_set_is_what_the_router_checks(self):
        assert {".qgs", ".qgz"} == set(QGIS_EXTENSIONS)
