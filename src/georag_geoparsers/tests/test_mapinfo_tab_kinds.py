"""Not every .tab that fails to open is a broken vector table.

Three of the RedStar delivery's five .tab files are not map layers at all,
and all three were reported as "missing .map, .dat — re-upload the table
with every sidecar". Two of them have no sidecars anywhere in the delivery
because they never had any, so that advice cannot be followed; and none of
the three becomes vector data if it is.
"""

import pytest

from georag_geoparsers.spatial_parser import (
    _inspect_mapinfo_tab,
    _mapinfo_declared_crs,
)

GCP_HEADER = r'''!table
!version 300
!charset WindowsLatin1

Definition Table
  Type NATIVE Charset "Neutral"
  Fields 10
    ID Integer ;
    Use Logical ;
    Image_X Integer ;
    Image_Y Integer ;
    Map_X Float ;
    Map_Y Float ;
    RMS Float ;
    ResidualX Float ;
    ResidualY Float ;
    Description Char (100) ;
ReadOnly
begin_metadata
"\Discover" = ""
"\Discover\Warp" = ""
"\Discover\Warp\Projection" = "CoordSys Earth Projection 8, 74,'m', -159, 0, 0.9996, 500000, 0"
"\Discover\Warp\ProjectionName" = "UTM Zone 4 (NAD 83)"
end_metadata
'''

XSECT_HEADER = r'''!table
!version 300
!charset WindowsLatin1

Definition Table
  Type NATIVE Charset "WindowsLatin1"
  Fields 3
    ID Integer ;
    NumVal Float ;
    StrVal Char (50) ;
begin_metadata
"\Discover\xsects" = ""
"\Discover\xsects\project" = "Sitka_tr"
"\Discover\xsects\depth_units" = "m"
end_metadata
'''

PLAIN_HEADER = '''!table
!version 300
!charset WindowsLatin1

Definition Table
  Type NATIVE Charset "Neutral"
  Fields 2
    ID Integer ;
    Rock_Type Char (40) ;
'''


def _tab(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="latin-1")
    return str(p)


class TestGcpTables:
    def test_a_control_point_table_is_not_a_broken_layer(self, tmp_path):
        path = _tab(tmp_path, "trench_gcp.TAB", GCP_HEADER)
        with pytest.raises(NotImplementedError) as err:
            _inspect_mapinfo_tab(path)
        msg = str(err.value)
        assert "control-point table" in msg
        # The advice that cannot work must be gone.
        assert "every sidecar" not in msg
        # The advice that can: upload the image it rectifies.
        assert ".tif" in msg

    def test_it_reports_the_crs_the_header_declares(self, tmp_path):
        path = _tab(tmp_path, "trench_gcp.TAB", GCP_HEADER)
        with pytest.raises(NotImplementedError) as err:
            _inspect_mapinfo_tab(path)
        assert "UTM Zone 4 (NAD 83)" in str(err.value)

    def test_gcp_wins_over_the_sidecar_check(self, tmp_path):
        # No .map and no .dat beside it — the sidecar branch would fire
        # first if this were ordered the other way, and its message would
        # send the reader looking for files that never existed.
        path = _tab(tmp_path, "trench_gcp.TAB", GCP_HEADER)
        with pytest.raises(NotImplementedError):
            _inspect_mapinfo_tab(path)


class TestCrossSectionTables:
    def test_a_discover_section_is_named_as_one(self, tmp_path):
        path = _tab(tmp_path, "Sitka_trA.tab", XSECT_HEADER)
        with pytest.raises(NotImplementedError) as err:
            _inspect_mapinfo_tab(path)
        msg = str(err.value)
        assert "cross-section definition" in msg
        assert "collar and interval tables" in msg


class TestOrdinaryTables:
    def test_a_real_vector_tab_still_reports_missing_sidecars(self, tmp_path):
        path = _tab(tmp_path, "geology.tab", PLAIN_HEADER)
        with pytest.raises(FileNotFoundError) as err:
            _inspect_mapinfo_tab(path)
        assert "missing .map, .dat" in str(err.value)

    def test_a_complete_vector_tab_passes_preflight(self, tmp_path):
        path = _tab(tmp_path, "geology.tab", PLAIN_HEADER)
        (tmp_path / "geology.map").write_bytes(b"\x00")
        (tmp_path / "geology.dat").write_bytes(b"\x00")
        _inspect_mapinfo_tab(path)   # must not raise


class TestDeclaredCrs:
    def test_prefers_the_projection_name(self):
        assert _mapinfo_declared_crs(GCP_HEADER) == "UTM Zone 4 (NAD 83)"

    def test_falls_back_to_the_coordsys_clause(self):
        header = 'Type "RASTER"\n  CoordSys Earth Projection 8, 74, "m", -159, 0\n'
        assert _mapinfo_declared_crs(header).startswith("CoordSys Earth Projection 8")

    def test_none_when_the_header_declares_nothing(self):
        assert _mapinfo_declared_crs(PLAIN_HEADER) is None
