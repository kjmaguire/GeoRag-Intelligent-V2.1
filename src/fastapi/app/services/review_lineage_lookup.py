"""Review-lineage lookup service — Track A.1 Phase 5.C.

Wraps the `silver.get_review_lineage(target_table, silver_pk)` Postgres
function (created in migration 2026_04_30_120000) for the FastAPI citation
pipeline. Called at `answer_citation_items` insert time when a citation
references a silver row directly (e.g. structured_record evidence_type per
§10p-i) — the lookup populates `answer_citation_items.review_lineage` with
the reviewer-correction lineage shape.

Result is the JSONB returned by the SQL function, matching the Pydantic
``ReviewLineage`` model in :mod:`app.models.review_queue` and the JSONB
column shape on ``silver.answer_citation_items.review_lineage``. Returns
None when the silver_pk has no queue lineage or the decision was not
``approve_with_corrections``.

§10w P1 alignment: when the lookup returns a non-None payload, the
EvidenceInspector (§10s) renders the reviewer's authorship in the
support-rationale panel — surfacing geologist-as-hero authorship rather
than AI inference.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.models.review_queue import ReviewLineage

logger = logging.getLogger(__name__)


async def lookup_review_lineage(
    pool: object,
    *,
    target_table: str,
    silver_pk: UUID | str,
) -> dict[str, Any] | None:
    """Fetch review_lineage JSONB for a silver row, or None.

    Args:
        pool: asyncpg Pool (typically ``app.state.pg_pool``).
        target_table: fully-qualified silver table name, e.g.
            ``'silver.collars'``. Must match the value the queue row was
            written with — case-sensitive.
        silver_pk: the row's primary key. Accepts UUID or string forms;
            the SQL function expects UUID type so str values are
            cast inside the query.

    Returns:
        JSONB-equivalent dict matching :class:`ReviewLineage` shape, or
        None when no lineage applies.

    The function is STABLE in Postgres so repeated calls within one
    statement amortize. Calling it once per citation row (the typical
    pattern) is fine; bulk citation generation can wrap multiple calls
    in a single SELECT for a small additional saving.
    """
    if pool is None:
        logger.warning("lookup_review_lineage: pg_pool is None — returning None")
        return None

    sql = """
        SELECT silver.get_review_lineage($1::text, $2::uuid) AS lineage
    """

    try:
        async with pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(sql, target_table, str(silver_pk))
    except Exception:
        logger.warning(
            "lookup_review_lineage: SQL call failed for table=%s pk=%s "
            "(non-fatal — citation will be inserted without review_lineage)",
            target_table, silver_pk, exc_info=True,
        )
        return None

    if row is None or row["lineage"] is None:
        return None

    # asyncpg returns JSONB as Python dict (after json codec registration in
    # the application's connection pool init). If the codec isn't registered
    # the result will be a string — handle both shapes defensively.
    raw = row["lineage"]
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning(
                "lookup_review_lineage: lineage payload was a string but not JSON",
            )
            return None

    if not isinstance(raw, dict):
        logger.warning(
            "lookup_review_lineage: unexpected payload shape %s",
            type(raw).__name__,
        )
        return None

    return raw


async def validated_review_lineage(
    pool: object,
    *,
    target_table: str,
    silver_pk: UUID | str,
) -> ReviewLineage | None:
    """Same as :func:`lookup_review_lineage` but returns a typed Pydantic
    model. Useful when the caller needs to assert the shape downstream
    (e.g. before serializing into a chat-stream payload).

    Validation failures are logged at WARNING and the function returns
    None — the citation is still insertable; the lineage just won't surface
    in this turn's response. Defensive against any drift between the SQL
    function's output and the Pydantic model.
    """
    raw = await lookup_review_lineage(pool, target_table=target_table, silver_pk=silver_pk)
    if raw is None:
        return None
    try:
        return ReviewLineage.model_validate(raw)
    except Exception:
        logger.warning(
            "validated_review_lineage: shape validation failed for table=%s pk=%s",
            target_table, silver_pk, exc_info=True,
        )
        return None


async def resolve_review_lineage_for_evidence(
    pool: object,
    *,
    evidence_id: UUID | str,
) -> dict[str, Any] | None:
    """Resolve review_lineage from a citation's evidence_id.

    Called from :func:`app.services.answer_run_store.insert_citation_item`
    at citation-creation time. Walks the chain:

        evidence_id
          → silver.evidence_items.structured_ref (JSONB)
          → {"schema": "silver", "table": "<table>", "pk": {"<col>": "<uuid>"}}
          → silver.get_review_lineage(target_table, silver_pk)

    Returns the lineage JSONB (dict shape matching :class:`ReviewLineage`)
    or None when:
      - evidence_id is None
      - the evidence row doesn't exist
      - evidence_type is not 'structured_record' (passages, graph edges,
        and map features don't have silver-row lineage to surface)
      - structured_ref is malformed (missing table or pk)
      - the silver row has no queue lineage (Phase 5 commit never happened
        with corrections, or the parser wrote directly per legacy path)

    Non-fatal on every error — returns None and the caller writes the
    citation without review_lineage. This matches the citation-insert
    audit-write contract: citation correctness is more important than
    optional lineage enrichment.
    """
    if pool is None or evidence_id is None:
        return None

    sql = """
        SELECT structured_ref, evidence_type
          FROM silver.evidence_items
         WHERE evidence_id = $1
    """

    try:
        async with pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(sql, str(evidence_id))
    except Exception:
        logger.warning(
            "resolve_review_lineage_for_evidence: evidence_items lookup failed "
            "for evidence_id=%s",
            evidence_id, exc_info=True,
        )
        return None

    if row is None:
        return None

    if row["evidence_type"] != "structured_record":
        # Only structured_record evidence can carry silver-row lineage.
        return None

    structured_ref = row["structured_ref"]
    if isinstance(structured_ref, str):
        import json
        try:
            structured_ref = json.loads(structured_ref)
        except (TypeError, ValueError):
            return None
    if not isinstance(structured_ref, dict):
        return None

    schema = structured_ref.get("schema") or "silver"
    table = structured_ref.get("table")
    pk_dict = structured_ref.get("pk")
    if not table or not isinstance(pk_dict, dict) or not pk_dict:
        return None

    # silver tables have a single UUID PK by convention (queue's
    # committed_silver_pk column is UUID-typed). Take the first PK value.
    pk_value = next(iter(pk_dict.values()), None)
    if pk_value is None:
        return None

    target_table = f"{schema}.{table}"

    return await lookup_review_lineage(
        pool,
        target_table=target_table,
        silver_pk=str(pk_value),
    )
