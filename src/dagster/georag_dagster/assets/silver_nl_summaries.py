"""ADR-0012 — synthesize NL summary passages from structured silver tables.

The canonical retrieval corpus ``silver.document_passages`` only holds
prose chunks from PDFs (NI 43-101 reports + the Earle textbook). All
other silver tables hold the structured geological data — assays,
lithology, collars, samples, structures, LAS curves, field
observations — which never make it to Qdrant as text-retrievable
content.

This module fills the gap by template-rendering deterministic NL
summaries from each meaningful silver table and inserting them as
new passages with ``chunk_kind='structured_summary'``. The existing
embed cron (ADR-0010 §A) carries them into ``georag_chunks``
automatically; the next reranker training cycle picks them up
through the standard ``silver.document_passages → reranker_label_dataset``
path.

Idempotency: ``passage_id = uuid5(NAMESPACE_OID, f'{source_table}:{source_row_id}')``.
Same source row always produces the same passage UUID. Re-runs UPSERT
when the source's ``updated_at`` is newer than the passage's.

NOTE: Do NOT add ``from __future__ import annotations`` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that
import breaks runtime annotation evaluation.
"""

import hashlib
import uuid
from typing import Any

import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.resources import PostgresResource

# Stable namespace UUID for derived passage IDs. NAMESPACE_OID is the
# UUID5 reserved for OID-style identifiers — appropriate for our
# "{table}:{row_id}" composite keys.
_NAMESPACE = uuid.NAMESPACE_OID

# Chunk kind that distinguishes synthesized rows from PDF prose.
CHUNK_KIND_STRUCTURED = "structured_summary"

# Tracking field on the synthesized passage — operators filter on this
# when debugging which template produced what.
PARSER_USED = "structured_summary_v1"


# ---------------------------------------------------------------------------
# Helpers shared across synthesizers
# ---------------------------------------------------------------------------


def _derive_passage_id(source_table: str, source_row_id: uuid.UUID) -> uuid.UUID:
    """uuid5 over the source row — same input → same output forever."""
    return uuid.uuid5(_NAMESPACE, f"{source_table}:{source_row_id}")


def _text_hash(text: str) -> str:
    """sha256 hex, truncated to 64 chars (matches silver.document_passages CHAR(64))."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:64]


_UPSERT_PASSAGE_SQL = """
INSERT INTO silver.document_passages (
    passage_id, document_id, workspace_id, revision_number,
    text, text_hash, ordinal, chunk_kind, parser_used,
    created_at, updated_at
)
VALUES (
    %(passage_id)s, %(document_id)s, %(workspace_id)s, 1,
    %(text)s, %(text_hash)s, %(ordinal)s, %(chunk_kind)s, %(parser_used)s,
    NOW(), NOW()
)
ON CONFLICT (passage_id) DO UPDATE SET
    text       = EXCLUDED.text,
    text_hash  = EXCLUDED.text_hash,
    chunk_kind = EXCLUDED.chunk_kind,
    parser_used = EXCLUDED.parser_used,
    updated_at = NOW(),
    -- Force re-embed by NULLing the embedding_id when text changed.
    embedding_id = CASE
        WHEN silver.document_passages.text_hash = EXCLUDED.text_hash
        THEN silver.document_passages.embedding_id
        ELSE NULL
    END
