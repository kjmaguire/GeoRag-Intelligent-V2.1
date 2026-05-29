"""Phase 1 Step 5B — v1.49 → ``silver.shadow_runs`` write-back hook.

Called from the Dagster ``silver_reports`` asset after ``parse_pdf_report``
completes. If Laravel's ShadowRouter (Step 5A) had previously inserted a
``classification='partial'`` row for this same upload, this hook fills the
v1.49 side (``v149_result`` + ``v149_duration_ms`` + ``v149_audit_run_id``)
on that row.

Pairing rule:
  * key = (workspace_id, minio_key, v149_result IS NULL)
  * if no partial row matches ⇒ this PDF wasn't dual-routed; quietly skip.
  * if multiple match ⇒ pick the most recent (started_at DESC, LIMIT 1).

Workspace resolution:
  * If a workspace_id is passed in directly, use it (preferred — Dagster
    asset-config might gain workspace_id later).
  * Else, parse the project_id segment from ``minio_key``
    (``reports/{projectId}/...``) and look up
    ``silver.projects.workspace_id``. Returns None on miss.

The hook is intentionally write-only and best-effort: any DB error is
logged + swallowed so a transient outage on the shadow path never blocks
the v1.49 critical path.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from uuid import UUID


log = logging.getLogger("georag_dagster.shadow_v149")


def _project_id_from_key(minio_key: str) -> str | None:
    """Bronze keys are ``reports/{projectId}/{ts}_{filename}.pdf``."""
    if not minio_key:
        return None
    parts = minio_key.split("/")
    if len(parts) >= 2 and parts[0] == "reports":
        return parts[1]
    return None


def record_v149_for_shadow(
    *,
    postgres_conn,
    minio_key: str,
    parse_result: Any,
    duration_ms: int,
    workspace_id: str | UUID | None = None,
    audit_run_id: str | UUID | None = None,
    error: str | None = None,
    log_fn=None,
) -> str | None:
    """UPSERT v1.49 result onto a partial shadow_runs row, if one exists.

    Args:
        postgres_conn: psycopg2 connection (the Dagster ``PostgresResource``
            shape used elsewhere in the assets module).
        minio_key: full bronze S3 key of the PDF.
        parse_result: the ``ReportParseResult`` returned by
            ``parse_pdf_report``. Anything non-None is serialised via
            ``_serialise_parse_result``.
        duration_ms: wall-clock time the v1.49 path took (asset start →
            parse end).
        workspace_id: optional override; if absent we derive it from
            project_id parsed out of minio_key.
        audit_run_id: the Dagster run_id (or whatever string the v1.49
            audit emitter uses as ``trace_id``). The diff worker uses this
            to collect per-side ``audit.action_type`` sets for §10.3.
        error: if v1.49 raised (and caller wants the row to capture it),
            pass the message here. Caller will then NOT pass parse_result.
        log_fn: optional caller logger (e.g. Dagster's ``context.log.info``).

    Returns:
        The shadow_runs.id that was updated (text uuid), or None if no
        partial row matched (i.e. this upload was not dual-routed).
    """
    _log = log_fn or log.info

    if workspace_id is None:
        project_id = _project_id_from_key(minio_key)
        if project_id is None:
            _log("shadow_v149: no project_id in minio_key=%s; skipping", minio_key)
            return None
        try:
            with postgres_conn.cursor() as cur:
                cur.execute(
                    "SELECT workspace_id FROM silver.projects WHERE project_id = %s",
                    (project_id,),
                )
                row = cur.fetchone()
                if row is None or row[0] is None:
                    _log("shadow_v149: no workspace_id for project_id=%s; skipping",
                         project_id)
                    return None
                workspace_id = row[0]
        except Exception as e:
            log.warning("shadow_v149: workspace lookup failed: %s", e)
            return None

    payload = _serialise_parse_result(minio_key, parse_result) if parse_result else None

    try:
        with postgres_conn.cursor() as cur:
            # Find the most recent partial row for this (workspace_id, minio_key)
            # with no v1.49 side yet.
            cur.execute(
                """
                SELECT id::text
                FROM silver.shadow_runs
                WHERE workspace_id = %s::uuid
                  AND minio_key   = %s
                  AND v149_result IS NULL
                  AND error_v149  IS NULL
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (str(workspace_id), minio_key),
            )
            r = cur.fetchone()
            if r is None:
                _log("shadow_v149: no partial row for ws=%s key=%s; not dual-routed",
                     workspace_id, minio_key)
                return None
            shadow_id = r[0]

            # RLS GUC then UPDATE.
            cur.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(workspace_id),),
            )
            cur.execute(
                """
                UPDATE silver.shadow_runs
                   SET v149_result      = %s::jsonb,
                       v149_duration_ms = %s,
                       v149_audit_run_id = NULLIF(%s, '')::uuid,
                       error_v149       = %s,
                       completed_at     = COALESCE(completed_at, now())
                 WHERE id = %s::uuid
                """,
                (
                    json.dumps(payload, default=str) if payload is not None else None,
                    int(duration_ms or 0),
                    _coerce_uuid_str(audit_run_id),
                    (error or None) and error[:1000],
                    shadow_id,
                ),
            )
        postgres_conn.commit()
        _log("shadow_v149: updated id=%s ws=%s key=%s", shadow_id, workspace_id, minio_key)
        return shadow_id
    except Exception as e:
        # Roll back the failed UPDATE so the surrounding asset transaction
        # can continue with a clean connection state.
        try:
            postgres_conn.rollback()
        except Exception:  # pragma: no cover
            pass
        log.warning("shadow_v149: UPDATE failed for ws=%s key=%s err=%s",
                    workspace_id, minio_key, e)
        return None


