"""Stage 4 — Layout region detection via Docling.

§04p Phase 1.C-i responsibilities:
  - Detect layout regions (text/figure/table/header/footer/formula/title/
    caption/footnote/list/page_number) from PDF pages using Docling document
    conversion.
  - Map Docling's internal label vocabulary to the GeoRAG CHECK enum.
  - Cache results durably in silver.pdf_layout_regions so cross-process and
    cross-restart cache hits avoid redundant (expensive) Docling inference.

Docling label mapping
---------------------
Docling's DocItem labels are strings from the docling_core.types.doc label set.
The mapping below covers the labels observed in Docling >= 2.13:

  Docling label          → GeoRAG region_type
  ─────────────────────────────────────────────
  text                   → text
  paragraph              → text            (alias)
  picture                → figure
  table                  → table
  section_header         → header
  page_header            → header
  page_footer            → footer
  formula                → formula
  title                  → title
  caption                → caption
  footnote               → footnote
  list_item              → list
  page_number            → page_number
  <anything else>        → unknown

The constant `_DOCLING_LABEL_MAP` is the canonical source of truth.  Any label
NOT present in the map falls through to the 'unknown' bucket — this is
intentional so that new Docling labels introduced in future releases don't crash
the pipeline.

Threading model — PROCESS workers, not threads
-----------------------------------------------
Docling's first inference call is heavy (model load + ONNX JIT) and is CPU-bound.
Running in a separate ProcessPoolExecutor:
  - Eliminates GIL contention for CPU work inside torch/onnxruntime.
  - Isolates model-load failures from the FastAPI event loop.
  - Keeps the layout pool independent of PdfExtractService's pool (different
    saturation profiles — layout is heavier per call).

Pickling note
-------------
The worker function _detect_layout_worker is defined at MODULE LEVEL so
ProcessPoolExecutor can locate it by qualified name.  Only plain bytes +
primitives cross the process boundary — no Docling objects.

Docling availability guard
--------------------------
`_DOCLING_AVAILABLE` is set at module import time.  If docling is not installed,
`PdfLayoutService` raises RuntimeError on any `detect_layout()` call (the router
translates this to 503).  This avoids a hard import-time crash that would
prevent the whole FastAPI app from starting just because Docling is absent.

Lifespan integration
--------------------
PdfLayoutService is a singleton held on app.state.pdf_layout_service.
Initialise it in the FastAPI lifespan startup hook after the asyncpg pool::

    app.state.pdf_layout_service = PdfLayoutService(pool=app.state.pg_pool)

Shut it down before DB pools::

    await app.state.pdf_layout_service.shutdown()

Cache-on-detect pattern
------------------------
detect_layout() follows the same pattern as PdfExtractService.extract_text():
  1. Check silver.pdf_layout_regions for an existing cache hit (asyncpg).
  2. On miss: dispatch to ProcessPoolExecutor (blocking Docling inference).
  3. Bulk-INSERT results into silver.pdf_layout_regions.
  4. Return (regions_dict_list, cache_hit).
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

logger = logging.getLogger(__name__)

_DEFAULT_LAYOUT_WORKERS = max(1, (os.cpu_count() or 2) // 2)

# ---------------------------------------------------------------------------
# Docling label → GeoRAG region_type mapping
# ---------------------------------------------------------------------------
# This is the canonical source of truth for the label translation.
# Docling labels come from docling_core.types.doc.DocItemLabel (an enum of
# strings). We map to the CHECK constraint vocabulary in silver.pdf_layout_regions.
#
# Any label absent from this dict maps to 'unknown' — intentional forward
# compatibility with new Docling releases.

_DOCLING_LABEL_MAP: dict[str, str] = {
    # Primary text body
    "text": "text",
    "paragraph": "text",
    # Visual / figure content
    "picture": "figure",
    # Tables (structural layout — not TableFormer cell extraction)
    "table": "table",
    # Headers — Docling uses distinct labels for section vs page headers
    "section_header": "header",
    "page_header": "header",
    # Footers
    "page_footer": "footer",
    # Mathematical content
    "formula": "formula",
    # Document title (top-level)
    "title": "title",
    # Captions accompanying figures or tables
    "caption": "caption",
    # Footnotes at bottom of page
    "footnote": "footnote",
    # List items (unordered / ordered)
    "list_item": "list",
    # Page number text blocks
    "page_number": "page_number",
}

# ---------------------------------------------------------------------------
# Defensive Docling import — availability flag avoids hard crash at startup
# ---------------------------------------------------------------------------

try:
    import docling  # noqa: F401 — just to test availability

    _DOCLING_AVAILABLE: bool = True
    logger.debug("Docling is available: %s", docling.__version__ if hasattr(docling, "__version__") else "unknown")
except ImportError:
    _DOCLING_AVAILABLE = False
    logger.warning(
        "docling is not installed — pdf_layout_service will be unavailable. "
        "Run: uv pip install 'docling>=2.13' to enable layout detection."
    )


# ---------------------------------------------------------------------------
# Module-level worker function (must be picklable → top-level definition)
# ---------------------------------------------------------------------------


def _detect_layout_worker(
    pdf_bytes: bytes,
    page: int | None,
) -> list[dict]:
    """Detect layout regions in a PDF using Docling document conversion.

    Runs inside a ProcessPoolExecutor worker process.  Must be a top-level
    function (picklable by name) — not a method or nested closure.

    Parameters
    ----------
    pdf_bytes:
        Raw bytes of the normalised PDF (from Bronze store).
    page:
        1-indexed page to detect, or None for all pages.

    Returns
    -------
    list of dicts with keys:
        page, region_index, region_type, bbox_x0, bbox_y0, bbox_x1, bbox_y1,
        region_confidence (float | None)

    The returned dicts use only plain Python primitives + None so they can
    safely cross the process boundary without pickling Docling objects.
    """
    try:
        import io as _io  # noqa: PLC0415

        from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
        from docling.datamodel.document import ConversionResult  # noqa: PLC0415
        from docling.datamodel.pipeline_options import PdfPipelineOptions  # noqa: PLC0415
        from docling.document_converter import DocumentConverter  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "docling is not installed. "
            "Run: uv pip install 'docling>=2.13'"
        ) from exc

    # Configure Docling for layout-only detection (no OCR, no table structure
    # refinement in Phase 1.C-i).  TableFormer is disabled to avoid the extra
    # inference cost — raw table bbox comes from the layout pass only.
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False
    pipeline_options.do_table_structure = False

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: pipeline_options,  # type: ignore[misc]
        }
    )

    pdf_stream = _io.BytesIO(pdf_bytes)
    result: ConversionResult = converter.convert(pdf_stream)
    doc = result.document

    results: list[dict] = []

    # Walk the DocItem tree.  Docling's document.pages is a dict keyed by
    # 1-indexed page numbers.  Each page exposes .size (width, height in pts).
    # DocItems carry .prov (list of ProvenanceItem with page_no, bbox).
    region_counters: dict[int, int] = {}  # per-page 0-indexed counter

    for item, _level in doc.iterate_items():
        # Only items with provenance carry a bbox on a specific page.
        if not hasattr(item, "prov") or not item.prov:
            continue

        for prov in item.prov:
            item_page: int = prov.page_no  # 1-indexed

            # Apply page filter if requested.
            if page is not None and item_page != page:
                continue

            # Get the page object to access its dimensions for coordinate
            # normalisation.  Docling bboxes may be in a normalised [0, 1]
            # coordinate space or in absolute points depending on the version.
            page_obj = doc.pages.get(item_page) if hasattr(doc, "pages") else None
            page_width: float = float(page_obj.size.width) if page_obj and page_obj.size else 1.0
            page_height: float = float(page_obj.size.height) if page_obj and page_obj.size else 1.0

            # Docling BoundingBox: l (left), t (top), r (right), b (bottom)
            # in the page coordinate system (origin top-left, y-down).
            # We convert to PDF user-space (origin bottom-left, y-up).
            bbox = prov.bbox
            if bbox is None:
                continue

            # Docling may expose bbox as normalised [0,1] floats or as
            # absolute points — detect by checking if values exceed 1.0.
            # Normalised path: multiply by page dimensions.
            l_raw = float(bbox.l)
            t_raw = float(bbox.t)
            r_raw = float(bbox.r)
            b_raw = float(bbox.b)

            if l_raw <= 1.0 and r_raw <= 1.0 and t_raw <= 1.0 and b_raw <= 1.0:
                # Normalised coordinates — scale to points.
                l_pts = l_raw * page_width
                r_pts = r_raw * page_width
                t_pts = t_raw * page_height
                b_pts = b_raw * page_height
            else:
                # Already in absolute points.
                l_pts, r_pts, t_pts, b_pts = l_raw, r_raw, t_raw, b_raw

            # Convert from top-left origin (y-down) to PDF user-space (y-up).
            # pdf_y0 = bottom edge in user-space = page_height - b_pts (Docling bottom, y-down)
            # pdf_y1 = top edge in user-space    = page_height - t_pts (Docling top, y-down)
            bbox_x0 = l_pts
            bbox_y0 = page_height - b_pts
            bbox_x1 = r_pts
            bbox_y1 = page_height - t_pts

            # Map Docling label to GeoRAG region_type.
            raw_label: str = item.label.value if hasattr(item.label, "value") else str(item.label)
            region_type: str = _DOCLING_LABEL_MAP.get(raw_label, "unknown")

            # Docling >= 2.x: confidence may not be present on all items.
            region_confidence: float | None = None
            if hasattr(item, "confidence") and item.confidence is not None:
                try:
                    conf_val = float(item.confidence)
                    region_confidence = max(0.0, min(1.0, conf_val))
                except (TypeError, ValueError):
                    region_confidence = None

            # Increment per-page region counter.
            region_index = region_counters.get(item_page, 0)
            region_counters[item_page] = region_index + 1

            results.append({
                "page": item_page,
                "region_index": region_index,
                "region_type": region_type,
                "bbox_x0": bbox_x0,
                "bbox_y0": bbox_y0,
                "bbox_x1": bbox_x1,
                "bbox_y1": bbox_y1,
                "region_confidence": region_confidence,
            })

    return results


# ---------------------------------------------------------------------------
# PdfLayoutService singleton
# ---------------------------------------------------------------------------


class PdfLayoutService:
    """Stage 4 layout detection service — singleton held on app.state.pdf_layout_service.

    Holds:
      - An asyncpg pool reference for Silver-tier cache reads and writes.
      - A dedicated ProcessPoolExecutor for Docling document conversion
        (process workers, not threads, per §04p CPU-bound extraction spec).

    The ProcessPoolExecutor is independent of PdfExtractService's and
    PdfRenderService's pools.  Docling's first inference is heavy (ONNX model
    load); isolation prevents it from starving the lighter pdfminer / pypdfium2
    workers under concurrent load.

    Usage in FastAPI lifespan::

        app.state.pdf_layout_service = PdfLayoutService(pool=app.state.pg_pool)
        yield
        await app.state.pdf_layout_service.shutdown()

    Then in route handlers::

        service = request.app.state.pdf_layout_service
        regions, cache_hit = await service.detect_layout(pdf_bytes, pdf_id, page=1)
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        if not _DOCLING_AVAILABLE:
            logger.warning(
                "PdfLayoutService created but docling is not installed. "
                "All detect_layout() calls will raise RuntimeError."
            )
        self._pool = pool
        self._executor = ProcessPoolExecutor(max_workers=_DEFAULT_LAYOUT_WORKERS)
        logger.info(
            "PdfLayoutService ready: process_pool_workers=%d docling_available=%s",
            _DEFAULT_LAYOUT_WORKERS,
            _DOCLING_AVAILABLE,
        )

    # -----------------------------------------------------------------------
    # Cache helpers
    # -----------------------------------------------------------------------

    async def _cache_hit(
        self,
        pdf_id: str,
        page: int | None,
        workspace_id: uuid.UUID,
    ) -> list[dict] | None:
        """Check the Silver cache for existing layout regions.

        Returns the cached rows as raw dicts (matching the worker output shape),
        or None if no regions are cached for this (workspace_id, pdf_id, page).
        """
        async with self._pool.acquire() as conn:
            if page is not None:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM silver.pdf_layout_regions"
                    " WHERE workspace_id = $1 AND pdf_id = $2 AND page = $3",
                    workspace_id, pdf_id, page,
                )
                if not count:
                    return None
                rows = await conn.fetch(
                    "SELECT region_id, page, region_index, region_type,"
                    "       bbox_x0, bbox_y0, bbox_x1, bbox_y1, region_confidence"
                    " FROM silver.pdf_layout_regions"
                    " WHERE workspace_id = $1 AND pdf_id = $2 AND page = $3"
                    " ORDER BY region_index",
                    workspace_id, pdf_id, page,
                )
            else:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM silver.pdf_layout_regions"
                    " WHERE workspace_id = $1 AND pdf_id = $2",
                    workspace_id, pdf_id,
                )
                if not count:
                    return None
                rows = await conn.fetch(
                    "SELECT region_id, page, region_index, region_type,"
                    "       bbox_x0, bbox_y0, bbox_x1, bbox_y1, region_confidence"
                    " FROM silver.pdf_layout_regions"
                    " WHERE workspace_id = $1 AND pdf_id = $2"
                    " ORDER BY page, region_index",
                    workspace_id, pdf_id,
                )

        return [dict(r) for r in rows]

    async def _persist_regions(
        self,
        pdf_id: str,
        workspace_id: uuid.UUID,
        regions: list[dict],
    ) -> None:
        """Bulk-insert detected layout regions into silver.pdf_layout_regions."""
        if not regions:
            return

        now = datetime.now(tz=UTC)
        records = [
            (
                uuid.uuid4(),
                workspace_id,
                pdf_id,
                r["page"],
                r["region_index"],
                r["region_type"],
                r["bbox_x0"],
                r["bbox_y0"],
                r["bbox_x1"],
                r["bbox_y1"],
                r.get("region_confidence"),
                "docling",
                now,
            )
            for r in regions
        ]

        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO silver.pdf_layout_regions"
                " (region_id, workspace_id, pdf_id, page, region_index, region_type,"
                "  bbox_x0, bbox_y0, bbox_x1, bbox_y1,"
                "  region_confidence, source_method, extracted_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)"
                " ON CONFLICT DO NOTHING",
                records,
            )
        logger.debug(
            "Persisted %d layout regions for pdf_id=%s", len(records), pdf_id[:16]
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def detect_layout(
        self,
        pdf_bytes: bytes,
        pdf_id: str,
        workspace_id: uuid.UUID,
        page: int | None = None,
    ) -> tuple[list[dict], bool]:
        """Detect layout regions in a PDF, with Silver-tier cache.

        Parameters
        ----------
        pdf_bytes:
            Raw bytes of the normalised PDF.
        pdf_id:
            SHA-256 hex of the PDF (cache discriminator).
        workspace_id:
            Tenant workspace UUID. Required — silver.pdf_layout_regions.workspace_id
            is NOT NULL, and the cache is scoped per-workspace.
        page:
            1-indexed page to detect, or None for all pages.

        Returns
        -------
        (regions, cache_hit)
            regions: list of dicts with layout region data (matching
                     PdfLayoutRegion shape)
            cache_hit: True if results came from the Silver cache, False on
                       fresh Docling detection

        Raises
        ------
        RuntimeError
            If docling is not installed (operator must run uv pip install).
        """
        if not _DOCLING_AVAILABLE:
            raise RuntimeError(
                "docling is not installed — layout detection is unavailable. "
                "Run: uv pip install 'docling>=2.13'"
            )

        cached = await self._cache_hit(pdf_id, page, workspace_id)
        if cached is not None:
            logger.debug(
                "detect_layout cache HIT pdf_id=%s page=%s regions=%d",
                pdf_id[:16], page, len(cached),
            )
            return cached, True

        logger.debug(
            "detect_layout cache MISS pdf_id=%s page=%s — dispatching to worker",
            pdf_id[:16], page,
        )
        loop = asyncio.get_running_loop()
        regions: list[dict] = await loop.run_in_executor(
            self._executor,
            _detect_layout_worker,
            pdf_bytes,
            page,
        )

        await self._persist_regions(pdf_id, workspace_id, regions)
        return regions, False

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Shut down the layout detection process pool gracefully.

        In-flight detection tasks complete before the pool shuts down.
        Call this in the FastAPI lifespan teardown hook before the DB pools
        are closed.
        """
        self._executor.shutdown(wait=True)
        logger.info("PdfLayoutService process pool shut down")
