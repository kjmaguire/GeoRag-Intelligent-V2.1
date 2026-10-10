"""A spatial re-upload never deletes what it cannot replace, and no feature is
stored off the planet (audit finding 4).

WHY THIS FILE EXISTS
    ``run_ingest_spatial`` ran ``_replace_previous_upload`` (delete every
    feature of the earlier upload of the same file) BEFORE it knew whether the
    re-upload had produced anything: a zip whose members all failed to parse,
    or which no longer held one layer of a multi-layer delivery, erased that
    data and wrote nothing in its place. The layer list the delete is scoped
    to, and the refusal when there is nothing to store, are decided first now.

    Separately, ``_INSERT_SQL`` stored coordinates at SRID 4326 whatever they
    were. A misdeclared ``.prj`` yields projected metres (longitude four
    hundred thousand degrees) that break ``silver.coverage_density`` for the
    whole project; ``_write_features`` now leaves such features out and
    reports ``crs_implausible``.

    The live-database half is test_ingest_spatial_replace_integration.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.hatchet_workflows import ingest_spatial as sp


def _feat(wkt: str, name: str = "f", layer: str | None = None) -> SimpleNamespace:
    props = {"_layer_name": layer} if layer else {}
    return SimpleNamespace(
        feature_type="boundary", name=name, properties=props, geometry_wkt=wkt,
    )


def _result(*features: SimpleNamespace, crs: str = "EPSG:4326") -> SimpleNamespace:
    return SimpleNamespace(source_crs=crs, features=list(features), layer_names=[])


GOOD = "POINT (-105.5 52.1)"
UTM = "POINT (400797.89 6117305.85)"           # EPSG:26904 metres under a 4326 label


class TestSplitImplausible:
    def test_projected_metres_are_not_a_place_on_earth(self) -> None:
        kept, dropped = sp._split_implausible([_feat(GOOD, "a"), _feat(UTM, "b")])

        assert [f.name for f in kept] == ["a"]
        assert [f.name for f, _bounds in dropped] == ["b"]
        assert dropped[0][1] == pytest.approx((400797.89, 6117305.85, 400797.89, 6117305.85))

    @pytest.mark.parametrize("wkt", [
        "POINT (180 90)", "POINT (-180 -90)",                  # the limits themselves
        "POINT (180.0000005 0)",                                # rounding residue
        "LINESTRING (179.9 10, 180 11)",
        "POINT Z (10 20 5000)",                                 # Z is not a coordinate
    ])
    def test_limits_and_residue_are_kept(self, wkt: str) -> None:
        kept, dropped = sp._split_implausible([_feat(wkt)])
        assert len(kept) == 1 and dropped == []

    @pytest.mark.parametrize("wkt", [
        "POINT (180.01 0)", "POINT (0 -90.5)", "LINESTRING (0 0, 181 1)",
        "POLYGON ((0 0, 400000 0, 400000 6000000, 0 0))",
    ])
    def test_outside_is_dropped(self, wkt: str) -> None:
        kept, dropped = sp._split_implausible([_feat(wkt)])
        assert kept == [] and len(dropped) == 1

    def test_geometry_without_bounds_is_left_to_postgis(self) -> None:
        kept, dropped = sp._split_implausible(
            [_feat("NOT WKT"), _feat("POLYGON EMPTY"), _feat(None)],
        )
        assert len(kept) == 3 and dropped == []

    def test_no_features(self) -> None:
        assert sp._split_implausible([]) == ([], [])


class _Conn:
    def __init__(self) -> None:
        self.rows: list[tuple[Any, ...]] = []

    async def executemany(self, _sql: str, rows: list) -> None:
        self.rows.extend(rows)


async def _write(result: SimpleNamespace, warnings: list[dict[str, Any]]) -> tuple[int, _Conn]:
    conn = _Conn()
    n = await sp._write_features(
        conn,  # type: ignore[arg-type]
        workspace_id="w", project_id="p", parse_result=result, source_file="claims.zip",
        source_file_sha256=None, source_label="zip", layer_override="claims",
        georef_method="declared", crs_confidence=1.0, warnings_out=warnings,
    )
    return n, conn


class TestWriteFeaturesLeavesOutWhatIsNotOnEarth:
    @pytest.mark.asyncio
    async def test_the_rest_of_the_layer_is_written_and_the_rest_reported(self) -> None:
        warnings: list[dict[str, Any]] = []

        n, conn = await _write(
            _result(_feat(GOOD, "a"), _feat(UTM, "b"), _feat("POINT (-105.6 52.2)", "c")),
            warnings,
        )

        assert n == 2 and [r[3] for r in conn.rows] == ["a", "c"]
        (note,) = warnings
        assert note["code"] == "crs_implausible"
        assert note["context"]["dropped"] == 1 and note["context"]["total"] == 3
        assert "'b'" in note["detail"] and "400797" in note["detail"]
        assert "other 2 feature(s) of the layer were written" in note["detail"]
        assert note["message"]

    @pytest.mark.asyncio
    async def test_a_layer_with_no_placeable_feature_writes_nothing(self) -> None:
        warnings: list[dict[str, Any]] = []

        n, conn = await _write(_result(_feat(UTM, "a"), _feat(UTM, "b")), warnings)

        assert n == 0 and conn.rows == []
        assert "No feature of this layer was written" in warnings[0]["detail"]

    @pytest.mark.asyncio
    async def test_a_clean_layer_has_no_warning(self) -> None:
        warnings: list[dict[str, Any]] = []

        n, _conn = await _write(_result(_feat(GOOD)), warnings)

        assert n == 1 and warnings == []


class TestWhatThisRunWillWrite:
    def test_the_parsers_layer_wins_and_a_lone_file_takes_the_override(self) -> None:
        assert sp._stored_layer(_feat(GOOD, layer="outcrops"), "eagle") == "outcrops"
        assert sp._stored_layer(_feat(GOOD), "eagle") == "eagle"
        assert sp._stored_layer(_feat(GOOD), None) is None

    def test_layers_come_from_the_features_that_can_be_stored(self) -> None:
        parsed = [
            ("eagle", _result(_feat(GOOD, layer="collars"), _feat(GOOD, layer="outcrops"))),
            ("faults", _result(_feat(GOOD))),
            ("bad", _result(_feat(UTM))),                       # unplaceable: not rewritten
        ]
        layers = sp._layers_to_write(
            parsed, filename="d.zip", manifest_only=False, warnings=[],
        )
        assert layers == {"collars", "outcrops", "faults"}

    def test_an_unnamed_lone_layer_is_none(self) -> None:
        layers = sp._layers_to_write(
            [(None, _result(_feat(GOOD)))], filename="a.shp", manifest_only=False, warnings=[],
        )
        assert layers == {None}

    def test_nothing_parsed_raises_and_names_why(self) -> None:
        warnings = [
            {"code": "archive_member_failed", "member": "faults.shp", "detail": "bad shx"},
            {"code": "donated_wkt_unresolved", "detail": "unrelated"},
        ]
        with pytest.raises(ValueError) as exc:
            sp._layers_to_write([], filename="d.zip", manifest_only=False, warnings=warnings)

        message = str(exc.value)
        assert "nothing was written" in message and "left in place" in message
        assert "faults.shp: bad shx" in message and "unrelated" not in message

    def test_a_parsed_layer_with_no_features_raises_too(self) -> None:
        with pytest.raises(ValueError, match="no features that could be stored"):
            sp._layers_to_write(
                [("a", _result())], filename="a.zip", manifest_only=False, warnings=[],
            )

    def test_every_feature_unplaceable_raises_with_the_extent(self) -> None:
        with pytest.raises(ValueError) as exc:
            sp._layers_to_write(
                [("a", _result(_feat(UTM)))], filename="a.zip", manifest_only=False,
                warnings=[],
            )
        assert "outside longitude" in str(exc.value) and "400797" in str(exc.value)
        assert "left in place" in str(exc.value)

    def test_a_qgis_project_with_no_data_is_not_a_refusal(self) -> None:
        assert sp._layers_to_write(
            [], filename="p.qgz", manifest_only=True, warnings=[],
        ) == set()


class _SqlConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql: str, *args: Any) -> int:
        self.calls.append((sql, args))
        return 3


class TestReplaceIsScopedToTheLayersRewritten:
    @pytest.mark.asyncio
    async def test_layers_narrow_the_delete(self) -> None:
        conn = _SqlConn()

        await sp._replace_previous_upload(
            conn,  # type: ignore[arg-type]
            project_id="p", filename="20260929_081500_geology.zip", layers={"faults", "claims", None},
        )

        sql, args = conn.calls[0]
        assert "source_layer = ANY($5::text[])" in sql and "$6::boolean AND source_layer IS NULL" in sql
        assert args[4] == ["claims", "faults"] and args[5] is True       # sorted; None -> unnamed

    @pytest.mark.asyncio
    async def test_a_named_only_set_does_not_touch_unnamed_rows(self) -> None:
        conn = _SqlConn()

        await sp._replace_previous_upload(
            conn, project_id="p", filename="g.zip", layers={"a"},  # type: ignore[arg-type]
        )

        assert conn.calls[0][1][5] is False

    @pytest.mark.asyncio
    async def test_no_layer_list_keeps_the_whole_file_replace(self) -> None:
        conn = _SqlConn()

        await sp._replace_previous_upload(
            conn, project_id="p", filename="g.zip",  # type: ignore[arg-type]
        )

        sql, args = conn.calls[0]
        assert "source_layer" not in sql and len(args) == 4

    @pytest.mark.asyncio
    async def test_an_empty_layer_set_deletes_nothing(self) -> None:
        conn = _SqlConn()

        await sp._replace_previous_upload(
            conn, project_id="p", filename="g.zip", layers=set(),  # type: ignore[arg-type]
        )

        assert conn.calls[0][1][4:] == ([], False)
