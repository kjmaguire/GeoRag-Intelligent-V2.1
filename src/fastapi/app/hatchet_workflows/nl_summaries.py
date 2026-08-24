"""ADR-0012 — give structured geological data a text-retrievable form.

THE GAP THIS CLOSES
    The canonical corpus is PDF prose. No chunk in ``georag_chunks``
    mentions a sample ID, an exact assay interval, a QA/QC flag or a
    method code, because none of that lives in a document -- it lives in
    ``silver.assays_v2``, ``silver.lithology`` and ``silver.collars``.

    So "which holes returned above 1% U3O8 and what QA/QC flags were on
    those samples?" has no good answer. ``search_documents`` returns prose
    about the drilling programme; ``query_assay_data`` returns rows but is
    dispatched only for two intents and takes no hole or threshold
    argument at the agentic layer. The narrative branch has no vocabulary
    for sample IDs; the structured branch has no retrieval ranking. The
    geologist gets a prose answer citing not one assay.

    This workflow synthesizes one passage per structured row, writes it to
    ``silver.document_passages`` with ``chunk_kind='structured_summary'``,
    and lets the existing embed sweep index it like any other passage. The
    sweep already anticipates these rows: ``passage_embedder`` LEFT JOINs
    ``silver.reports`` and names ``structured_summary from ADR-0012
    synthesizers`` in the comment explaining why.

PORTED, NOT WRITTEN FROM SCRATCH
    The three renderers come from
    ``src/dagster/georag_dagster/assets/silver_nl_summaries.py``, which has
    been stranded in the dormant Dagster tree since 2026-07-28 -- no
    container app, no scheduler, no way to run. Every column its SQL reads
    was re-verified against the migrations before porting (including
    ``assays_v2.instrument``, added later than the asset, and the five
    collar columns from the 2026-05-20 drillhole extension).

ONE DELIBERATE CHANGE FROM THE ORIGINAL
    The Dagster fetches had no workspace predicate at all. That was
    tolerable for an asset running as a privileged role over the whole
    warehouse; it is wrong here. Every fetch below is workspace-scoped and
    runs under ``bind_workspace_scope``, so a run can only read and write
    one tenant's rows. Without that, a single run would synthesize
    passages across every workspace and the RLS policy would silently
    decide which of them landed.

NO CRON, DELIBERATELY
    Same reasoning as ``enrich_passage_context``'s cost note. A first run
    over an existing corpus writes one passage per assay group, per
    lithology interval and per drillhole, and every one of them is then
    embedded -- a real, sizeable spend on a corpus nobody has sized yet.
    Registering the workflow makes it triggerable; scheduling it is an
    operator decision.

RE-RUNS ARE IDEMPOTENT, AND CHEAP WHEN NOTHING CHANGED
    ``passage_id`` is a uuid5 over ``{table}:{row_id}``, so the same source
    row always produces the same passage. The upsert only NULLs
    ``embedding_id`` when the text actually changed, so a re-run over
    unchanged data costs one UPDATE and no re-embedding.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

import asyncpg
from hatchet_sdk import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
    Context,
)
from pydantic import BaseModel, Field, field_validator, model_validator

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.nl_summaries")

#: uuid5 namespace for derived passage ids. NAMESPACE_OID is the one
#: reserved for OID-style identifiers, which is what "{table}:{row_id}" is.
_NAMESPACE = uuid.NAMESPACE_OID

#: Distinguishes a synthesized row from PDF prose everywhere downstream --
#: retrieval, the narrative GC in ingest_pdf (which deletes by
#: chunk_kind='narrative' and must never touch these), and the embed
#: sweep's title fallback.
CHUNK_KIND_STRUCTURED = "structured_summary"

#: Written to document_passages.parser_used so an operator can tell which
#: template produced a passage. Bump the suffix when a renderer's output
#: changes shape, so a re-run's diff is attributable.
PARSER_USED = "structured_summary_v1"

SOURCES = ("assays", "lithology", "collars")

_dsn = build_dsn


def derive_passage_id(source_table: str, source_row_id: Any) -> str:
    """Stable id for a synthesized passage: same source row, same id.

    This is what makes a re-run an UPDATE rather than a duplicate. It also
    means deleting a source row leaves its passage behind -- see
    ``prune_orphans`` on the output model for why that is recorded rather
    than silently handled.
    """
    return str(uuid.uuid5(_NAMESPACE, f"{source_table}:{source_row_id}"))


def text_hash(text: str) -> str:
    """sha256 hex. The column is CHAR(64), which is exactly its length."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Renderers. Pure functions of one fetched row -- no I/O, no globals, so
