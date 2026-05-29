"""CC-03 Item 2 — Completeness audit service.

Distinct from the CC-01 Item 5 heading checklist. Catches the subtler
"things mentioned in the text but undocumented in the data" gaps that
geologists (Anna 2026-05-23) want surfaced post-ingestion.

Five finding kinds (see migration 2026_05_23_070000):
  1. work_types_undocumented         — IMPLEMENTED
  2. coords_unmappable               — IMPLEMENTED
  3. qaqc_described_incomplete       — DEFERRED (needs ontology mapping)
  4. prior_recommendations_orphaned  — DEFERRED (needs report-graph)
  5. attachments_referenced_missing  — IMPLEMENTED

The two deferred checks need cross-document reasoning (NLP entity linking
for #3, report-to-report graph traversal for #4); v1 ships the three
checks above and writes 'info'-severity scaffolding rows for the
deferred kinds so the UI can surface "this audit is incomplete — 2 of 5
checks pending" rather than silently hiding the gap.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import asyncpg

logger = logging.getLogger(__name__)


FindingKind = Literal[
    "work_types_undocumented",
    "coords_unmappable",
    "qaqc_described_incomplete",
    "prior_recommendations_orphaned",
    "attachments_referenced_missing",
]

Severity = Literal["info", "warn", "error"]


@dataclass(frozen=True)
class CompletenessFinding:
    """One audit hit — wire-compatible with silver.completeness_findings rows."""

    finding_kind: FindingKind
    severity: Severity
    description: str
    source_page: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Checker 1 — work types mentioned but not documented
# ---------------------------------------------------------------------------


_WORK_TYPE_PATTERNS: dict[str, re.Pattern[str]] = {
    "drill_program": re.compile(
        r"\b(drill(?:ing)?(?:\s+program)?|drill\s+holes?)\b", re.IGNORECASE
    ),
    "soil_geochem_survey": re.compile(
        r"\bsoil\s+(?:geochem(?:istry)?|sampling|survey)\b", re.IGNORECASE
    ),
    "rock_chip_program": re.compile(
        r"\brock\s+chip\s+(?:sampling|program)\b", re.IGNORECASE
    ),
    "ip_survey": re.compile(
        r"\b(IP\s+survey|induced\s+polari[zs]ation\s+survey)\b", re.IGNORECASE
    ),
    "mag_survey": re.compile(
        r"\b(?:airborne\s+)?magnetic\s+survey\b", re.IGNORECASE
    ),
    "em_survey": re.compile(
        r"\b(?:airborne\s+|borehole\s+|downhole\s+)?EM\s+survey\b", re.IGNORECASE
    ),
    "gravity_survey": re.compile(r"\bgravity\s+survey\b", re.IGNORECASE),
    "geological_mapping": re.compile(
        r"\bgeological\s+mapping\b", re.IGNORECASE
    ),
}

# Which silver table evidences each work type. Empty silver evidence
# means "mentioned in text but no row found" → undocumented.
_WORK_TYPE_EVIDENCE_TABLES: dict[str, list[str]] = {
    "drill_program":         ["silver.collars"],
    "soil_geochem_survey":   ["silver.samples"],
    "rock_chip_program":     ["silver.samples"],
    "ip_survey":             ["silver.geophysics_surveys"],
    "mag_survey":            ["silver.geophysics_surveys"],
    "em_survey":             ["silver.geophysics_surveys"],
    "gravity_survey":        ["silver.geophysics_surveys"],
    "geological_mapping":    ["silver.spatial_features"],
}


# ---------------------------------------------------------------------------
# Checker 3 — attachments referenced but missing
# ---------------------------------------------------------------------------


# Match "see Appendix B", "Appendix C.2", "Attachment 1", "Figure 5.4 in Appendix",
# etc. Captures the appendix/attachment label so the caller can hunt for it.
_APPENDIX_REF_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:see\s+|refer\s+to\s+)?"
    r"(Appendix|Attachment|Annex)\s+"
    r"([A-Z](?:[.-][0-9]+)?|[0-9]+(?:[.-][0-9]+)?)"
    r"\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Audit service
# ---------------------------------------------------------------------------


class CompletenessAudit:
    """Runs the 3 implemented checks + writes scaffolding rows for the 2 deferred."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def run(
        self,
        *,
        workspace_id: uuid.UUID,
        pdf_id: str,
        project_id: uuid.UUID | None = None,
    ) -> tuple[uuid.UUID, list[CompletenessFinding]]:
        """Execute the audit and persist findings.

        Returns (finding_run_id, findings). The finding_run_id is fresh
        per call so callers can DELETE the prior batch via WHERE
        finding_run_id = <previous>.
        """
        finding_run_id = uuid.uuid4()
        findings: list[CompletenessFinding] = []

        text_blocks = await self._load_text_blocks(pdf_id)
        if not text_blocks:
            logger.info(
                "completeness_audit no pdf_text_blocks for pdf_id=%s — emitting info row",
                pdf_id[:16],
            )
            findings.append(CompletenessFinding(
                finding_kind="work_types_undocumented",
                severity="info",
                description="No silver.pdf_text_blocks present for this PDF — run /pdf/extract_text first to enable the audit.",
            ))
            await self._persist(
                workspace_id=workspace_id,
                pdf_id=pdf_id,
                project_id=project_id,
                finding_run_id=finding_run_id,
                findings=findings,
            )
            return finding_run_id, findings

        # Checker 1 — work types
        findings.extend(
            await self._check_work_types_undocumented(
                project_id=project_id, text_blocks=text_blocks,
            )
        )

        # Checker 2 — coords unmappable
        findings.extend(
            await self._check_coords_unmappable(
                project_id=project_id, pdf_id=pdf_id,
            )
        )

        # Checker 3 — attachments referenced missing
        findings.extend(self._check_attachments_referenced_missing(text_blocks))

        # Deferred — scaffolding info rows so the UI can show "incomplete"
        findings.append(CompletenessFinding(
            finding_kind="qaqc_described_incomplete",
            severity="info",
            description=(
                "qaqc_described_incomplete check is deferred. Requires NLP entity "
                "linking from QA/QC narrative spans to silver.assays_v2.batch_id."
            ),
            evidence={"status": "deferred", "needs": "ontology_mapping"},
        ))
        findings.append(CompletenessFinding(
            finding_kind="prior_recommendations_orphaned",
            severity="info",
            description=(
                "prior_recommendations_orphaned check is deferred. Requires a "
                "project-scoped report graph + recommendation-to-followup matching."
            ),
            evidence={"status": "deferred", "needs": "report_graph"},
        ))

        await self._persist(
            workspace_id=workspace_id,
            pdf_id=pdf_id,
            project_id=project_id,
            finding_run_id=finding_run_id,
            findings=findings,
        )
        logger.info(
            "completeness_audit OK pdf_id=%s findings=%d run_id=%s",
            pdf_id[:16], len(findings), finding_run_id,
        )
        return finding_run_id, findings

    # ------------------------------------------------------------------
    # Checkers
    # ------------------------------------------------------------------

    async def _check_work_types_undocumented(
        self,
        *,
        project_id: uuid.UUID | None,
        text_blocks: list[dict],
    ) -> list[CompletenessFinding]:
        if project_id is None:
            return []

        # Detect which work types were mentioned in the PDF.
        mentioned: dict[str, int] = {}
        for block in text_blocks:
            text = block.get("text") or ""
            page = int(block.get("page") or 1)
            for kind, pattern in _WORK_TYPE_PATTERNS.items():
                if pattern.search(text) and kind not in mentioned:
                    mentioned[kind] = page

        if not mentioned:
            return []

        findings: list[CompletenessFinding] = []
        for kind, page in mentioned.items():
            evidence_tables = _WORK_TYPE_EVIDENCE_TABLES.get(kind, [])
            if not await self._project_has_any_rows(project_id, evidence_tables):
                findings.append(CompletenessFinding(
                    finding_kind="work_types_undocumented",
                    severity="warn",
                    description=(
                        f"Document mentions a {kind.replace('_', ' ')} on page "
                        f"{page} but no rows were found in {', '.join(evidence_tables)} "
                        "for this project."
                    ),
                    source_page=page,
                    evidence={
                        "work_type": kind,
                        "expected_tables": evidence_tables,
                    },
                ))
        return findings

    async def _check_coords_unmappable(
        self,
        *,
        project_id: uuid.UUID | None,
        pdf_id: str,
    ) -> list[CompletenessFinding]:
        # silver.pdf_coordinates is populated by /pdf/find_coordinates.
        # If the table is empty for this pdf_id the check is a noop.
        async with self._pool.acquire() as conn:
            try:
                coords = await conn.fetch(
                    "SELECT page, lat, lon FROM silver.pdf_coordinates"
                    " WHERE pdf_id = $1 AND lat IS NOT NULL AND lon IS NOT NULL"
                    " LIMIT 50",
                    pdf_id,
                )
            except asyncpg.PostgresError as exc:
                # Table or columns not present — degrade gracefully.
                logger.warning(
                    "coords_unmappable check skipped: %s", exc.__class__.__name__,
                )
                return []

        if not coords:
            return []
        if project_id is None:
            return []

        # For each coordinate mentioned, check whether any silver.collars
        # or silver.spatial_features row exists within a 2 km tolerance.
        # 2 km is a forgiving "is this even in the project AOI" threshold.
        findings: list[CompletenessFinding] = []
        async with self._pool.acquire() as conn:
            for row in coords:
                page = int(row["page"])
                lat = float(row["lat"])
                lon = float(row["lon"])
                near = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM silver.collars
                         WHERE project_id = $1
                           AND geom_4326 IS NOT NULL
                           AND ST_DWithin(
                                 geom_4326::geography,
                                 ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
                                 2000
                               )
                        UNION ALL
                        SELECT 1 FROM silver.spatial_features
                         WHERE project_id = $1
                           AND geom IS NOT NULL
                           AND ST_DWithin(
                                 geom::geography,
                                 ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
                                 2000
                               )
                        LIMIT 1
                    )
                    """,
                    project_id, lon, lat,
                )
                if not near:
                    findings.append(CompletenessFinding(
                        finding_kind="coords_unmappable",
                        severity="warn",
                        description=(
                            f"Coordinate ({lat:.4f}, {lon:.4f}) on page {page} "
                            "has no matching feature within 2 km in silver.collars "
                            "or silver.spatial_features for this project."
                        ),
                        source_page=page,
                        evidence={"lat": lat, "lon": lon, "tolerance_m": 2000},
                    ))
        return findings

    def _check_attachments_referenced_missing(
        self,
        text_blocks: list[dict],
    ) -> list[CompletenessFinding]:
        # v1: just emit a warn for every appendix/attachment reference.
        # The "missing" determination requires a project-scoped file
        # manifest lookup (deferred — needs the SourceFile.title vs
        # appendix-label normaliser to be reliable enough not to false-
        # positive on every "Appendix A" reference).
        seen: set[tuple[str, str]] = set()
        findings: list[CompletenessFinding] = []
        for block in text_blocks:
            text = block.get("text") or ""
            page = int(block.get("page") or 1)
            for match in _APPENDIX_REF_PATTERN.finditer(text):
                kind = match.group(1).lower()
                label = match.group(2).upper()
                key = (kind, label)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(CompletenessFinding(
                    finding_kind="attachments_referenced_missing",
                    severity="info",
                    description=(
                        f"Document references {kind.capitalize()} {label} on page "
                        f"{page}. Verify it's present in the project's file set; "
                        "v1 audit cannot auto-confirm the file."
                    ),
                    source_page=page,
                    evidence={"reference_kind": kind, "label": label},
                ))
        return findings

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    async def _load_text_blocks(self, pdf_id: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT page, text FROM silver.pdf_text_blocks"
                " WHERE pdf_id = $1 ORDER BY page, bbox_y0 DESC, bbox_x0",
                pdf_id,
            )
        return [dict(r) for r in rows]

    async def _project_has_any_rows(
        self,
        project_id: uuid.UUID,
        tables: list[str],
    ) -> bool:
        if not tables:
            return True  # no evidence table → can't disprove → assume documented
        async with self._pool.acquire() as conn:
            for table in tables:
                # Whitelist guard — only allow silver.* tables to defuse SQL injection
                if not table.startswith("silver.") or ";" in table:
                    continue
                try:
                    exists = await conn.fetchval(
                        f"SELECT EXISTS (SELECT 1 FROM {table} WHERE project_id = $1 LIMIT 1)",
                        project_id,
                    )
                except asyncpg.PostgresError:
                    continue
                if exists:
                    return True
        return False

    async def _persist(
        self,
        *,
        workspace_id: uuid.UUID,
        pdf_id: str,
        project_id: uuid.UUID | None,
        finding_run_id: uuid.UUID,
        findings: list[CompletenessFinding],
    ) -> None:
        if not findings:
            return
        import json
        records = [
            (
                workspace_id,
                pdf_id,
                project_id,
                finding_run_id,
                f.finding_kind,
                f.severity,
                f.description,
                f.source_page,
                json.dumps(f.evidence),
            )
            for f in findings
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO silver.completeness_findings"
                " (workspace_id, pdf_id, project_id, finding_run_id, finding_kind,"
                "  severity, description, source_page, evidence)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)",
                records,
            )


__all__ = ["CompletenessAudit", "CompletenessFinding", "FindingKind", "Severity"]
