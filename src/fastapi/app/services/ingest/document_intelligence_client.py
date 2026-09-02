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
import contextlib
import logging
import os
import re
import threading
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

logger = logging.getLogger("georag.ingest.document_intelligence")

# `_run_sync` bridges any coroutine onto the persistent DI loop thread —
# a single PageOcrResult for the per-page entry points, a per-page mapping
# for the block one — so its return type travels with the caller.

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

#: How far the outer cap sits beyond the inner one.
#:
#: Derived rather than written twice. As two literals (180.0 / 210.0) the
#: ordering the comment above depends on was a coincidence maintained by
#: hand: raising the analyze cap to 240 for a slow high-resolution
#: deployment left the bridge at 210, so it fired FIRST and every slow page
#: came back as the `di_poller_hung` sentinel instead of a clean fail-soft
#: result with a real error string -- while the comment still said the loop
#: normally wins. The block path already derives its pair
#: (`_block_timeout_seconds(page_count) + 30.0`); this matches it.
_BRIDGE_MARGIN_SECONDS = 30.0
_SYNC_BRIDGE_TIMEOUT_SECONDS = _ANALYZE_TIMEOUT_SECONDS + _BRIDGE_MARGIN_SECONDS

# Analyze options (2026-08-20). prebuilt-layout has supported all three of
# these since the 2024-11-30 GA API and we were requesting none of them —
# we have been paying for layout and taking read-tier output.
#
# `output_content_format=markdown` is the significant one. Without it, page
# text is rebuilt from the `lines` collection: a flat list of visual lines
# with no notion of a heading, a paragraph boundary, or a table. With it,
# Document Intelligence returns the document's semantic structure —
# `#`-prefixed headings, blank-line-separated paragraphs, `<table>` markup
# for merged cells and multirow headers, `<figure>` blocks that keep a
# chart's axis labels attached to its caption. That is materially better
# input for chunking and for the model reading the chunk.
_MARKDOWN_ENV = "AZURE_DI_OUTPUT_MARKDOWN"
# Billed as an add-on per page, so this one is opt-in. It matters for small
# text on geological charts and hand-annotated drill logs — exactly the
# 1940s-70s scanned material in the corpus — but it should be turned on
# deliberately, per-tenant, after looking at what it costs.
_HIGH_RESOLUTION_ENV = "AZURE_DI_OCR_HIGH_RESOLUTION"

# PageHeader/PageFooter/PageNumber/PageBreak metadata is emitted as HTML
# comments in markdown mode. Useful to a renderer, pure noise to an
# embedder — and it drags the alphabetic-character ratio that
# `ocr_quality._assess_ocr_result` scores down for no reason.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


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


from .ocr_types import OcrWord, PageOcrResult  # noqa: E402 — re-exported


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


# Batch analyze (2026-08-20) — Document Intelligence bills and analyzes per
# page, but it does NOT require one HTTP request per page. The per-page
# request shape below dates from the F0 free tier, which rejected
# ``pages=N`` for N > 2; on S0 a single request happily takes a multi-page
# PDF. Measured 2026-08-20: 1,930 DI calls produced 603 billed pages, i.e.
# ~3.2 HTTP round-trips per page once async polling is counted. Batching
# the full-document OCR path into blocks collapses that to
# ceil(pages / block) submissions, which is where the wall-clock win is.
#
# Block size is a latency/blast-radius trade: a bigger block means fewer
# round-trips but a coarser failure unit (a wedged block re-drives every
# one of its pages through the per-page path) and a longer single wait.
_BLOCK_SIZE_ENV = "AZURE_DI_PAGES_PER_BATCH"
_DEFAULT_BLOCK_SIZE = 25
_MAX_BLOCK_SIZE = 100  # well under the S0 2000-page request ceiling

# The per-page cap (_ANALYZE_TIMEOUT_SECONDS) is far too tight for a block:
# 25 pages of prebuilt-layout is comfortably a 2-4 minute analysis. Scale
# the deadline with the block size rather than picking one large constant,
# so a 2-page block still fails fast.
_BLOCK_BASE_TIMEOUT_SECONDS = 60.0
_BLOCK_PER_PAGE_TIMEOUT_SECONDS = 8.0
_BLOCK_MAX_TIMEOUT_SECONDS = 900.0


