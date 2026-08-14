"""Azure Document Intelligence OCR adapter (#28, 2026-07-28).

Azure is selected explicitly through ``OCR_ENGINE``. Tesseract remains
the last-resort fallback in ``pdf_report.py``. The adapter retains
word-level confidence and polygons so oversized pages can be tiled and
reconstructed without losing source coordinates.

Gated by ``OCR_ENGINE`` (default ``"tesseract"`` — i.e. this module is
inert unless something explicitly opts in), mirroring the
``os.environ.get(...)``-based flag convention `pdf_report.py` already
uses for `PDF_PARSER_TESSERACT_FALLBACK_ENABLED` rather than routing
through `app.config.Settings`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("georag.ingest.document_intelligence")

ENDPOINT_ENV = "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
KEY_ENV = "AZURE_DOCUMENT_INTELLIGENCE_KEY"
ENGINE_ENV = "OCR_ENGINE"
# prebuilt-layout supersedes prebuilt-read (2026-08-11): layout returns the
# same content/pages word stream AND a `tables` collection, which is the only
# way scanned tables survive OCR as structure instead of flat word soup.
# ~2x/page cost (product-owner approved); the env var is the escape hatch
# back to "prebuilt-read" if cost bites (tables then simply stay empty).
_MODEL_ID = os.environ.get("AZURE_DI_MODEL_ID", "prebuilt-layout")

# F12 (2026-08-11) — bounded waits. The aio SDK's AsyncLROPoller.result()
# takes no timeout parameter (unlike the sync poller), so the polling wait
# is capped with asyncio.wait_for. The sync-bridge cap is deliberately
# larger than the polling cap so a page normally fails inside the loop
# (clean fail-soft PageOcrResult) and the bridge cap only fires if the
# loop itself is wedged (transport hang before the poller even exists).
# (prebuilt-layout analyzes slower than prebuilt-read, hence the wider caps.)
_ANALYZE_TIMEOUT_SECONDS = 180.0  # per-page cap on begin_analyze + polling
_SYNC_BRIDGE_TIMEOUT_SECONDS = 210.0  # outer cap on the thread bridge


class DocumentIntelligenceNotConfigured(RuntimeError):
    """OCR_ENGINE=azure_document_intelligence but the endpoint/key are absent.

    Raised at call time (not import time) so importing this module never
    requires credentials — only actually invoking `ocr_page` does.
    """


def is_engine_selected() -> bool:
    """True when OCR_ENGINE opts into Azure Document Intelligence.

    Default is "tesseract" (unset behaves the same as "tesseract") so
    this is a strict opt-in — no live behavior changes until an operator
    sets OCR_ENGINE=azure_document_intelligence AND supplies credentials.
    """
    return os.environ.get(ENGINE_ENV, "tesseract").strip().lower() == ("azure_document_intelligence")


def is_configured() -> bool:
    """True when both endpoint and key are present in the environment."""
    return bool(os.environ.get(ENDPOINT_ENV)) and bool(os.environ.get(KEY_ENV))


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One Document Intelligence word with page-local pixel coordinates."""

    text: str
    confidence: float
    polygon: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class PageOcrResult:
    """Same (text, mean_confidence) shape as pdf_report._ocr_single_page
    with return_confidence=True, so a future caller can select an engine
    without changing its own downstream handling.
    """

    text: str
    mean_confidence: float  # 0.0-1.0, averaged over word-level confidences
    words: tuple[OcrWord, ...] = ()
    detected_region_count: int = 0
    request_succeeded: bool = True
    error: str | None = None
    # Row-major text grids from prebuilt-layout: tables[t][row][col].
    # Always [] under prebuilt-read (no `tables` collection in the result)
    # and on the failure sentinels.
    tables: list[list[list[str]]] = field(default_factory=list)


# F12 — one cached client (one HTTP session) per (endpoint, key) instead
# of a fresh DocumentIntelligenceClient per page. The cached client's
# aiohttp session is loop-bound, so all coroutines run on the single
# persistent background loop owned by `_run_sync` (see below); the cache
# is only ever touched from that loop, so no locking is needed. The
# client is deliberately never closed — it lives for the process, like
# the loop thread itself.
_CLIENT_CACHE: dict[tuple[str, str], Any] = {}


def _build_client():
    endpoint = os.environ.get(ENDPOINT_ENV)
    key = os.environ.get(KEY_ENV)
    if not endpoint or not key:
        raise DocumentIntelligenceNotConfigured(
            f"{ENDPOINT_ENV} and {KEY_ENV} must both be set to use the azure_document_intelligence OCR engine."
        )

    cache_key = (endpoint, key)
    client = _CLIENT_CACHE.get(cache_key)
    if client is not None:
        return client

    # Imported lazily so `azure-ai-documentintelligence` being installed
    # doesn't force-import at module load for callers that never use it.
    from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential

    client = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))
    _CLIENT_CACHE[cache_key] = client
    return client