"""


def _bulk_upsert(cur, rows: list[dict[str, Any]]) -> int:
    """psycopg2 execute_batch for the UPSERT. Returns the row count."""
    if not rows:
        return 0
    psycopg2.extras.execute_batch(cur, _UPSERT_PASSAGE_SQL, rows, page_size=500)
    return len(rows)


# ---------------------------------------------------------------------------
# 1. silver.assays_v2 → NL summary (one passage per (sample_id, depth interval))
# ---------------------------------------------------------------------------

_ASSAY_FETCH_SQL = """
WITH grouped AS (
    SELECT
        a.workspace_id,
        a.collar_id,
        a.sample_id,
        a.from_depth,
        a.to_depth,
        a.lab_name,
        a.certificate_ref,
        a.analysis_method,
        a.instrument,
        a.qaqc_flag,
        a.created_at AS source_updated_at,
        -- Aggregate per-element results into a JSON object for inline render
        jsonb_object_agg(
            a.element,
            jsonb_build_object(
                'value', a.value,
                'unit',  a.unit,
                'value_ppm', a.value_ppm,
                'over_detection', a.over_detection,
                'under_detection', a.under_detection
            )
        ) AS elements,
        -- One representative id per group (assays don't have a sample-level id)
        (array_agg(a.id ORDER BY a.element))[1] AS representative_id
    FROM silver.assays_v2 a
    GROUP BY
        a.workspace_id, a.collar_id, a.sample_id, a.from_depth, a.to_depth,
        a.lab_name, a.certificate_ref, a.analysis_method, a.instrument,
        a.qaqc_flag, a.created_at
)
SELECT
    g.*,
    c.hole_id,
    p.project_name,
    l.rock_code,
    l.rock_name
FROM grouped g
LEFT JOIN silver.collars c ON c.collar_id = g.collar_id
LEFT JOIN silver.projects p ON p.project_id = c.project_id
LEFT JOIN LATERAL (
    SELECT rock_code, rock_name FROM silver.lithology lit
    WHERE lit.collar_id = g.collar_id
      AND lit.from_depth <= g.from_depth
      AND lit.to_depth >= g.to_depth
    LIMIT 1
) l ON true
ORDER BY g.workspace_id, c.hole_id NULLS LAST, g.from_depth
"""


def _format_element_line(element: str, payload: dict[str, Any]) -> str:
    """e.g. 'U3O8 0.45 wt%' or 'Mo 12 ppm (below detection)'"""
    val = payload.get("value")
    unit = payload.get("unit") or ""
    if val is None:
        return f"{element} ND"
    suffix = ""
    if payload.get("under_detection"):
        suffix = " (below detection)"
    elif payload.get("over_detection"):
        suffix = " (above detection)"
    return f"{element} {val} {unit}{suffix}".strip()


def _render_assay_passage(row: dict[str, Any]) -> str:
    elements_str = ", ".join(
        _format_element_line(el, payload)
        for el, payload in sorted((row["elements"] or {}).items())
    )
    hole = row.get("hole_id") or "(unknown hole)"
    project = row.get("project_name") or "(unknown project)"
    method = row.get("analysis_method") or "unspecified method"
    lab = row.get("lab_name") or "unspecified lab"
    cert = row.get("certificate_ref")
    cert_clause = f" (certificate {cert})" if cert else ""
    rock_clause = ""
    if row.get("rock_name") or row.get("rock_code"):
        rock_name = row.get("rock_name") or row["rock_code"]
        rock_clause = f" Host rock at interval: {rock_name}."
    qaqc = row.get("qaqc_flag") or "unknown"
    instrument = row.get("instrument")
    instr_clause = f" Instrument: {instrument}." if instrument else ""

    return (
        f"Assay sample {row['sample_id']} from drillhole {hole} "
        f"({project} project), interval {row['from_depth']} to "
        f"{row['to_depth']} m. Results: {elements_str}. "
        f"Analytical method {method} at {lab}{cert_clause}.{instr_clause} "
        f"QA/QC: {qaqc}.{rock_clause}"
    )


@asset(
    description=(
        "ADR-0012 — synthesize one structured_summary passage per "
        "(sample_id, depth interval) group in silver.assays_v2. Joins "
        "to collars / projects / lithology for inline context. UPSERTs "
        "into silver.document_passages with chunk_kind='structured_summary'."
    ),
    group_name="silver_nl_summaries",
    compute_kind="postgres",
)
def silver_assays_v2_nl_summary(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> MaterializeResult:
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_ASSAY_FETCH_SQL)
            source_rows = cur.fetchall()

        context.log.info("silver_assays_v2_nl_summary: %d source groups", len(source_rows))

        rendered_rows = []
        for r in source_rows:
            passage_id = _derive_passage_id("silver.assays_v2", r["representative_id"])
            text = _render_assay_passage(r)
            rendered_rows.append({
                "passage_id":   str(passage_id),
                "document_id":  None,  # synthesized — not tied to silver.reports
                "workspace_id": str(r["workspace_id"]),
                "text":         text,
                "text_hash":    _text_hash(text),
                "ordinal":      0,
                "chunk_kind":   CHUNK_KIND_STRUCTURED,
                "parser_used":  PARSER_USED,
            })

        with conn.cursor() as cur:
            n = _bulk_upsert(cur, rendered_rows)
        conn.commit()

        context.log.info("silver_assays_v2_nl_summary: upserted %d passages", n)

    return MaterializeResult(metadata={
        "source_groups":     MetadataValue.int(len(source_rows)),
        "passages_upserted": MetadataValue.int(n),
        "chunk_kind":        MetadataValue.text(CHUNK_KIND_STRUCTURED),
        "parser_used":       MetadataValue.text(PARSER_USED),
    })


# ---------------------------------------------------------------------------
# 2. silver.lithology → NL summary (one passage per interval row)
# ---------------------------------------------------------------------------

_LITHOLOGY_FETCH_SQL = """
SELECT
    l.id,
    l.workspace_id,
    l.collar_id,
    l.from_depth,
    l.to_depth,
    l.rock_code,
    l.rock_name,
    l.description,
    l.colour,
    l.grain_size,
    l.texture,
    l.weathering,
    l.hardness,
    l.logged_by,
    l.logged_date,
    l.created_at AS source_updated_at,
    c.hole_id,
    p.project_name
FROM silver.lithology l
LEFT JOIN silver.collars c ON c.collar_id = l.collar_id
LEFT JOIN silver.projects p ON p.project_id = c.project_id
"""


def _render_lithology_passage(row: dict[str, Any]) -> str:
    hole = row.get("hole_id") or "(unknown hole)"
    project = row.get("project_name") or "(unknown project)"
    rock_name = row.get("rock_name") or row.get("rock_code") or "unspecified rock type"
    rock_clause = f"{rock_name}"
    if row.get("rock_code") and row.get("rock_name") and row["rock_code"] != row["rock_name"]:
        rock_clause = f"{rock_name} (rock code {row['rock_code']})"

    attribute_bits = []
    for label, key in (
        ("colour", "colour"), ("grain size", "grain_size"),
        ("texture", "texture"), ("weathering", "weathering"),
        ("hardness", "hardness"),
    ):
        if row.get(key):
            attribute_bits.append(f"{label} {row[key]}")
    attribute_clause = ""
    if attribute_bits:
        attribute_clause = " Attributes: " + ", ".join(attribute_bits) + "."

    desc_clause = ""
    if row.get("description"):
        desc = str(row["description"]).strip()
        if len(desc) > 280:
            desc = desc[:280] + "…"
        desc_clause = f" Description: {desc}"

    logger_clause = ""
    if row.get("logged_by"):
        date_part = f" on {row['logged_date']}" if row.get("logged_date") else ""
        logger_clause = f" Logged by {row['logged_by']}{date_part}."

    return (
        f"Lithology interval in drillhole {hole} ({project} project), "
        f"from {row['from_depth']} to {row['to_depth']} m: {rock_clause}."
        f"{attribute_clause}{desc_clause}{logger_clause}"
    ).strip()


@asset(
    description=(
        "ADR-0012 — one structured_summary passage per silver.lithology "
        "interval row, joined to collars + projects for hole context."
    ),
    group_name="silver_nl_summaries",
    compute_kind="postgres",
)
def silver_lithology_nl_summary(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> MaterializeResult:
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_LITHOLOGY_FETCH_SQL)
            source_rows = cur.fetchall()

        context.log.info("silver_lithology_nl_summary: %d source intervals", len(source_rows))

        rendered_rows = []
        for r in source_rows:
            passage_id = _derive_passage_id("silver.lithology", r["id"])
            text = _render_lithology_passage(r)
            rendered_rows.append({
                "passage_id":   str(passage_id),
                "document_id":  None,
                "workspace_id": str(r["workspace_id"]),
                "text":         text,
                "text_hash":    _text_hash(text),
                "ordinal":      0,
                "chunk_kind":   CHUNK_KIND_STRUCTURED,
                "parser_used":  PARSER_USED,
            })

        with conn.cursor() as cur:
            n = _bulk_upsert(cur, rendered_rows)
        conn.commit()

        context.log.info("silver_lithology_nl_summary: upserted %d passages", n)

    return MaterializeResult(metadata={
        "source_intervals":  MetadataValue.int(len(source_rows)),
        "passages_upserted": MetadataValue.int(n),
    })


# ---------------------------------------------------------------------------
# 3. silver.collars → NL summary (one passage per drillhole)
# ---------------------------------------------------------------------------

_COLLAR_FETCH_SQL = """
SELECT
    c.collar_id,
    c.workspace_id,
    c.hole_id,
    c.easting, c.northing, c.elevation,
    c.total_depth,
    c.hole_type, c.drill_type,
    c.azimuth, c.dip,
    c.drill_date,
    c.status, c.hole_status,
    c.purpose,
    c.driller, c.geologist,
    c.updated_at AS source_updated_at,
    p.project_name
FROM silver.collars c
LEFT JOIN silver.projects p ON p.project_id = c.project_id
"""


def _render_collar_passage(row: dict[str, Any]) -> str:
    hole = row.get("hole_id") or "(unknown hole)"
    project = row.get("project_name") or "(unknown project)"
    hole_type = row.get("hole_type") or row.get("drill_type") or "drillhole"
    az = row.get("azimuth")
    dip = row.get("dip")
    orientation = ""
    if az is not None and dip is not None:
        orientation = f" Azimuth {az}°, dip {dip}°."
    elif az is not None:
        orientation = f" Azimuth {az}°."
    elif dip is not None:
        orientation = f" Dip {dip}°."

    coord_clause = (
        f" Collared at easting {row['easting']}, northing {row['northing']}"
        + (f", elevation {row['elevation']} m" if row.get("elevation") is not None else "")
        + "."
    )

    depth_clause = f" Total depth {row['total_depth']} m."

    date_clause = (
        f" Drilled {row['drill_date']}." if row.get("drill_date") else ""
    )

    status_clause = ""
    status = row.get("hole_status") or row.get("status")
    if status:
        status_clause = f" Status: {status}."

    crew_bits = []
    if row.get("driller"):
        crew_bits.append(f"drilled by {row['driller']}")
    if row.get("geologist"):
        crew_bits.append(f"logged by {row['geologist']}")
    crew_clause = ("; " + ", ".join(crew_bits) + ".") if crew_bits else ""

    purpose_clause = f" Purpose: {row['purpose']}." if row.get("purpose") else ""

    return (
        f"Drillhole {hole} on the {project} project, type {hole_type}."
        f"{coord_clause}{orientation}{depth_clause}"
        f"{date_clause}{status_clause}{purpose_clause}{crew_clause}"
    ).strip()


@asset(
    description=(
        "ADR-0012 — one structured_summary passage per silver.collars "
        "drillhole, joined to projects."
    ),
    group_name="silver_nl_summaries",
    compute_kind="postgres",
)
def silver_collars_nl_summary(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> MaterializeResult:
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_COLLAR_FETCH_SQL)
            source_rows = cur.fetchall()

        context.log.info("silver_collars_nl_summary: %d source rows", len(source_rows))

        rendered_rows = []
        for r in source_rows:
            passage_id = _derive_passage_id("silver.collars", r["collar_id"])
            text = _render_collar_passage(r)
            rendered_rows.append({
                "passage_id":   str(passage_id),
                "document_id":  None,
                "workspace_id": str(r["workspace_id"]),
                "text":         text,
                "text_hash":    _text_hash(text),
                "ordinal":      0,
                "chunk_kind":   CHUNK_KIND_STRUCTURED,
                "parser_used":  PARSER_USED,
            })

        with conn.cursor() as cur:
            n = _bulk_upsert(cur, rendered_rows)
        conn.commit()

        context.log.info("silver_collars_nl_summary: upserted %d passages", n)

    return MaterializeResult(metadata={
        "source_rows":       MetadataValue.int(len(source_rows)),
        "passages_upserted": MetadataValue.int(n),
    })


# ---------------------------------------------------------------------------
# 4-8. Stubs for the remaining source-type synthesizers
#
# Each is a one-or-two-day follow-up PR with its own template + tests.
# No upstream-design dependency between them — they can ship in any
# order. Listed here as marker symbols so the asset group registration
# in georag_dagster/definitions.py can be updated in one shot when the
# real implementations land.
# ---------------------------------------------------------------------------


def silver_samples_nl_summary_TODO():
    """TODO ADR-0012 §5 — synthesize per-sample metadata passages.

    Source: silver.samples joined to silver.collars + silver.projects.
    Template includes: sample id, hole, interval, sample type
    (core/trench/grab), QA category, sample weight, taken_by, taken_on.
    """
    raise NotImplementedError("silver_samples_nl_summary not yet implemented")


def silver_structures_nl_summary_TODO():
    """TODO ADR-0012 §5 — structural measurements (strike/dip/lineation).

    Source: silver.structures joined to collars (if downhole) or
    spatial features (if outcrop). Template includes: measurement id,
    location, foliation/lineation/fault attitude, rock type at
    measurement, observer, date.
    """
    raise NotImplementedError("silver_structures_nl_summary not yet implemented")


def silver_las_curves_nl_summary_TODO():
    """TODO ADR-0012 §6 — depth-banded LAS curve summaries.

    Source: silver.las_curves joined to collars. For each (hole, curve,
    depth-band) emit a passage describing mean / max / min values
    + count of high-anomaly intervals. Bands: 50 m default, configurable.
    """
    raise NotImplementedError("silver_las_curves_nl_summary not yet implemented")


def silver_review_queue_nl_summary_TODO():
    """TODO ADR-0012 §7 — QField field observation passages.

    Source: silver.review_queue WHERE source='qfield'. Template
    includes: observation id, geologist, date, GPS coordinates, rock
    type, alteration, mineralisation notes, photograph refs.
    """
    raise NotImplementedError("silver_review_queue_nl_summary not yet implemented")


def silver_public_geo_nl_summary_TODO():
    """TODO ADR-0012 §8 — public geoscience descriptions.

    Sources: silver.public_drillholes, silver.public_minoccurrences,
    silver.public_assessment_reports. Pulls the raw description fields
    where bronze.public_geo_sources has prose richer than what
    silver normalised. One passage per occurrence.
    """
    raise NotImplementedError("silver_public_geo_nl_summary not yet implemented")