def pages_per_batch() -> int:
    """Block size for `ocr_page_block_sync`, clamped to [1, _MAX_BLOCK_SIZE].

    ``1`` restores the historical one-request-per-page behavior exactly,
    which is the escape hatch if a tenant's documents turn out to break
    batching (see `_split_result_by_page` for the correctness argument).
    """
    try:
        requested = int(os.environ.get(_BLOCK_SIZE_ENV, str(_DEFAULT_BLOCK_SIZE)))
    except (TypeError, ValueError):
        return _DEFAULT_BLOCK_SIZE
    return max(1, min(_MAX_BLOCK_SIZE, requested))


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def markdown_enabled() -> bool:
    """Whether to ask prebuilt-layout for semantic markdown output."""
    return _env_flag(_MARKDOWN_ENV, True)


def high_resolution_enabled() -> bool:
    """Whether to request the billed `ocrHighResolution` add-on."""
    return _env_flag(_HIGH_RESOLUTION_ENV, False)


def _apply_analyze_options(kwargs: dict[str, Any]) -> None:
    """Attach the output-format and feature options to an analyze request.

    Both are no-ops on `prebuilt-read` (the AZURE_DI_MODEL_ID escape
    hatch), which rejects neither but honours neither, so there is no need
    to branch on the model id.
    """
    from azure.ai.documentintelligence.models import (  # noqa: PLC0415
        DocumentAnalysisFeature,
        DocumentContentFormat,
    )

    if markdown_enabled():
        kwargs["output_content_format"] = DocumentContentFormat.MARKDOWN
    if high_resolution_enabled():
        kwargs["features"] = [DocumentAnalysisFeature.OCR_HIGH_RESOLUTION]


def _block_timeout_seconds(page_count: int) -> float:
    scaled = _BLOCK_BASE_TIMEOUT_SECONDS + (
        _BLOCK_PER_PAGE_TIMEOUT_SECONDS * max(1, page_count)
    )
    return min(_BLOCK_MAX_TIMEOUT_SECONDS, max(_ANALYZE_TIMEOUT_SECONDS, scaled))


async def _submit_analyze(
    body: bytes,
    *,
    pages: str | None,
    log_page: int | None,
    timeout: float,
) -> Any:
    """Submit one analyze request and poll it to completion.

    Raises on terminal failure. The fail-soft translation lives in the
    callers because they disagree about what "failed" should look like:
    one sentinel `PageOcrResult` for the single-page entry points, an
    empty mapping for the block entry point.
    """
    from azure.core.exceptions import HttpResponseError

    # F12 — the client is cached per (endpoint, key) and never closed here;
    # no `async with` so the underlying HTTP session survives across pages.
    client = _build_client()

    kwargs: dict[str, Any] = {"body": body}
    if pages is not None:
        kwargs["pages"] = pages
    _apply_analyze_options(kwargs)

    async def _begin_and_poll():
        # F12 — AsyncLROPoller.result() accepts no timeout parameter,
        # so the submit + polling pair shares one asyncio.wait_for
        # deadline. TimeoutError propagates to the caller (fail-soft).
        poller = await client.begin_analyze_document(_MODEL_ID, **kwargs)
        return await poller.result()

    # Throttle/outage retry (429/503 only): 3 attempts with 1s/2s/4s
    # exponential backoff, honoring a numeric Retry-After header
    # (capped at 30s). Any other error propagates immediately.
    for attempt in range(1, _MAX_REQUEST_ATTEMPTS + 1):
        try:
            return await asyncio.wait_for(_begin_and_poll(), timeout=timeout)
        except HttpResponseError as exc:
            status = getattr(exc, "status_code", None)
            if status not in _RETRYABLE_STATUS_CODES or attempt == _MAX_REQUEST_ATTEMPTS:
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

    # Unreachable: the loop either returns or raises on the final attempt.
    raise RuntimeError("document_intelligence: retry loop exited without a result")


