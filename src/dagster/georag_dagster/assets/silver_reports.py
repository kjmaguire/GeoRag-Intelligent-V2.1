"""Silver layer asset — parse NI 43-101 PDFs from Bronze and insert into silver.reports.

Downloads the PDF from MinIO Bronze, runs it through the pdf_report parser,
and inserts a structured row into the silver.reports PostGIS table.

silver.reports schema contract (Section 04e):
  report_id        UUID PRIMARY KEY
  title            TEXT
  authors          TEXT[]
  company          TEXT
  filing_date      DATE
  commodity        TEXT
  project_name     TEXT
  region           TEXT
  resource_estimate JSONB
  sections_text    JSONB        -- {"1": "...", "2": "...", "preamble": "..."}
  embedding_ids    TEXT[]       -- populated by index_reports asset
  geom             POLYGON      -- populated when spatial extent is known (future)

ON CONFLICT on report_id performs an upsert so re-runs are safe.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import json
import tempfile
import time
import uuid

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.bronze_reports import BRONZE_BUCKET, REPORTS_PREFIX, bronze_reports
from georag_dagster.hooks.shadow_v149 import emit_v149_audits, record_v149_for_shadow
from georag_dagster.parsers.pdf_report import parse_pdf_report
from georag_dagster.resources import S3Resource, PostgresResource


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

INSERT_REPORT_SQL = """
INSERT INTO silver.reports (
    report_id,
    title,
    authors,
    company,
    filing_date,
    commodity,
    project_name,
    region,
    resource_estimate,
    sections_text,
    embedding_ids,
    report_type
) VALUES (
    %(report_id)s,
    %(title)s,
    %(authors)s::text[],
    %(company)s,
    %(filing_date)s,
    %(commodity)s,
    %(project_name)s,
    %(region)s,
    %(resource_estimate)s::jsonb,
    %(sections_text)s::jsonb,
    ARRAY[]::text[],
    %(report_type)s
)
ON CONFLICT (report_id) DO UPDATE SET
    sections_text  = EXCLUDED.sections_text,
    report_type    = COALESCE(silver.reports.report_type, EXCLUDED.report_type),
    updated_at     = NOW()
;
"""

# Ensure a GIST index exists on the geometry column and refresh query planner
# statistics. This mirrors the post-load tuning pattern from silver.py.
POSTLOAD_SQL = """
DO $$
BEGIN
    -- DB review #5 — converge on the Laravel-migration index name
    -- (idx_reports_geom) so Dagster doesn't race-create a duplicate GIST.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'reports'
          AND indexname  = 'idx_reports_geom'
    ) THEN
        CREATE INDEX idx_reports_geom ON silver.reports USING GIST (geom);
    END IF;
END$$;

