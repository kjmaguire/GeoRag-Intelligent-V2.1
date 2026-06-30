"""Interval-overlap detector — CC-01 Item 1 Slice 3 + Plan §6a.

For drillhole interval tables (silver.lithology, silver.assays_v2),
find any pair of rows that share a collar_id and have overlapping
(from_depth, to_depth) ranges. Each overlap means a geologist has
duplicated or contradicted themselves and the silver-tier data is
inconsistent.

Three-step contract:

1. :func:`find_overlaps` runs a self-join SQL on a table and returns the
   raw overlap pairs (one Python dict per pair). Pure read; safe to call
   from anywhere with a Postgres connection.

2. :func:`write_overlaps_to_review_queue` adapts the pairs into the
   shape ``silver.review_queue.outlier_flags`` expects
   (``{"interval_overlap": [{a: {from, to}, b: {from, to}}]}``) and
   bulk-inserts review_queue rows via the shared writer.

3. :func:`summarize_overlaps_as_dq_flags` collapses each affected collar
   into ONE summary :class:`DataQualityFlag` per source table so the
   DrillholeDetail badge (Plan §6a) lights up with a count. This is
   the user-facing surface; review_queue is the reviewer-action surface.

The pair is keyed by ``a.id < b.id`` so each overlap surfaces exactly
once (no symmetric duplicates).

Why not a database CHECK constraint: a CHECK can only see one row at a
time, so cross-row interval logic has to be enforced at the validator
layer. We catch the violation post-commit (Dagster asset check), report
it, route the offending pair to review_queue for resolution, AND fan
out a summary DQ flag per collar so the badge stays in sync.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg2.extras

from georag_dagster.clients.review_queue_writer import (
    build_review_queue_row,
    write_review_queue_rows,
)
from georag_dagster.dq_writer import DataQualityFlag

logger = logging.getLogger(__name__)

# Tables this check supports — keeps the SQL gated to schemas we own.
_SUPPORTED_TABLES: frozenset[str] = frozenset({
    "silver.lithology",
    "silver.assays_v2",
})

# Map source-table → flag_type discriminator. The DrillholeDetail badge
# uses flag_type as the dedup key (along with workspace + record_id),
# so picking distinct strings here keeps lithology + assay overlaps
# rendering as separate badge rows rather than overwriting each other.
_TABLE_TO_FLAG_TYPE: dict[str, str] = {
    "silver.lithology": "lithology.interval_overlap",
    "silver.assays_v2": "assay.interval_overlap",
}

# Pinned across re-runs — bumping retires the prior rows for SME review.
INTERVAL_OVERLAP_RULE_VERSION = "v1.0"


@dataclass(frozen=True)
class OverlapPair:
    """One pair of intervals on the same collar that overlap each other."""

    collar_id: str
    a_id: str
    a_from: float
    a_to: float
    b_id: str
    b_from: float
    b_to: float

    def as_flag_value(self) -> dict[str, Any]:
        """Shape this pair for silver.review_queue.outlier_flags."""
        return {
            "a": {"id": self.a_id, "from": self.a_from, "to": self.a_to},
            "b": {"id": self.b_id, "from": self.b_from, "to": self.b_to},
        }


def _validate_table_name(table: str) -> None:
    if table not in _SUPPORTED_TABLES:
        raise ValueError(
            f"interval_overlap: table {table!r} not in supported set "
            f"{sorted(_SUPPORTED_TABLES)}. The SQL is parameterised on table "
            f"name; adding a new target requires updating the allow-list to "
            f"prevent SQL injection through the table identifier."
        )


def find_overlaps_sql(table: str) -> str:
    """Return the self-join SQL string for the given table.

    The half-open-interval overlap test ``a.from < b.to AND b.from < a.to``
    matches the semantics in silver.* — adjacency (``a.to = b.from``)
    is intentionally NOT flagged.
    """
    _validate_table_name(table)

    return f"""
    SELECT
        a.collar_id::text     AS collar_id,
        a.id::text            AS a_id,
        a.from_depth::float8  AS a_from,
        a.to_depth::float8    AS a_to,
        b.id::text            AS b_id,
        b.from_depth::float8  AS b_from,
        b.to_depth::float8    AS b_to
    FROM {table} a
    JOIN {table} b
      ON a.collar_id = b.collar_id
     AND a.id < b.id
     AND a.from_depth < b.to_depth
     AND b.from_depth < a.to_depth
    WHERE (%(collar_ids)s::uuid[] IS NULL OR a.collar_id = ANY(%(collar_ids)s::uuid[]))
    ORDER BY a.collar_id, a.from_depth, b.from_depth
    """


def find_overlaps(
    conn,
    table: str,
    collar_ids: list[str] | None = None,
) -> list[OverlapPair]:
    """Run the overlap check against ``table``.

    ``collar_ids=None`` checks the whole table (whole-table sweep).
    Pass a list to scope the check to a known-touched subset — typically
    the collars whose intervals were just written in the current asset
    materialisation.
    """
    _validate_table_name(table)

    sql = find_overlaps_sql(table)
    params = {"collar_ids": collar_ids}  # asyncpg / psycopg2 binds NULL fine

    pairs: list[OverlapPair] = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            pairs.append(
                OverlapPair(
                    collar_id=str(row["collar_id"]),
                    a_id=str(row["a_id"]),
                    a_from=float(row["a_from"]),
                    a_to=float(row["a_to"]),
                    b_id=str(row["b_id"]),
                    b_from=float(row["b_from"]),
                    b_to=float(row["b_to"]),
                )
            )

    return pairs


def write_overlaps_to_review_queue(
    *,
    conn,
    pairs: list[OverlapPair],
    workspace_id: str,
    project_id: str,
    target_table: str,
    bronze_uri: str,
    parser_version: str = "interval_overlap:1.0",
) -> int:
    """Bulk-insert one review_queue row per overlap pair.

    The payload mirrors what a reviewer needs to resolve the conflict —
    both intervals' ids + depths + the shared collar. The flag itself
    carries the same data under the canonical ``interval_overlap`` key.

    Returns the number of rows inserted.
    """
    if not pairs:
        return 0

    rows = [
        build_review_queue_row(
            workspace_id=workspace_id,
            project_id=project_id,
            target_table=target_table,
            target_record_kind="interval",
            bronze_uri=bronze_uri,
            payload={
                "collar_id": p.collar_id,
                "a": {"id": p.a_id, "from_depth": p.a_from, "to_depth": p.a_to},
                "b": {"id": p.b_id, "from_depth": p.b_from, "to_depth": p.b_to},
                "overlap_length": min(p.a_to, p.b_to) - max(p.a_from, p.b_from),
            },
            outlier_flags={"interval_overlap": [p.as_flag_value()]},
            parser_version=parser_version,
        )
        for p in pairs
    ]

    return write_review_queue_rows(conn=conn, rows=rows)


def resolve_workspace_for_project(conn, project_id: str) -> str | None:
    """Workspace lookup helper — shared with the unit-ambiguity writer."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT workspace_id::text AS workspace_id FROM silver.projects WHERE project_id = %(project_id)s",
            {"project_id": project_id},
        )
        row = cur.fetchone()

    return row["workspace_id"] if row else None


