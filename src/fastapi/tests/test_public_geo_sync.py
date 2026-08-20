"""Unit tests for the public-geoscience sync layer.

``app/services/public_geo/sync.py`` maps one GeoJSON feature per canonical
type into a column dict and generates the upsert. No network and no database
here — the mappers are pure and the SQL is generated, so both are testable
directly. The feature fixtures below use the REAL field names each layer
publishes, read off its own ``?f=json`` metadata on 2026-08-20.

Run with:
    pytest tests/test_public_geo_sync.py -v
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.services.public_geo import sync as S
from app.services.public_geo.registry import CANONICAL_TYPES, source_by_id

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _feature(props: dict[str, Any], *, oid: Any = 42, geom: Any = None) -> dict[str, Any]:
    return {
        "id": oid,
        "type": "Feature",
        "properties": props,
        "geometry": geom if geom is not None else {
            "type": "Point", "coordinates": [-105.6, 57.2],
        },
    }


class _Aliases(S.AliasTables):
    """AliasTables preloaded from literals instead of the database."""

    def __init__(self) -> None:
        super().__init__(
            grouping_by_alias={"u": "uranium", "uranium": "uranium", "gold": "precious_metals"},
            status_by_source_value={
                ("CA-SK", "mine", "producing mine"): "producing",
                ("CA-SK", "mineral_occurrence", "deposit: production"): "producer",
            },
        )


@pytest.fixture
def aliases() -> _Aliases:
    return _Aliases()


def _src(source_id: str):
    src = source_by_id(source_id)
    assert src is not None, f"{source_id} is missing from the registry"
    return src


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


class TestAsDate:
    """ArcGIS emits dates as epoch MILLISECONDS, but not always."""

    def test_epoch_milliseconds(self) -> None:
        # 2020-01-01T00:00:00Z
        assert S._as_date(1577836800000) == date(2020, 1, 1)

    def test_epoch_milliseconds_as_string(self) -> None:
        assert S._as_date("1577836800000") == date(2020, 1, 1)

    def test_iso_string(self) -> None:
        assert S._as_date("2020-01-01T00:00:00Z") == date(2020, 1, 1)

    def test_date_only_string(self) -> None:
        assert S._as_date("2020-01-01") == date(2020, 1, 1)

    def test_epoch_seconds_is_rejected_not_silently_1970(self) -> None:
        """A seconds value read as ms lands in January 1970. Storing that
        would be a confident, wrong date — worse than no date."""
        assert S._as_date(1577836800) is None  # 2020-01-01 in SECONDS

    def test_bare_year_is_rejected(self) -> None:
        assert S._as_date(2019) is None

    def test_zero_standing_in_for_null_is_rejected(self) -> None:
        assert S._as_date(0) is None

    def test_pre_1973_timestamps_are_the_accepted_cost(self) -> None:
        """Documents the tradeoff rather than leaving it to be rediscovered:
        below ~1e11 ms a genuine 1971 date and a seconds-encoded modern one
        are indistinguishable, so the whole window is dropped."""
        assert S._as_date(31536000000) is None      # 1971-01-01 in ms — lost
        assert S._as_date(126230400000) == date(1974, 1, 1)  # just above, kept

    def test_garbage_returns_none(self) -> None:
        assert S._as_date("not a date") is None
        assert S._as_date("") is None
        assert S._as_date(None) is None


class TestDec:
    """Numeric columns need Decimal — asyncpg rejects float for numeric."""

    def test_returns_decimal(self) -> None:
        assert S._dec("123.45") == Decimal("123.45")
        assert isinstance(S._dec(1), Decimal)

    def test_blank_and_garbage_return_none(self) -> None:
        assert S._dec("") is None
        assert S._dec(None) is None
        assert S._dec("N/A") is None


class TestInt:
    def test_range_gate_drops_out_of_range(self) -> None:
        """potential_rank has a 1..6 CHECK; a legend key of 12 is not a rank."""
        assert S._int(3, lo=1, hi=6) == 3
        assert S._int(12, lo=1, hi=6) is None
        assert S._int(0, lo=1, hi=6) is None

    def test_float_string_truncates(self) -> None:
        assert S._int("3.9", lo=1, hi=6) == 3


class TestSplitList:
    def test_handles_all_three_separators(self) -> None:
        assert S._split_list("Au, Ag; Cu | Pb") == ["Au", "Ag", "Cu", "Pb"]

    def test_strips_and_drops_blanks(self) -> None:
        assert S._split_list(" Au , , Ag ") == ["Au", "Ag"]

    def test_empty(self) -> None:
        assert S._split_list(None) == []
        assert S._split_list("") == []


class TestFit:
    """Column widths are read from the catalogue; _fit applies them."""

    def test_truncates_to_limit(self) -> None:
        assert S._fit("abcdefghij", 4) == "abcd"

    def test_leaves_short_values_alone(self) -> None:
        assert S._fit("abc", 128) == "abc"

    def test_none_limit_is_a_no_op(self) -> None:
        assert S._fit("abc", None) == "abc"

    def test_non_strings_pass_through(self) -> None:
        assert S._fit(None, 4) is None
        assert S._fit([1, 2], 4) == [1, 2]


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------


class TestAliasTables:
    def test_unmapped_status_is_reported_not_swallowed(self, aliases: _Aliases) -> None:
        """Defaulting 140 of 140 mines to 'unknown' while reporting a clean
        sync is the failure mode this second return value exists to prevent."""
        status, unmapped = aliases.status(
            "Some Brand New Label", jurisdiction="CA-SK", canonical_type="mine",
        )
        assert status == "unknown"
        assert unmapped == "Some Brand New Label"

    def test_mapped_status_reports_no_gap(self, aliases: _Aliases) -> None:
        status, unmapped = aliases.status(
            "Producing Mine", jurisdiction="CA-SK", canonical_type="mine",
        )
        assert status == "producing"
        assert unmapped is None

    def test_lookup_is_case_and_whitespace_insensitive(self, aliases: _Aliases) -> None:
        status, _ = aliases.status(
            "  producing MINE  ", jurisdiction="CA-SK", canonical_type="mine",
        )
        assert status == "producing"

    def test_scope_is_respected(self, aliases: _Aliases) -> None:
        """A mineral_occurrence mapping must not leak onto a mine."""
        status, unmapped = aliases.status(
            "Deposit: Production", jurisdiction="CA-SK", canonical_type="mine",
        )
        assert status == "unknown"
        assert unmapped == "Deposit: Production"

    def test_unmapped_grouping_is_null_not_invented(self, aliases: _Aliases) -> None:
        """commodity_grouping is nullable; 'other' would be a guess."""
        assert aliases.grouping("Unobtainium") is None

    def test_grouping_of_any_scans_in_order(self, aliases: _Aliases) -> None:
        assert aliases.grouping_of_any(["Unobtainium", "Gold"]) == "precious_metals"
        assert aliases.grouping_of_any(["Unobtainium"]) is None


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


class TestMappers:
    def test_every_canonical_type_has_a_mapper_and_spec(self) -> None:
        for t in CANONICAL_TYPES:
            assert t in S.MAPPERS, f"{t} has no mapper — its table would never fill"
            assert t in S.SPECS, f"{t} has no TableSpec"

    def test_mapper_output_covers_exactly_the_spec_columns(self, aliases: _Aliases) -> None:
        """A mapper that omits a declared column raises KeyError at bind time,
        on the live sync, per feature — not here, where it is cheap to see."""
        cases = {
            "mine": ("CA-SK-MINE-LOC", {"NAME": "X", "STATUS": "Producing Mine"}),
            "mineral_occurrence": ("CA-SK-SMDI", {"NAME": "X", "SMDI": "1"}),
            "drillhole_collar": ("CA-SK-DRILLHOLE", {"DRILLHOLE_NAME": "X"}),
            "resource_potential_zone": ("CA-SK-RESOURCE-POTENTIAL-URANIUM", {}),
            "rock_sample": ("CA-SK-ROCK-SAMPLES", {"STATION": "S1"}),
            "assessment_survey": ("CA-SK-ASSESSMENT-AIRBORNE", {"FILENUMBER": "F1"}),
            "mineral_disposition": ("CA-SK-MINERAL-DISPOSITION-MINING-5", {}),
        }
        for canonical_type, (source_id, props) in cases.items():
            row = S.MAPPERS[canonical_type](_src(source_id), _feature(props), aliases)
            assert row is not None
            row.pop("_status_raw", None)
            expected = set(S.SPECS[canonical_type].columns) | set(S._COMMON_COLUMNS)
            # status is filled by sync_source from the _status_raw sentinel.
            missing = expected - set(row) - {"status"}
            assert not missing, f"{canonical_type} mapper omits {missing}"

    def test_feature_without_object_id_is_skipped(self, aliases: _Aliases) -> None:
        feature = {"type": "Feature", "properties": {"NAME": "X"}, "geometry": None}
        assert S._map_mine(_src("CA-SK-MINE-LOC"), feature, aliases) is None

    def test_source_crs_is_4326_not_the_native_projection(self, aliases: _Aliases) -> None:
        """We request outSR=4326. Writing SK's native 2957 beside a 4326
        geometry claims the coordinates are in a projection they are not in."""
        src = _src("CA-SK-MINE-LOC")
        assert src.source_crs != 4326, "fixture assumes a non-4326 native CRS"
        row = S._map_mine(src, _feature({"NAME": "X"}), aliases)
        assert row["source_crs"] == 4326

    def test_raw_attributes_are_preserved_verbatim(self, aliases: _Aliases) -> None:
        """A mapping gap must lose nothing — later mappers backfill from here."""
        props = {"NAME": "X", "SOME_UNMAPPED_FIELD": "keep me"}
        row = S._map_mine(_src("CA-SK-MINE-LOC"), _feature(props), aliases)
        assert "keep me" in row["source_attributes"]

    def test_checksum_is_stable_under_key_reordering(self, aliases: _Aliases) -> None:
        """Otherwise ArcGIS field ordering churns updated_at every run."""
        assert S._checksum({"a": 1, "b": 2}) == S._checksum({"b": 2, "a": 1})

    def test_checksum_changes_when_a_value_changes(self) -> None:
        assert S._checksum({"a": 1}) != S._checksum({"a": 2})

    def test_minfile_numbered_commodities_split_primary_from_associated(
        self, aliases: _Aliases,
    ) -> None:
        """CA-BC-MINFILE has no delimited commodity column — it spreads eight
        numbered ones, and the first is the primary."""
        row = S._map_mineral_occurrence(
            _src("CA-BC-MINFILE"),
            _feature({
                "MINFILE_NAME1": "SLIDE",
                "COMMODITY_DESCRIPTION1": "Uranium",
                "COMMODITY_DESCRIPTION2": "Niobium",
                "COMMODITY_DESCRIPTION3": "Thorium",
            }),
            aliases,
        )
        assert row["primary_commodities"] == ["Uranium"]
        assert row["associated_commodities"] == ["Niobium", "Thorium"]

    def test_smdi_delimited_commodities(self, aliases: _Aliases) -> None:
        row = S._map_mineral_occurrence(
            _src("CA-SK-SMDI"),
            _feature({
                "NAME": "Key Lake",
                "PRIMARYCOMMODITIES": "U",
                "ASSOCIATEDCOMMODITIES": "Arsenic,Cu",
                "GROUPING": "Uranium",
            }),
            aliases,
        )
        assert row["primary_commodities"] == ["U"]
        assert row["associated_commodities"] == ["Arsenic", "Cu"]
        assert row["commodity_grouping"] == "uranium"

    def test_drillhole_lifts_stratigraphic_contacts_to_stable_keys(
        self, aliases: _Aliases,
    ) -> None:
        row = S._map_drillhole_collar(
            _src("CA-SK-DRILLHOLE"),
            _feature({
                "DRILLHOLE_NAME": "H1",
                "BASE_OF_QUATERNARY_DEPTH_M": "12.5",
                "BASE_OF_QUATERNARY_ELEV_M": "480.0",
            }),
            aliases,
        )
        assert '"base_of_quaternary"' in row["stratigraphic_depths"]
        assert "12.5" in row["stratigraphic_depths"]

    def test_drillhole_prefers_the_dem_corrected_elevation(
        self, aliases: _Aliases,
    ) -> None:
        row = S._map_drillhole_collar(
            _src("CA-SK-DRILLHOLE"),
            _feature({
                "DRILLHOLE_NAME": "H1",
                "ORIGINAL_COLLAR_ELEVATION_M": "500",
                "ELEV_CORRECTED_1ARCSEC_DEM_M": "497.3",
            }),
            aliases,
        )
        assert row["collar_elevation_m"] == Decimal("497.3")

    def test_potential_zone_commodity_falls_back_to_the_layer_identity(
        self, aliases: _Aliases,
    ) -> None:
        """Six of eleven Resource_Map layers publish no COMMODITY column, but
        the column is NOT NULL — and the commodity is what the layer IS."""
        row = S._map_resource_potential_zone(
            _src("CA-SK-RESOURCE-POTENTIAL-URANIUM"), _feature({}), aliases,
        )
        assert row["commodity"] == "uranium"
        assert row["commodity_grouping"] == "uranium"

    def test_potential_zone_prefers_the_published_commodity(
        self, aliases: _Aliases,
    ) -> None:
        row = S._map_resource_potential_zone(
            _src("CA-SK-RESOURCE-POTENTIAL-GOLD"), _feature({"COMMODITY": "Gold"}), aliases,
        )
        assert row["commodity"] == "Gold"

    def test_survey_type_comes_from_the_layer_not_the_feature(
        self, aliases: _Aliases,
    ) -> None:
        for source_id, expected in [
            ("CA-SK-ASSESSMENT-AIRBORNE", "airborne"),
            ("CA-SK-ASSESSMENT-GROUND", "ground"),
            ("CA-SK-ASSESSMENT-UNDERGROUND", "underground"),
        ]:
            row = S._map_assessment_survey(_src(source_id), _feature({}), aliases)
            assert row["survey_type"] == expected

    def test_disposition_type_and_status_come_from_the_layer(
        self, aliases: _Aliases,
    ) -> None:
        """Every feature in Mining/4 is a lapsed mineral disposition whatever
        its attributes say — the survey encodes it by publishing each
        combination as its own layer."""
        row = S._map_mineral_disposition(
            _src("CA-SK-MINERAL-DISPOSITION-MINING-4"), _feature({}), aliases,
        )
        assert (row["disposition_type"], row["status"]) == ("mineral", "lapsed")

        row = S._map_mineral_disposition(
            _src("CA-SK-MINERAL-DISPOSITION-CROWN-OIL-GAS"), _feature({}), aliases,
        )
        assert (row["disposition_type"], row["status"]) == ("oil_gas", "active")

    @pytest.mark.parametrize(
        ("props", "expected_ha"),
        [
            ({"HECTARES": "100"}, Decimal("100.00")),
            ({"PARCELHECT": "64.75"}, Decimal("64.75")),
            ({"ACRES": "100"}, Decimal("40.47")),      # 100 ac = 40.4686 ha
            ({"AREA_M2": "1000000"}, Decimal("100.00")),  # 1e6 m2 = 100 ha
            ({}, None),
        ],
    )
    def test_disposition_area_is_normalised_to_hectares(
        self, aliases: _Aliases, props: dict[str, Any], expected_ha: Any,
    ) -> None:
        """Three different units across the ten layers; the column is hectares."""
        row = S._map_mineral_disposition(
            _src("CA-SK-MINERAL-DISPOSITION-MINING-5"), _feature(props), aliases,
        )
        assert row["area_ha"] == expected_ha

    def test_legacy_and_modern_disposition_field_names_both_resolve(
        self, aliases: _Aliases,
    ) -> None:
        """Mining/0 uses ten-character legacy names, Mining/5-8 modern ones."""
        legacy = S._map_mineral_disposition(
            _src("CA-SK-MINERAL-DISPOSITION-MINING-0"),
            _feature({"DISPOSITIO": "ML-1234", "OWNERS": "Acme Ltd"}),
            aliases,
        )
        modern = S._map_mineral_disposition(
            _src("CA-SK-MINERAL-DISPOSITION-MINING-5"),
            _feature({"DISPOSITION": "KP-9", "HOLDER": "Acme Ltd"}),
            aliases,
        )
        assert legacy["disposition_number"] == "ML-1234"
        assert legacy["holder_name"] == "Acme Ltd"
        assert modern["disposition_number"] == "KP-9"
        assert modern["holder_name"] == "Acme Ltd"


# ---------------------------------------------------------------------------
# Geometry + generated SQL
# ---------------------------------------------------------------------------


class TestGeometry:
    def test_point_geometry_serialises(self) -> None:
        out = S._geom_geojson(_feature({}))
        assert out is not None and '"Point"' in out

    def test_missing_geometry_is_none_not_an_error(self) -> None:
        assert S._geom_geojson({"properties": {}, "geometry": None}) is None
        assert S._geom_geojson({"properties": {}}) is None


class TestBuildUpsert:
    def test_bind_order_matches_the_placeholder_count(self) -> None:
        for spec in S.SPECS.values():
            sql, bind_order = S.build_upsert(spec)
            assert f"${len(bind_order)}" in sql
            # One more parameter than columns: the trailing geometry.
            assert f"${len(bind_order) + 1}" in sql

    def test_natural_key_is_never_overwritten(self) -> None:
        for spec in S.SPECS.values():
            sql, _ = S.build_upsert(spec)
            update_clause = sql.split("DO UPDATE SET", 1)[1]
            assert "source_id = EXCLUDED.source_id" not in update_clause
            assert "source_feature_id = EXCLUDED.source_feature_id" not in update_clause

    def test_updated_at_only_churns_on_a_checksum_change(self) -> None:
        sql, _ = S.build_upsert(S.SPECS["mine"])
        assert "checksum IS DISTINCT FROM EXCLUDED.checksum" in sql

    def test_polygon_types_are_made_valid_before_insert(self) -> None:
        """ArcGIS emits ring sets that GeoJSON calls one Polygon but that are
        geometrically several. Storing those invalid breaks ST_Intersects for
        every map query that touches them."""
        sql, _ = S.build_upsert(S.SPECS["mineral_disposition"])
        assert "ST_MakeValid" in sql
        assert "ST_CollectionExtract" in sql
        assert "ST_Multi" in sql

    def test_point_types_skip_the_polygon_repair(self) -> None:
        sql, _ = S.build_upsert(S.SPECS["mine"])
        assert "ST_MakeValid" not in sql

    def test_wkt_is_materialised_for_points_only(self) -> None:
        """On polygon feeds the text copy roughly doubles the table for no
        gain — geom is authoritative either way."""
        point_sql, _ = S.build_upsert(S.SPECS["mine"])
        poly_sql, _ = S.build_upsert(S.SPECS["assessment_survey"])
        assert "ST_AsText" in point_sql
        assert "ST_AsText" not in poly_sql

    def test_geometry_parameter_is_cast_at_every_use_site(self) -> None:
        """Without the ::text cast asyncpg cannot infer a type for a parameter
        that only appears inside a function call, and every row fails."""
        for spec in S.SPECS.values():
            sql, bind_order = S.build_upsert(spec)
            geom_param = f"${len(bind_order) + 1}"
            assert f"{geom_param}::text" in sql
            assert sql.count(f"{geom_param}::text") >= 2
