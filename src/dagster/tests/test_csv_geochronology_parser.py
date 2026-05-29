"""Unit tests for the CSV geochronology parser (CC-03 Item 3).

Covers the three test cases the task brief calls for:
  1. well-formed CSV → expected silver row
  2. bad uncertainty_kind → reject (row counted in ``skipped_rows``)
  3. geom from lat/lon (parser emits ``geom_wkt`` = ``POINT(lon lat)``)
"""

from __future__ import annotations

import textwrap
from io import StringIO

import pytest

from georag_dagster.parsers.csv_geochronology import (
    ISOTOPIC_SYSTEM_SUB_TYPE_ID,
    VALID_ISOTOPIC_SYSTEMS,
    VALID_UNCERTAINTY_KINDS,
    parse_csv_geochronology,
)


GOOD_CSV = textwrap.dedent("""\
    sample_id,isotopic_system,mineral_dated,age_ma,age_uncertainty_ma,uncertainty_kind,analytical_method,laboratory,publication_ref,rock_type,latitude,longitude
    BR-2018-01,U-Pb,zircon,1742.3,4.1,2sigma,LA-ICP-MS,PCIGR,doi:10.1234/foo,granodiorite,55.12345,-105.67890
    BR-2018-02,Ar-Ar,biotite,1738.0,8.0,2sigma,Step-heating,ANU,doi:10.1234/bar,amphibolite,55.13000,-105.68000
""")


def test_well_formed_csv_produces_expected_silver_row():
    """A clean CSV → one record per data row with canonical field values."""
    result = parse_csv_geochronology(StringIO(GOOD_CSV))

    assert result.total_rows == 2
    assert result.valid_rows == 2
    assert result.skipped_rows == 0
    assert result.parse_quality_pct == 100.0

    first = result.records[0]
    assert first["sample_id"] == "BR-2018-01"
    assert first["isotopic_system"] == "U-Pb"
    assert first["isotopic_system"] in VALID_ISOTOPIC_SYSTEMS
    assert first["mineral_dated"] == "zircon"
    assert first["age_ma"] == pytest.approx(1742.3)
    assert first["age_uncertainty_ma"] == pytest.approx(4.1)
    assert first["uncertainty_kind"] == "2sigma"
    assert first["uncertainty_kind"] in VALID_UNCERTAINTY_KINDS
    assert first["analytical_method"] == "LA-ICP-MS"
    assert first["laboratory"] == "PCIGR"
    assert first["publication_ref"] == "doi:10.1234/foo"
    assert first["rock_type"] == "granodiorite"
    assert first["latitude"] == pytest.approx(55.12345)
    assert first["longitude"] == pytest.approx(-105.67890)

    # Sub-type lookup the asset relies on must cover every isotopic system
    # the parser is willing to emit, otherwise the multi-domain tag write
    # would silently drop a sub-type. Guard that contract here.
    for system in VALID_ISOTOPIC_SYSTEMS:
        assert system in ISOTOPIC_SYSTEM_SUB_TYPE_ID, (
            f"sub-type lookup missing entry for {system}"
        )


def test_bad_uncertainty_kind_is_rejected():
    """A row carrying a non-vocabulary uncertainty_kind is dropped, not nulled."""
    bad_csv = textwrap.dedent("""\
        sample_id,isotopic_system,age_ma,age_uncertainty_ma,uncertainty_kind
        BR-2018-03,U-Pb,1742.3,4.1,three_sigma
    """)

    result = parse_csv_geochronology(StringIO(bad_csv))

    assert result.total_rows == 1
    assert result.valid_rows == 0
    assert result.skipped_rows == 1
    assert result.records == []
    assert result.skipped_details[0]["code"] == "invalid_uncertainty_kind"


def test_geom_wkt_built_from_lat_lon():
    """Lat/lon should fold into a WKT POINT(lon lat) the asset can hand to ST_GeomFromText."""
    result = parse_csv_geochronology(StringIO(GOOD_CSV))
    assert result.valid_rows == 2

    first = result.records[0]
    # WKT axis order is (lon lat) — explicitly verify, since flipping it
    # would put every sample in the wrong hemisphere with no DB error.
    assert first["geom_wkt"] == "POINT(-105.6789 55.12345)"

    # A row missing lat/lon produces a NULL geom_wkt without rejection
    # (academic / publication-only records are valid silver rows).
    no_geom_csv = textwrap.dedent("""\
        sample_id,isotopic_system,age_ma
        BR-2018-04,Sm-Nd,2150.0
    """)
    result2 = parse_csv_geochronology(StringIO(no_geom_csv))
    assert result2.valid_rows == 1
    assert result2.records[0]["geom_wkt"] is None