def _log_analyze_failure(
    exc: Exception,
    log_page: int | None,
    *,
    label: str = "",
) -> None:
    # 2026-08-14 — 403 is what Azure returns when the tier quota is
    # exhausted (e.g. F0's 500 pages/month). It is NOT in
    # _RETRYABLE_STATUS_CODES, so before this it fell into the generic
    # warning and the silent tesseract fallback hid the exhaustion.
    if log_page is not None:
        where = f" on page {log_page}"
    elif label:
        where = f" on {label}"
    else:
        where = ""
    if getattr(exc, "status_code", None) == 403:
        logger.error(
            "document_intelligence: HTTP 403%s — DI quota likely exhausted "
            "for the tier. Falling back to tesseract; raise the tier or "
            "wait for the quota window to reset. Error: %s",
            where,
            exc,
        )
    else:
        logger.warning("document_intelligence: OCR failed%s: %s", where, exc)


def _meter_pages(count: int) -> None:
    """Best-effort billed-page metering; must never fail an OCR result.

    2026-08-20 — this used to ``inc()`` once per *request*, which was only
    accurate while every request carried exactly one page. Blocks bill per
    page, so the increment is now the page count.
    """
    with contextlib.suppress(Exception):
        from app.metrics import OCR_PAGES_TOTAL  # noqa: PLC0415
        OCR_PAGES_TOTAL.labels(engine="document_intelligence").inc(max(0, count))


def _result_content(result: Any) -> str | None:
    """The document-level markdown, or None when we didn't ask for it.

    Guarded on `markdown_enabled()` rather than on the attribute alone:
    `result.content` is populated in TEXT mode too, and slicing plain text
    by span would silently swap out the line-based reconstruction (with
    its deliberate newline-per-line shape that the MULTILINE section
    regexes depend on) for a run-together fragment.
    """
    if not markdown_enabled():
        return None
    content = getattr(result, "content", None)
    return content if isinstance(content, str) and content else None


def _markdown_for_pages(sdk_pages: Sequence[Any], content: str) -> str:
    """Cut this page's fragment out of the document-level markdown.

    In markdown mode the semantic output lives in ONE top-level
    `result.content` string for the whole submitted document; `pages[]`
    keeps its words and lines but the structure (headings, paragraph
    breaks, table and figure markup) exists only in `content`. Each page
    carries `spans` — (offset, length) pairs into that string — which is
    what makes a per-page pipeline like ours compatible with a
    document-level output format at all.

    Returns "" when the spans are unusable, which the caller reads as
    "fall back to the line-based reconstruction".
    """
    fragments: list[str] = []
    for page in sdk_pages:
        for span in getattr(page, "spans", None) or []:
            offset = getattr(span, "offset", None)
            length = getattr(span, "length", None)
            if not isinstance(offset, int) or not isinstance(length, int):
                continue
            if offset < 0 or length <= 0 or offset >= len(content):
                continue
            fragments.append(content[offset : offset + length])
    if not fragments:
        return ""
    text = "\n".join(fragment for fragment in fragments if fragment.strip())
    return _HTML_COMMENT_RE.sub("", text).strip()


def _page_ocr_from_sdk_pages(
    sdk_pages: Sequence[Any],
    tables: list[list[list[str]]],
    content: str | None = None,
) -> PageOcrResult:
    """Build one PageOcrResult from a slice of a result's `pages` collection.

    ``content`` is the document-level markdown when markdown output was
    requested. Note that a page's markdown may repeat a table that
    `_extract_tables` also renders as its own chunk — that duplication is
    not new (the old `lines` reconstruction included the table's text too)
    and it is deliberate: the inline copy keeps the table in the prose
    that discusses it, the separate chunk is what a precise numeric query
    retrieves.
    """
    sdk_words = [
        word
        for page in sdk_pages
        for word in (getattr(page, "words", None) or [])
    ]
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
    text = ""
    if content:
        # Markdown mode: prefer the structured fragment. Falls through to
        # the line join below when the spans don't resolve, so a malformed
        # or mocked result degrades to the old behavior rather than to an
        # empty page.
        text = _markdown_for_pages(sdk_pages, content)
    if not text:
        line_texts: list[str] = []
        for page in sdk_pages:
            for line in getattr(page, "lines", None) or []:
                line_content = getattr(line, "content", None)
                if isinstance(line_content, str) and line_content.strip():
                    line_texts.append(line_content.strip())
        text = (
            "\n".join(line_texts)
            if line_texts
            else " ".join(word.text for word in words)
        )
    confidences = [word.confidence for word in words]
    mean_confidence = (sum(confidences) / len(confidences)) if confidences else 0.0
    # Count every detected word region, including empty-content regions that
    # were filtered from output. This makes output coverage sensitive to OCR
    # regions that Azure detected but could not transcribe.
    return PageOcrResult(
        text=text.strip(),
        mean_confidence=_clamp_confidence(mean_confidence),
        words=words,
        detected_region_count=len(sdk_words),
        tables=tables,
    )


