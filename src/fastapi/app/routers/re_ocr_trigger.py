"""Trigger endpoint for the re_ocr_page Hatchet workflow.

Master-plan §3 Step 8e, doc-phase 63. Laravel's IngestionReviewController
calls this when an operator selects "Re-OCR requested" disposition in
the Silver Review queue.

Auth: shares the X-Service-Key gate with the other /internal routes.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from app.config import settings
from app.hatchet_workflows.re_ocr_page import ReOcrPageInput, re_ocr_page


log = logging.getLogger("georag.re_ocr_trigger")

router = APIRouter(prefix="/internal/v1/re_ocr_page", tags=["re-ocr"])


def _check_service_key(x_service_key: str | None = Header(default=None)) -> None:
    expected = settings.FASTAPI_SERVICE_KEY
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FASTAPI_SERVICE_KEY not configured",
        )
    if x_service_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid X-Service-Key",
        )


class TriggerReOcrResponse(BaseModel):
    workflow_run_id: str
    report_id: str
    page: int


@router.post(
    "/trigger",
    response_model=TriggerReOcrResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_check_service_key)],
)
async def trigger_re_ocr(payload: ReOcrPageInput) -> TriggerReOcrResponse:
    """Trigger the re_ocr_page Hatchet workflow.

    Returns 202 Accepted with workflow_run_id. Caller does NOT wait
    for completion.
    """
    log.info(
        "trigger_re_ocr: workspace=%s report=%s page=%s review_item=%s",
        payload.workspace_id, payload.report_id, payload.page,
        payload.review_item_id,
    )
    ref = await re_ocr_page.aio_run_no_wait(payload)
    return TriggerReOcrResponse(
        workflow_run_id=ref.workflow_run_id,
        report_id=str(payload.report_id),
        page=payload.page,
    )