def summarize_overlaps_as_dq_flags(
    *,
    pairs: list[OverlapPair],
    collar_map: dict[str, tuple[str, str]],
    source_table: str,
    hole_id_map: dict[str, str] | None = None,
) -> list[DataQualityFlag]:
    """Collapse overlap pairs → one summary flag per affected collar.

    Each collar with ≥1 overlapping pair on ``source_table`` gets ONE
    :class:`DataQualityFlag` row keyed by ``record_type='collar'`` so
    the DrillholeDetail badge renders it. Multiple pairs on the same
    collar collapse into a single flag whose description carries the
    count + a depth-range summary; the full pair list lives in
    ``threshold_payload`` for drill-down.

    This is deliberately a fan-IN (pairs → flag-per-collar), not a fan-
    OUT (pair → flag). Rationale:

    * The badge UI counts flags per record_id. Emitting one flag per
      pair would inflate the count to 2x the actual issue count when a
      collar has multiple overlaps.
    * The reviewer-action surface (review_queue) keeps pair-level
      granularity, so no information is lost.
    * Idempotency: a re-run with the same overlaps upserts the same
      row (key = workspace + collar + flag_type + rule_version).

    Args:
        pairs: overlap pairs from :func:`find_overlaps`.
        collar_map: ``{collar_id: (workspace_id, project_id)}`` from the
            silver.collars lookup the asset check already does.
        source_table: e.g. ``silver.lithology`` — picks the flag_type
            discriminator.
        hole_id_map: optional ``{collar_id: hole_id}`` for human-friendly
            descriptions. Falls back to the collar_id prefix when absent.

    Returns:
        One :class:`DataQualityFlag` per (collar, source_table) with
        ≥1 pair. Severity is always WARNING — overlaps are a data-
        quality concern, not corruption (silver rows remain queryable).
    """
    if not pairs:
        return []

    if source_table not in _TABLE_TO_FLAG_TYPE:
        raise ValueError(
            f"summarize_overlaps_as_dq_flags: no flag_type mapping for "
            f"table {source_table!r}. Add an entry to _TABLE_TO_FLAG_TYPE "
            f"before extending the check to a new table."
        )

    flag_type = _TABLE_TO_FLAG_TYPE[source_table]
    hole_id_map = hole_id_map or {}

    # Group pairs by collar so each collar contributes exactly one flag.
    by_collar: dict[str, list[OverlapPair]] = {}
    for p in pairs:
        by_collar.setdefault(p.collar_id, []).append(p)

    flags: list[DataQualityFlag] = []
    for collar_id, collar_pairs in by_collar.items():
        lookup = collar_map.get(collar_id)
        if lookup is None:
            # Skip if we can't resolve tenancy — RLS would reject the
            # write anyway. The pair still lands in review_queue via
            # the sibling write path.
            logger.warning(
                "summarize_overlaps_as_dq_flags: no workspace/project "
                "for collar_id=%s — skipping DQ-flag emission",
                collar_id,
            )
            continue

        workspace_id, project_id = lookup
        hole_id = hole_id_map.get(collar_id) or collar_id[:8]
        n = len(collar_pairs)
        # Show the first overlap's depth range in the description; the
        # full list is in threshold_payload.
        first = collar_pairs[0]
        rest = "" if n == 1 else f" (+{n - 1} more on this collar)"
        depth_summary = (
            f"{first.a_from:g}-{first.a_to:g} m overlaps "
            f"{first.b_from:g}-{first.b_to:g} m"
        )
        flags.append(DataQualityFlag(
            workspace_id=workspace_id,
            project_id=project_id,
            record_type="collar",
            record_id=collar_id,
            flag_type=flag_type,
            severity="WARNING",
            description=(
                f"Collar {hole_id}: {n} overlapping interval pair"
                f"{'' if n == 1 else 's'} in {source_table.split('.')[-1]}. "
                f"{depth_summary}{rest}. See review queue for full list."
            ),
            rule_id=flag_type,
            rule_version=INTERVAL_OVERLAP_RULE_VERSION,
            threshold_payload={
                "source_table": source_table,
                "pair_count": n,
                "pairs": [p.as_flag_value() for p in collar_pairs[:50]],
            },
        ))

    return flags


__all__ = [
    "INTERVAL_OVERLAP_RULE_VERSION",
    "OverlapPair",
    "find_overlaps_sql",
    "find_overlaps",
    "write_overlaps_to_review_queue",
    "summarize_overlaps_as_dq_flags",
    "resolve_workspace_for_project",
]
