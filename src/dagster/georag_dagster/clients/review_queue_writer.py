"""Silver-tier review-queue writer — CC-01 Item 1 Slice 2.

Single entry point: :func:`write_review_queue_rows`. Given a list of parsed
records + per-record outlier flags + parser metadata, builds and inserts
``silver.review_queue`` rows for any record whose flags are non-empty.

Stays narrow on purpose:

* Routing decision is computed from a simple "any flag → review_required"
  rule for v1. The richer threshold logic (auto_pass / auto_reject) lives
  in the parser library Phase 1.B per the review_queue.py docstring.
* No lifecycle transitions, no audit log writes — those belong to the
  /review API surface (Phase 3 of the SRQ plan).
* No FK enforcement on workspace_id / project_id — caller (the asset)
  resolves those before calling in.

Why this lives under ``clients/`` rather than ``parsers/``: the parser
library must stay DB-free for unit-testability. The writer is the
DB-touching adapter.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Iterable

import psycopg2.extras

logger = logging.getLogger(__name__)

INSERT_REVIEW_QUEUE_SQL = """
INSERT INTO silver.review_queue (
    queue_id,
    workspace_id,
    project_id,
    target_table,
    target_record_kind,
    bronze_uri,
    bronze_row_offset,
    payload,
    confidence_per_field,
    confidence_record,
    parser_version,
    routing_decision,
    routing_reason,
    outlier_flags,
    lifecycle
) VALUES (
    %(queue_id)s,
    %(workspace_id)s,
    %(project_id)s,
    %(target_table)s,
    %(target_record_kind)s,
    %(bronze_uri)s,
    %(bronze_row_offset)s,
    %(payload)s,
    %(confidence_per_field)s,
    %(confidence_record)s,
    %(parser_version)s,
    %(routing_decision)s,
    %(routing_reason)s,
    %(outlier_flags)s,
    'pending'
)
"""


def _confidence_from_flags(flags: dict[str, list[Any]]) -> float:
    """Cheap per-record confidence scoring for v1.

    Each non-empty flag category drops confidence by 0.2. Clean records
    return 1.0; capped at 0.0 floor.
    """
    if not flags:
        return 1.0
    penalty = 0.2 * sum(1 for v in flags.values() if v)
    return max(0.0, min(1.0, 1.0 - penalty))


def build_review_queue_row(
    *,
    workspace_id: str,
    project_id: str,
    target_table: str,
    target_record_kind: str,
    bronze_uri: str,
    payload: dict[str, Any],
    outlier_flags: dict[str, list[Any]],
    parser_version: str,
    bronze_row_offset: int | None = None,
) -> dict[str, Any]:
    """Shape one record + its flags into an insert-ready dict.

    Caller is expected to filter out records whose ``outlier_flags`` are
    empty before calling — this function does NOT short-circuit on clean
    rows because the SRQ phase semantics allow callers to enqueue rows
    for review on signals other than outlier_flags too.
    """
    routing_decision = "review_required" if outlier_flags else "auto_pass"
    confidence = _confidence_from_flags(outlier_flags)

    # outlier_flags is documented as a jsonb ARRAY in review_queue.py
    # (list[dict[str, Any]]), so wrap the single category map in a one-
    # element list. Empty categories filtered out so the JSONB stays tidy.
    flags_array: list[dict[str, Any]] = []
    if outlier_flags:
        non_empty = {k: v for k, v in outlier_flags.items() if v}
        if non_empty:
            flags_array.append(non_empty)

    routing_reason: str | None = None
    if flags_array:
        # First-category-wins for a human-readable reason; the full
        # detail stays in the jsonb column.
        first_cat = next(iter(flags_array[0].keys()))
        routing_reason = f"{first_cat}: {len(flags_array[0][first_cat])} flag(s)"

    return {
        "queue_id": str(uuid.uuid4()),
        "workspace_id": workspace_id,
        "project_id": project_id,
        "target_table": target_table,
        "target_record_kind": target_record_kind,
        "bronze_uri": bronze_uri,
        "bronze_row_offset": bronze_row_offset,
        "payload": psycopg2.extras.Json(payload),
        "confidence_per_field": psycopg2.extras.Json({}),
        "confidence_record": confidence,
        "parser_version": parser_version,
        "routing_decision": routing_decision,
        "routing_reason": routing_reason,
        "outlier_flags": psycopg2.extras.Json(flags_array),
    }


def write_review_queue_rows(
    *,
    conn,
    rows: Iterable[dict[str, Any]],
) -> int:
    """Bulk-insert the prepared review-queue rows. Returns rows inserted.

    Uses ``execute_batch`` for amortised round-trip cost. The caller owns
    the connection lifecycle (commit / rollback / close).
    """
    materialised = list(rows)
    if not materialised:
        return 0

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            INSERT_REVIEW_QUEUE_SQL,
            materialised,
            page_size=200,
        )

    logger.info("review_queue_writer: inserted %d rows", len(materialised))
    return len(materialised)


__all__ = [
    "build_review_queue_row",
    "write_review_queue_rows",
]
