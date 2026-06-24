"""Hatchet workflow: TIFF → PDF normalise + trigger ingest_pdf (ADR-0005).

Stream a multi-page TIFF from MinIO, wrap losslessly to PDF via
``tiff_to_pdf``, land the derived PDF under ``bronze/reports/...`` with
provenance metadata, and trigger the existing ``ingest_pdf`` workflow.

The §04p PDF stack (docling layout + tables, PaddleOCR opt-in, tesseract
psm=3 with preprocessing, ocr_confidence capture, figure linking,
p04p_dual_write into the 5 quality tables, ocr_quality_check retry
agent, inline embed dispatch) runs unchanged on the derived PDF.

Idempotency: derived key is deterministic
(``reports/{project_id}/tiff-derived-{sha256:8}-{stem}.pdf``); if the
object exists with a matching ``x-georag-derived-from-tiff-sha256`` tag
we skip normalise and trigger ingest_pdf directly.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional
from uuid import UUID

import boto3
from botocore.config import Config as BotoConfig
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet
from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf
from app.services.ingest.tiff_to_pdf import (
    TiffNormalizeError,
    tiff_to_pdf,
)

log = logging.getLogger("georag.hatchet.tiff_normalize")


_BRONZE_BUCKET = os.environ.get("S3_BUCKET_BRONZE", "bronze")
_REPORTS_PREFIX = "reports"
_TIFF_DERIVED_TAG = "x-georag-derived-from-tiff-sha256"


class TiffNormalizeInput(BaseModel):
    """Trigger payload. Mirrors IngestPdfInput so the Laravel side can
    use a single ``minio_key`` field whether the upload was PDF or TIFF.

    The ``minio_key`` points at the TIFF under ``tiff/{project_id}/...``.
    """

    workspace_id: UUID = Field(..., description="Workspace context for RLS.")
    project_id: str = Field(..., description="Project the upload belongs to.")
    minio_key: str = Field(..., description="Bronze S3 key of the source TIFF.")
    file_size: int = Field(..., description="Bytes (from Laravel multipart upload).")
    vendor_profile_id: int | None = Field(default=None)
    correlation_token: str = Field(
        ...,
        description="Shared token for shadow_runs row pairing — also the dedupe key.",
    )
    actor_id: int | None = Field(default=None, description="public.users.id of uploader.")

    # Defence-in-depth UUID guard on project_id (typed str for downstream
    # ergonomics). Mirrors IngestPdfInput + IngestZipArchiveInput.
    # 2026-06-03 audit — see AUDIT_AND_FIX_REPORT.md Theme G.
    from pydantic import field_validator as _fv

    @_fv("project_id")
    @classmethod
    def _validate_project_id_uuid(cls, v: str) -> str:
        import re as _re
        if not _re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            v,
            _re.IGNORECASE,
        ):
            raise ValueError(
                "TiffNormalizeInput.project_id must be a UUID (canonical 8-4-4-4-12 form)."
            )
        return v


class TiffNormalizeOutput(BaseModel):
    """Workflow output. Captures whether normalise actually ran or was
    skipped on the idempotency check, and the ingest_pdf workflow_run_id
    we delegated to."""

    source_sha256: str
    derived_minio_key: str
    page_count: int
    truncated_at_cap: bool
    normalize_skipped: bool
    ingest_pdf_workflow_run_id: str | None = None


def _s3_client():
    s3_endpoint = os.environ.get("S3_ENDPOINT_URL") or os.environ.get("MINIO_ENDPOINT")
    aws_key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("MINIO_ROOT_USER")
    aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("MINIO_ROOT_PASSWORD")
    if not (s3_endpoint and aws_key and aws_secret):
        raise RuntimeError(
            "tiff_normalize: S3 endpoint / credentials not configured "
            "(S3_ENDPOINT_URL + AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY)",
        )
    return boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=aws_key,
        aws_secret_access_key=aws_secret,
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4"),
    )


_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


def derived_pdf_key(
    project_id: str,
    source_minio_key: str,
    source_sha256: str,
) -> str:
    """Deterministic derived-PDF key.

    Stable across re-runs of the same source — required for idempotency.
    Includes the first 8 hex of the source SHA so two different TIFFs
    with the same filename land at different keys.
    """
    stem = Path(source_minio_key).stem or "tiff"
    safe_stem = _SAFE_STEM_RE.sub("_", stem)[:80]
    sha8 = source_sha256[:8]
    return f"{_REPORTS_PREFIX}/{project_id}/tiff-derived-{sha8}-{safe_stem}.pdf"


def _derived_already_present(
    s3,
    derived_key: str,
    source_sha256: str,
) -> bool:
    """True iff the derived PDF already exists with matching source-SHA.

    A pre-existing key with a different (or missing) source-SHA tag is
    treated as not-derived-from-us; we overwrite to avoid a stale
    collision blocking re-ingest.
    """
    try:
        head = s3.head_object(Bucket=_BRONZE_BUCKET, Key=derived_key)
    except Exception:
        return False
    meta = head.get("Metadata") or {}
    return meta.get(_TIFF_DERIVED_TAG) == source_sha256


tiff_normalize = hatchet.workflow(
    name="tiff_normalize",
    input_validator=TiffNormalizeInput,
)


@tiff_normalize.task(execution_timeout="20m", retries=1)
async def normalize(
    input: TiffNormalizeInput, ctx: Context
) -> TiffNormalizeOutput:
    """Normalise a TIFF to PDF and trigger ingest_pdf.

    Single task — the wrap step is in-memory and bounded by the
    MAX_TIFF_BYTES + MAX_FRAMES caps in ``tiff_to_pdf``. Failures are
    routed to TiffNormalizeError so a hand-malformed TIFF doesn't burn
    Hatchet retries forever.
    """
    log.info(
        "tiff_normalize.start ws=%s project=%s key=%s size=%d",
        input.workspace_id, input.project_id, input.minio_key, input.file_size,
    )

    s3 = _s3_client()

    # 1. Stream the source TIFF down.
    resp = s3.get_object(Bucket=_BRONZE_BUCKET, Key=input.minio_key)
    source_bytes = resp["Body"].read()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    derived_key = derived_pdf_key(input.project_id, input.minio_key, source_sha256)

    # 2. Idempotency — if the derived PDF is already in MinIO with the
    # matching source-sha tag, skip the wrap and trigger ingest_pdf
    # directly. This makes Hatchet retries safe.
    normalize_skipped = _derived_already_present(s3, derived_key, source_sha256)
    page_count = 0
    truncated = False

    if not normalize_skipped:
        # 3. Wrap to PDF (lossless, in-memory).
        try:
            result = tiff_to_pdf(source_bytes)
        except TiffNormalizeError as exc:
            log.warning(
                "tiff_normalize.wrap_failed key=%s err=%s — surfacing for triage",
                input.minio_key, exc,
            )
            raise

        page_count = result.page_count
        truncated = result.truncated_at_cap

        # 4. Upload derived PDF with provenance metadata.
        s3.put_object(
            Bucket=_BRONZE_BUCKET,
            Key=derived_key,
            Body=result.pdf_bytes,
            ContentType="application/pdf",
            Metadata={
                _TIFF_DERIVED_TAG: source_sha256,
                "x-georag-tiff-source-key": input.minio_key,
                "x-georag-tiff-frames": str(result.page_count),
                "x-georag-tiff-truncated": "true" if truncated else "false",
            },
        )

        log.info(
            "tiff_normalize.derived key=%s frames=%d truncated=%s",
            derived_key, page_count, truncated,
        )
    else:
        log.info(
            "tiff_normalize.skip_present derived_key=%s source_sha=%s",
            derived_key, source_sha256[:8],
        )
        # Pull frame count from the metadata for a complete output record.
        head = s3.head_object(Bucket=_BRONZE_BUCKET, Key=derived_key)
        meta = head.get("Metadata") or {}
        try:
            page_count = int(meta.get("x-georag-tiff-frames", "0"))
        except (TypeError, ValueError):
            page_count = 0
        truncated = (meta.get("x-georag-tiff-truncated") == "true")

    # 5. Trigger ingest_pdf against the derived key. We pass through
    # workspace_id / project_id / vendor_profile_id / actor_id /
    # correlation_token unchanged; the only delta is minio_key (now the
    # derived PDF) and file_size (derived PDF size).
    head = s3.head_object(Bucket=_BRONZE_BUCKET, Key=derived_key)
    derived_size = int(head.get("ContentLength") or 0)

    downstream_input = IngestPdfInput(
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        minio_key=derived_key,
        file_size=derived_size,
        vendor_profile_id=input.vendor_profile_id,
        correlation_token=input.correlation_token,
        actor_id=input.actor_id,
    )

    ref = await ingest_pdf.aio_run_no_wait(downstream_input)
    log.info(
        "tiff_normalize.dispatched_ingest_pdf workflow_run_id=%s derived_key=%s",
        ref.workflow_run_id, derived_key,
    )

    return TiffNormalizeOutput(
        source_sha256=source_sha256,
        derived_minio_key=derived_key,
        page_count=page_count,
        truncated_at_cap=truncated,
        normalize_skipped=normalize_skipped,
        ingest_pdf_workflow_run_id=ref.workflow_run_id,
    )


__all__ = [
    "tiff_normalize",
    "TiffNormalizeInput",
    "TiffNormalizeOutput",
    "derived_pdf_key",
]
