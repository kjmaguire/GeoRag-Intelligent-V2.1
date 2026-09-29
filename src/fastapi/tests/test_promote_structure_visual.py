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