async def ocr_page(pdf_bytes: bytes, page_num: int) -> PageOcrResult:
    """OCR one page of a PDF via Azure Document Intelligence (`_MODEL_ID`,
    prebuilt-layout by default).

    Unlike the Tesseract path, this does not need local rasterisation
    (pdf2image) — Document Intelligence accepts raw PDF bytes plus a
    1-indexed `pages` selector and returns word-level text + confidence
    directly.

    Fails soft (returns an empty PageOcrResult) on any per-call error,
    matching `_ocr_single_page`'s behavior — the only exception this
    raises is `DocumentIntelligenceNotConfigured`, which is a startup/
    config error a caller should surface loudly rather than swallow.
    """
    return await _analyze_document(pdf_bytes, pages=str(page_num), log_page=page_num)


async def ocr_image(image_bytes: bytes) -> PageOcrResult:
    """OCR one bounded raster image and retain word polygons."""

    return await _analyze_document(image_bytes, pages=None, log_page=None)


_RETRYABLE_STATUS_CODES = (429, 503)
_MAX_REQUEST_ATTEMPTS = 3
_RETRY_AFTER_CAP_SECONDS = 30.0


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract a numeric Retry-After header from an azure-core error, if any."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


async def _analyze_document(
    body: bytes,
    *,
    pages: str | None,
    log_page: int | None,
) -> PageOcrResult:
    from azure.core.exceptions import HttpResponseError

    # F12 — the client is cached per (endpoint, key) and never closed here;
    # no `async with` so the underlying HTTP session survives across pages.
    client = _build_client()
    try:
        kwargs: dict[str, Any] = {"body": body}
        if pages is not None:
            kwargs["pages"] = pages

        async def _begin_and_poll():
            # F12 — AsyncLROPoller.result() accepts no timeout parameter,
            # so the submit + polling pair shares one asyncio.wait_for
            # deadline. TimeoutError falls to the outer except (fail-soft).
            poller = await client.begin_analyze_document(_MODEL_ID, **kwargs)
            return await poller.result()

        # Throttle/outage retry (429/503 only): 3 attempts with 1s/2s/4s
        # exponential backoff, honoring a numeric Retry-After header
        # (capped at 30s). Any other error keeps the existing fail-soft
        # behavior via the outer except below.
        for attempt in range(1, _MAX_REQUEST_ATTEMPTS + 1):
            try:
                result = await asyncio.wait_for(
                    _begin_and_poll(), timeout=_ANALYZE_TIMEOUT_SECONDS
                )
                break
            except HttpResponseError as exc:
                status = getattr(exc, "status_code", None)
                if (
                    status not in _RETRYABLE_STATUS_CODES
                    or attempt == _MAX_REQUEST_ATTEMPTS
                ):
                    raise
                delay = float(2 ** (attempt - 1))
                retry_after = _retry_after_seconds(exc)
                if retry_after is not None:
                    delay = min(retry_after, _RETRY_AFTER_CAP_SECONDS)
                logger.warning(
                    "document_intelligence: HTTP %s%s — retrying in %.1fs "
                    "(attempt %d/%d)",
                    status,
                    f" on page {log_page}" if log_page is not None else "",
                    delay,
                    attempt,
                    _MAX_REQUEST_ATTEMPTS,
                )
                await asyncio.sleep(delay)
    except DocumentIntelligenceNotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "document_intelligence: OCR failed%s: %s",
            f" on page {log_page}" if log_page is not None else "",
            exc,
        )
        return PageOcrResult(
            "",
            0.0,
            request_succeeded=False,
            error=str(exc),
        )

    pages = getattr(result, "pages", None) or []
    sdk_words = [word for page in pages for word in (page.words or [])]
    words = tuple(
        OcrWord(
            text=str(word.content),
            confidence=_clamp_confidence(getattr(word, "confidence", None)),
            polygon=_extract_polygon(getattr(word, "polygon", None)),
        )
        for word in sdk_words
        if getattr(word, "content", None)
    )
    # 2026-08-14 — reconstruct page text from DI's `lines` collection
    # instead of one giant space-joined word soup. pdf_report's section
    # regexes (SECTION_HEADING_RE et al) are ^...$ MULTILINE: with no
    # newlines, no heading ever matched on scanned docs, so parse_quality
    # sat at 0 and every chunk was labeled "Document". Word-level
    # confidence/polygon handling (tiling) is intentionally unchanged.
    # Falls back to the word join when no usable lines exist (image tiles
    # from odd models, defensive against mocked results).
    line_texts: list[str] = []
    for page in pages:
        for line in getattr(page, "lines", None) or []:
            content = getattr(line, "content", None)
            if isinstance(content, str) and content.strip():
                line_texts.append(content.strip())
    if line_texts:
        text = "\n".join(line_texts)
    else:
        text = " ".join(word.text for word in words)
    confidences = [word.confidence for word in words]
    mean_confidence = (sum(confidences) / len(confidences)) if confidences else 0.0
    # Count every detected word region, including empty-content regions that
    # were filtered from output. This makes output coverage sensitive to OCR
    # regions that Azure detected but could not transcribe.
    detected_region_count = len(sdk_words)
    return PageOcrResult(
        text=text.strip(),
        mean_confidence=_clamp_confidence(mean_confidence),
        words=words,
        detected_region_count=detected_region_count,
        tables=_extract_tables(result),
    )


