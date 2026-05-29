"""CC-01 Item 5 — Assessment Report Structured Summary endpoints.

Two endpoints, both gated on the standard service-key + JWT auth used by
the rest of the §04p PDF surface:

  POST /assessment_summary/{pdf_id}
      Generate (or regenerate) the structured summary for the given PDF.
      Body: AssessmentSummaryGenerateRequest. Returns the full envelope.

  GET  /assessment_summary/{pdf_id}
      Return the cached envelope if one exists for the caller's workspace,
      else 404 — does not trigger generation. Use POST for that.

Both endpoints honour workspace tenancy: the `workspace_id` is pulled from
the authenticated `UserContext` and the FastAPI router never accepts a
workspace_id in the URL or body.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.models.assessment_summary import (
    AssessmentReportSummary,
    AssessmentSummaryGenerateRequest,
)
from app.services.assessment_summarizer import AssessmentSummarizer
from app.services.auth import UserContext, extract_user_context, verify_service_key

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/assessment_summary",
    tags=["assessment_summary"],
    dependencies=[Depends(verify_service_key)],
)


_BRONZE_PDF_KEY_TEMPLATE = "pdfs/{pdf_id}.pdf"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_summarizer(request: Request) -> AssessmentSummarizer:
    svc = getattr(request.app.state, "assessment_summarizer", None)
    if svc is None:
        logger.error("assessment_summarizer not initialised on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="assessment_summarizer_not_ready",
        )
    return svc


def _resolve_workspace_id(user: UserContext) -> uuid.UUID:
    if not user.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="workspace_id_missing_on_jwt",
        )
    try:
        return uuid.UUID(user.workspace_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="workspace_id_malformed",
        ) from exc


async def _fetch_pdf_bytes(request: Request, pdf_id: str) -> bytes:
    bronze_store = getattr(request.app.state, "bronze_store", None)
    if bronze_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="bronze_store_not_ready",
        )
    key = _BRONZE_PDF_KEY_TEMPLATE.format(pdf_id=pdf_id)
    pdf_bytes: bytes | None = await bronze_store.get(key)
    if pdf_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pdf_not_found",
        )
    return pdf_bytes


def _validate_pdf_id(pdf_id: str) -> None:
    if len(pdf_id) != 64 or any(c not in "0123456789abcdef" for c in pdf_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pdf_id_must_be_sha256_hex",
        )


# ---------------------------------------------------------------------------
# POST /assessment_summary/{pdf_id}
# ---------------------------------------------------------------------------


@router.post(
    "/{pdf_id}",
    response_model=AssessmentReportSummary,
    status_code=status.HTTP_200_OK,
)
async def generate_assessment_summary(
    request: Request,
    pdf_id: str,
    body: AssessmentSummaryGenerateRequest = Body(default_factory=AssessmentSummaryGenerateRequest),
    user: UserContext = Depends(extract_user_context),
) -> AssessmentReportSummary:
    """Generate (or regenerate) the structured summary for a PDF.

    Responses
    ---------
    200  Generated or cache hit (see `cache_hit` field on the envelope)
    401  Missing service key, JWT, or workspace_id
    404  PDF not in bronze store
    422  pdf_id is not a 64-char SHA-256 hex string
    503  Summarizer service not initialised
    """
    _validate_pdf_id(pdf_id)
    workspace_id = _resolve_workspace_id(user)
    summarizer = _get_summarizer(request)
    pdf_bytes = await _fetch_pdf_bytes(request, pdf_id)

    envelope = await summarizer.get_or_generate(
        workspace_id=workspace_id,
        pdf_id=pdf_id,
        pdf_bytes=pdf_bytes,
        sections=body.sections,
        force_regenerate=body.force_regenerate,
    )
    return envelope


# ---------------------------------------------------------------------------
# GET /assessment_summary/{pdf_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{pdf_id}",
    response_model=AssessmentReportSummary,
)
async def fetch_cached_assessment_summary(
    request: Request,
    pdf_id: str,
    user: UserContext = Depends(extract_user_context),
) -> AssessmentReportSummary:
    """Return the cached summary envelope, or 404 if not yet generated.

    Never triggers generation. Use POST for that.

    Responses
    ---------
    200  Cached envelope (cache_hit always True)
    401  Missing service key, JWT, or workspace_id
    404  No cached summary for this (workspace, pdf_id, model)
    422  pdf_id is not a 64-char SHA-256 hex string
    503  Summarizer service not initialised
    """
    _validate_pdf_id(pdf_id)
    workspace_id = _resolve_workspace_id(user)
    summarizer = _get_summarizer(request)

    model_id = summarizer._vl._model_id  # noqa: SLF001 — singleton, stable surface
    cached = await summarizer._load_cached(  # noqa: SLF001
        workspace_id=workspace_id, pdf_id=pdf_id, model_id=model_id,
    )
    if cached is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="assessment_summary_not_cached",
        )
    return cached
