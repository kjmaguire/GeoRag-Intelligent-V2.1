"""Hatchet workflow: TIFF → PDF normalise + trigger ingest_pdf (ADR-0005).

Stream a multi-page TIFF (or a standalone scanned image: JPEG, PNG, BMP, GIF,
WebP) from MinIO, wrap losslessly to PDF via ``tiff_to_pdf``, land the derived
PDF under ``bronze/reports/...`` with provenance metadata, and trigger the
existing ``ingest_pdf`` workflow.

The standard PDF parser, OCR provenance capture, figure linking, and
inline embedding dispatch run unchanged on the derived PDF.

Idempotency: derived key is deterministic
(``reports/{project_id}/tiff-derived-{sha256:8}-{stem}.pdf``); if the
object exists with a matching ``derived_from_tiff_sha256`` tag we skip
normalise and trigger ingest_pdf directly.

Metadata keys here are identifier-safe (letters, digits, underscore).
Azure Blob requires C# identifier names and answers HTTP 400
InvalidMetadata for anything containing a hyphen, which is what the
original keys used; georag_object_storage now enforces that rule on
every backend so the S3/MinIO path rejects them identically.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from georag_object_storage import Bucket, ObjectStorage, get_storage_client
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import hatchet
from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf
from app.services.ingest.file_hash import sha256_file
from app.services.ingest.raster_metadata import persist_raster_metadata
from app.services.ingest.tiff_to_pdf import (
    MAX_TIFF_BYTES,
    TiffNormalizeError,
    tiff_to_pdf,
)

log = logging.getLogger("georag.hatchet.tiff_normalize")


_REPORTS_PREFIX = "reports"

#: Source extensions this workflow wraps. Must equal
#: ``UploadController::RASTER_REPORT_EXTS`` (and the ZIP fan-out's
#: ``_RASTER_EXTS``, which has no dot): an extension the API accepts that is
#: missing here fails opaquely inside Pillow, and one missing from the API
#: never arrives. Pinned by tests/test_tiff_normalize_image_formats.py.
SUPPORTED_EXTENSIONS = frozenset({
    ".tif", ".tiff", ".rrd", ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp",
})
# Must be a valid C# identifier to survive Azure Blob (Set Blob Metadata,
# REST 2009-09-19+). See georag_object_storage.metadata.
_TIFF_DERIVED_TAG = "derived_from_tiff_sha256"


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
    #: Frames the source holds (None when an earlier run's derived PDF
    #: predates the tag). With ``truncated_at_cap`` says how many were lost.
    total_frames: int | None = None
    #: Animation frames of a GIF / WebP / APNG that were not made pages.
    frames_ignored: int = 0
    #: Warnings attached to the run; any of them makes the run ``partial``.
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    #: Set when the raster was recorded and deliberately NOT sent through
    #: the OCR stack. Distinct from ``normalize_skipped``, which means the
    #: derived PDF was already present from an earlier run. When this is
    #: set, ``derived_minio_key`` is empty because no PDF was produced.
    ocr_skipped_reason: str | None = None


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
    store: ObjectStorage,
    derived_key: str,
    source_sha256: str,
) -> bool:
    """True iff the derived PDF already exists with matching source-SHA.

    A pre-existing key with a different (or missing) source-SHA tag is
    treated as not-derived-from-us; we overwrite to avoid a stale
    collision blocking re-ingest.
    """
    try:
        head = store.head(Bucket.BRONZE, derived_key)
    except Exception:  # noqa: BLE001 — absent or unreadable both mean "not reusable"
        log.debug("tiff_normalize: no reusable derived PDF at %s", derived_key, exc_info=True)
        return False
    meta = head.get("metadata") or {}
    return meta.get(_TIFF_DERIVED_TAG) == source_sha256


def _frame_warnings(
    *,
    page_count: int,
    total_frames: int | None,
    truncated: bool,
    frames_ignored: int,
) -> list[dict[str, Any]]:
    """Warnings for pages that did not make it into the derived PDF.

    The cap and the first-frame-only rule used to be visible only in the
    workflow output and the PDF's metadata, so the run ended ``completed``
    with the tail of the document missing from the index. Any warning here
    turns the run ``partial`` (``_progress.terminal_status``).
    """
    out: list[dict[str, Any]] = []
    if truncated:
        of_total = f" of {total_frames}" if total_frames else ""
        lost = (
            f" Frames {page_count + 1}-{total_frames} are NOT in the index."
            if total_frames else " The remaining frames are NOT in the index."
        )
        out.append({
            "code": "raster_frames_truncated",
            "detail": (
                f"This multi-page image was read up to its frame limit: "
                f"{page_count}{of_total} frames were converted to pages."
                f"{lost} Split the file and upload the remainder."
            ),
            "pages_read": page_count,
            "total_frames": total_frames,
        })
    if frames_ignored:
        out.append({
            "code": "image_frames_ignored",
            "detail": (
                f"This image has {frames_ignored + 1} frames (an animation); only "
                f"the first was read. {frames_ignored} further frame(s) are not "
                f"in the index. Upload them as separate images if they matter."
            ),
            "frames_ignored": frames_ignored,
        })
    return out


def _measurement_outcome(capture: Any) -> str:
    """First sentence of the raster-skipped message: what really happened to
    the catalogue row. It used to say "Recorded as a raster layer" whatever
    ``capture.written`` was, including when the row was never written."""
    reason = capture.reason or ""
    if capture.written:
        return f"Recorded as a raster layer ({capture.crs})."
    if reason == "already_recorded":
        return f"Already recorded as a raster layer ({capture.crs}) by an earlier attempt."
    if reason.startswith("persist_failed"):
        return (
            "NOT recorded as a raster layer: the database write failed, so its "
            "coordinates, extent and band statistics were not saved."
        )
    if not capture.crs:
        return "NOT recorded as a raster layer: the file carries no CRS."
    return f"NOT recorded as a raster layer ({reason or 'unknown reason'})."


tiff_normalize = hatchet.workflow(
    name="tiff_normalize",
    input_validator=TiffNormalizeInput,
)


# F6 (2026-08-11) — schedule_timeout added so a queue-saturated normalize is
# cancelled by Hatchet (and surfaced via on_failure below) instead of being
# silently dropped with the default schedule window.
@tiff_normalize.task(execution_timeout="20m", schedule_timeout="2h", retries=1)
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

    # F6 (2026-08-11) — flip the dispatch-time row (queued → started) so the
    # IngestionRuns UI shows the normalize as live and the on_failure hook
    # has an active row to close.
    await ingest_progress.mark_started(
        workspace_id=str(input.workspace_id),
        project_id=str(input.project_id),
        minio_key=input.minio_key,
        step="preflight",
        workflow_run_id=getattr(ctx, "workflow_run_id", None),
    )

    suffix = Path(input.minio_key).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise TiffNormalizeError(
            f"tiff_normalize cannot wrap {suffix or 'a file with no extension'!r} "
            f"({input.minio_key}); supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    store = get_storage_client()

    # F6b (2026-08-11) — georag_object_storage is sync boto3; every S3
    # round-trip in this task goes off-loop via asyncio.to_thread so a slow
    # SeaweedFS/SMB call can't starve the Hatchet worker's event loop
    # (same pattern as ingest_pdf.py's figure-persist block).
    # 1. Stream the source TIFF down — to DISK (ING-17). It used to be
    # get_bytes of the whole object: a multi-GB DEM or magnetics grid held in
    # memory on a worker that runs many slots, only for its header to be read.
    # Now the header is read from the file, and the bytes are loaded only for
    # the one path that needs them (the scanned-sheet wrap, capped at
    # MAX_TIFF_BYTES).
    with tempfile.TemporaryDirectory(prefix="georag_tiff_") as tmpdir:
        source_path = str(
            Path(tmpdir) / f"source{Path(input.minio_key).suffix or '.tif'}"
        )
        await asyncio.to_thread(
            store.get_file, Bucket.BRONZE, input.minio_key, source_path,
        )
        return await _normalize_downloaded(input, store, source_path)


async def _normalize_downloaded(
    input: TiffNormalizeInput, store: ObjectStorage, source_path: str,
) -> TiffNormalizeOutput:
    """Everything after the download; *source_path* is removed by the caller."""
    source_sha256 = await asyncio.to_thread(sha256_file, source_path)
    #: The source's bytes, loaded only where a step needs them in memory.
    source_bytes: bytes | None = None

    # An ERDAS .rrd joins the raster path here rather than getting a workflow
    # of its own: the finest pyramid level is extracted to TIFF bytes and
    # everything downstream proceeds unchanged.
    #
    # These are NOT throwaway previews in practice. In the delivery this was
    # written for, neither .rrd's parent raster was present, so the pyramid
    # holds the ONLY copy of the image — a legible 1504x2007 colour geological
    # map and a 364x371 underground mine plan. Refusing them as "rendering
    # companions" loses both.
    #
    # The sha is taken on the ORIGINAL bytes above, so provenance and the
    # idempotency key still identify the file the user actually uploaded.
    if Path(input.minio_key).suffix.lower() == ".rrd":
        from georag_geoparsers.erdas_rrd import rrd_to_tiff_bytes  # noqa: PLC0415

        # The download is already a file; the pyramid read needs no copy.
        source_bytes = await asyncio.to_thread(rrd_to_tiff_bytes, source_path)

        log.info(
            "tiff_normalize.rrd_extracted key=%s tiff_bytes=%d",
            input.minio_key, len(source_bytes),
        )

    derived_key = derived_pdf_key(input.project_id, input.minio_key, source_sha256)

    # 2. Idempotency — if the derived PDF is already in SeaweedFS with the
    # matching source-sha tag, skip the wrap and trigger ingest_pdf
    # directly. This makes Hatchet retries safe. (to_thread: does an S3 head.)
    normalize_skipped = await asyncio.to_thread(
        _derived_already_present, store, derived_key, source_sha256,
    )
    page_count = 0
    truncated = False
    total_frames: int | None = None
    frames_ignored = 0

    # 2b. Capture georeferencing before the wrap throws it away. The PDF
    # container preserves every pixel and no coordinates, so a scanned map
    # sheet would otherwise arrive as a picture with no idea where it is.
    # Additive and non-fatal by construction — see raster_metadata's
    # docstring for why it lives outside this module.
    capture = await persist_raster_metadata(
        source_bytes=source_bytes,
        # An .rrd's extracted TIFF is in memory; anything else is read from
        # the downloaded file, never loaded whole.
        source_path=None if source_bytes is not None else source_path,
        source_key=input.minio_key,
        source_sha256=source_sha256,
        project_id=str(input.project_id),
        workspace_id=str(input.workspace_id),
    )

    # 2c. Measurement raster — record it and stop.
    #
    # ADR-0005 wraps a TIFF to PDF because a scanned map sheet is a
    # picture of a page with text on it. A DEM, an airborne magnetics
    # grid or a multispectral scene is not: there is no text, so
    # the remote OCR engine bills for reading a continuous-tone
    # surface and whatever character noise comes back is chunked,
    # embedded and indexed as retrievable passages that compete in
    # the recall set of every future query, with a citation attached.
    #
    # `persist_raster_metadata` has always read the exact signal
    # needed to tell the two apart — the CRS and the per-band bit
    # depth. The return value was simply discarded.
    if capture.is_measurement_raster:
        # The first sentence is the ACTUAL outcome of the catalogue write:
        # this used to claim "Recorded as a raster layer" even when
        # capture.written was False (no CRS, database failure, retry).
        recorded = capture.written or capture.reason == "already_recorded"
        reason = (
            f"{_measurement_outcome(capture)} Not sent to OCR: "
            "this is a continuous-tone measurement grid, not a scanned sheet, "
            "so it carries no text to extract. "
            + (
                "Its coordinates, extent and band statistics are queryable; "
                "its pixels are not searchable in chat."
                if recorded else
                "Its pixels are not searchable in chat."
            )
        )
        log.info(
            "tiff_normalize.ocr_skipped key=%s crs=%s reason=%s",
            input.minio_key, capture.crs, capture.reason,
        )

        # Terminal write with the reason attached. This lands as
        # 'partial', not 'completed', and that is the intent: the run
        # did everything correctly, and the file the geologist
        # uploaded is still not findable in chat. A green
        # 'Completed' on a document you cannot then retrieve is the
        # worse of the two lies.
        run_id = await ingest_progress.lookup_active_run_id(
            workspace_id=str(input.workspace_id),
            minio_key=input.minio_key,
        )
        if run_id:
            # capture.warnings (raster_crs_missing for a DEM with no CRS) come
            # first: they are the actionable ones.
            raster_warnings = [
                *capture.warnings,
                {"code": "raster_not_ocred", "detail": reason},
            ]
            rows_written = 1 if capture.written else 0
            transitioned = await ingest_progress.mark_completed_by_run(
                run_id=run_id,
                rows_written=rows_written,
                warnings=raster_warnings,
            )
            if transitioned:
                # This branch ends the run here — no derived PDF is
                # dispatched, so nothing downstream will ever report on the
                # user's behalf. Say so, and say why: "we kept the image,
                # we could not read text off it" is the whole outcome, and
                # it reached no product surface at all before this.
                await ingest_progress.broadcast_terminal(
                    workspace_id=str(input.workspace_id),
                    project_id=str(input.project_id),
                    run_id=run_id,
                    stage="parse",
                    status=ingest_progress.terminal_status(
                        rows_written=rows_written, warnings=raster_warnings,
                    ),
                    message=ingest_progress.terminal_message(
                        rows_written=rows_written,
                        warnings=raster_warnings,
                        noun="image",
                    ),
                )

        return TiffNormalizeOutput(
            source_sha256=source_sha256,
            derived_minio_key="",
            page_count=0,
            truncated_at_cap=False,
            normalize_skipped=False,
            ingest_pdf_workflow_run_id=None,
            ocr_skipped_reason=reason,
            warnings=[*capture.warnings, {"code": "raster_not_ocred", "detail": reason}],
        )

    if not normalize_skipped:
        # 3. Wrap to PDF (lossless, in-memory). The size cap is checked on
        # the file first so an oversized scan is refused without loading it.
        if source_bytes is None:
            size = (await asyncio.to_thread(os.stat, source_path)).st_size
            if size > MAX_TIFF_BYTES:
                raise TiffNormalizeError(
                    f"input exceeds {MAX_TIFF_BYTES} bytes ({size})"
                )
            source_bytes = await asyncio.to_thread(Path(source_path).read_bytes)
        try:
            # Off the event loop: decoding and re-encoding every frame is
            # seconds-to-minutes of CPU on a worker's heartbeat thread.
            result = await asyncio.to_thread(tiff_to_pdf, source_bytes)
        except TiffNormalizeError as exc:
            log.warning(
                "tiff_normalize.wrap_failed key=%s err=%s — surfacing for triage",
                input.minio_key, exc,
            )
            raise

        page_count = result.page_count
        truncated = result.truncated_at_cap
        total_frames = getattr(result, "total_frames", None)
        frames_ignored = getattr(result, "frames_ignored", 0)

        # 4. Upload derived PDF with provenance metadata.
        await asyncio.to_thread(
            store.put_bytes,
            Bucket.BRONZE,
            derived_key,
            result.pdf_bytes,
            content_type="application/pdf",
            metadata={
                _TIFF_DERIVED_TAG: source_sha256,
                "tiff_source_key": input.minio_key,
                "tiff_frames": str(result.page_count),
                "tiff_truncated": "true" if truncated else "false",
                "tiff_total_frames": str(total_frames or page_count),
                "tiff_frames_ignored": str(frames_ignored),
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
        head = await asyncio.to_thread(store.head, Bucket.BRONZE, derived_key)
        meta = head.get("metadata") or {}
        try:
            page_count = int(meta.get("tiff_frames", "0"))
        except (TypeError, ValueError):
            log.debug("tiff_normalize: unreadable tiff_frames tag on %s", derived_key, exc_info=True)
            page_count = 0
        truncated = (meta.get("tiff_truncated") == "true")
        # Tags written by this version; a derived PDF from an older run lacks
        # them and then the warning simply omits the counts.
        try:
            total_frames = int(meta["tiff_total_frames"])
        except (KeyError, TypeError, ValueError):
            log.debug("tiff_normalize: no tiff_total_frames tag on %s (older run)", derived_key, exc_info=True)
            total_frames = None
        try:
            frames_ignored = int(meta.get("tiff_frames_ignored", "0"))
        except (TypeError, ValueError):
            log.debug("tiff_normalize: unreadable tiff_frames_ignored tag on %s", derived_key, exc_info=True)
            frames_ignored = 0

    # 5. Trigger ingest_pdf against the derived key. We pass through
    # workspace_id / project_id / vendor_profile_id / actor_id /
    # correlation_token unchanged; the only delta is minio_key (now the
    # derived PDF) and file_size (derived PDF size).
    head = await asyncio.to_thread(store.head, Bucket.BRONZE, derived_key)
    derived_size = int(head.get("size") or 0)

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

    # F6 (2026-08-11) — the source-TIFF progress row ends here: the derived
    # PDF is tracked by its own row (created by ingest_pdf's preflight).
    # Without this terminal write the row would sit non-terminal until the
    # stale sweep timed it out as a false alarm.
    #
    # Pages that did not make it into the derived PDF (frame cap, animation
    # frames) end this row 'partial' with the reason attached, not a green
    # 'Completed' on a document whose tail is missing from the index.
    frame_warnings = _frame_warnings(
        page_count=page_count,
        total_frames=total_frames,
        truncated=truncated,
        frames_ignored=frames_ignored,
    )
    closed_with_warnings = False
    if frame_warnings:
        run_id = await ingest_progress.lookup_active_run_id(
            workspace_id=str(input.workspace_id),
            minio_key=input.minio_key,
        )
        if run_id:
            closed_with_warnings = True
            transitioned = await ingest_progress.mark_completed_by_run(
                run_id=run_id,
                warnings=frame_warnings,
            )
            if transitioned:
                await ingest_progress.broadcast_terminal(
                    workspace_id=str(input.workspace_id),
                    project_id=str(input.project_id),
                    run_id=run_id,
                    stage="parse",
                    status=ingest_progress.terminal_status(
                        rows_written=None, warnings=frame_warnings,
                    ),
                    message=ingest_progress.terminal_message(
                        rows_written=None, warnings=frame_warnings, noun="page",
                    ),
                )
    if not closed_with_warnings:
        await ingest_progress.mark_completed(
            workspace_id=str(input.workspace_id),
            minio_key=input.minio_key,
        )

    return TiffNormalizeOutput(
        source_sha256=source_sha256,
        derived_minio_key=derived_key,
        page_count=page_count,
        truncated_at_cap=truncated,
        normalize_skipped=normalize_skipped,
        ingest_pdf_workflow_run_id=ref.workflow_run_id,
        total_frames=total_frames,
        frames_ignored=frames_ignored,
        warnings=frame_warnings,
    )


# F6 (2026-08-11) — workflow-level failure hook, mirroring ingest_pdf's.
# Before this, a cancelled (queue-saturation) or retries-exhausted normalize
# left its ingest_progress row non-terminal forever: the TIFF just vanished
# from the UI until the stale sweep timed it out with no context.
@tiff_normalize.on_failure_task(
    name="on_failure",
    execution_timeout="30s",
    schedule_timeout="30m",
    retries=2,
)
async def on_failure(input: TiffNormalizeInput, ctx: Context) -> dict:
    """Mark the source-TIFF progress row failed when the workflow dies."""
    from app.services.laravel_bridge import post_ingestion_progress

    workspace_id = str(input.workspace_id)

    run_id = await ingest_progress.lookup_active_run_id(
        workspace_id=workspace_id, minio_key=input.minio_key,
    )
    if run_id is None:
        log.warning(
            "tiff_normalize.on_failure: no active run found for (ws=%s, key=%s) — "
            "skipping terminal update", workspace_id, input.minio_key,
        )
        return {"updated": False, "reason": "no_active_run"}

    row = await ingest_progress.get_run(run_id=run_id)
    current_stage = (row or {}).get("current_stage") or "preflight"

    transitioned = await ingest_progress.mark_failed_by_run(
        run_id=run_id,
        stage=current_stage,
        error="tiff_normalize workflow failure hook fired",
    )

    if transitioned:
        try:
            await post_ingestion_progress(
                workspace_id=workspace_id,
                project_id=str(input.project_id),
                run_id=run_id,
                stage=current_stage,
                status="failed",
                message="TIFF normalise exhausted retries or was cancelled.",
            )
        except Exception as exc:
            log.warning(
                "tiff_normalize.on_failure: broadcast failed run=%s: %s", run_id, exc,
            )

    return {
        "updated": transitioned,
        "run_id": run_id,
        "current_stage": current_stage,
    }


__all__ = [
    "SUPPORTED_EXTENSIONS",
    "tiff_normalize",
    "TiffNormalizeInput",
    "TiffNormalizeOutput",
    "derived_pdf_key",
]
