"""How deep a hole goes when ``silver.collars.total_depth`` is NULL.

Since 2026-09-29 (§04e, SME-approved, Kyle) ``total_depth`` is optional: a
collar table with no EOH column lands its collars with ``total_depth`` NULL,
never 0. A reader that needs a length to draw — a straight-line trace, a long
section — falls back to the deepest depth the hole actually has on record:
its deepest survey station, lithology interval or sample interval. That is a
measured depth, not an invented one, and it is always <= the true EOH.

The fragment is SQL rather than Python so the fallback costs one query, not
one per child table per collar. It is a correlated expression over a collar
aliased ``c``; each subquery is served by the ``collar_id`` btree indexes
(2026_09_29_210100_add_collar_id_indexes_to_drill_child_tables.php).
``GREATEST`` ignores NULLs, so a hole with no children at all yields NULL and
the caller skips it rather than drawing a zero-length hole.
"""

from __future__ import annotations

#: Deepest recorded depth for the collar aliased ``c`` (metres), or NULL.
DEEPEST_RECORDED_DEPTH_SQL = """GREATEST(
    (SELECT max(s.depth)    FROM silver.surveys s        WHERE s.collar_id = c.collar_id),
    (SELECT max(l.to_depth) FROM silver.lithology_logs l WHERE l.collar_id = c.collar_id),
    (SELECT max(m.to_depth) FROM silver.samples m        WHERE m.collar_id = c.collar_id)
)"""

#: ``total_depth`` when recorded, else the deepest recorded depth.
EFFECTIVE_TOTAL_DEPTH_SQL = f"COALESCE(c.total_depth, {DEEPEST_RECORDED_DEPTH_SQL})"

__all__ = ["DEEPEST_RECORDED_DEPTH_SQL", "EFFECTIVE_TOTAL_DEPTH_SQL"]