# every sentence below is unit-testable without a database.
# ---------------------------------------------------------------------------

def format_element(element: str, payload: dict[str, Any]) -> str:
    """e.g. ``U3O8 0.45 wt%`` or ``Mo 12 ppm (below detection)``.

    A detection-limit qualifier is not decoration: "0.001 ppm" and "below
    detection at 0.001 ppm" mean different things to a geologist deciding
    whether an element is absent or merely unmeasured, and the difference
    has to survive into the text a retriever will match on.
    """
    value = payload.get("value")
    unit = payload.get("unit") or ""
    if value is None:
        return f"{element} ND"
    suffix = ""
    if payload.get("under_detection"):
        suffix = " (below detection)"
    elif payload.get("over_detection"):
        suffix = " (above detection)"
    return f"{element} {value} {unit}{suffix}".strip()


def render_assay_passage(row: dict[str, Any]) -> str:
    elements = ", ".join(
        format_element(element, payload)
        for element, payload in sorted((row.get("elements") or {}).items())
    )
    hole = row.get("hole_id") or "(unknown hole)"
    project = row.get("project_name") or "(unknown project)"
    method = row.get("analysis_method") or "unspecified method"
    lab = row.get("lab_name") or "unspecified lab"
    certificate = row.get("certificate_ref")
    certificate_clause = f" (certificate {certificate})" if certificate else ""
    rock_clause = ""
    if row.get("rock_name") or row.get("rock_code"):
        rock = row.get("rock_name") or row["rock_code"]
        rock_clause = f" Host rock at interval: {rock}."
    qaqc = row.get("qaqc_flag") or "unknown"
    instrument = row.get("instrument")
    instrument_clause = f" Instrument: {instrument}." if instrument else ""

    return (
        f"Assay sample {row['sample_id']} from drillhole {hole} "
        f"({project} project), interval {row['from_depth']} to "
        f"{row['to_depth']} m. Results: {elements}. "
        f"Analytical method {method} at {lab}{certificate_clause}."
        f"{instrument_clause} QA/QC: {qaqc}.{rock_clause}"
    )


def render_lithology_passage(row: dict[str, Any]) -> str:
    hole = row.get("hole_id") or "(unknown hole)"
    project = row.get("project_name") or "(unknown project)"
    rock_name = row.get("rock_name") or row.get("rock_code") or "unspecified rock type"
    rock_clause = str(rock_name)
    if (
        row.get("rock_code")
        and row.get("rock_name")
        and row["rock_code"] != row["rock_name"]
    ):
        rock_clause = f"{rock_name} (rock code {row['rock_code']})"

    attributes = [
        f"{label} {row[key]}"
        for label, key in (
            ("colour", "colour"),
            ("grain size", "grain_size"),
            ("texture", "texture"),
            ("weathering", "weathering"),
            ("hardness", "hardness"),
        )
        if row.get(key)
    ]
    attribute_clause = (
        " Attributes: " + ", ".join(attributes) + "." if attributes else ""
    )

    description_clause = ""
    if row.get("description"):
        description = str(row["description"]).strip()
        # Truncated because a free-text log entry can run to a page, and a
        # passage that is 95% one geologist's prose retrieves as prose --
        # which the PDF corpus already covers. The structured fields are
        # what this passage exists to make findable.
        if len(description) > 280:
            description = description[:280] + "…"
        description_clause = f" Description: {description}"

    logger_clause = ""
    if row.get("logged_by"):
        date_part = f" on {row['logged_date']}" if row.get("logged_date") else ""
        logger_clause = f" Logged by {row['logged_by']}{date_part}."

    return (
        f"Lithology interval in drillhole {hole} ({project} project), "
        f"from {row['from_depth']} to {row['to_depth']} m: {rock_clause}."
        f"{attribute_clause}{description_clause}{logger_clause}"
    ).strip()