def _coerce_uuid_str(x: Any) -> str:
    """Return a UUID-shaped string or ''. The SQL above ``NULLIF('', '')``s it."""
    if x is None:
        return ""
    if isinstance(x, uuid.UUID):
        return str(x)
    s = str(x)
    try:
        return str(uuid.UUID(s))
    except (ValueError, TypeError):
        return ""


def _serialise_parse_result(minio_key: str, r: Any) -> dict:
    """Mirror of the Hatchet ``ParseOut`` shape so the diff classifier can
    compare apples-to-apples."""
    sections = []
    for s in (getattr(r, "sections", None) or []):
        sections.append({
            "section_number": getattr(s, "section_number", None),
            "section_title": getattr(s, "section_title", None),
            "text": getattr(s, "text", None),
        })
    page_count = 0
    provenance = getattr(r, "provenance", None)
    if provenance is not None:
        page_count = getattr(provenance, "page_count", 0) or 0
        sha256 = getattr(provenance, "sha256", "") or ""
        page_languages = list(getattr(provenance, "page_languages", []) or [])
    else:
        sha256 = getattr(r, "sha256", "") or ""
        page_languages = list(getattr(r, "page_languages", []) or [])

    return {
        "sha256": sha256,
        "minio_key": minio_key,
        "page_count": int(page_count),
        "title": getattr(r, "title", None),
        "authors": list(getattr(r, "authors", []) or []),
        "company": getattr(r, "company", None),
        "filing_date": getattr(r, "filing_date", None),
        "commodity": getattr(r, "commodity", None),
        "project_name": getattr(r, "project_name", None),
        "region": getattr(r, "region", None),
        "sections": sections,
        "sections_count": len(sections),
        "parse_quality_pct": float(getattr(r, "parse_quality_pct", 0.0) or 0.0),
        "parser_used": str(getattr(r, "parser_used", "unknown") or "unknown"),
        "page_languages": page_languages,
        "resource_tables": list(getattr(r, "resource_tables", []) or []),
        "resource_tables_count": len(getattr(r, "resource_tables", []) or []),
        "is_scanned": bool(getattr(r, "is_scanned", False)),
        "warnings_count": len(getattr(r, "warnings", []) or []),
        "skipped_elements": int(getattr(r, "skipped_elements", 0) or 0),
    }