def _split_result_by_page(result: Any, page_count: int) -> dict[int, PageOcrResult]:
    """Fan one multi-page analyze result out into per-page results.

    Keys are 1-based page numbers *within the submitted block*, which is
    exactly what DI puts in ``page.page_number`` because the block is
    uploaded as its own standalone PDF. Every page in ``range(1,
    page_count + 1)`` is present in the returned mapping: a page DI
    returned nothing for maps to an empty (but ``request_succeeded=True``)
    result, which is the same signal the single-page path already uses to
    escalate to raster tiling and then tesseract.
    """
    pages_by_number: dict[int, list[Any]] = {}
    for page in getattr(result, "pages", None) or []:
        number = getattr(page, "page_number", None)
        if not isinstance(number, int):
            continue
        pages_by_number.setdefault(number, []).append(page)

    tables_by_number: dict[int, list[list[list[str]]]] = {}
    for page_number, grid in _extract_tables_with_pages(result):
        if page_number is None:
            continue
        tables_by_number.setdefault(page_number, []).append(grid)

    content = _result_content(result)
    return {
        number: _page_ocr_from_sdk_pages(
            pages_by_number.get(number, ()),
            tables_by_number.get(number, []),
            content,
        )
        for number in range(1, page_count + 1)
    }


async def analyze_page_block(
    pdf_bytes: bytes,
    page_count: int,
) -> dict[int, PageOcrResult]:
    """OCR a multi-page PDF block in ONE Document Intelligence request.

    Returns ``{}`` (not a mapping of empty results) when the request
    itself failed, so the caller can tell "the block never ran" apart
    from "the block ran and page 7 came back blank" and re-drive only the
    former through the per-page path.
    """
    try:
        result = await _submit_analyze(
            pdf_bytes,
            pages=None,
            log_page=None,
            timeout=_block_timeout_seconds(page_count),
        )
    except DocumentIntelligenceNotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001
        _log_analyze_failure(exc, None, label=f"a {page_count}-page block")
        return {}

    _meter_pages(page_count)
    return _split_result_by_page(result, page_count)


