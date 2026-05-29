"""ADR-0012 first slice — synthesize NL summary passages from silver.samples.

The canonical retrieval corpus ``silver.document_passages`` only holds
prose chunks from PDFs. silver.samples carries the per-sample metadata
(sample type, interval, lab id, QA category, commodity assay results)
that geologists routinely ask about by sample ID — and none of it is
text-retrievable today.

This asset renders one deterministic NL passage per silver.samples row
and UPSERTs it into silver.document_passages with
``chunk_kind='structured_summary'``. The existing embed cron (ADR-0010
§A) picks them up through its LEFT JOIN to silver.reports, so
``document_id=NULL`` is harmless on the embed path.

Idempotency: ``passage_id = uuid5(NAMESPACE_OID, f'silver_samples:{sample_id}')``.
Same row → same UUID forever; re-runs UPSERT-overwrite text + null
``embedding_id`` when text changed so the embed cron re-embeds.

Sibling implementation note: silver_assays_v2 / silver_lithology /
silver_collars NL summary assets live in ``silver_nl_summaries.py``.
This file ships separately per the ADR-0012 first-slice plan; the
shared helpers are imported from the sibling module so behaviour
(passage_id derivation, text_hash, UPSERT SQL) stays in lockstep.

NOTE: Do NOT add ``from __future__ import annotations`` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that
import breaks runtime annotation evaluation.
"""

from typing import Any

import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.assets.silver_nl_summaries import (
    CHUNK_KIND_STRUCTURED,
    PARSER_USED,
    _bulk_upsert,
    _derive_passage_id,
    _text_hash,
)
from georag_dagster.resources import PostgresResource


# ---------------------------------------------------------------------------
# Source SQL — silver.samples joined to silver.collars + silver.projects for
# hole + project context. LEFT JOINs so a sample with a stale collar FK still
# renders (with placeholder "(unknown hole)" downstream).
# ---------------------------------------------------------------------------

_SAMPLES_FETCH_SQL = """
SELECT
    s.sample_id,
    s.workspace_id,
    s.collar_id,
    s.from_depth,
    s.to_depth,
    s.sample_type,
    s.lab_id,
    s.qaqc_type,
    s.commodity_assays,
    s.commodity_assay_flags,
    s.updated_at AS source_updated_at,
    c.hole_id,
    p.project_name
FROM silver.samples s
LEFT JOIN silver.collars  c ON c.collar_id  = s.collar_id
LEFT JOIN silver.projects p ON p.project_id = c.project_id
ORDER BY s.workspace_id, c.hole_id NULLS LAST, s.from_depth
"""


# ---------------------------------------------------------------------------
# Template rendering — deterministic, no LLM
# ---------------------------------------------------------------------------


def _split_element_unit(key: str) -> tuple[str, str]:
    """``Au_ppb`` → ``('Au', 'ppb')``; ``U3O8`` → ``('U3O8', '')``.

    silver.samples.commodity_assays packs the unit into the JSONB key
    suffix (``_ppm``, ``_ppb``, ``_pct``, ``_wt_pct``). When the key has
    no recognised suffix we return it verbatim with an empty unit.
    """
    # Order matters — longer / more-specific suffixes first so
    # "U3O8_wt_pct" hits "wt_pct" before the bare "pct" suffix would
    # consume the trailing fragment alone.
    known_units = ("wt_pct", "oz_t", "g_t", "ppm", "ppb", "pct", "wt%")
    for unit in known_units:
        suffix = f"_{unit}"
        if key.endswith(suffix):
            return key[: -len(suffix)], unit.replace("_", " ").replace("wt pct", "wt%")
    return key, ""


def _format_assay_pair(key: str, value: Any) -> str:
    """e.g. ``Au_ppb: 22`` → ``'Au 22 ppb'``."""
    element, unit = _split_element_unit(key)
    if unit:
        return f"{element} {value} {unit}"
    return f"{element} {value}".strip()


def _render_samples_passage(row: dict[str, Any]) -> str:
    """Render a 1-3 sentence NL passage for one silver.samples row.

    Output shape (single template, deterministic on row contents):

        ``Sample {sample_id} ({sample_type}) from drillhole {hole_id}
        ({project} project), interval {from} to {to} m. Assay results:
        {element1 value1 unit1, element2 value2 unit2, ...}. Lab id
        {lab_id}. QA category: {qaqc_type}.``

    Missing fields gracefully degrade — e.g. no commodity_assays JSON
    drops the "Assay results:" sentence entirely.
    """
    hole = row.get("hole_id") or "(unknown hole)"
    project = row.get("project_name") or "(unknown project)"
    sample_type = row.get("sample_type") or "sample"

    head = (
        f"Sample {row['sample_id']} ({sample_type}) from drillhole {hole} "
        f"({project} project), interval {row['from_depth']} to "
        f"{row['to_depth']} m."
    )

    assays = row.get("commodity_assays") or {}
    assay_clause = ""
    if isinstance(assays, dict) and assays:
        pairs = ", ".join(
            _format_assay_pair(k, v)
            for k, v in sorted(assays.items())
            if v is not None
        )
        if pairs:
            assay_clause = f" Assay results: {pairs}."

    lab_clause = f" Lab id {row['lab_id']}." if row.get("lab_id") else ""
    qaqc_clause = (
        f" QA category: {row['qaqc_type']}." if row.get("qaqc_type") else ""
    )

    return f"{head}{assay_clause}{lab_clause}{qaqc_clause}".strip()


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------


@asset(
    description=(
        "ADR-0012 first slice — synthesize one structured_summary "
        "passage per silver.samples row, joined to collars + projects "
        "for hole context. UPSERTs into silver.document_passages with "
        "chunk_kind='structured_summary'. The embed cron carries the "
        "rows into Qdrant downstream."
    ),
    group_name="nl_summaries",
    compute_kind="postgres",
)
def silver_samples_nl_summary(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Scan silver.samples → template-render → UPSERT into document_passages."""
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_SAMPLES_FETCH_SQL)
            source_rows = cur.fetchall()

        context.log.info(
            "silver_samples_nl_summary: %d source samples", len(source_rows),
        )

        rendered_rows: list[dict[str, Any]] = []
        for r in source_rows:
            passage_id = _derive_passage_id("silver_samples", r["sample_id"])
            text = _render_samples_passage(r)
            rendered_rows.append({
                "passage_id":   str(passage_id),
                "document_id":  None,  # synthesised — not tied to silver.reports
                "workspace_id": str(r["workspace_id"]),
                "text":         text,
                "text_hash":    _text_hash(text),
                "ordinal":      0,
                "chunk_kind":   CHUNK_KIND_STRUCTURED,
                "parser_used":  "silver_samples_nl_summary_v1",
            })

        with conn.cursor() as cur:
            n = _bulk_upsert(cur, rendered_rows)
        conn.commit()

        context.log.info(
            "silver_samples_nl_summary: upserted %d passages", n,
        )

    return MaterializeResult(metadata={
        "source_rows":       MetadataValue.int(len(source_rows)),
        "passages_upserted": MetadataValue.int(n),
        "chunk_kind":        MetadataValue.text(CHUNK_KIND_STRUCTURED),
        "parser_used":       MetadataValue.text("silver_samples_nl_summary_v1"),
    })
