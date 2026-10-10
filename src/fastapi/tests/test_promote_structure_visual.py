"""Promoting silver.structure to gold must be CHECK-safe and idempotent.

WHY THIS FILE EXISTS
    promote_silver_to_gold copied silver.structure into
    gold.structure_measurements_visual with one INSERT ... SELECT that ended
    ``ON CONFLICT DO NOTHING``. Two defects sat under it, both invisible while
    nothing wrote silver.structure and both live the moment something did:

      * the gold table has no unique key, so that clause could never conflict
        and every promotion run APPENDED a second copy of every measurement -
        the stereonet doubled on the first re-ingest;
      * the gold table CHECKs structure_type (twelve values), dip (0-90), dip
        direction (0-360) and depth (>= 0), while silver.structure.structure_type
        is free text. One out-of-vocabulary type anywhere in a project failed
        the whole INSERT and promoted nothing for that project.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.hatchet_workflows import promote_silver_to_gold as promo

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parents[-1]
GOLD_MIGRATION = (
    REPO_ROOT / "database" / "migrations"
    / "2026_05_13_080002_create_gold_structure_measurements_visual.php"
)

_needs_migrations = pytest.mark.skipif(
    not GOLD_MIGRATION.exists(),
    reason="database/migrations is not mounted (container run of src/fastapi only)",
)


def _gold_type_vocabulary() -> set[str]:
    block = re.search(
        r"structure_type IN \((.*?)\)\)", GOLD_MIGRATION.read_text(), re.DOTALL,
    )
    assert block, "gold structure_type CHECK not found"
    return set(re.findall(r"'([a-z_]+)'", block.group(1)))


class TestPromotionIsSafeAndIdempotent:
    def test_the_old_append_that_could_never_conflict_is_gone(self) -> None:
        assert "ON CONFLICT" not in promo._STRUCTURES_VISUAL

    def test_the_projects_rows_are_cleared_before_the_rebuild(self) -> None:
        source = Path(promo.__file__).read_text()
        clear = source.index("await conn.execute(_STRUCTURES_VISUAL_CLEAR")
        insert = source.index("await conn.execute(_STRUCTURES_VISUAL,")
        assert clear < insert
        # ... in ONE transaction, so a failed rebuild cannot leave the
        # project's stereonet empty.
        assert "async with conn.transaction():" in source[clear - 200:clear]
        assert "WHERE project_id = $1::uuid" in promo._STRUCTURES_VISUAL_CLEAR

    def test_an_out_of_vocabulary_type_cannot_fail_the_whole_insert(self) -> None:
        sql = promo._STRUCTURES_VISUAL
        assert "ELSE 'other' END AS structure_type" in sql
        for value in (
            "fault", "shear", "fracture", "joint", "vein", "foliation",
            "cleavage", "bedding", "contact", "fold_axis", "lineation", "other",
        ):
            assert f"'{value}'" in sql

    @_needs_migrations
    def test_the_sql_vocabulary_is_the_gold_check_constraint(self) -> None:
        block = re.search(
            r"structure_type IN \((.*?)\)\s+THEN", promo._STRUCTURES_VISUAL, re.DOTALL,
        )
        assert block
        assert set(re.findall(r"'([a-z_]+)'", block.group(1))) == _gold_type_vocabulary()

    def test_out_of_range_angles_become_null_instead_of_failing_the_batch(self) -> None:
        sql = promo._STRUCTURES_VISUAL
        assert "st.true_dip BETWEEN 0 AND 90" in sql
        assert "st.true_dip_dir BETWEEN 0 AND 360" in sql
        assert "st.depth >= 0" in sql

    def test_the_stereonet_maths_is_unchanged(self) -> None:
        sql = promo._STRUCTURES_VISUAL
        assert "'equal_area'" in sql and "SQRT(2)" in sql
        assert "MOD((s.dip_dir - 90 + 360)::numeric, 360)" in sql   # RHR strike


def _stereonet_xy(dip: float, dip_dir: float) -> tuple[float, float]:
    """Evaluate the stereonet x / y expressions of ``_STRUCTURES_VISUAL``.

    The expressions are plain arithmetic over SIN / COS / RADIANS / SQRT / MOD,
    so they can be evaluated here without a database: the SQL TEXT is what is
    under test, not a copy of it. (The same statement runs against PostGIS in
    test_promote_gis_pg.py.)
    """
    import math

    blocks = re.findall(r"ELSE\s+(SQRT\(2\).*?)\s+END", promo._STRUCTURES_VISUAL, re.DOTALL)
    assert len(blocks) == 2, "expected one x and one y expression"

    def evaluate(expr: str) -> float:
        py = (
            " ".join(expr.split())
            .replace("::numeric", "")
            .replace("s.dip_dir", repr(dip_dir))
            .replace("s.dip", repr(dip))
            .replace("SQRT(", "math.sqrt(")
            .replace("SIN(", "math.sin(")
            .replace("COS(", "math.cos(")
            .replace("RADIANS(", "math.radians(")
            .replace("MOD(", "_mod(")
        )
        return float(eval(py, {"math": math, "_mod": lambda a, b: a % b}))  # noqa: S307 - our own constant

    return evaluate(blocks[0]), evaluate(blocks[1])


class TestStereonetIsThePoleOfThePlane:
    """GIS audit 2026-10: the radius was the LINE formula with dip as the plunge.

    sqrt(2) * sin((90 - dip) / 2) is the radius of a LINE plunging ``dip``;
    the point plotted is the POLE, which plunges 90 - dip, so the radius is
    sqrt(2) * sin(dip / 2). The two are mirror images: a horizontal bed (pole
    vertical) landed on the rim and a vertical plane (pole horizontal) at the
    centre.
    """

    def test_a_horizontal_bed_plots_at_the_centre(self) -> None:
        x, y = _stereonet_xy(dip=0.0, dip_dir=135.0)
        assert (x, y) == pytest.approx((0.0, 0.0), abs=1e-9)

    @pytest.mark.parametrize(
        "dip_dir,expected",
        [(90.0, (-1.0, 0.0)), (0.0, (0.0, -1.0)), (270.0, (1.0, 0.0)), (180.0, (0.0, 1.0))],
    )
    def test_a_vertical_plane_plots_on_the_rim_opposite_its_dip_direction(
        self, dip_dir: float, expected: tuple[float, float],
    ) -> None:
        """Lower hemisphere: the pole trends dip direction + 180."""
        assert _stereonet_xy(dip=90.0, dip_dir=dip_dir) == pytest.approx(expected, abs=1e-9)

    @pytest.mark.parametrize("dip", [0.0, 10.0, 30.0, 45.0, 60.0, 80.0, 90.0])
    def test_the_radius_is_the_equal_area_radius_of_the_pole(self, dip: float) -> None:
        import math

        x, y = _stereonet_xy(dip=dip, dip_dir=40.0)
        pole_plunge = 90.0 - dip
        expected = math.sqrt(2.0) * math.sin(math.radians((90.0 - pole_plunge) / 2.0))
        assert math.hypot(x, y) == pytest.approx(expected, abs=1e-9)
        assert math.hypot(x, y) <= 1.0 + 1e-9, "normalised to the primitive circle"

    def test_the_old_line_formula_is_gone(self) -> None:
        assert "(90 - s.dip)" not in promo._STRUCTURES_VISUAL

    def test_the_gold_rows_are_rebuilt_every_promotion_so_old_x_y_correct_themselves(self) -> None:
        """No hash, no skip: DELETE the project's rows and INSERT them again."""
        source = Path(promo.__file__).read_text()
        clear = source.index("await conn.execute(_STRUCTURES_VISUAL_CLEAR")
        insert = source.index("await conn.execute(_STRUCTURES_VISUAL,")
        assert clear < insert
        assert "DELETE FROM gold.structure_measurements_visual" in promo._STRUCTURES_VISUAL_CLEAR
