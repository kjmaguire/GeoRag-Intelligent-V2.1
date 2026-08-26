"""The silver → gold promotion, and proof it is actually wired in.

Half of this file is arithmetic on pure functions. The other half exists
because arithmetic on pure functions is exactly what did NOT catch the bug
this module fixes.

`silver_drill_traces` and `gold_structure_measurements_visual` were Dagster
assets with their own passing test suites right up until Dagster was retired
on 2026-07-28. The maths kept working; nothing called it. From that day
`gold.drillhole_intervals_visual`, `gold.structure_measurements_visual` and
`silver.drill_traces` had no writer, and the Workspace's SECTION / 3D /
STRUCTURE / LOGS / COMPARE modes were blank for every project regardless of
what was ingested — measured empty on the live database 2026-08-25 beside 5
collars and 10 surveys that had ingested cleanly.

So `TestItIsActuallyReachable` below asserts the WIRING structurally: the
workflow is registered on the worker, ingest_tabular dispatches it, the
nightly sweep dispatches it, and no live writer for these tables lives under
the retired `src/dagster` tree. Those are the assertions that would have gone
red on 2026-07-28.
"""
from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
WORKFLOWS = APP / "hatchet_workflows"


# ---------------------------------------------------------------------------
# Reachability — the half that matters
# ---------------------------------------------------------------------------
class TestItIsActuallyReachable:
    def test_registered_on_the_worker(self):
        """A workflow the worker does not register never runs."""
        source = (WORKFLOWS / "worker.py").read_text(encoding="utf-8")
        assert "from app.hatchet_workflows.promote_silver_to_gold import" in source
        # Imported AND listed. The import alone registers nothing.
        tree = ast.parse(source)
        names = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        assert "promote_silver_to_gold" in names

    def test_ingest_tabular_dispatches_it(self):
        """A drill upload must promote itself, not wait for the nightly."""
        source = (WORKFLOWS / "ingest_tabular.py").read_text(encoding="utf-8")
        assert "promote_silver_to_gold" in source
        assert "aio_run_no_wait" in source

    def test_the_nightly_sweep_dispatches_it(self):
        """Belt and braces: an ingest that predates this still gets promoted."""
        source = (WORKFLOWS / "nightly_ingestion_integrity.py").read_text(
            encoding="utf-8",
        )
        assert "promote_silver_to_gold" in source

    def test_the_dispatch_cannot_fail_the_ingest(self):
        """Silver is already written by the time we promote.

        A promotion that throws must not relabel a good ingest as failed, so
        the dispatch is wrapped. Asserted structurally rather than by reading
        the comment beside it.
        """
        source = (WORKFLOWS / "ingest_tabular.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        guarded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "promote_silver_to_gold" in body and node.handlers:
                guarded = True
        assert guarded, "the promote dispatch must sit inside a try/except"

    def test_no_live_writer_lives_under_retired_dagster(self):
        """The regression guard.

        `src/dagster` is retired (2026-07-28) and nothing builds it, so a
        table whose ONLY writer is there has no writer at all. This asserts
        the three tables that lost theirs now have one under src/fastapi.
        """
        promote = (WORKFLOWS / "promote_silver_to_gold.py").read_text(
            encoding="utf-8",
        )
        for table in (
            "silver.drill_traces",
            "gold.drillhole_intervals_visual",
            "gold.structure_measurements_visual",
        ):
            assert f"INSERT INTO {table}" in promote, (
                f"{table} has no writer outside the retired Dagster tree"
            )


# ---------------------------------------------------------------------------
# Survey hygiene
# ---------------------------------------------------------------------------
class TestCleanStations:
    def _rows(self, triples):
        return [
            {"depth": d, "azimuth": a, "dip": p} for d, a, p in triples
        ]

    def test_sorts_by_depth(self):
        from app.hatchet_workflows.promote_silver_to_gold import _clean_stations

        out = _clean_stations(self._rows([(30, 90, -60), (0, 90, -60), (15, 90, -60)]))
        assert [d for d, _, _ in out] == [0.0, 15.0, 30.0]

    def test_drops_null_angles(self):
        from app.hatchet_workflows.promote_silver_to_gold import _clean_stations

        out = _clean_stations(self._rows([(0, None, -60), (10, 90, None), (20, 90, -60)]))
        assert [d for d, _, _ in out] == [20.0]

    @pytest.mark.parametrize("bad_dip", [5.0, 0.5, -90.5, -180.0])
    def test_drops_impossible_dips(self, bad_dip):
        """Up-going or past-vertical is a data error, not a steep hole."""
        from app.hatchet_workflows.promote_silver_to_gold import _clean_stations

        assert _clean_stations(self._rows([(0, 90, bad_dip)])) == []

    def test_horizontal_and_vertical_are_both_legal(self):
        from app.hatchet_workflows.promote_silver_to_gold import _clean_stations

        out = _clean_stations(self._rows([(0, 90, 0.0), (10, 90, -90.0)]))
        assert len(out) == 2

    def test_duplicate_depth_keeps_the_last_row(self):
        """Matches the retired asset's 'keep latest updated_at' rule."""
        from app.hatchet_workflows.promote_silver_to_gold import _clean_stations

        out = _clean_stations(self._rows([(10, 90, -60), (10, 270, -45)]))
        assert out == [(10.0, 270.0, -45.0)]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
class TestSurveyHash:
    def test_row_order_does_not_change_the_hash(self):
        """A re-run must not rewrite a trace just because ORDER BY tied."""
        from app.hatchet_workflows.promote_silver_to_gold import _survey_hash

        a = _survey_hash([(0.0, 90.0, -60.0), (30.0, 92.0, -59.0)])
        b = _survey_hash([(30.0, 92.0, -59.0), (0.0, 90.0, -60.0)])
        assert a == b

    def test_a_changed_angle_changes_the_hash(self):
        from app.hatchet_workflows.promote_silver_to_gold import _survey_hash

        a = _survey_hash([(0.0, 90.0, -60.0)])
        b = _survey_hash([(0.0, 90.1, -60.0)])
        assert a != b

    def test_it_is_hex_sha256(self):
        from app.hatchet_workflows.promote_silver_to_gold import _survey_hash

        digest = _survey_hash([(0.0, 90.0, -60.0)])
        assert len(digest) == 64
        int(digest, 16)  # raises if not hex


# ---------------------------------------------------------------------------
# Dogleg severity
# ---------------------------------------------------------------------------
class TestDogleg:
    def test_a_straight_hole_has_no_dogleg(self):
        from app.hatchet_workflows.promote_silver_to_gold import _dogleg_deg_per_30m

        assert _dogleg_deg_per_30m((0, 90, -60), (30, 90, -60)) == pytest.approx(0.0, abs=1e-9)

    def test_a_straight_vertical_hole_does_not_raise(self):
        """acos() domain guard.

        cos(beta) computes to 1.0000000000000002 on a perfectly straight
        hole and math.acos raises ValueError on anything above 1. Without
        the clamp this is the commonest hole in any dataset.
        """
        from app.hatchet_workflows.promote_silver_to_gold import _dogleg_deg_per_30m

        assert _dogleg_deg_per_30m((0, 0, -90), (100, 0, -90)) == pytest.approx(0.0, abs=1e-9)

    def test_a_ten_degree_bend_over_thirty_metres_is_ten(self):
        from app.hatchet_workflows.promote_silver_to_gold import _dogleg_deg_per_30m

        got = _dogleg_deg_per_30m((0, 0, -80), (30, 0, -70))
        assert got == pytest.approx(10.0, abs=0.01)

    def test_severity_is_per_thirty_metres_not_per_interval(self):
        """The same bend over half the distance is twice the severity."""
        from app.hatchet_workflows.promote_silver_to_gold import _dogleg_deg_per_30m

        got = _dogleg_deg_per_30m((0, 0, -80), (15, 0, -70))
        assert got == pytest.approx(20.0, abs=0.02)

    def test_zero_length_interval_is_not_infinite(self):
        from app.hatchet_workflows.promote_silver_to_gold import _dogleg_deg_per_30m

        assert _dogleg_deg_per_30m((10, 0, -90), (10, 180, -45)) == 0.0

    def test_an_azimuth_swing_registers(self):
        """A 90° azimuth change on an inclined hole is a real dogleg."""
        from app.hatchet_workflows.promote_silver_to_gold import _dogleg_deg_per_30m

        got = _dogleg_deg_per_30m((0, 0, -45), (30, 90, -45))
        assert got > 40.0

    def test_an_azimuth_swing_on_a_vertical_hole_is_not_a_dogleg(self):
        """A vertical hole has no bearing to change.

        sin(inclination) is 0 at vertical, so the azimuth term drops out —
        which is correct, and is why the formula uses inclination rather
        than dip directly.
        """
        from app.hatchet_workflows.promote_silver_to_gold import _dogleg_deg_per_30m

        got = _dogleg_deg_per_30m((0, 0, -90), (30, 180, -90))
        assert got == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# The unsurveyed hole
# ---------------------------------------------------------------------------
class TestStraightLineFallback:
    def test_a_complete_collar_orientation_gives_two_stations(self):
        from app.hatchet_workflows.promote_silver_to_gold import _straight_line_stations

        out = _straight_line_stations(45.0, -60.0, 250.0)
        assert out == [(0.0, 45.0, -60.0), (250.0, 45.0, -60.0)]

    @pytest.mark.parametrize(
        "az,dip,td",
        [(None, -60.0, 250.0), (45.0, None, 250.0), (45.0, -60.0, None)],
    )
    def test_a_partial_orientation_gives_nothing(self, az, dip, td):
        """Not a vertical hole at an unknown depth — nothing.

        Inventing geometry here would put a hole on the map that nobody
        drilled, which is worse than a hole that is missing from the 3D view
        and counted as skipped.
        """
        from app.hatchet_workflows.promote_silver_to_gold import _straight_line_stations

        assert _straight_line_stations(az, dip, td) is None

    def test_a_zero_depth_hole_gives_nothing(self):
        from app.hatchet_workflows.promote_silver_to_gold import _straight_line_stations

        assert _straight_line_stations(45.0, -60.0, 0.0) is None


# ---------------------------------------------------------------------------
# Command-tag parsing
# ---------------------------------------------------------------------------
class TestAffected:
    @pytest.mark.parametrize(
        "tag,expected",
        [("INSERT 0 42", 42), ("INSERT 0 0", 0), ("UPDATE 7", 7), ("DELETE 3", 3)],
    )
    def test_it_reads_the_row_count(self, tag, expected):
        from app.hatchet_workflows.promote_silver_to_gold import _affected

        assert _affected(tag) == expected

    @pytest.mark.parametrize("tag", ["", "SELECT", "nonsense here"])
    def test_an_unparseable_tag_is_zero_not_an_exception(self, tag):
        """A miscounted statistic must not fail a promotion that wrote rows."""
        from app.hatchet_workflows.promote_silver_to_gold import _affected

        assert _affected(tag) == 0


# ---------------------------------------------------------------------------
# The SQL itself
# ---------------------------------------------------------------------------
class TestPromotionSql:
    def test_intervals_take_tenancy_from_the_collar(self):
        """Not from the interval row.

        silver.lithology_logs and silver.samples carry their own
        workspace_id and NO project_id, so the project scope has to come
        through the collar join anyway. Taking workspace_id from the same
        place means a log row stamped with the wrong workspace cannot write
        a band into another tenant's project.
        """
        from app.hatchet_workflows import promote_silver_to_gold as m

        for sql in (m._INTERVALS_LITHOLOGY, m._INTERVALS_SAMPLES):
            assert "c.workspace_id, c.project_id" in sql
            assert "l.workspace_id" not in sql
            assert "s.workspace_id" not in sql

    def test_intervals_respect_the_depth_check(self):
        """gold.drillhole_intervals_visual CHECKs depth_to > depth_from >= 0.

        A NULL or inverted interval in silver must be filtered out, not sent
        to the database to raise and abort the whole promotion.
        """
        from app.hatchet_workflows import promote_silver_to_gold as m

        for sql in (m._INTERVALS_LITHOLOGY, m._INTERVALS_SAMPLES):
            assert "IS NOT NULL" in sql
            assert "> " in sql and ">= 0" in sql

    def test_every_kind_is_one_the_check_constraint_allows(self):
        from app.hatchet_workflows import promote_silver_to_gold as m

        allowed = {
            "lithology", "alteration", "structure",
            "assay_high_grade", "sample_window",
        }
        assert "'lithology'" in m._INTERVALS_LITHOLOGY
        assert "'sample_window'" in m._INTERVALS_SAMPLES
        for kind in ("lithology", "sample_window"):
            assert kind in allowed

    def test_traces_are_translated_onto_the_collar_then_transformed(self):
        """Offsets about (0,0), translated onto the collar, then to 4326.

        v1 wrote ST_Transform(ST_SetSRID(<absolute easting/northing>,
        ST_SRID(c.geom)), 4326) — and `silver.collars.geom` is declared
        geometry(POINT, 32613), so ST_SRID returns 32613 for a hole anywhere
        on earth. Alaskan zone-4N eastings were read as zone-13N.
        """
        from app.hatchet_workflows import promote_silver_to_gold as m

        assert "ST_Translate(" in m._TRACE_UPSERT
        assert "), 4326)" in m._TRACE_UPSERT or "4326\n    )" in m._TRACE_UPSERT
        assert "ST_SRID(c.geom)" not in m._TRACE_UPSERT, (
            "the collar geom column is SRID-pinned; it cannot report the CRS "
            "the easting/northing were surveyed in"
        )

    def test_the_collar_position_comes_from_geom_4326(self):
        """Not from easting/northing, whose projection is not recorded.

        Asserted against the module source because the query is inline. A
        promotion that reads `c.easting` again is the v1 bug returning.
        """
        import inspect

        from app.hatchet_workflows import promote_silver_to_gold as m

        src = inspect.getsource(m._promote_traces)
        assert "ST_X(c.geom_4326)" in src and "ST_Y(c.geom_4326)" in src
        assert "c.easting" not in src, (
            "easting/northing carry no CRS; geom_4326 is the only column on "
            "silver.collars that is correct wherever the hole was surveyed"
        )

    def test_the_interpolator_is_given_a_zero_origin(self):
        """So it returns OFFSETS, which is what the SQL translates.

        Passing the real easting/northing here would put absolute
        zone-unknown coordinates into a linestring that then gets translated
        onto the collar as well — the collar counted twice.
        """
        import inspect

        from app.hatchet_workflows import promote_silver_to_gold as m

        src = inspect.getsource(m._promote_traces)
        assert "collar_easting=0.0" in src
        assert "collar_northing=0.0" in src


class TestCollarLocalUtm:
    """Picking the zone the collar actually sits in.

    The whole point is that no column records the CRS the easting/northing
    were surveyed in, so the zone is derived from the collar's true position
    instead. These are the two corpora that matter plus the edges.
    """

    def test_unga_island_alaska_is_zone_4_north(self):
        from app.hatchet_workflows.promote_silver_to_gold import _collar_local_utm

        # The RedStar delivery: lon -160.558, lat 55.192.
        assert _collar_local_utm(-160.558, 55.192) == 32604

    def test_athabasca_is_zone_13_north(self):
        from app.hatchet_workflows.promote_silver_to_gold import _collar_local_utm

        # Every corpus before RedStar. v1 was accidentally right here, which
        # is exactly why the bug survived to production.
        assert _collar_local_utm(-106.5, 57.0) == 32613

    @pytest.mark.parametrize(
        "lon,lat,expected",
        [
            (-179.9, 10.0, 32601),   # first zone
            (179.9, 10.0, 32660),    # last zone
            (180.0, 10.0, 32660),    # clamped, not wrapped to 61
            (-180.0, 10.0, 32601),
            (0.5, 51.5, 32631),      # Greenwich
            (-70.0, -33.0, 32719),   # southern hemisphere -> 327xx
            (-70.0, 0.0, 32619),     # the equator counts as north
        ],
    )
    def test_zone_and_hemisphere(self, lon, lat, expected):
        from app.hatchet_workflows.promote_silver_to_gold import _collar_local_utm

        assert _collar_local_utm(lon, lat) == expected

    def test_no_zone_is_ever_out_of_range(self):
        from app.hatchet_workflows.promote_silver_to_gold import _collar_local_utm

        for lon in range(-180, 181):
            for lat in (-80.0, 0.0, 80.0):
                srid = _collar_local_utm(float(lon), lat)
                base = 32600 if lat >= 0 else 32700
                assert 1 <= srid - base <= 60, f"lon={lon} lat={lat} -> {srid}"


class TestBuilderVersionInvalidatesCachedTraces:
    """A corrected builder must not be skipped as "unchanged".

    `survey_hash` is the skip key. When v1's projection bug was fixed the
    surveys behind every trace were unchanged, so a re-run recognised the
    stored hashes as current and skipped all of them — the fix would have
    shipped and changed nothing until someone re-surveyed a hole.
    """

    def test_the_version_is_part_of_the_digest(self):
        from app.hatchet_workflows import promote_silver_to_gold as m

        stations = [(0.0, 90.0, -60.0), (30.0, 92.0, -59.0)]
        before = m._survey_hash(stations)

        original = m._TRACE_BUILDER_VERSION
        try:
            m._TRACE_BUILDER_VERSION = original + 1
            after = m._survey_hash(stations)
        finally:
            m._TRACE_BUILDER_VERSION = original

        assert before != after, (
            "bumping the builder version must invalidate every stored trace"
        )

    def test_it_is_still_stable_for_a_fixed_version(self):
        from app.hatchet_workflows.promote_silver_to_gold import _survey_hash

        a = _survey_hash([(0.0, 90.0, -60.0), (30.0, 92.0, -59.0)])
        b = _survey_hash([(30.0, 92.0, -59.0), (0.0, 90.0, -60.0)])
        assert a == b

    def test_the_version_is_past_one(self):
        """v1 is the projection bug; anything still on it is unfixed."""
        from app.hatchet_workflows.promote_silver_to_gold import _TRACE_BUILDER_VERSION

        assert _TRACE_BUILDER_VERSION >= 2

    def test_traces_upsert_on_the_unique_index(self):
        """silver.drill_traces has UNIQUE (collar_id).

        A plain INSERT would raise on the second nightly run for every hole
        in the database.
        """
        from app.hatchet_workflows import promote_silver_to_gold as m

        assert "ON CONFLICT (collar_id) DO UPDATE" in m._TRACE_UPSERT

    def test_intervals_upsert_on_the_unique_index(self):
        from app.hatchet_workflows import promote_silver_to_gold as m

        for sql in (m._INTERVALS_LITHOLOGY, m._INTERVALS_SAMPLES):
            assert "ON CONFLICT (collar_id, depth_from, depth_to, interval_kind)" in sql

    def test_the_stereonet_projection_is_equal_area(self):
        """Schmidt, not Wulff — and labelled as such in the row.

        A pole plotted with the wrong projection still looks like structural
        data, so the row records which one produced x/y.
        """
        from app.hatchet_workflows import promote_silver_to_gold as m

        assert "'equal_area'" in m._STRUCTURES_VISUAL
        assert "SQRT(2)" in m._STRUCTURES_VISUAL


# ---------------------------------------------------------------------------
# End-to-end maths against the shared interpolator
# ---------------------------------------------------------------------------
class TestAgainstTheInterpolator:
    def test_a_vertical_hole_goes_straight_down(self):
        """Sanity that the module we depend on is the one we think it is."""
        interp = pytest.importorskip("georag_geoparsers._survey_interp")

        out = interp.minimum_curvature(
            collar_easting=500000.0,
            collar_northing=6000000.0,
            collar_elevation=100.0,
            stations=[
                interp.SurveyStation(depth_m=0.0, azimuth_deg=0.0, dip_deg=-90.0),
                interp.SurveyStation(depth_m=100.0, azimuth_deg=0.0, dip_deg=-90.0),
            ],
        )
        _, end = out[-1]
        assert end.east_m == pytest.approx(500000.0, abs=1e-6)
        assert end.north_m == pytest.approx(6000000.0, abs=1e-6)
        assert end.elev_m == pytest.approx(-100.0, abs=1e-6)

    def test_the_returned_tuple_mixes_two_frames(self):
        """The reason `_promote_traces` adds the collar elevation back.

        `minimum_curvature` takes a `collar_elevation` argument and does NOT
        apply it: east and north come back ABSOLUTE, elevation comes back
        RELATIVE to the collar (XYZ.elev_m is documented as "0 at collar,
        negative downhole"). Writing the tuple straight into a LINESTRINGZ —
        which is what the retired Dagster asset did — starts every trace at
        Z = 0 and flattens a camp with 400 m of relief onto one datum.

        Pinned here rather than left as a comment because the day this
        function starts honouring its own argument, `_promote_traces` will
        double-count the collar elevation and every hole will float. This
        test is what goes red first.
        """
        interp = pytest.importorskip("georag_geoparsers._survey_interp")

        stations = [
            interp.SurveyStation(depth_m=0.0, azimuth_deg=0.0, dip_deg=-90.0),
            interp.SurveyStation(depth_m=50.0, azimuth_deg=0.0, dip_deg=-90.0),
        ]
        at_zero = interp.minimum_curvature(0.0, 0.0, 0.0, stations)
        at_height = interp.minimum_curvature(0.0, 0.0, 1000.0, stations)

        assert at_zero[-1][1].elev_m == at_height[-1][1].elev_m, (
            "elev_m is relative to the collar; collar_elevation is ignored"
        )
        assert at_zero[0][1].elev_m == pytest.approx(0.0, abs=1e-9)

    def test_a_horizontal_hole_travels_its_full_length(self):
        interp = pytest.importorskip("georag_geoparsers._survey_interp")

        out = interp.minimum_curvature(
            collar_easting=0.0, collar_northing=0.0, collar_elevation=0.0,
            stations=[
                interp.SurveyStation(depth_m=0.0, azimuth_deg=90.0, dip_deg=0.0),
                interp.SurveyStation(depth_m=100.0, azimuth_deg=90.0, dip_deg=0.0),
            ],
        )
        _, end = out[-1]
        assert math.hypot(end.east_m, end.north_m) == pytest.approx(100.0, abs=1e-6)