ANALYZE silver.reports;
"""


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverReportsConfig(Config):
    """Runtime configuration for the silver_reports asset."""

    # Basename of the PDF file uploaded in the bronze_reports asset.
    # Example: "PLS-2024-Technical-Report.pdf"
    pdf_filename: str

    # Sprint 5 Phase 1 plumbing — vendor column-mapping profile ID.
    # Extracted from MinIO object metadata x-georag-vendor-profile-id by the
    # minio_upload_sensor.  The parser does NOT use this yet (Phase 2).
    vendor_profile_id: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_sections_dict(sections) -> dict:
    """Convert a list of ReportSection objects into a JSON-serialisable dict.

    Keys are section numbers ("1", "2", ...) or "preamble" / "document" for
    unnumbered leading text. Values are the body text strings.
    """
    result: dict = {}
    for section in sections:
        key = section.section_number if section.section_number is not None else section.section_title.lower()
        # Deduplicate keys by appending a suffix (edge case: two headings with
        # the same number should not occur in a well-formed NI 43-101 but can
        # appear in OCR artefacts).
        if key in result:
            key = f"{key}_dup"
        result[key] = section.text
    return result


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=[bronze_reports],
    description=(
        "Download NI 43-101 PDF from MinIO Bronze, parse with pdf_report parser, "
        "and insert structured metadata + section text into silver.reports."
    ),
)
def silver_reports(
    context: AssetExecutionContext,
    config: SilverReportsConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Parse Bronze PDF → extract metadata + sections → upsert into silver.reports."""

    context.log.info("vendor_profile_id: %s", config.vendor_profile_id)
    object_name = f"{REPORTS_PREFIX}/{config.pdf_filename}"
    context.log.info(
        "Silver reports: downloading '%s/%s' from MinIO", BRONZE_BUCKET, object_name
    )

    # --- Download PDF from Bronze to a temporary file ---
    pdf_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    context.log.info(
        "Silver reports: downloaded %d bytes to temp file '%s'", len(pdf_bytes), tmp_path
    )

    # --- Parse ---
    # TODO (Module 3 Phase B): invoke RAGFlow as the primary parser here.
    # parse_pdf_report is the fallback-only path (fitz-first + pdfplumber).
    # It must only be called after a recorded RAGFlow failure for the same
    # bronze_sha256. See parsers/pdf_report.py module docstring for details.
    # Kyle-approved 2026-04-20 as temporary fallback-only invocation.
    _parse_started = time.monotonic()
    parse_result = parse_pdf_report(tmp_path)
    _parse_duration_ms = int((time.monotonic() - _parse_started) * 1000)

    # Phase 1 Step 5B — write back to silver.shadow_runs if Laravel's
    # ShadowRouter dual-routed this upload. Best-effort and silent on miss.
    _shadow_minio_key = f"{REPORTS_PREFIX}/{config.pdf_filename}"
    with postgres.get_connection() as _shadow_conn:
        record_v149_for_shadow(
            postgres_conn=_shadow_conn,
            minio_key=_shadow_minio_key,
            parse_result=parse_result,
            duration_ms=_parse_duration_ms,
            audit_run_id=context.run_id,
            log_fn=context.log.info,
        )

    context.log.info(
        "Silver reports: parse complete — parser=%s, sections=%d, quality=%.1f%%, "
        "title='%s', company=%s, commodity=%s",
        parse_result.parser_used,
        len(parse_result.sections),
        parse_result.parse_quality_pct * 100,
        (parse_result.title or "")[:60],
        parse_result.company,
        parse_result.commodity,
    )

    if parse_result.skipped_elements > 0:
        context.log.info(
            "Silver reports: %d non-text PDF elements were skipped during extraction",
            parse_result.skipped_elements,
        )

    # --- Build insert parameters ---
    report_id = str(uuid.uuid4())
    numbered_sections = [s for s in parse_result.sections if s.section_number is not None]
    section_count = len(numbered_sections)

    sections_dict = _build_sections_dict(parse_result.sections)
    sections_json = psycopg2.extras.Json(sections_dict)

    # Build resource_estimate payload. Store under a versioned key so future
    # richer extractors (e.g. Camelot) can coexist alongside pdfplumber output.
    # If a pre-existing value is already on the row (via upsert), the ON
    # CONFLICT clause only updates sections_text + updated_at — so the existing
    # resource_estimate is never clobbered on re-runs. New rows always get the
    # full payload here.
    resource_estimate_payload: dict = {}
    if getattr(parse_result, "resource_tables", None):
        resource_estimate_payload["pdfplumber_v1"] = {
            "tables": parse_result.resource_tables,
            "source": "pdfplumber_v1",
        }
    resource_estimate_json = psycopg2.extras.Json(resource_estimate_payload)

    # psycopg2 converts a Python list[str] to a PostgreSQL text[] literal
    authors_list = parse_result.authors if parse_result.authors else []

    # Plan §1c — classify the document type from filename + title +
    # body. classify_document_type is a pure function (no I/O) so it
    # plays nice with Dagster's execution model. Three-tier signal
    # hierarchy: filename (0.95 confidence) > title (0.85) > body (0.70).
    # When classifier returns "Unknown" with confidence 0.0, we leave
    # report_type as NULL — downstream §3b authority ranking treats
    # NULL the same as document_type="unknown" (default mid-rank).
    try:
        from app.agent.document_classifier import classify_document_type
        # First 8K chars of joined sections — same body_budget_chars
        # default the classifier uses.
        body_text = " ".join(
            (s.text or "")
            for s in (parse_result.sections or [])
        )[:8000]
        classification = classify_document_type(
            text=body_text,
            filename=config.pdf_filename,
        )
        report_type = (
            classification.document_class
            if classification.document_class != "Unknown"
            else None
        )
        context.log.info(
            "Silver reports: classified report_type=%r (signal=%s, "
            "confidence=%.2f, evidence=%r)",
            report_type,
            classification.signal,
            classification.confidence,
            classification.evidence_text[:80],
        )
    except Exception:
        # Foundation classifier failure should NEVER block ingest —
        # legacy NULL report_type is the safe fallback.
        context.log.warning(
            "Silver reports: classify_document_type failed; report_type=NULL",
            exc_info=True,
        )
        report_type = None

    params = {
        "report_id":         report_id,
        "title":             parse_result.title,
        "authors":           authors_list,
        "company":           parse_result.company,
        "filing_date":       parse_result.filing_date,  # ISO string or None → psycopg2 DATE
        "commodity":         parse_result.commodity,
        "project_name":      parse_result.project_name,
        "region":            parse_result.region,
        "resource_estimate": resource_estimate_json,
        "sections_text":     sections_json,
        "report_type":       report_type,
    }

    # --- Insert into silver.reports ---
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(INSERT_REPORT_SQL, params)
        conn.commit()

    context.log.info(
        "Silver reports: inserted report_id='%s' (%d sections) into silver.reports",
        report_id,
        section_count,
    )

    # Phase 1 R-P1-1 — emit ingest_pdf.parse.complete + silver.reports.write
    # so the shadow_diff classifier can find a matching audit-action set on
    # the v1.49 side. Best-effort and silent on miss.
    with postgres.get_connection() as conn:
        emit_v149_audits(
            postgres_conn=conn,
            workspace_id=None,  # resolved inside the helper from minio_key
            report_id=report_id,
            minio_key=_shadow_minio_key,
            parse_result=parse_result,
            duration_ms=_parse_duration_ms,
            audit_run_id=context.run_id,
            log_fn=context.log.info,
        )

    # --- Post-load PostGIS tuning (GIST index + ANALYZE) ---
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(POSTLOAD_SQL)
        conn.commit()

    context.log.info("Silver reports: GIST index ensured and ANALYZE run on silver.reports")

    return MaterializeResult(
        metadata={
            "report_id":         MetadataValue.text(report_id),
            "title":             MetadataValue.text(parse_result.title or ""),
            "company":           MetadataValue.text(parse_result.company or ""),
            "commodity":         MetadataValue.text(parse_result.commodity or ""),
            "filing_date":       MetadataValue.text(parse_result.filing_date or ""),
            "section_count":     MetadataValue.int(section_count),
            "total_sections":    MetadataValue.int(len(parse_result.sections)),
            "parse_quality_pct": MetadataValue.float(parse_result.parse_quality_pct),
            "parser_used":       MetadataValue.text(parse_result.parser_used),
            "pdf_filename":      MetadataValue.text(config.pdf_filename),
            "vendor_profile_id":   MetadataValue.text(str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"),
            "authors":           MetadataValue.text(json.dumps(parse_result.authors)),
        }
    )