def ocr_page_sync(pdf_bytes: bytes, page_num: int) -> PageOcrResult:
    """Synchronous bridge to `ocr_page`, for `pdf_report.py`'s fully sync
    parse pipeline (`_ocr_single_page`, `_attempt_ocr` are plain `def`s,
    not `async def`s — there is no `await` anywhere in that call chain).

    Always runs the coroutine on a dedicated background thread with a
    persistent event loop, rather than `asyncio.run()` directly on the
    calling thread. `asyncio.run()` raises "cannot be called from a
    running event loop" if the caller happens to be invoked from inside
    FastAPI's event loop thread (e.g. a future caller that doesn't route
    parsing through a process/thread pool executor first); the dedicated
    thread makes this safe regardless of the caller's own context. The
    loop is persistent (F12) because the cached client's HTTP session is
    bound to the loop it first ran on.
    """
    return _run_sync(lambda: ocr_page(pdf_bytes, page_num))


def ocr_image_sync(image_bytes: bytes) -> PageOcrResult:
    """Synchronous bridge for tiled raster OCR."""

    return _run_sync(lambda: ocr_image(image_bytes))


# F12 — one persistent background loop thread for all sync-bridged calls.
# A fresh loop per call would strand the cached client's loop-bound HTTP
# session; a persistent loop also lets a hung poller be abandoned (the
# daemon thread keeps running) without blocking the parse pipeline.
_LOOP_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None


def _get_background_loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=loop.run_forever,
                daemon=True,
                name="di-ocr-loop",
            ).start()
            _LOOP = loop
    return _LOOP


def _run_sync(
    coroutine_factory: Callable[[], Awaitable[PageOcrResult]],
) -> PageOcrResult:
    import concurrent.futures

    loop = _get_background_loop()
    future = asyncio.run_coroutine_threadsafe(coroutine_factory(), loop)
    try:
        return future.result(timeout=_SYNC_BRIDGE_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        # F12 — the poller outlived even the polling cap's margin: cancel
        # the coroutine and fail this page distinctly so the caller falls
        # through to tesseract instead of blocking the parse forever.
        future.cancel()
        logger.warning(
            "document_intelligence: sync bridge exceeded %.0fs — abandoning call",
            _SYNC_BRIDGE_TIMEOUT_SECONDS,
        )
        return PageOcrResult(
            "",
            0.0,
            request_succeeded=False,
            error="di_poller_hung",
        )


def _extract_tables(result: Any) -> list[list[list[str]]]:
    """Convert a layout result's `tables` collection into row-major grids.

    Each grid is ``tables[t][row][col]`` of stripped cell text. Spanning
    cells (column_span/row_span > 1) appear once in the SDK's cells[] at
    their anchor (row_index, column_index) — the content lands there and
    the covered positions stay "". Defensive throughout: a missing/None
    `tables` attribute (e.g. the prebuilt-read escape hatch) yields [].
    """
    grids: list[list[list[str]]] = []
    for table in getattr(result, "tables", None) or []:
        try:
            row_count = int(getattr(table, "row_count", 0) or 0)
            column_count = int(getattr(table, "column_count", 0) or 0)
        except (TypeError, ValueError):
            continue
        if row_count <= 0 or column_count <= 0:
            continue
        grid = [["" for _ in range(column_count)] for _ in range(row_count)]
        for cell in getattr(table, "cells", None) or []:
            row_index = getattr(cell, "row_index", None)
            column_index = getattr(cell, "column_index", None)
            if (
                not isinstance(row_index, int)
                or not isinstance(column_index, int)
                or not (0 <= row_index < row_count)
                or not (0 <= column_index < column_count)
            ):
                continue
            content = str(getattr(cell, "content", "") or "").strip()
            if content:
                grid[row_index][column_index] = content
        grids.append(grid)
    return grids


def _clamp_confidence(value: float | None) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _extract_polygon(polygon: Sequence[Any] | None) -> tuple[float, ...]:
    if not polygon:
        return ()

    coordinates: list[float] = []
    for point in polygon:
        if hasattr(point, "x") and hasattr(point, "y"):
            coordinates.extend((float(point.x), float(point.y)))
        elif isinstance(point, Sequence) and not isinstance(point, (str, bytes)):
            if len(point) >= 2:
                coordinates.extend((float(point[0]), float(point[1])))
        else:
            coordinates.append(float(point))
    return tuple(coordinates) if len(coordinates) >= 4 and len(coordinates) % 2 == 0 else ()


__all__ = [
    "ENDPOINT_ENV",
    "KEY_ENV",
    "ENGINE_ENV",
    "DocumentIntelligenceNotConfigured",
    "OcrWord",
    "PageOcrResult",
    "is_engine_selected",
    "is_configured",
    "ocr_image",
    "ocr_image_sync",
    "ocr_page",
    "ocr_page_sync",
]
