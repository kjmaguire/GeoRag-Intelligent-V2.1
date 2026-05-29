"""End-to-end Cameco cluster ingest runner.

Doc-phase 179 — Phase B Tier 1.

Walks an extracted inner-zip directory (e.g. /extract/028N079W36/...)
and dispatches each file to the appropriate Tier 1 ingester:

  - *.LAS / *.las  →  las_ingester
  - *.pdf          →  pdf_ingester
  - *.xlsx / *.xls →  xlsx_ingester
  - *.log          →  cameco_log_ingester (header parse + collar coord update)

Tier 2 (TIFFs requiring OCR) is deferred.

Per file:
  - Wraps each ingest in its own asyncpg transaction
  - Sets RLS GUCs (app.workspace_id + georag.project_id + georag.workspace_id)
  - Collects per-file result for the summary

Per cluster (overall run):
  - Returns ClusterIngestSummary with counts by file_type + per-type results
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg

from app.services.ingest.cameco_log_ingester import (
    emit_log_provenance,
    parse_cameco_log_header,
    upsert_collar_from_log,
    update_collar_with_log_coords,
)
from app.services.ingest.las_ingester import ingest_las_file
from app.services.ingest.pdf_ingester import ingest_pdf_file
from app.services.ingest.xlsx_ingester import ingest_xlsx_file

log = logging.getLogger("georag.ingest.cluster_runner")


@dataclass
class ClusterIngestSummary:
    cluster_dir: str
    workspace_id: str
    plss_section_key: str | None = None
    las_files: int = 0
    las_ingested: int = 0
    las_skipped: int = 0
    las_curves: int = 0
    pdf_files: int = 0
    pdf_ingested: int = 0
    pdf_skipped_scanned: int = 0
    pdf_passages: int = 0
    xlsx_files: int = 0
    xlsx_ingested: int = 0
    xlsx_passages: int = 0
    log_files: int = 0
    log_collars_updated: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


async def _set_rls_gucs(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    project_id: str | None = None,
) -> None:
    """Apply the GUCs that the RLS policies check."""
    await conn.execute(
        "SELECT set_config('app.workspace_id', $1, true)", workspace_id,
    )
    await conn.execute(
        "SELECT set_config('georag.workspace_id', $1, true)", workspace_id,
    )
    if project_id:
        await conn.execute(
            "SELECT set_config('georag.project_id', $1, true)", project_id,
        )
    else:
        await conn.execute("RESET georag.project_id")


async def _find_project_id(
    conn: asyncpg.Connection, *, company_hint: str = "CAMECO",
) -> str | None:
    """Locate the Cameco project_id (created by LAS ingester first pass)."""
    row = await conn.fetchrow(
        "SELECT project_id::text FROM silver.projects "
        "WHERE project_name ILIKE $1 LIMIT 1",
        f"%{company_hint}%",
    )
    return row["project_id"] if row else None


async def ingest_cluster(
    cluster_dir: str,
    *,
    workspace_id: str,
    plss_section_key: str | None = None,
    conn: asyncpg.Connection | None = None,
    progress_every: int = 25,
    project_name: str = "Cameco Shirley Basin Uranium",
    project_slug: str = "cameco-shirley-basin",
    project_company: str = "CAMECO RESOURCES",
    project_region: str = "CARBON, WY",
) -> ClusterIngestSummary:
    """Walk `cluster_dir` and ingest every Tier 1 file.

    Args:
        cluster_dir: filesystem path to the extracted inner-zip directory
        workspace_id: silver.workspaces UUID for RLS scoping
        plss_section_key: e.g. "028N079W36" for coordinate fallback
        conn: optional pre-existing asyncpg connection; if None one is created
        progress_every: log line every N files processed

    Returns:
        ClusterIngestSummary populated with per-type counts.
    """
    cluster_dir = str(cluster_dir).rstrip("/")
    summary = ClusterIngestSummary(
        cluster_dir=cluster_dir,
        workspace_id=workspace_id,
        plss_section_key=plss_section_key,
    )

    own_conn = False
    if conn is None:
        user = os.environ["POSTGRES_USER"]
        password = os.environ["POSTGRES_PASSWORD"]
        host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
        port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
        db = os.environ.get("POSTGRES_DB", "georag")
        conn = await asyncpg.connect(
            f"postgres://{user}:{password}@{host}:{port}/{db}",
            statement_cache_size=0,
        )
        own_conn = True

    try:
        # ── Pass 0 — Pre-create project so every subsequent INSERT has
        # a valid georag.project_id GUC value. silver.collars has an
        # RLS policy that requires the GUC to be NULL OR a valid uuid;
        # leaving it as empty-string ("" from a prior session) breaks
        # the ::uuid cast.
        async with conn.transaction():
            await _set_rls_gucs(conn, workspace_id=workspace_id)
            # Idempotent stub project — refined later by LAS pass
            stub_row = await conn.fetchrow(
                """
                INSERT INTO silver.projects
                    (project_id, project_name, slug, company, region, commodity,
                     crs_datum, crs_epsg, orientation_reference, status, workspace_id,
                     created_at, updated_at)
                VALUES (gen_random_uuid(),
                        $1, $2, $3, $4,
                        'uranium',
                        'EPSG:32613', 32613, 'grid_north', 'active', $5::uuid,
                        NOW(), NOW())
                ON CONFLICT (slug) DO UPDATE SET updated_at = NOW()
                RETURNING project_id::text AS project_id
                """,
                project_name, project_slug, project_company, project_region, workspace_id,
            )
            stub_project_id = stub_row["project_id"]

            # Auto-link new project to every admin user so Foundry/Inertia
            # surfaces (which scope via $user->projects() pivot) see it
            # immediately. Without this, ingested projects exist in
            # silver.projects but are invisible to the UI until an admin
            # manually inserts project_user rows. Idempotent — only fires
            # for rows the user isn't already linked to.
            try:
                await conn.execute(
                    """
                    INSERT INTO project_user
                        (user_id, project_id, role, created_at, updated_at)
                    SELECT u.id, $1::uuid, 'owner', NOW(), NOW()
                      FROM users u
                     WHERE u.is_admin = TRUE
                       AND NOT EXISTS (
                           SELECT 1 FROM project_user pu
                            WHERE pu.user_id = u.id AND pu.project_id = $1::uuid
                       )
                    """,
                    stub_project_id,
                )
            except Exception as link_err:  # pragma: no cover — never fail ingest on UI-link
                log.warning(
                    "cluster_runner.admin_link_failed project_id=%s err=%s",
                    stub_project_id, link_err,
                )
        log.info("cluster_runner.stub_project_created project_id=%s", stub_project_id)

        # ── Pass 1 — LAS files create projects + collars ─────────────
        las_paths = sorted(
            list(Path(cluster_dir).rglob("*.LAS")) +
            list(Path(cluster_dir).rglob("*.las"))
        )
        summary.las_files = len(las_paths)
        log.info("cluster_runner.las_start count=%d", len(las_paths))

        for i, p in enumerate(las_paths):
            try:
                async with conn.transaction():
                    await _set_rls_gucs(
                        conn, workspace_id=workspace_id, project_id=stub_project_id,
                    )
                    result = await ingest_las_file(
                        conn, str(p),
                        workspace_id=workspace_id,
                        plss_section_key=plss_section_key,
                        project_id_override=stub_project_id,
                    )
                if result.skipped:
                    summary.las_skipped += 1
                else:
                    summary.las_ingested += 1
                    summary.las_curves += result.curves_inserted
            except Exception as e:
                summary.errors.append({"type": "las", "file": str(p), "err": str(e)})
                log.warning("cluster_runner.las_failed file=%s err=%s", p, e)

            if (i + 1) % progress_every == 0:
                log.info(
                    "cluster_runner.las_progress %d/%d ingested=%d curves=%d",
                    i + 1, len(las_paths),
                    summary.las_ingested, summary.las_curves,
                )

        # Locate the project_id we just created. We have a guaranteed
        # slug via the stub-project upsert above, so use it directly
        # rather than the company-hint search (which falsely returns the
        # first Cameco project when multiple basins coexist).
        await _set_rls_gucs(conn, workspace_id=workspace_id)
        project_id = await conn.fetchval(
            "SELECT project_id::text FROM silver.projects WHERE slug = $1",
            project_slug,
        )
        if not project_id:
            project_id = await _find_project_id(conn, company_hint=project_company)
        if not project_id:
            project_id = await _find_project_id(conn, company_hint="")
        log.info("cluster_runner.project_id=%s", project_id)

        # ── Pass 2 — Cameco .log binary headers update collar coords ─
        log_paths = sorted(set(
            list(Path(cluster_dir).rglob("*.log"))
            + list(Path(cluster_dir).rglob("*.LOG"))
        ))
        summary.log_files = len(log_paths)
        log.info("cluster_runner.log_start count=%d", len(log_paths))

        for i, p in enumerate(log_paths):
            try:
                parsed = parse_cameco_log_header(str(p))
                if parsed.skipped or not parsed.hole_id:
                    continue
                async with conn.transaction():
                    await _set_rls_gucs(
                        conn, workspace_id=workspace_id, project_id=project_id,
                    )
                    if not project_id:
                        continue

                    # Phase C (2026-05-18) — try the LAS-side update path first;
                    # if no collar matches (the common case in Wyoming because
                    # operator hole IDs like 'IC-7' / 'SRE09-6' don't collide
                    # with WSGS PLSS-sequential IDs like '36-1042'), fall back
                    # to upserting a fresh collar from the .log header.
                    updated = await update_collar_with_log_coords(
                        conn, project_id=project_id, parsed=parsed,
                    )
                    collar_id: str | None = None
                    if updated:
                        summary.log_collars_updated += 1
                        row = await conn.fetchrow(
                            "SELECT collar_id::text FROM silver.collars "
                            "WHERE project_id = $1::uuid AND hole_id = $2",
                            project_id, parsed.hole_id,
                        )
                        collar_id = row["collar_id"] if row else None
                    else:
                        new_collar_id = await upsert_collar_from_log(
                            conn,
                            project_id=project_id,
                            workspace_id=workspace_id,
                            parsed=parsed,
                        )
                        if new_collar_id:
                            summary.log_collars_updated += 1
                            collar_id = new_collar_id

                    if collar_id:
                        await emit_log_provenance(
                            conn, file_path=str(p), target_id=collar_id,
                        )
            except Exception as e:
                summary.errors.append({"type": "log", "file": str(p), "err": str(e)})
                log.warning("cluster_runner.log_failed file=%s err=%s", p, e)

            if (i + 1) % progress_every == 0:
                log.info(
                    "cluster_runner.log_progress %d/%d updated=%d",
                    i + 1, len(log_paths), summary.log_collars_updated,
                )

        # ── Pass 3 — PDFs → silver.document_passages ─────────────────
        # Phase B sections carry mixed-case extensions (.pdf + .PDF + .Pdf
        # in the same cluster); match all of them.
        pdf_paths = sorted(set(
            list(Path(cluster_dir).rglob("*.pdf")) +
            list(Path(cluster_dir).rglob("*.PDF")) +
            list(Path(cluster_dir).rglob("*.Pdf"))
        ))
        summary.pdf_files = len(pdf_paths)
        log.info("cluster_runner.pdf_start count=%d", len(pdf_paths))

        for i, p in enumerate(pdf_paths):
            try:
                async with conn.transaction():
                    await _set_rls_gucs(
                        conn, workspace_id=workspace_id, project_id=project_id,
                    )
                    result = await ingest_pdf_file(
                        conn, str(p),
                        workspace_id=workspace_id,
                        project_id=project_id,
                    )
                if result.skipped:
                    if result.skipped_reason == "empty_or_scanned_pdf_no_native_text":
                        summary.pdf_skipped_scanned += 1
                else:
                    summary.pdf_ingested += 1
                    summary.pdf_passages += result.passages_inserted
            except Exception as e:
                summary.errors.append({"type": "pdf", "file": str(p), "err": str(e)})
                log.warning("cluster_runner.pdf_failed file=%s err=%s", p, e)

            if (i + 1) % progress_every == 0:
                log.info(
                    "cluster_runner.pdf_progress %d/%d ingested=%d passages=%d scanned=%d",
                    i + 1, len(pdf_paths),
                    summary.pdf_ingested, summary.pdf_passages,
                    summary.pdf_skipped_scanned,
                )

        # ── Pass 4 — XLSX → silver.document_passages ─────────────────
        xlsx_paths = sorted(set(
            list(Path(cluster_dir).rglob("*.xlsx")) +
            list(Path(cluster_dir).rglob("*.xls")) +
            list(Path(cluster_dir).rglob("*.XLSX")) +
            list(Path(cluster_dir).rglob("*.XLS"))
        ))
        summary.xlsx_files = len(xlsx_paths)
        log.info("cluster_runner.xlsx_start count=%d", len(xlsx_paths))

        for p in xlsx_paths:
            try:
                async with conn.transaction():
                    await _set_rls_gucs(
                        conn, workspace_id=workspace_id, project_id=project_id,
                    )
                    result = await ingest_xlsx_file(
                        conn, str(p),
                        workspace_id=workspace_id,
                        project_id=project_id,
                    )
                if not result.skipped:
                    summary.xlsx_ingested += 1
                    summary.xlsx_passages += result.passages_inserted
            except Exception as e:
                summary.errors.append({"type": "xlsx", "file": str(p), "err": str(e)})
                log.warning("cluster_runner.xlsx_failed file=%s err=%s", p, e)

        log.info("cluster_runner.complete summary=%s", summary)
        return summary
    finally:
        if own_conn:
            await conn.close()


__all__ = ["ingest_cluster", "ClusterIngestSummary"]