def emit_v149_audits(
    *,
    postgres_conn,
    workspace_id: str | UUID | None,
    report_id: str,
    minio_key: str,
    parse_result: Any,
    duration_ms: int,
    audit_run_id: str | UUID | None = None,
    log_fn=None,
) -> None:
    """Emit ``ingest_pdf.parse.complete`` + ``silver.reports.write`` rows
    on the v1.49 side.

    This closes Phase 1 R-P1-1: without these the diff classifier fires
    ``audit.action_types.critical_missing`` even for byte-identical
    parses. The hash-chain trigger on ``audit.audit_ledger`` fills
    ``previous_hash`` + ``hash``; we only set the user-facing columns.

    workspace_id may arrive None (caller didn't resolve it). In that
    case we set RLS GUC to NULL — the policy allows NULL workspace_id
    when ``app.workspace_id`` is unset, which is what we want for a
    Dagster-asset-side write.
    """
    _log = log_fn or log.info

    if workspace_id is None:
        project_id = _project_id_from_key(minio_key)
        if project_id is not None:
            try:
                with postgres_conn.cursor() as cur:
                    cur.execute(
                        "SELECT workspace_id FROM silver.projects WHERE project_id = %s",
                        (project_id,),
                    )
                    row = cur.fetchone()
                    if row is not None and row[0] is not None:
                        workspace_id = row[0]
            except Exception as e:
                log.warning("emit_v149_audits: workspace lookup failed: %s", e)

    title = getattr(parse_result, "title", None)
    company = getattr(parse_result, "company", None)
    filing_date = getattr(parse_result, "filing_date", None)
    parser_used = getattr(parse_result, "parser_used", "unknown")
    parse_quality_pct = float(getattr(parse_result, "parse_quality_pct", 0.0) or 0.0)
    sections_count = len(getattr(parse_result, "sections", None) or [])
    tables_count = len(getattr(parse_result, "resource_tables", None) or [])

    sha256 = ""
    page_count = 0
    provenance = getattr(parse_result, "provenance", None)
    if provenance is not None:
        sha256 = getattr(provenance, "sha256", "") or ""
        page_count = int(getattr(provenance, "page_count", 0) or 0)

    parse_payload = {
        "minio_key": minio_key,
        "sha256": sha256,
        "parser_used": parser_used,
        "parse_quality_pct": parse_quality_pct,
        "page_count": page_count,
        "sections_count": sections_count,
        "resource_tables_count": tables_count,
        "parse_duration_ms": int(duration_ms or 0),
        "report_id": report_id,
        "side": "v149",
    }
    write_payload = {
        "minio_key": minio_key,
        "sha256": sha256,
        "report_id": report_id,
        "title": title,
        "company": company,
        "filing_date": filing_date,
        "side": "v149",
    }

    try:
        with postgres_conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(workspace_id) if workspace_id else "",),
            )
            cur.execute(
                """
                INSERT INTO audit.audit_ledger
                    (workspace_id, actor_kind, action_type,
                     target_schema, target_table, target_id, payload, trace_id)
                VALUES (%s::uuid, 'workflow', 'ingest_pdf.parse.complete',
                        'silver', 'reports', %s, %s::jsonb, %s)
                """,
                (
                    str(workspace_id) if workspace_id else None,
                    report_id,
                    json.dumps(parse_payload, default=str),
                    _coerce_uuid_str(audit_run_id) or None,
                ),
            )
            cur.execute(
                """
                INSERT INTO audit.audit_ledger
                    (workspace_id, actor_kind, action_type,
                     target_schema, target_table, target_id, payload, trace_id)
                VALUES (%s::uuid, 'workflow', 'silver.reports.write',
                        'silver', 'reports', %s, %s::jsonb, %s)
                """,
                (
                    str(workspace_id) if workspace_id else None,
                    report_id,
                    json.dumps(write_payload, default=str),
                    _coerce_uuid_str(audit_run_id) or None,
                ),
            )
        postgres_conn.commit()
        _log("v149_audits: emitted parse.complete + reports.write for report_id=%s",
             report_id)
    except Exception as e:
        try:
            postgres_conn.rollback()
        except Exception:  # pragma: no cover
            pass
        log.warning("v149_audits: INSERT failed report_id=%s err=%s", report_id, e)


__all__ = ["record_v149_for_shadow", "emit_v149_audits"]