async def _analyze_document(
    body: bytes,
    *,
    pages: str | None,
    log_page: int | None,
) -> PageOcrResult:
    try:
        result = await _submit_analyze(
            body,
            pages=pages,
            log_page=log_page,
            timeout=_ANALYZE_TIMEOUT_SECONDS,
        )
    except DocumentIntelligenceNotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001
        _log_analyze_failure(exc, log_page)
        return PageOcrResult(
            "",
            0.0,
            request_succeeded=False,
            error=str(exc),
        )

    _meter_pages(1)
    return _page_ocr_from_sdk_pages(
        getattr(result, "pages", None) or [],
        _extract_tables(result),
        _result_content(result),
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


def ocr_page_block_sync(
    pdf_bytes: bytes,
    page_count: int,
) -> dict[int, PageOcrResult]:
    """Synchronous bridge to `analyze_page_block`.

    The bridge deadline tracks the block's own scaled analyze deadline
    (plus the same 30s margin the per-page constants use) so a block
    normally fails *inside* the polling loop — where it degrades to an
    empty mapping — and this outer cap only fires when the transport
    itself is wedged. Both failure shapes are ``{}``, which the caller
    reads as "re-drive these pages one at a time".
    """
    return _run_sync(
        lambda: analyze_page_block(pdf_bytes, page_count),
        timeout=_block_timeout_seconds(page_count) + 30.0,
        on_timeout=dict,
    )


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


def _run_sync[BridgedT](
    coroutine_factory: Callable[[], Awaitable[BridgedT]],
    *,
    timeout: float = _SYNC_BRIDGE_TIMEOUT_SECONDS,
    on_timeout: Callable[[], BridgedT] | None = None,
) -> BridgedT:
    import concurrent.futures

    loop = _get_background_loop()
    future = asyncio.run_coroutine_threadsafe(coroutine_factory(), loop)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # F12 — the poller outlived even the polling cap's margin: cancel
        # the coroutine and fail this page distinctly so the caller falls
        # through to tesseract instead of blocking the parse forever.
        future.cancel()
        logger.warning(
            "document_intelligence: sync bridge exceeded %.0fs — abandoning call",
            timeout,
        )
        if on_timeout is not None:
            return on_timeout()
        return PageOcrResult(  # type: ignore[return-value]
            "",
            0.0,
            request_succeeded=False,
            error="di_poller_hung",
        )


def _extract_tables(result: Any) -> list[list[list[str]]]:
    """Convert a layout result's `tables` collection into row-major grids."""
    return [grid for _page, grid in _extract_tables_with_pages(result)]


def _table_page_number(table: Any) -> int | None:
    """1-based page a table starts on, or None when DI didn't say.

    A table that spans a page break carries several bounding regions; the
    first one is its anchor, which is where the rendered grid belongs.
    """
    for region in getattr(table, "bounding_regions", None) or []:
        number = getattr(region, "page_number", None)
        if isinstance(number, int):
            return number
    return None


def _extract_tables_with_pages(result: Any) -> list[tuple[int | None, list[list[str]]]]:
    """Convert a layout result's `tables` collection into row-major grids.

    Each grid is ``tables[t][row][col]`` of stripped cell text, paired
    with the page it is anchored to so a batched multi-page result can be
    fanned back out per page. Defensive throughout: a missing/None `tables`
    attribute (e.g. the prebuilt-read escape hatch) yields [].

    Spanning cells are propagated across the positions they cover. The
    docstring used to state that they "appear once at their anchor and the
    covered positions stay empty" as though that were the intended shape —
    it is what the SDK gives you, and leaving it there loses real content.

    A scanned resource table typically has a two-row header: row 0 is
    ``Category | Tonnes | Grade (g/t Au)`` spanning three columns |
    ``Contained oz`` spanning two, and row 1 carries
    ``Measured | Indicated | Inferred``. Read anchor-only, that renders as
    ``| Category | Tonnes | Grade (g/t Au) |  |  | Contained oz |  |`` — the
    unit is attached to one of the three grade columns and the other two
    have a blank header. A question about the Inferred grade then retrieves
    a column with no name and no unit.
    """
    grids: list[tuple[int | None, list[list[str]]]] = []
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
            if not content:
                continue

            # Fill every position the cell covers, not just its anchor.
            # Spans default to 1, and a malformed value must not widen the
            # write beyond the grid.
            try:
                row_span = max(1, int(getattr(cell, "row_span", 1) or 1))
                column_span = max(1, int(getattr(cell, "column_span", 1) or 1))
            except (TypeError, ValueError):
                row_span = column_span = 1

            for r in range(row_index, min(row_index + row_span, row_count)):
                for c in range(column_index, min(column_index + column_span, column_count)):
                    # An anchor never overwrites another anchor: if two cells
                    # disagree about a position, the one that owns it wins.
                    if not grid[r][c]:
                        grid[r][c] = content
        grids.append((_table_page_number(table), grid))
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
    "analyze_page_block",
    "is_engine_selected",
    "is_configured",
    "ocr_image",
    "ocr_image_sync",
    "ocr_page",
    "ocr_page_block_sync",
    "ocr_page_sync",
    "pages_per_batch",
]
