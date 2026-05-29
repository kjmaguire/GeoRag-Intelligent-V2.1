"""Stage 3 — Text + layout extraction via pdfminer.six and pdfplumber.

§04p Phase 1.B responsibilities:
  - Extract text blocks with bboxes and font metadata via pdfminer.six
    (primary text source — OCR is fallback only per §04p Stage 3 spec).
  - Extract table matrices with cell-level bboxes via pdfplumber find_tables().
  - Cache results durably in silver.pdf_text_blocks / silver.pdf_table_cells so
    cross-process and cross-restart cache hits avoid redundant extraction.

Threading model — PROCESS workers, not threads
-----------------------------------------------
Both pdfminer.six and pdfplumber are synchronous and CPU-bound.  They benefit
from process isolation rather than thread isolation:
  - GIL is released for I/O but NOT for CPU work inside C extensions.
  - Running in a separate process eliminates the GIL contention entirely.
  - Failures in the worker (e.g., corrupted PDF) cannot crash the FastAPI
    event loop.

A dedicated ProcessPoolExecutor is used (not shared with PdfRenderService).
Render and extract have different cache + saturation profiles — render is
heavier per call but less frequent; extract is lighter per call but may fan
out across many pages in a single agent turn.  Keeping pools independent
avoids priority inversion.

Pickling note
-------------
Functions submitted to ProcessPoolExecutor must be picklable.  All worker
functions in this module are defined at MODULE LEVEL — not inside the class —
so the multiprocessing start method can locate them by qualified name.  Only
plain bytes + primitives cross the process boundary.

Lifespan integration
--------------------
PdfExtractService is a singleton held on app.state.pdf_extract_service.
Initialise it in the FastAPI lifespan startup hook after the asyncpg pool:

    pool = app.state.pg_pool
    app.state.pdf_extract_service = PdfExtractService(pool=pool)

Shut it down before DB pools:

    await app.state.pdf_extract_service.shutdown()

Cache-on-extract pattern
------------------------
Both extract_text() and extract_tables() follow the same pattern:
  1. Check silver tables for an existing cache hit (asyncpg).
  2. On miss: dispatch to ProcessPoolExecutor (blocking extraction).
  3. Bulk-INSERT results into the Silver table.
  4. Return Pydantic models built from the extracted data.

This mirrors Phase 1.A's LRU render cache but uses durable PostgreSQL storage
so results survive process restarts and are shared across uvicorn workers.
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

_DEFAULT_EXTRACT_WORKERS = max(2, (os.cpu_count() or 4) // 2)


# ---------------------------------------------------------------------------
# Module-level worker functions (must be picklable -> top-level definitions)
# ---------------------------------------------------------------------------
# Only plain bytes + primitives are accepted as arguments.
# Library imports happen INSIDE the worker so no unpicklable state crosses
# the process boundary.


def _extract_text_worker(
    pdf_bytes: bytes,
    page_number: int | None,
) -> list[dict]:
    """Extract text blocks from a PDF using pdfminer.six.

    Runs inside a ProcessPoolExecutor worker process.  Must be a top-level
    function (picklable by name) — not a method or nested closure.

    Parameters
    ----------
    pdf_bytes:
        Raw bytes of the normalised PDF (from Bronze store).
    page_number:
        1-indexed page to extract, or None for all pages.

    Returns
    -------
    list of dicts with keys:
        page, bbox_x0, bbox_y0, bbox_x1, bbox_y1, text, font_name, font_size
    """
    try:
        import io as _io  # noqa: PLC0415

        from pdfminer.high_level import extract_pages  # noqa: PLC0415
        from pdfminer.layout import LAParams, LTChar, LTTextBox, LTTextLine  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "pdfminer.six is not installed. "
            "Run: uv pip install 'pdfminer.six>=20240706'"
        ) from exc

    results: list[dict] = []
    pdf_file = _io.BytesIO(pdf_bytes)
    laparams = LAParams()

    # page_numbers is 0-indexed in pdfminer; page_number kwarg is 1-indexed.
    page_numbers: list[int] | None = [page_number - 1] if page_number is not None else None

    for page_layout in extract_pages(pdf_file, laparams=laparams, page_numbers=page_numbers):
        # pdfminer page numbers are 0-indexed internally; we store 1-indexed.
        current_page: int = page_layout.pageid  # pageid is 1-indexed

        for element in page_layout:
            if not isinstance(element, LTTextBox):
                continue

            # Gather font info from the first LTChar in the first line.
            font_name: str | None = None
            font_size: float | None = None
            for line in element:
                if not isinstance(line, LTTextLine):
                    continue
                for char in line:
                    if isinstance(char, LTChar):
                        font_name = char.fontname or None
                        font_size = char.size if char.size > 0 else None
                        break
                if font_name is not None:
                    break

            # pdfminer bbox: (x0, y0, x1, y1) in PDF user-space (y-up, bottom-left origin).
            x0, y0, x1, y1 = element.bbox
            text = element.get_text().strip()
            if not text:
                continue

            results.append({
                "page": current_page,
                "bbox_x0": float(x0),
                "bbox_y0": float(y0),
                "bbox_x1": float(x1),
                "bbox_y1": float(y1),
                "text": text,
                "font_name": font_name,
                "font_size": float(font_size) if font_size is not None else None,
            })

    return results


def _extract_tables_worker(
    pdf_bytes: bytes,
    page_number: int | None,
) -> list[dict]:
    """Extract table matrices with cell-level bboxes using pdfplumber.

    Runs inside a ProcessPoolExecutor worker process.

    Parameters
    ----------
    pdf_bytes:
        Raw bytes of the normalised PDF.
    page_number:
        1-indexed page to process, or None for all pages.

    Returns
    -------
    list of dicts with keys:
        page, table_index, rows, cell_bboxes

    Where:
        rows: list[list[str | None]]        — cell text matrix (None = empty cell)
        cell_bboxes: list[list[tuple]]     — corresponding (x0,y0,x1,y1) per cell
    """
    try:
        import io as _io  # noqa: PLC0415

        import pdfplumber  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "pdfplumber is not installed. "
            "Run: uv pip install 'pdfplumber>=0.11'"
        ) from exc

    results: list[dict] = []
    pdf_file = _io.BytesIO(pdf_bytes)

    with pdfplumber.open(pdf_file) as pdf:
        # Determine which pages to process (pdfplumber is 0-indexed).
        if page_number is not None:
            pages_to_process = [pdf.pages[page_number - 1]] if page_number <= len(pdf.pages) else []
            page_offset = page_number
        else:
            pages_to_process = list(pdf.pages)
            page_offset = 1  # 1-indexed offset for first page

        for page_idx, page in enumerate(pages_to_process):
            current_page = (page_number if page_number is not None else page_offset + page_idx)
            tables = page.find_tables()

            for table_idx, table in enumerate(tables):
                # extract_words() / rows gives cell text; cells gives bboxes.
                rows: list[list[str | None]] = table.extract()  # type: ignore[assignment]
                plumber_cells = table.cells  # list of (x0, top, x1, bottom) per cell

                if rows is None:
                    rows = []

                # pdfplumber cells are in CropBox coords (top-left origin, y-down).
                # We convert to PDF user-space (bottom-left origin, y-up) using
                # the page height.
                page_height = float(page.height)

                # Flatten the cell bbox list to match the rows×cols shape.
                n_rows = len(rows)
                n_cols = max((len(r) for r in rows), default=0)
                total_cells = n_rows * n_cols

                cell_bboxes: list[list[tuple[float, float, float, float]]] = []
                cell_idx = 0
                for row in rows:
                    row_bboxes: list[tuple[float, float, float, float]] = []
                    for _ in row:
                        if cell_idx < len(plumber_cells):
                            c = plumber_cells[cell_idx]
                            # pdfplumber cell: (x0, top, x1, bottom) where top/bottom
                            # are measured from the top of the page (y-down).
                            # Convert to PDF user-space (y-up):
                            #   pdf_y0 = page_height - bottom  (lower edge in user-space)
                            #   pdf_y1 = page_height - top     (upper edge in user-space)
                            x0_c = float(c[0])
                            y0_c = page_height - float(c[3])   # bottom in user-space
                            x1_c = float(c[2])
                            y1_c = page_height - float(c[1])   # top in user-space
                            row_bboxes.append((x0_c, y0_c, x1_c, y1_c))
                        else:
                            # Guard: more cells in rows than in plumber_cells.
                            row_bboxes.append((0.0, 0.0, 0.0, 0.0))
                        cell_idx += 1
                    cell_bboxes.append(row_bboxes)

                results.append({
                    "page": current_page,
                    "table_index": table_idx,
                    "rows": rows,
                    "cell_bboxes": cell_bboxes,
                    "total_cells": total_cells,
                })

    return results


# ---------------------------------------------------------------------------
# PdfExtractService singleton
# ---------------------------------------------------------------------------


class PdfExtractService:
    """Stage 3 extraction service — singleton held on app.state.pdf_extract_service.

    Holds:
      - An asyncpg pool reference for Silver-tier cache reads and writes.
      - A dedicated ProcessPoolExecutor for pdfminer.six / pdfplumber calls
        (process workers, not threads, per §04p CPU-bound extraction spec).

    The ProcessPoolExecutor is independent of PdfRenderService's pool so the
    two can saturate independently without blocking each other.

    Usage in FastAPI lifespan::

        app.state.pdf_extract_service = PdfExtractService(pool=app.state.pg_pool)
        yield
        await app.state.pdf_extract_service.shutdown()

    Then in route handlers::

        service = request.app.state.pdf_extract_service
        blocks = await service.extract_text(pdf_bytes, pdf_id, page=1)
        tables = await service.extract_tables(pdf_bytes, pdf_id, page=None)
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._executor = ProcessPoolExecutor(max_workers=_DEFAULT_EXTRACT_WORKERS)
        logger.info(
            "PdfExtractService ready: process_pool_workers=%d",
            _DEFAULT_EXTRACT_WORKERS,
        )

    # -----------------------------------------------------------------------
    # Text extraction (pdfminer.six)
    # -----------------------------------------------------------------------

    async def _cache_hit_text(
        self,
        pdf_id: str,
        page: int | None,
        workspace_id: uuid.UUID,
    ) -> list[dict] | None:
        """Check the Silver cache for existing text blocks.

        Returns the cached rows as raw dicts (matching the worker output shape),
        or None if the cache is empty for this (workspace_id, pdf_id, page) tuple.
        """
        async with self._pool.acquire() as conn:
            if page is not None:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM silver.pdf_text_blocks"
                    " WHERE workspace_id = $1 AND pdf_id = $2 AND page = $3",
                    workspace_id, pdf_id, page,
                )
                if not count:
                    return None
                rows = await conn.fetch(
                    "SELECT page, bbox_x0, bbox_y0, bbox_x1, bbox_y1,"
                    "       text, font_name, font_size"
                    " FROM silver.pdf_text_blocks"
                    " WHERE workspace_id = $1 AND pdf_id = $2 AND page = $3"
                    " ORDER BY bbox_y1 DESC, bbox_x0",
                    workspace_id, pdf_id, page,
                )
            else:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM silver.pdf_text_blocks"
                    " WHERE workspace_id = $1 AND pdf_id = $2",
                    workspace_id, pdf_id,
                )
                if not count:
                    return None
                rows = await conn.fetch(
                    "SELECT page, bbox_x0, bbox_y0, bbox_x1, bbox_y1,"
                    "       text, font_name, font_size"
                    " FROM silver.pdf_text_blocks"
                    " WHERE workspace_id = $1 AND pdf_id = $2"
                    " ORDER BY page, bbox_y1 DESC, bbox_x0",
                    workspace_id, pdf_id,
                )

        return [dict(r) for r in rows]

    async def _persist_text_blocks(
        self,
        pdf_id: str,
        workspace_id: uuid.UUID,
        blocks: list[dict],
    ) -> None:
        """Bulk-insert extracted text blocks into silver.pdf_text_blocks."""
        if not blocks:
            return

        records = [
            (
                uuid.uuid4(),
                workspace_id,
                pdf_id,
                b["page"],
                b["bbox_x0"],
                b["bbox_y0"],
                b["bbox_x1"],
                b["bbox_y1"],
                b["text"],
                b.get("font_name"),
                b.get("font_size"),
                "pdfminer",
                1.0,
                datetime.now(tz=UTC),
            )
            for b in blocks
        ]

        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO silver.pdf_text_blocks"
                " (block_id, workspace_id, pdf_id, page,"
                "  bbox_x0, bbox_y0, bbox_x1, bbox_y1,"
                "  text, font_name, font_size, source_method, extraction_confidence,"
                "  extracted_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)"
                " ON CONFLICT DO NOTHING",
                records,
            )
        logger.debug(
            "Persisted %d text blocks for pdf_id=%s", len(records), pdf_id[:16]
        )

    async def extract_text(
        self,
        pdf_bytes: bytes,
        pdf_id: str,
        workspace_id: uuid.UUID,
        page: int | None = None,
    ) -> tuple[list[dict], bool]:
        """Extract text blocks from a PDF, with Silver-tier cache.

        Parameters
        ----------
        pdf_bytes:
            Raw bytes of the normalised PDF.
        pdf_id:
            SHA-256 hex of the PDF (cache discriminator).
        workspace_id:
            Tenant workspace UUID. Required — silver.pdf_text_blocks.workspace_id
            is NOT NULL, and the cache is scoped per-workspace to prevent
            cross-tenant cache hits.
        page:
            1-indexed page to extract, or None for all pages.

        Returns
        -------
        (blocks, cache_hit)
            blocks: list of dicts with text block data (matches PdfTextBlock shape)
            cache_hit: True if results came from the Silver cache, False on fresh extraction
        """
        cached = await self._cache_hit_text(pdf_id, page, workspace_id)
        if cached is not None:
            logger.debug(
                "extract_text cache HIT pdf_id=%s page=%s blocks=%d",
                pdf_id[:16], page, len(cached),
            )
            return cached, True

        logger.debug(
            "extract_text cache MISS pdf_id=%s page=%s — dispatching to worker",
            pdf_id[:16], page,
        )
        loop = asyncio.get_running_loop()
        blocks: list[dict] = await loop.run_in_executor(
            self._executor,
            _extract_text_worker,
            pdf_bytes,
            page,
        )

        await self._persist_text_blocks(pdf_id, workspace_id, blocks)
        return blocks, False

    # -----------------------------------------------------------------------
    # Table extraction (pdfplumber)
    # -----------------------------------------------------------------------

    async def _cache_hit_tables(
        self,
        pdf_id: str,
        page: int | None,
        workspace_id: uuid.UUID,
    ) -> list[dict] | None:
        """Check the Silver cache for existing table cells.

        Returns a reconstructed list of table dicts (matching the worker output
        shape), or None if no cells are cached for this (workspace_id, pdf_id, page).
        """
        async with self._pool.acquire() as conn:
            if page is not None:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM silver.pdf_table_cells"
                    " WHERE workspace_id = $1 AND pdf_id = $2 AND page = $3",
                    workspace_id, pdf_id, page,
                )
            else:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM silver.pdf_table_cells"
                    " WHERE workspace_id = $1 AND pdf_id = $2",
                    workspace_id, pdf_id,
                )

            if not count:
                return None

            if page is not None:
                cell_rows = await conn.fetch(
                    "SELECT page, table_index, row_index, col_index,"
                    "       bbox_x0, bbox_y0, bbox_x1, bbox_y1, cell_text"
                    " FROM silver.pdf_table_cells"
                    " WHERE workspace_id = $1 AND pdf_id = $2 AND page = $3"
                    " ORDER BY page, table_index, row_index, col_index",
                    workspace_id, pdf_id, page,
                )
            else:
                cell_rows = await conn.fetch(
                    "SELECT page, table_index, row_index, col_index,"
                    "       bbox_x0, bbox_y0, bbox_x1, bbox_y1, cell_text"
                    " FROM silver.pdf_table_cells"
                    " WHERE workspace_id = $1 AND pdf_id = $2"
                    " ORDER BY page, table_index, row_index, col_index",
                    workspace_id, pdf_id,
                )

        # Reconstruct table dicts from cell rows.
        tables: dict[tuple[int, int], dict] = {}
        for cr in cell_rows:
            key = (cr["page"], cr["table_index"])
            if key not in tables:
                tables[key] = {
                    "page": cr["page"],
                    "table_index": cr["table_index"],
                    "rows": {},
                    "cell_bboxes": {},
                }
            t = tables[key]
            ri, ci = cr["row_index"], cr["col_index"]
            if ri not in t["rows"]:
                t["rows"][ri] = {}
                t["cell_bboxes"][ri] = {}
            t["rows"][ri][ci] = cr["cell_text"]
            t["cell_bboxes"][ri][ci] = (
                float(cr["bbox_x0"]),
                float(cr["bbox_y0"]),
                float(cr["bbox_x1"]),
                float(cr["bbox_y1"]),
            )

        # Convert row dicts to sorted lists.
        result: list[dict] = []
        for t in tables.values():
            rows_dict: dict[int, dict[int, str | None]] = t["rows"]
            bboxes_dict: dict[int, dict[int, tuple]] = t["cell_bboxes"]
            max_row = max(rows_dict.keys(), default=-1)
            max_col = max(
                (max(r.keys(), default=-1) for r in rows_dict.values()),
                default=-1,
            )
            rows_list: list[list[str | None]] = []
            bboxes_list: list[list[tuple[float, ...]]] = []
            for ri in range(max_row + 1):
                row_cells = rows_dict.get(ri, {})
                row_bboxes = bboxes_dict.get(ri, {})
                rows_list.append([row_cells.get(ci) for ci in range(max_col + 1)])
                bboxes_list.append(
                    [row_bboxes.get(ci, (0.0, 0.0, 0.0, 0.0)) for ci in range(max_col + 1)]
                )
            result.append({
                "page": t["page"],
                "table_index": t["table_index"],
                "rows": rows_list,
                "cell_bboxes": bboxes_list,
                "total_cells": (max_row + 1) * (max_col + 1),
            })

        return result

    async def _persist_table_cells(
        self,
        pdf_id: str,
        workspace_id: uuid.UUID,
        tables: list[dict],
    ) -> None:
        """Bulk-insert extracted table cells into silver.pdf_table_cells."""
        if not tables:
            return

        records = []
        now = datetime.now(tz=UTC)
        for t in tables:
            rows: list[list[str | None]] = t["rows"]
            cell_bboxes: list[list[tuple[float, ...]]] = t["cell_bboxes"]
            for ri, row in enumerate(rows):
                for ci, cell_text in enumerate(row):
                    if ri < len(cell_bboxes) and ci < len(cell_bboxes[ri]):
                        bbox = cell_bboxes[ri][ci]
                    else:
                        bbox = (0.0, 0.0, 0.0, 0.0)
                    records.append((
                        uuid.uuid4(),
                        workspace_id,
                        pdf_id,
                        t["page"],
                        t["table_index"],
                        ri,
                        ci,
                        float(bbox[0]),
                        float(bbox[1]),
                        float(bbox[2]),
                        float(bbox[3]),
                        cell_text,
                        "pdfplumber",
                        1.0,
                        now,
                    ))

        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO silver.pdf_table_cells"
                " (cell_id, workspace_id, pdf_id, page, table_index, row_index, col_index,"
                "  bbox_x0, bbox_y0, bbox_x1, bbox_y1,"
                "  cell_text, source_method, extraction_confidence, extracted_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)"
                " ON CONFLICT DO NOTHING",
                records,
            )
        logger.debug(
            "Persisted %d table cells (%d tables) for pdf_id=%s",
            len(records), len(tables), pdf_id[:16],
        )

    async def extract_tables(
        self,
        pdf_bytes: bytes,
        pdf_id: str,
        workspace_id: uuid.UUID,
        page: int | None = None,
    ) -> tuple[list[dict], bool]:
        """Extract table matrices from a PDF, with Silver-tier cache.

        Parameters
        ----------
        pdf_bytes:
            Raw bytes of the normalised PDF.
        pdf_id:
            SHA-256 hex of the PDF (cache discriminator).
        workspace_id:
            Tenant workspace UUID. Required — silver.pdf_table_cells.workspace_id
            is NOT NULL, and the cache is scoped per-workspace.
        page:
            1-indexed page to extract, or None for all pages.

        Returns
        -------
        (tables, cache_hit)
            tables: list of table dicts (matching PdfTable shape)
            cache_hit: True if results came from the Silver cache, False on fresh extraction
        """
        cached = await self._cache_hit_tables(pdf_id, page, workspace_id)
        if cached is not None:
            logger.debug(
                "extract_tables cache HIT pdf_id=%s page=%s tables=%d",
                pdf_id[:16], page, len(cached),
            )
            return cached, True

        logger.debug(
            "extract_tables cache MISS pdf_id=%s page=%s — dispatching to worker",
            pdf_id[:16], page,
        )
        loop = asyncio.get_running_loop()
        tables: list[dict] = await loop.run_in_executor(
            self._executor,
            _extract_tables_worker,
            pdf_bytes,
            page,
        )

        await self._persist_table_cells(pdf_id, workspace_id, tables)
        return tables, False

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Shut down the extract process pool gracefully.

        In-flight extraction tasks complete before the pool shuts down.
        Call this in the FastAPI lifespan teardown hook before the DB pools
        are closed.
        """
        self._executor.shutdown(wait=True)
        logger.info("PdfExtractService process pool shut down")
