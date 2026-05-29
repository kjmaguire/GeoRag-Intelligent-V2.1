"""Asset checks that surface interval-overlap violations to Dagit.

CC-01 Item 1 Slice 3 — both checks are attached to silver assets that
write drillhole intervals (silver_lithology, silver_samples). When a
check fails it:

1. Emits an AssetCheckResult with severity=ERROR + the offending pairs
   in metadata (visible in Dagit's "Checks" tab).
2. Writes one silver.review_queue row per overlap pair so the reviewer
   can act on them from the Foundry DrillReview surface.

We choose WARN severity (not ERROR) on these checks because an overlap
is a data-quality concern, not a corruption — silver.* rows remain
queryable. A reviewer must decide whether to keep both intervals, drop
one, or merge them. Blocking the asset run would force-reject ALL
intervals for a single overlap, which is worse than the violation.

NOTE: Do NOT add ``from __future__ import annotations`` to this file —
Dagster's asset_check decorator inspects runtime annotations.
"""

from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from georag_dagster.checks.interval_overlap import (
    find_overlaps,
    summarize_overlaps_as_dq_flags,
    write_overlaps_to_review_queue,
    resolve_workspace_for_project,
)
from georag_dagster.dq_writer import upsert_flags_sync
from georag_dagster.resources import PostgresResource


# ---------------------------------------------------------------------------
# Internal — shared between both checks
# ---------------------------------------------------------------------------

def _run_overlap_check(
    *,
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
    table: str,
) -> AssetCheckResult:
    with postgres.get_connection() as conn:
        pairs = find_overlaps(conn, table=table, collar_ids=None)

    if not pairs:
        return AssetCheckResult(
            passed=True,
            severity=AssetCheckSeverity.WARN,
            description=f"{table}: no overlapping intervals found.",
            metadata={"overlap_count": 0, "table": table},
        )

    # Cap the metadata payload so Dagit doesn't render thousands of
    # rows when a bad batch lands. The full set still goes to review_queue.
    metadata_sample = [p.as_flag_value() for p in pairs[:20]]

    queue_inserted = 0
    dq_flags_written = 0
    try:
        # We don't have project_id at check-time — group pairs by collar
        # and look up workspace + project + hole_id via silver.collars.
        # Single-pass: collar → (workspace, project, hole_id) cache.
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT collar_id::text, workspace_id::text, "
                    "project_id::text, hole_id "
                    "FROM silver.collars WHERE collar_id = ANY(%s::uuid[])",
                    ([p.collar_id for p in pairs],),
                )
                # collar_map keeps the (workspace, project) tuple the
                # legacy review_queue path expects; hole_id_map is a
                # parallel lookup for friendlier flag descriptions.
                collar_map: dict[str, tuple[str, str]] = {}
                hole_id_map: dict[str, str] = {}
                for row in cur.fetchall():
                    collar_map[row[0]] = (row[1], row[2])
                    if row[3]:
                        hole_id_map[row[0]] = row[3]

            # Group pairs by (workspace, project) so each write_overlaps call
            # gets a tenant-coherent batch.
            grouped: dict[tuple[str, str], list] = {}
            for p in pairs:
                lookup = collar_map.get(p.collar_id)
                if lookup is None:
                    continue
                grouped.setdefault(lookup, []).append(p)

            with postgres.get_connection() as conn:
                for (workspace_id, project_id), batch in grouped.items():
                    queue_inserted += write_overlaps_to_review_queue(
                        conn=conn,
                        pairs=batch,
                        workspace_id=workspace_id,
                        project_id=project_id,
                        target_table=table,
                        bronze_uri=f"(asset check sweep on {table})",
                    )
                conn.commit()

            # Plan §6a: fan-in to one summary flag per affected collar
            # so the DrillholeDetail badge surfaces overlaps. The
            # review_queue path above stays the reviewer-action surface;
            # this is the user-visible surface.
            dq_flags = summarize_overlaps_as_dq_flags(
                pairs=pairs,
                collar_map=collar_map,
                source_table=table,
                hole_id_map=hole_id_map,
            )
            if dq_flags:
                with postgres.get_connection() as conn:
                    try:
                        dq_flags_written = upsert_flags_sync(conn, dq_flags)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        context.log.exception(
                            "interval_overlap_check: dq-flag upsert failed"
                        )
    except Exception as exc:
        context.log.warning(
            "interval_overlap_check: review_queue / dq-flag write skipped — %s",
            exc,
        )

    return AssetCheckResult(
        passed=False,
        severity=AssetCheckSeverity.WARN,
        description=(
            f"{table}: {len(pairs)} overlapping interval pair(s) detected. "
            f"{queue_inserted} routed to silver.review_queue; "
            f"{dq_flags_written} summary flag(s) emitted to "
            f"silver.data_quality_flags."
        ),
        metadata={
            "overlap_count": len(pairs),
            "queue_rows_inserted": queue_inserted,
            "dq_flags_written": dq_flags_written,
            "sample_pairs": metadata_sample,
            "table": table,
        },
    )


# ---------------------------------------------------------------------------
# Attached checks
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_lithology",
    name="lithology_interval_overlap",
    description=(
        "Find rows in silver.lithology where two intervals on the same "
        "collar overlap each other. Emits WARN + writes each pair to "
        "silver.review_queue.outlier_flags for reviewer resolution."
    ),
    blocking=False,
)
def silver_lithology_interval_overlap(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    return _run_overlap_check(
        context=context, postgres=postgres, table="silver.lithology",
    )


@asset_check(
    asset="silver_samples",
    name="assays_v2_interval_overlap",
    description=(
        "Find rows in silver.assays_v2 where two intervals on the same "
        "collar overlap each other. Emits WARN + writes each pair to "
        "silver.review_queue.outlier_flags for reviewer resolution."
    ),
    blocking=False,
)
def silver_samples_assays_v2_interval_overlap(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    # Even though the check is attached to silver_samples (the asset that
    # the reviewer associates with assay ingestion), the SQL target is
    # silver.assays_v2 — the v2 table is the contract per CC-03 Item 1.
    return _run_overlap_check(
        context=context, postgres=postgres, table="silver.assays_v2",
    )