def render_collar_passage(row: dict[str, Any]) -> str:
    hole = row.get("hole_id") or "(unknown hole)"
    project = row.get("project_name") or "(unknown project)"
    hole_type = row.get("hole_type") or row.get("drill_type") or "drillhole"

    azimuth = row.get("azimuth")
    dip = row.get("dip")
    if azimuth is not None and dip is not None:
        orientation = f" Azimuth {azimuth}°, dip {dip}°."
    elif azimuth is not None:
        orientation = f" Azimuth {azimuth}°."
    elif dip is not None:
        orientation = f" Dip {dip}°."
    else:
        orientation = ""

    coordinates = (
        f" Collared at easting {row['easting']}, northing {row['northing']}"
        + (
            f", elevation {row['elevation']} m"
            if row.get("elevation") is not None
            else ""
        )
        + "."
    )

    depth = f" Total depth {row['total_depth']} m."
    drilled = f" Drilled {row['drill_date']}." if row.get("drill_date") else ""

    status_value = row.get("hole_status") or row.get("status")
    status = f" Status: {status_value}." if status_value else ""

    crew = []
    if row.get("driller"):
        crew.append(f"drilled by {row['driller']}")
    if row.get("geologist"):
        crew.append(f"logged by {row['geologist']}")
    crew_clause = ("; " + ", ".join(crew) + ".") if crew else ""

    purpose = f" Purpose: {row['purpose']}." if row.get("purpose") else ""

    return (
        f"Drillhole {hole} on the {project} project, type {hole_type}."
        f"{coordinates}{orientation}{depth}{drilled}{status}{purpose}"
        f"{crew_clause}"
    ).strip()


# ---------------------------------------------------------------------------
# Fetches. Every one is workspace-scoped -- see the module docstring.
# ---------------------------------------------------------------------------

ASSAY_FETCH_SQL = """
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
        -- assays_v2 is one row per ELEMENT, so a sample interval has no id
        -- of its own. The lowest element's id, taken deterministically, is
        -- what makes the derived passage_id stable across runs.
        (array_agg(a.id ORDER BY a.element))[1] AS representative_id
    FROM silver.assays_v2 a
    WHERE a.workspace_id = $1::uuid
    GROUP BY
        a.workspace_id, a.collar_id, a.sample_id, a.from_depth, a.to_depth,
        a.lab_name, a.certificate_ref, a.analysis_method, a.instrument,
        a.qaqc_flag
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
ORDER BY c.hole_id NULLS LAST, g.from_depth
"""

LITHOLOGY_FETCH_SQL = """
SELECT
    l.id, l.workspace_id, l.collar_id,
    l.from_depth, l.to_depth,
    l.rock_code, l.rock_name, l.description,
    l.colour, l.grain_size, l.texture, l.weathering, l.hardness,
    l.logged_by, l.logged_date,
    c.hole_id,
    p.project_name
FROM silver.lithology l
LEFT JOIN silver.collars c ON c.collar_id = l.collar_id
LEFT JOIN silver.projects p ON p.project_id = c.project_id
WHERE l.workspace_id = $1::uuid
ORDER BY c.hole_id NULLS LAST, l.from_depth
"""

