"""CC-01 Item 5 — Assessment Report Structured Summarizer.

Composes the existing §04p :class:`PdfVlService` to produce one structured,
source-cited summary per canonical section of an ingested assessment
report. The output is the user-facing structure of CC-01 Item 5 — every
claim carries (page, bbox) provenance per §04i Citation completeness.

Pipeline
--------
For each canonical section_id (property_project, location, commodities,
operator, year, work_performed, qa_qc, recommendations):

    1.  Resolve a page range by scanning ``silver.pdf_text_blocks`` for
        section-specific heading patterns.  Falls back to None when no
        heading matches — that section enters the missing-checklist.
    2.  Call :meth:`PdfVlService.summarize_section` with
        ``{"kind": "page_range", "page_start": s, "page_end": e}``.
    3.  Convert VlClaim rows into :class:`SummaryClaim` rows (same shape).

After all sections complete, the service:
    -  Computes ``mean_claim_confidence`` across every claim.
    -  Builds a :class:`CompletenessChecklist` from the per-section
       found/missing state.
    -  Persists the envelope to ``silver.assessment_report_summaries``
       keyed on (workspace_id, pdf_id, model_id).

Cache semantics
---------------
``silver.assessment_report_summaries`` has a UNIQUE (workspace_id, pdf_id,
model_id) constraint. ``get_or_generate`` returns the cached row when
present and ``force_regenerate=False``. Per-section ``pdf_vl_summaries``
rows are reused independently via the upstream cache, so even a forced
regenerate at the assessment level can hit warm per-section caches.

Failure semantics
-----------------
Individual section failures (VL backend error, no headings found) do not
fail the whole envelope — the failing section gets ``summary_text=""``,
``claims=[]``, and is recorded as ``missing`` in the checklist. Only an
asyncpg failure on the final persist raises out.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg

from app.models.assessment_summary import (
    CANONICAL_SECTIONS,
    AssessmentReportSummary,
    CompletenessChecklist,
    CompletenessItem,
    ModelBackend,
    SectionId,
    SummaryClaim,
    SummarySection,
)
from app.services.pdf_vl import PdfVlService, VlBackendError, VlOutputShapeError, VlSectionTooLargeError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Section heading patterns (v1 — keyword match against silver.pdf_text_blocks)
# ---------------------------------------------------------------------------


#: Compiled heading patterns per canonical section. Order within the list
#: matters — the first hit wins, so put the most specific pattern first.
#: All patterns are case-insensitive; bounded by start-of-line / heading
#: layout so we don't false-positive on inline mentions.
_SECTION_PATTERNS: dict[SectionId, list[re.Pattern[str]]] = {
    "property_project": [
        re.compile(r"\bproperty description and location\b", re.IGNORECASE),
        re.compile(r"\bproperty description\b", re.IGNORECASE),
        re.compile(r"\bproperty location and tenure\b", re.IGNORECASE),
        re.compile(r"\b(?:the\s+)?project description\b", re.IGNORECASE),
    ],
    "location": [
        re.compile(r"\baccessibility,?\s*climate,?\s*(?:local resources|infrastructure)\b", re.IGNORECASE),
        re.compile(r"\blocation,?\s*access,?\s*and\b", re.IGNORECASE),
        re.compile(r"\bproject location\b", re.IGNORECASE),
    ],
    "commodities": [
        re.compile(r"\bmineral resources?\b", re.IGNORECASE),
        re.compile(r"\bmineralization\b", re.IGNORECASE),
        re.compile(r"\bcommoditie?s?\b", re.IGNORECASE),
        re.compile(r"\bdeposit type\b", re.IGNORECASE),
    ],
    "operator": [
        re.compile(r"\bissuer\b", re.IGNORECASE),
        re.compile(r"\boperator\b", re.IGNORECASE),
        re.compile(r"\bauthors?\s+of\s+the\s+(?:technical\s+)?report\b", re.IGNORECASE),
    ],
    "year": [
        re.compile(r"\beffective date\b", re.IGNORECASE),
        re.compile(r"\bdate of report\b", re.IGNORECASE),
        re.compile(r"\bfiled\b.*\b20\d{2}\b", re.IGNORECASE),
    ],
    "work_performed": [
        re.compile(r"\bexploration(?:\s+by\s+the\s+issuer)?\b", re.IGNORECASE),
        re.compile(r"\bhistorical\s+(?:work|exploration)\b", re.IGNORECASE),
        re.compile(r"\bdrilling\b", re.IGNORECASE),
        re.compile(r"\bwork\s+(?:program|performed|completed)\b", re.IGNORECASE),
    ],
    "qa_qc": [
        re.compile(r"\b(?:qa\s*/?\s*qc|quality\s+assurance(?:\s+and\s+quality\s+control)?)\b", re.IGNORECASE),
        re.compile(r"\bsample\s+preparation,?\s+analyses?\s+and\s+security\b", re.IGNORECASE),
        re.compile(r"\bdata\s+verification\b", re.IGNORECASE),
    ],
    "recommendations": [
        re.compile(r"\b(?:interpretations? and )?conclusions? and recommendations?\b", re.IGNORECASE),
        re.compile(r"\brecommendations?\b", re.IGNORECASE),
    ],
}


_SECTION_TITLES: dict[SectionId, str] = {
    "property_project": "Property / Project",
    "location": "Location & Access",
    "commodities": "Commodities & Mineralization",
    "operator": "Operator / Issuer",
    "year": "Date / Effective Date",
    "work_performed": "Work Performed",
    "qa_qc": "QA / QC",
    "recommendations": "Recommendations",
    "other": "Other",
}


#: Maximum span (in pages) a single section is allowed to claim. Wider
#: spans get clamped — keeps each VL call within PDF_VL_MAX_PAGES.
_MAX_SECTION_SPAN_PAGES = 4


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AssessmentSummarizer:
    """Singleton wired into ``app.state.assessment_summarizer``.

    Holds an asyncpg pool reference and a :class:`PdfVlService` reference.
    """

    def __init__(self, pool: asyncpg.Pool, vl_service: PdfVlService) -> None:
        self._pool = pool
        self._vl = vl_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_or_generate(
        self,
        *,
        workspace_id: uuid.UUID,
        pdf_id: str,
        pdf_bytes: bytes,
        report_id: uuid.UUID | None = None,
        sections: list[SectionId] | None = None,
        force_regenerate: bool = False,
    ) -> AssessmentReportSummary:
        """Return the cached envelope when present, else generate + persist."""
        model_id = self._vl._model_id  # noqa: SLF001 — singleton, stable surface
        model_backend = self._vl._backend  # noqa: SLF001

        if not force_regenerate:
            cached = await self._load_cached(
                workspace_id=workspace_id, pdf_id=pdf_id, model_id=model_id
            )
            if cached is not None:
                logger.info(
                    "assessment_summary cache HIT pdf_id=%s workspace=%s",
                    pdf_id[:16], str(workspace_id),
                )
                return cached.model_copy(update={"cache_hit": True})

        target_sections: list[SectionId] = list(sections) if sections else list(CANONICAL_SECTIONS)

        # Resolve page ranges for every requested section in one query.
        ranges = await self._resolve_section_ranges(pdf_id=pdf_id, sections=target_sections)

        # Materialise each section. Failures are absorbed per-section so the
        # whole envelope still completes.
        materialised: list[SummarySection] = []
        for section_id in target_sections:
            page_range = ranges.get(section_id)
            section = await self._materialise_section(
                section_id=section_id,
                page_range=page_range,
                pdf_id=pdf_id,
                pdf_bytes=pdf_bytes,
                workspace_id=workspace_id,
            )
            materialised.append(section)

        # Mean claim confidence across every claim in every section.
        all_claims = [c for s in materialised for c in s.claims]
        mean_conf = (
            sum(c.confidence for c in all_claims) / len(all_claims)
            if all_claims
            else None
        )

        checklist = _build_completeness_checklist(materialised)

        summary_id = await self._persist(
            workspace_id=workspace_id,
            pdf_id=pdf_id,
            report_id=report_id,
            sections=materialised,
            checklist=checklist,
            mean_claim_confidence=mean_conf,
            model_id=model_id,
            model_backend=model_backend,
        )

        envelope = AssessmentReportSummary(
            summary_id=summary_id,
            workspace_id=workspace_id,
            pdf_id=pdf_id,
            report_id=report_id,
            sections=materialised,
            completeness_checklist=checklist,
            mean_claim_confidence=mean_conf,
            model_id=model_id,
            model_backend=model_backend,
            generated_at=datetime.now(timezone.utc),
            cache_hit=False,
        )
        logger.info(
            "assessment_summary GENERATED pdf_id=%s sections=%d claims=%d mean_conf=%s",
            pdf_id[:16], len(materialised), len(all_claims),
            f"{mean_conf:.3f}" if mean_conf is not None else "None",
        )
        return envelope

    # ------------------------------------------------------------------
    # Per-section materialisation
    # ------------------------------------------------------------------

    async def _materialise_section(
        self,
        *,
        section_id: SectionId,
        page_range: tuple[int, int] | None,
        pdf_id: str,
        pdf_bytes: bytes,
        workspace_id: uuid.UUID,
    ) -> SummarySection:
        title = _SECTION_TITLES[section_id]
        if page_range is None:
            return SummarySection(
                section_id=section_id,
                title=title,
                summary_text="",
                claims=[],
                page_range=None,
            )

        page_start, page_end = page_range
        section_ref: dict[str, Any] = {
            "kind": "page_range",
            "page_start": page_start,
            "page_end": page_end,
        }

        try:
            result, _cache_hit = await self._vl.summarize_section(
                pdf_bytes=pdf_bytes,
                pdf_id=pdf_id,
                section_ref=section_ref,
                workspace_id=workspace_id,
            )
        except VlSectionTooLargeError as exc:
            logger.warning(
                "assessment_summary section_too_large section=%s pages=%d max=%d",
                section_id, exc.page_count, exc.max_pages,
            )
            return SummarySection(
                section_id=section_id,
                title=title,
                summary_text="",
                claims=[],
                page_range=page_range,
            )
        except (VlBackendError, VlOutputShapeError) as exc:
            logger.warning(
                "assessment_summary VL failure section=%s pdf_id=%s exc=%r",
                section_id, pdf_id[:16], exc,
            )
            return SummarySection(
                section_id=section_id,
                title=title,
                summary_text="",
                claims=[],
                page_range=page_range,
            )

        claims = [
            SummaryClaim(
                claim_text=c["claim_text"],
                page=c["page"],
                bbox=tuple(c["bbox"]),
                confidence=c["confidence"],
            )
            for c in result.get("claims", [])
        ]
        return SummarySection(
            section_id=section_id,
            title=title,
            summary_text=result.get("summary_text", ""),
            claims=claims,
            page_range=page_range,
        )

    # ------------------------------------------------------------------
    # Heading-pattern page-range resolver
    # ------------------------------------------------------------------

    async def _resolve_section_ranges(
        self,
        *,
        pdf_id: str,
        sections: list[SectionId],
    ) -> dict[SectionId, tuple[int, int] | None]:
        """Locate page ranges for each requested section using heading scan.

        v1 strategy: pull text + page for every text block in the PDF, scan
        each section's regex list, take the earliest matching page. Range
        end = min(start + _MAX_SECTION_SPAN_PAGES - 1, next-section-start - 1).
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT page, text FROM silver.pdf_text_blocks"
                " WHERE pdf_id = $1 ORDER BY page, bbox_y0 DESC, bbox_x0",
                pdf_id,
            )

        if not rows:
            logger.info(
                "assessment_summary no pdf_text_blocks for pdf_id=%s — every section missing",
                pdf_id[:16],
            )
            return {sid: None for sid in sections}

        # First-match page per section.
        first_match: dict[SectionId, int] = {}
        for row in rows:
            page = int(row["page"])
            text = row["text"] or ""
            for section_id in sections:
                if section_id in first_match:
                    continue
                for pattern in _SECTION_PATTERNS.get(section_id, []):
                    if pattern.search(text):
                        first_match[section_id] = page
                        break

        if not first_match:
            return {sid: None for sid in sections}

        # Sort matched section starts to clamp each range to the next start.
        ordered = sorted(first_match.items(), key=lambda kv: kv[1])
        next_start: dict[SectionId, int | None] = {}
        for i, (sid, _) in enumerate(ordered):
            next_start[sid] = ordered[i + 1][1] if i + 1 < len(ordered) else None

        ranges: dict[SectionId, tuple[int, int] | None] = {}
        for section_id in sections:
            start = first_match.get(section_id)
            if start is None:
                ranges[section_id] = None
                continue
            span_end = start + _MAX_SECTION_SPAN_PAGES - 1
            nxt = next_start.get(section_id)
            if nxt is not None:
                span_end = min(span_end, nxt - 1)
            ranges[section_id] = (start, max(start, span_end))
        return ranges

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------

    async def _load_cached(
        self,
        *,
        workspace_id: uuid.UUID,
        pdf_id: str,
        model_id: str,
    ) -> AssessmentReportSummary | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT summary_id, workspace_id, pdf_id, report_id,"
                "       sections, completeness_checklist, mean_claim_confidence,"
                "       model_id, model_backend, generated_at"
                "  FROM silver.assessment_report_summaries"
                " WHERE workspace_id = $1 AND pdf_id = $2 AND model_id = $3"
                " ORDER BY generated_at DESC LIMIT 1",
                workspace_id, pdf_id, model_id,
            )
        if row is None:
            return None
        return AssessmentReportSummary(
            summary_id=row["summary_id"],
            workspace_id=row["workspace_id"],
            pdf_id=row["pdf_id"],
            report_id=row["report_id"],
            sections=[SummarySection.model_validate(s) for s in row["sections"]],
            completeness_checklist=CompletenessChecklist.model_validate(
                row["completeness_checklist"]
            ),
            mean_claim_confidence=row["mean_claim_confidence"],
            model_id=row["model_id"],
            model_backend=row["model_backend"],
            generated_at=row["generated_at"],
            cache_hit=True,
        )

    async def _persist(
        self,
        *,
        workspace_id: uuid.UUID,
        pdf_id: str,
        report_id: uuid.UUID | None,
        sections: list[SummarySection],
        checklist: CompletenessChecklist,
        mean_claim_confidence: float | None,
        model_id: str,
        model_backend: ModelBackend,
    ) -> uuid.UUID:
        import json

        sections_json = json.dumps([s.model_dump(mode="json") for s in sections])
        checklist_json = json.dumps(checklist.model_dump(mode="json"))

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO silver.assessment_report_summaries"
                "       (workspace_id, pdf_id, report_id, sections,"
                "        completeness_checklist, mean_claim_confidence,"
                "        model_id, model_backend)"
                " VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $8)"
                " ON CONFLICT (workspace_id, pdf_id, model_id) DO UPDATE SET"
                "       sections = EXCLUDED.sections,"
                "       completeness_checklist = EXCLUDED.completeness_checklist,"
                "       mean_claim_confidence = EXCLUDED.mean_claim_confidence,"
                "       report_id = EXCLUDED.report_id,"
                "       model_backend = EXCLUDED.model_backend,"
                "       generated_at = now(),"
                "       updated_at = now()"
                " RETURNING summary_id",
                workspace_id, pdf_id, report_id, sections_json, checklist_json,
                mean_claim_confidence, model_id, model_backend,
            )
        return row["summary_id"]


# ---------------------------------------------------------------------------
# Completeness checklist builder — module-level so tests can hit it directly
# ---------------------------------------------------------------------------


def _build_completeness_checklist(
    sections: list[SummarySection],
) -> CompletenessChecklist:
    """Build the v1 rule-based completeness checklist from section results."""
    expected: list[SectionId] = list(CANONICAL_SECTIONS)
    found: list[SectionId] = []
    missing: list[SectionId] = []
    items: list[CompletenessItem] = []

    by_id: dict[SectionId, SummarySection] = {
        s.section_id: s for s in sections if s.section_id != "other"
    }
    for sid in expected:
        section = by_id.get(sid)
        is_found = section is not None and section.page_range is not None
        if is_found:
            found.append(sid)
            notes = None
        else:
            missing.append(sid)
            notes = "no heading match in silver.pdf_text_blocks"
        items.append(
            CompletenessItem(
                section_id=sid, expected=True, found=is_found, notes=notes,
            )
        )
    return CompletenessChecklist(
        expected_sections=expected,
        found_sections=found,
        missing_sections=missing,
        items=items,
    )


__all__ = ["AssessmentSummarizer"]
