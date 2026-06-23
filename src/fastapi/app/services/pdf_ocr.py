"""Stage 5 — Region-targeted OCR via PaddleOCR (PP-OCRv5).

§04p Phase 1.C-ii responsibilities:
  - Accept a pre-computed bbox from the agent (typically from a figure or table
    region whose text was not captured by Phase 1.B's pdfminer/pdfplumber path).
  - Render the cropped region via PdfRenderService.crop_region() FIRST, then
    pass only the PNG bytes to the OCR worker.
  - Cache results durably in silver.pdf_ocr_results keyed on
    (pdf_id, page, bbox, dpi) — same region at a different DPI is a separate
    cache slot because OCR output depends on rendered pixel density.

"Region-targeted only, never full-page by default" (§04p Stage 5)
------------------------------------------------------------------
The public ocr_region() API enforces a positive non-degenerate bbox.
Callers that want full-page OCR must supply an explicit bbox derived from
the page dimensions — the service itself never falls back to full-page
scanning because that defeats the precision-first principle and would
make the provenance bbox meaningless.

Threading model — dedicated ProcessPoolExecutor
-----------------------------------------------
PaddleOCR's first inference call triggers ONNX/PaddlePaddle model loading
(~200-800 MB depending on platform and build).  Running in a dedicated
ProcessPoolExecutor:
  - Isolates the heavy model load from the FastAPI event loop.
  - Prevents PaddleOCR's first-init latency from starving lighter render /
    extract / layout workers.
  - Keeps the OCR pool independent of the other three PDF pools so each
    can saturate independently.

PaddleOCR worker-singleton pattern
-----------------------------------
PaddleOCR objects are NOT picklable — they live entirely inside the worker's
address space.  A module-level ``_OCR_INSTANCE`` variable caches the object
per worker process so model weights are loaded once per worker, not once per
request.  ``_get_ocr_instance()`` is the accessor.

Pickling note
-------------
All worker functions are defined at MODULE LEVEL.  Only plain bytes + primitives
cross the process boundary.  PaddleOCR is lazy-imported inside the worker.

Lifespan integration
--------------------
PdfOcrService is a singleton held on app.state.pdf_ocr_service.
Initialise it in the FastAPI lifespan startup hook after the layout service::

    app.state.pdf_ocr_service = PdfOcrService(
        pool=app.state.pg_pool,
        render_service=app.state.pdf_render_service,
    )

Shut it down before DB pools::

    await app.state.pdf_ocr_service.shutdown()
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

    from app.services.pdf_render import PdfRenderService

logger = logging.getLogger(__name__)

_DEFAULT_OCR_WORKERS = max(1, (os.cpu_count() or 2) // 2)

# ---------------------------------------------------------------------------
# Defensive PaddleOCR import — availability flag prevents hard crash at startup
# ---------------------------------------------------------------------------

try:
    import paddleocr  # noqa: F401 — just to test availability

    _PADDLEOCR_AVAILABLE: bool = True
    logger.debug(
        "paddleocr is available: version=%s",
        getattr(paddleocr, "__version__", "unknown"),
    )
except ImportError:
    _PADDLEOCR_AVAILABLE = False
    logger.warning(
        "paddleocr is not installed — pdf_ocr_service will be unavailable. "
        "Run: uv pip install 'paddlepaddle>=3.1' 'paddleocr>=2.10' to enable OCR."
    )


# ---------------------------------------------------------------------------
# Worker-process singleton (one PaddleOCR instance per worker process)
# ---------------------------------------------------------------------------
# PaddleOCR objects are NOT picklable — they must stay inside the worker's
# address space.  The module-level variable is initialised on first access
# within each worker process and then reused for subsequent requests.

_OCR_INSTANCE = None  # type: ignore[var-annotated]


def _get_ocr_instance():  # type: ignore[return]
    """Return the per-worker PaddleOCR singleton, creating it on first call.

    Lazy-imports paddleocr so the heavy import runs once per worker process
    rather than once at module import time in the parent.
    """
    global _OCR_INSTANCE  # noqa: PLW0603

    if _OCR_INSTANCE is not None:
        return _OCR_INSTANCE

    try:
        import logging as _logging  # noqa: PLC0415
        from paddleocr import PaddleOCR  # noqa: PLC0415
        from app.ocr._paddleocr_gpu import paddleocr_use_gpu  # noqa: PLC0415

        lang = os.environ.get("PDF_OCR_LANG", "en")
        # Phase 9 (2026-05-22) — env-gated GPU acceleration. The helper
        # returns True only when PADDLEOCR_USE_GPU permits, paddle is
        # compiled with CUDA, and ≥ PADDLEOCR_MIN_FREE_VRAM_MB is free
        # on device 0. False otherwise → CPU path unchanged.
        use_gpu = paddleocr_use_gpu()
        # 2026-06-23 sweep — PaddleOCR 3.x migration (ADR-0016):
        #   use_angle_cls=True   -> use_textline_orientation=True
        #   use_gpu=<bool>       -> device="gpu:0" | "cpu"
        #   show_log=False       -> dropped (control via logging module)
        # The legacy kwargs still produce DeprecationWarnings on 3.x and
        # would be hard errors on a future 4.x — moving to the new names
        # now keeps us forward-compatible.
        _logging.getLogger("paddleocr").setLevel(_logging.WARNING)
        _OCR_INSTANCE = PaddleOCR(
            use_textline_orientation=True,
            lang=lang,
            device="gpu:0" if use_gpu else "cpu",
        )
        logger.debug(
            "PaddleOCR instance created in worker (lang=%s, device=%s)",
            lang, "gpu:0" if use_gpu else "cpu",
        )
        return _OCR_INSTANCE
    except ImportError as exc:
        raise RuntimeError(
            "paddleocr is not installed. "
            "Run: uv pip install 'paddlepaddle>=3.1' 'paddleocr>=3.7,<4.0'"
        ) from exc


# ---------------------------------------------------------------------------
# Module-level worker function (picklable — top-level definition)
# ---------------------------------------------------------------------------


def _ocr_worker(crop_png: bytes, dpi: int) -> dict:  # noqa: ARG001
    """Run PaddleOCR PP-OCRv5 on a cropped PNG image.

    Runs inside a ProcessPoolExecutor worker process.  Must be a top-level
    function (picklable by name) — not a method or nested closure.

    PaddleOCR is lazy-imported and held in a per-worker singleton so the
    model weights are loaded once per worker process, not once per request.
    No PaddleOCR objects cross the process boundary — only plain dicts and
    primitives are returned.

    Parameters
    ----------
    crop_png:
        PNG bytes of the rendered region crop (output of PdfRenderService.crop_region).
        The OCR operates entirely on these pixels — the dpi parameter is NOT used
        to re-render; it is accepted as a parameter for logging / future use only.
    dpi:
        DPI at which the crop was rendered.  Stored alongside results for
        provenance but not used inside this worker.

    Returns
    -------
    dict with keys:
        text_content: str   — full OCR text, newline-separated lines
        lines: list[dict]   — per-line: {text, bbox: [x0,y0,x1,y1], confidence}
                              bbox is in pixel coords relative to the crop (top-left origin, y-down)
        mean_confidence: float  — mean of per-line confidences; 1.0 when no lines detected
    """
    import io as _io  # noqa: PLC0415

    ocr = _get_ocr_instance()

    # PaddleOCR >= 2.8 accepts a numpy array or a file-like object.
    # Use numpy for efficiency — avoids a temp file round-trip.
    try:
        import numpy as _np  # noqa: PLC0415
        from PIL import Image as _Image  # noqa: PLC0415

        pil_img = _Image.open(_io.BytesIO(crop_png)).convert("RGB")
        img_array = _np.array(pil_img)
    except ImportError:
        # Fallback: pass the raw bytes — PaddleOCR can open PNG bytes.
        img_array = crop_png  # type: ignore[assignment]

    # 2026-06-23 sweep — PaddleOCR 3.x migration (ADR-0016):
    # `ocr.ocr(img, cls=True)` -> `ocr.predict(img)`. The cls toggle is
    # now controlled at constructor time via `use_textline_orientation`
    # so the per-call argument went away.
    result = ocr.predict(img_array)

    # PaddleOCR 3.x: predict() returns a list of OCRResult objects (one
    # per input image). Each has rec_texts (list[str]), rec_scores
    # (list[float]), rec_boxes (Nx4 ndarray of [x_min, y_min, x_max, y_max]
    # in pixel coords), and rec_polys (4-corner polygons). The 2.x nested
    # `[[bbox, (text, conf)], ...]` shape is gone.
    if not result or result[0] is None:
        return {
            "text_content": "",
            "lines": [],
            "mean_confidence": 1.0,  # no lines detected — no confidence to average
        }

    page = result[0]
    texts = getattr(page, "rec_texts", []) or []
    scores = getattr(page, "rec_scores", []) or []
    boxes = getattr(page, "rec_boxes", None)

    lines: list[dict] = []
    for idx, (text, conf) in enumerate(zip(texts, scores)):
        if not text:
            continue
        # rec_boxes is already axis-aligned in pixel coords — no min/max
        # over corner points needed (that was 2.x bookkeeping).
        if boxes is not None and idx < len(boxes):
            bbox = [
                float(boxes[idx][0]),
                float(boxes[idx][1]),
                float(boxes[idx][2]),
                float(boxes[idx][3]),
            ]
        else:
            bbox = [0.0, 0.0, 0.0, 0.0]
        lines.append({
            "text": str(text),
            "bbox": bbox,
            "confidence": float(conf),
        })

    mean_confidence = (
        sum(ln["confidence"] for ln in lines) / len(lines) if lines else 1.0
    )

    text_content = "\n".join(ln["text"] for ln in lines)

    return {
        "text_content": text_content,
        "lines": lines,
        "mean_confidence": mean_confidence,
    }


# ---------------------------------------------------------------------------
# PdfOcrService singleton
# ---------------------------------------------------------------------------


class PdfOcrService:
    """Stage 5 OCR service — singleton held on app.state.pdf_ocr_service.

    Holds:
      - An asyncpg pool reference for Silver-tier cache reads and writes.
      - A reference to PdfRenderService so it can crop the requested region
        to PNG bytes before passing them to the OCR worker.
      - A dedicated ProcessPoolExecutor for PaddleOCR calls (process workers,
        not threads — PaddleOCR's model loading is CPU-bound and heavy).

    The ProcessPoolExecutor is independent of PdfRenderService's, PdfExtractService's,
    and PdfLayoutService's pools so all four can saturate independently.

    Usage in FastAPI lifespan::

        app.state.pdf_ocr_service = PdfOcrService(
            pool=app.state.pg_pool,
            render_service=app.state.pdf_render_service,
        )
        yield
        await app.state.pdf_ocr_service.shutdown()

    Then in route handlers::

        service = request.app.state.pdf_ocr_service
        result, cache_hit = await service.ocr_region(
            pdf_bytes=pdf_bytes,
            pdf_id=pdf_id,
            page=1,
            bbox=(50.0, 400.0, 550.0, 700.0),
            dpi=300,
        )
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        render_service: PdfRenderService,
    ) -> None:
        if not _PADDLEOCR_AVAILABLE:
            logger.warning(
                "PdfOcrService created but paddleocr is not installed. "
                "All ocr_region() calls will raise RuntimeError."
            )
        self._pool = pool
        self._render_service = render_service
        self._executor = ProcessPoolExecutor(max_workers=_DEFAULT_OCR_WORKERS)
        logger.info(
            "PdfOcrService ready: process_pool_workers=%d paddleocr_available=%s",
            _DEFAULT_OCR_WORKERS,
            _PADDLEOCR_AVAILABLE,
        )

    # -----------------------------------------------------------------------
    # Cache helpers
    # -----------------------------------------------------------------------

    async def _cache_hit(
        self,
        pdf_id: str,
        page: int,
        bbox: tuple[float, float, float, float],
        dpi: int,
        workspace_id: uuid.UUID,
    ) -> dict | None:
        """Check the Silver cache for an existing OCR result.

        Returns the cached row as a raw dict or None on a cache miss.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ocr_id, text_content, lines, mean_confidence, source_method"
                " FROM silver.pdf_ocr_results"
                " WHERE workspace_id = $1"
                "   AND pdf_id = $2"
                "   AND page = $3"
                "   AND region_bbox_x0 = $4"
                "   AND region_bbox_y0 = $5"
                "   AND region_bbox_x1 = $6"
                "   AND region_bbox_y1 = $7"
                "   AND dpi = $8",
                workspace_id, pdf_id, page,
                bbox[0], bbox[1], bbox[2], bbox[3],
                dpi,
            )
        if row is None:
            return None

        import json  # noqa: PLC0415

        lines_raw = row["lines"]
        if isinstance(lines_raw, str):
            lines_raw = json.loads(lines_raw)

        return {
            "ocr_id": str(row["ocr_id"]),
            "text_content": row["text_content"],
            "lines": lines_raw if isinstance(lines_raw, list) else [],
            "mean_confidence": float(row["mean_confidence"]),
            "source_method": row["source_method"],
        }

    async def _persist(
        self,
        pdf_id: str,
        page: int,
        bbox: tuple[float, float, float, float],
        dpi: int,
        ocr_result: dict,
        workspace_id: uuid.UUID,
    ) -> str:
        """Insert a new OCR result into silver.pdf_ocr_results.

        Returns the generated ocr_id (UUID string) so the caller can include
        it in the response without a second round-trip.
        """
        import json  # noqa: PLC0415

        ocr_id = uuid.uuid4()
        lines_json = json.dumps(ocr_result["lines"])
        now = datetime.now(tz=UTC)

        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO silver.pdf_ocr_results"
                " (ocr_id, workspace_id, pdf_id, page,"
                "  region_bbox_x0, region_bbox_y0, region_bbox_x1, region_bbox_y1,"
                "  dpi, text_content, lines, mean_confidence, source_method, extracted_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)"
                " ON CONFLICT DO NOTHING",
                ocr_id,
                workspace_id,
                pdf_id,
                page,
                bbox[0], bbox[1], bbox[2], bbox[3],
                dpi,
                ocr_result["text_content"],
                lines_json,
                ocr_result["mean_confidence"],
                "paddle_ocr",
                now,
            )

        logger.debug(
            "Persisted OCR result ocr_id=%s pdf_id=%s page=%d dpi=%d lines=%d",
            ocr_id, pdf_id[:16], page, dpi, len(ocr_result["lines"]),
        )
        return str(ocr_id)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def ocr_region(
        self,
        pdf_bytes: bytes,
        pdf_id: str,
        page: int,
        bbox: tuple[float, float, float, float],
        workspace_id: uuid.UUID,
        dpi: int = 300,
    ) -> tuple[dict, bool]:
        """Run PaddleOCR PP-OCRv5 on a specific region of a PDF page.

        Region-targeted only — the caller is responsible for providing a
        positive non-degenerate bbox.  This method does NOT validate the bbox
        area; validation is the router's responsibility (§04p "never full-page
        by default" — the router rejects zero-area and inverted bboxes).

        Parameters
        ----------
        pdf_bytes:
            Raw bytes of the normalised PDF (from Bronze store).
        pdf_id:
            SHA-256 hex of the PDF (cache discriminator).
        page:
            1-indexed page number.
        bbox:
            (x0, y0, x1, y1) in PDF user-space points.  This is the REQUESTED
            region and forms part of the cache key — it is stored in
            silver.pdf_ocr_results alongside the OCR output.
        dpi:
            DPI at which the region is rendered before OCR.  Different DPIs
            produce different OCR results and are cached separately.

        Returns
        -------
        (ocr_result_dict, cache_hit)
            ocr_result_dict: dict with keys
                ocr_id, text_content, lines, mean_confidence, source_method
            cache_hit: True if results came from the Silver cache.

        Raises
        ------
        RuntimeError
            If paddleocr is not installed (operator must run uv pip install).
        """
        if not _PADDLEOCR_AVAILABLE:
            raise RuntimeError(
                "paddleocr is not installed — OCR is unavailable. "
                "Run: uv pip install 'paddlepaddle>=3.1' 'paddleocr>=2.10'"
            )

        # 1. Cache check.
        cached = await self._cache_hit(pdf_id, page, bbox, dpi, workspace_id)
        if cached is not None:
            logger.debug(
                "ocr_region cache HIT pdf_id=%s page=%d dpi=%d",
                pdf_id[:16], page, dpi,
            )
            return cached, True

        logger.debug(
            "ocr_region cache MISS pdf_id=%s page=%d bbox=%s dpi=%d — rendering crop",
            pdf_id[:16], page, bbox, dpi,
        )

        # 2. Render the cropped region to PNG bytes.
        crop_png = await self._render_service.crop_region(
            pdf_bytes=pdf_bytes,
            pdf_id=pdf_id,
            page=page,
            bbox=bbox,
            dpi=dpi,
        )

        # 3. Dispatch to OCR worker in a separate process.
        loop = asyncio.get_running_loop()
        ocr_raw: dict = await loop.run_in_executor(
            self._executor,
            _ocr_worker,
            crop_png,
            dpi,
        )

        # 4. Persist to Silver.
        ocr_id = await self._persist(pdf_id, page, bbox, dpi, ocr_raw, workspace_id)

        result = {
            "ocr_id": ocr_id,
            "text_content": ocr_raw["text_content"],
            "lines": ocr_raw["lines"],
            "mean_confidence": ocr_raw["mean_confidence"],
            "source_method": "paddle_ocr",
        }

        logger.debug(
            "ocr_region OK pdf_id=%s page=%d dpi=%d lines=%d mean_conf=%.3f",
            pdf_id[:16], page, dpi,
            len(result["lines"]),
            result["mean_confidence"],
        )
        return result, False

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Shut down the OCR process pool gracefully.

        In-flight OCR tasks complete before the pool shuts down.  Call this in
        the FastAPI lifespan teardown hook before the DB pools are closed.
        """
        self._executor.shutdown(wait=True)
        logger.info("PdfOcrService process pool shut down")