COLLAR_FETCH_SQL = """
SELECT
    c.collar_id, c.workspace_id, c.hole_id,
    c.easting, c.northing, c.elevation,
    c.total_depth,
    c.hole_type, c.drill_type,
    c.azimuth, c.dip,
    c.drill_date,
    c.status, c.hole_status,
    c.purpose,
    c.driller, c.geologist,
    p.project_name
FROM silver.collars c
LEFT JOIN silver.projects p ON p.project_id = c.project_id
WHERE c.workspace_id = $1::uuid
ORDER BY c.hole_id NULLS LAST
"""

#: ON CONFLICT targets the PRIMARY KEY, not the (document_id,
#: revision_number, text_hash) unique constraint -- document_id is NULL for
#: a synthesized passage, and Postgres treats NULLs as distinct, so that
#: constraint can never fire here.
UPSERT_PASSAGE_SQL = """
INSERT INTO silver.document_passages (
    passage_id, document_id, workspace_id, revision_number,
    text, text_hash, ordinal, chunk_kind, parser_used,
    created_at, updated_at
)
VALUES ($1::uuid, NULL, $2::uuid, 1, $3, $4, 0, $5, $6, NOW(), NOW())
ON CONFLICT (passage_id) DO UPDATE SET
    text        = EXCLUDED.text,
    text_hash   = EXCLUDED.text_hash,
    chunk_kind  = EXCLUDED.chunk_kind,
    parser_used = EXCLUDED.parser_used,
    updated_at  = NOW(),
    -- Re-embed ONLY when the text actually changed. Without this branch a
    -- re-run would null every embedding_id and put the whole corpus back
    -- through the embedder for no change in content.
    embedding_id = CASE
        WHEN silver.document_passages.text_hash = EXCLUDED.text_hash
        THEN silver.document_passages.embedding_id
        ELSE NULL
    END
"""

#: (source_table, fetch SQL, renderer, id column). The id column is what
#: derive_passage_id hashes; getting it wrong would make every run write
#: new passages instead of updating the old ones.
SYNTHESIZERS: dict[str, tuple[str, str, Any, str]] = {
    "assays": ("silver.assays_v2", ASSAY_FETCH_SQL, render_assay_passage,
               "representative_id"),
    "lithology": ("silver.lithology", LITHOLOGY_FETCH_SQL,
                  render_lithology_passage, "id"),
    "collars": ("silver.collars", COLLAR_FETCH_SQL, render_collar_passage,
                "collar_id"),
}


class NlSummariesInput(BaseModel):
    """Payload for one workspace's synthesis run.

    ``workspace_id`` is required. Unlike its sibling workflows there is no
    fan-out shape and no cron, so there is no payload that legitimately
    omits it -- and a synthesis run that guessed its tenant would write
    another workspace's data into this one's corpus.
    """

    workspace_id: str = Field(..., description="Workspace UUID. Required.")
    sources: list[str] = Field(
        default_factory=lambda: list(SOURCES),
        description=(
            "Which synthesizers to run. Defaults to all three; narrow it to "
            "re-render one template without touching the others."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "Render and count, write nothing. The first run over an "
            "existing corpus is a sizeable embed spend, so there is a way "
            "to find out how large before paying for it."
        ),
    )

    @field_validator("workspace_id")
    @classmethod
    def _must_be_uuid(cls, value: str) -> str:
        uuid.UUID(value)  # raises at the trigger boundary, not mid-run
        return value

    @model_validator(mode="after")
    def _sources_must_be_known(self) -> NlSummariesInput:
        unknown = [s for s in self.sources if s not in SYNTHESIZERS]
        if unknown:
            raise ValueError(
                f"unknown sources {unknown}; valid: {sorted(SYNTHESIZERS)}"
            )
        if not self.sources:
            raise ValueError("sources cannot be empty")
        return self


class NlSummariesOutput(BaseModel):
    workspace_id: str
    dry_run: bool = False
    #: source -> rows read from the warehouse
    source_rows: dict[str, int] = Field(default_factory=dict)
    #: source -> passages written (equal to source_rows unless one failed)
    passages_written: dict[str, int] = Field(default_factory=dict)
    total_written: int = 0
    errors: list[str] = Field(default_factory=list)
    #: Deleting a source row leaves its synthesized passage behind: nothing
    #: cascades, because the passage has no FK to the source table. Recorded
    #: here rather than solved, because pruning means DELETEing corpus rows
    #: and that wants its own decision.
    prune_orphans: bool = False


nl_summaries = hatchet.workflow(
    name="nl_summaries",
    # No on_crons. See the module docstring: the first run over an existing
    # corpus is an embed spend nobody has sized, and a cron should not be
    # the thing that starts it.
    input_validator=NlSummariesInput,
    concurrency=ConcurrencyExpression(
        expression=(
            "has(input.workspace_id) && string(input.workspace_id) != '' "
            "? string(input.workspace_id) : 'default'"
        ),
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


def build_rows(
    source: str, fetched: list[dict[str, Any]], workspace_id: str,
) -> list[tuple[str, str, str, str, str, str]]:
    """Turn fetched rows into UPSERT_PASSAGE_SQL parameter tuples.

    Separated from the I/O so the whole render-and-key step is testable
    against literal dicts. Rows whose id column is NULL are skipped rather
    than given a random id -- a passage whose id is not derived from its
    source row would be re-created on every run.
    """
    source_table, _sql, renderer, id_column = SYNTHESIZERS[source]
    rows: list[tuple[str, str, str, str, str, str]] = []
    for record in fetched:
        row_id = record.get(id_column)
        if row_id is None:
            continue
        text = renderer(record)
        rows.append((
            derive_passage_id(source_table, row_id),
            str(record.get("workspace_id") or workspace_id),
            text,
            text_hash(text),
            CHUNK_KIND_STRUCTURED,
            PARSER_USED,
        ))
    return rows


@nl_summaries.task(execution_timeout="2h", schedule_timeout="3h", retries=1)
async def synthesize(
    input: NlSummariesInput, ctx: Context,
) -> NlSummariesOutput:
    """Render one passage per structured row and upsert them."""
    out = NlSummariesOutput(workspace_id=input.workspace_id,
                            dry_run=input.dry_run)

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Not optional: silver.document_passages is fail-closed, and the
        # source tables are workspace-scoped too. Without the GUC every
        # fetch returns nothing and the run reports a clean zero.
        await bind_workspace_scope(
            conn,
            workspace_id=input.workspace_id,
            site="hatchet.nl_summaries",
            is_local=False,
        )

        for source in input.sources:
            _table, fetch_sql, _renderer, _id_column = SYNTHESIZERS[source]
            try:
                fetched = [
                    dict(record)
                    for record in await conn.fetch(fetch_sql, input.workspace_id)
                ]
                out.source_rows[source] = len(fetched)

                rows = build_rows(source, fetched, input.workspace_id)
                if input.dry_run:
                    out.passages_written[source] = 0
                    log.info(
                        "nl_summaries.dry_run source=%s rows=%d would_write=%d",
                        source, len(fetched), len(rows),
                    )
                    continue

                # One transaction per source: a failure in the lithology
                # template must not roll back assays that already landed.
                async with conn.transaction():
                    await conn.executemany(UPSERT_PASSAGE_SQL, rows)
                out.passages_written[source] = len(rows)
                out.total_written += len(rows)
                log.info(
                    "nl_summaries.done source=%s rows=%d written=%d",
                    source, len(fetched), len(rows),
                )
            except Exception as exc:  # noqa: BLE001 — one template must not sink the rest
                out.errors.append(f"{source}: {str(exc)[:300]}")
                log.exception("nl_summaries failed for source=%s", source)
    finally:
        await conn.close()

    log.info("nl_summaries complete: %s", out.model_dump())
    return out
