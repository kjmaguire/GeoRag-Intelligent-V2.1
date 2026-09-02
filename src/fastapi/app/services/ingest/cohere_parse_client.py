"""Cohere Parse v5 OCR adapter (ADR-0019, 2026-09-02).

Selected through ``OCR_ENGINE=cohere_parse``; Tesseract remains the
last-resort fallback in ``pdf_report.py``. Replaces the Azure Document
Intelligence adapter with the same public surface, so the parser's fallback
ladder, sparse-page batching and per-document page budget did not have to
change shape — only the engine behind them did.

What Parse is
-------------
A 2.3B vision-language document parser served from the SAME Azure AI
Foundry resource and credentials as Command A+, Embed v4 and Rerank v4
(``AZURE_FOUNDRY_ENDPOINT`` / ``AZURE_FOUNDRY_API_KEY``), deployed as
``AZURE_FOUNDRY_PARSE_DEPLOYMENT`` (catalog id ``Cohere-parse-v5``,
**Preview** — Foundry lists a 2026-12-15 retirement for the preview SKU;
re-check the deployment name before then). Input is ONE page image as a
base64 data URI; output is reading-order text with tables as HTML and
image descriptions.

Wire shape
----------
Mirrors the embed/rerank paths that were verified live on 2026-07-30::

    POST {endpoint}/providers/cohere/v2/parse
    api-key: <key>
    body: {"model": "<deployment>",
           "document": {"type": "image_url",
                        "image_url": {"url": "data:image/png;base64,..."}},
           "output_format": "blocks" | "markdown"}
    -> {"pages": [{"blocks": [{"type": "text"|"table"|"image", ...}]}]}
       or {"pages": [{"markdown": "..." | {"content": "...", "images": [...]}}]}

NOT YET EMPIRICALLY VERIFIED against a live deployment — run
``ops/validation/cohere_parse_probe.py`` with real credentials and update
this docstring, ``_PARSE_PATH`` and ``_page_from_payload`` from its report.
Until then the response adapter is tolerant about field names (``text`` /
``content`` / ``markdown`` for text, ``html`` / ``content`` for tables,
``description`` / ``caption`` for images).

What Parse does NOT return
--------------------------
No per-word confidence and no word polygons. ``PageOcrResult`` therefore
carries ``confidence_reported=False``, ``words=()`` and
``mean_confidence=0.0``; the quality router judges these pages on content
signals only and the persist path stores ``ocr_confidence`` as NULL.
Oversized plan sheets are DOWNSCALED to ``COHERE_PARSE_MAX_PIXELS`` rather
than tiled — there are no polygons to stitch tiles back together with —
and a warning is logged when that costs resolution.

Gated by ``OCR_ENGINE`` (default ``"tesseract"``), reading ``os.environ``
at call time like the adapter it replaces, so importing this module never
requires credentials.
"""

from __future__ import annotations

import base64
import contextlib
import io
import logging
import math
import os
import re
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.services._foundry_retry import with_foundry_retry

from . import ocr_engine
from .html_table import find_table_fragments, html_table_to_grid
from .ocr_types import OcrWord, PageOcrResult

logger = logging.getLogger("georag.ingest.cohere_parse")

ENGINE_VALUE = ocr_engine.COHERE_PARSE
ENDPOINT_ENV = "AZURE_FOUNDRY_ENDPOINT"
KEY_ENV = "AZURE_FOUNDRY_API_KEY"
DEPLOYMENT_ENV = "AZURE_FOUNDRY_PARSE_DEPLOYMENT"
OCR_METHOD = "cohere_parse"

_PARSE_PATH = "/providers/cohere/v2/parse"

_TIMEOUT_ENV = "COHERE_PARSE_TIMEOUT_S"
_DEFAULT_TIMEOUT_S = 120.0

# Pixel ceiling for the rendered page. Parse takes a single page image; the
# 4 MP default renders a US Letter page at ~210 DPI (comfortably OCR
# resolution) and an A0 plan sheet at ~55 DPI. Set from the probe once the
# live limit is known — a request over the vendor's cap fails as a 4xx and
# the page falls back to tesseract, so a too-high value is loud, not silent.
_MAX_PIXELS_ENV = "COHERE_PARSE_MAX_PIXELS"
_DEFAULT_MAX_PIXELS = 4_000_000
#: Below this DPI the render has visibly lost text a scanner captured.
_DOWNSCALE_WARN_DPI = 100.0

_OUTPUT_FORMAT_ENV = "COHERE_PARSE_OUTPUT_FORMAT"
_OUTPUT_FORMATS = ("blocks", "markdown")
_DEFAULT_OUTPUT_FORMAT = "blocks"

# Parse describes every figure it sees. Those descriptions can contain
# transcribed numbers with no confidence behind them — precisely what the
# page-image verbalizer is forbidden from doing — so they stay OUT of the
# retrievable text unless an operator opts in.
_IMAGE_DESCRIPTIONS_ENV = "COHERE_PARSE_INCLUDE_IMAGE_DESCRIPTIONS"

# "Pages per batch" survives from the Document Intelligence era, where it
# was pages per HTTP request. Parse takes one page per request, so a batch
# is now a group of pages rendered together and posted concurrently; the
# number of requests in flight is capped by PDF_OCR_PAGE_CONCURRENCY.
_BLOCK_SIZE_ENV = "OCR_PAGES_PER_BATCH"
_DEFAULT_BLOCK_SIZE = 8
_MAX_BLOCK_SIZE = 32
_CONCURRENCY_ENV = "PDF_OCR_PAGE_CONCURRENCY"
_DEFAULT_CONCURRENCY = 4

_PDF_POINTS_PER_INCH = 72.0

_MARKDOWN_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\([^)]*\)")


class CohereParseNotConfigured(RuntimeError):
    """OCR_ENGINE=cohere_parse but the Foundry endpoint/key/deployment are absent.

    Raised at call time (not import time) so importing this module never
    requires credentials — only actually invoking ``ocr_page_sync`` does.
    """


# ---------------------------------------------------------------------------
# Selection and configuration
# ---------------------------------------------------------------------------


def is_engine_selected() -> bool:
    """True when OCR_ENGINE opts into Cohere Parse (strict opt-in)."""
    return ocr_engine.selected_engine() == ENGINE_VALUE


def is_configured() -> bool:
    """True when endpoint, key and deployment are all present."""
    return all(
        bool(os.environ.get(name)) for name in (ENDPOINT_ENV, KEY_ENV, DEPLOYMENT_ENV)
    )


def _require_config() -> tuple[str, str, str]:
    endpoint = os.environ.get(ENDPOINT_ENV, "")
    key = os.environ.get(KEY_ENV, "")
    deployment = os.environ.get(DEPLOYMENT_ENV, "")
    if not endpoint or not key or not deployment:
        raise CohereParseNotConfigured(
            f"{ENDPOINT_ENV}, {KEY_ENV} and {DEPLOYMENT_ENV} must all be set to use the "
            f"{ENGINE_VALUE} OCR engine."
        )
    return endpoint, key, deployment


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def timeout_seconds() -> float:
    return max(1.0, _env_float(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_S))


def max_pixels() -> int:
    return max(100_000, _env_int(_MAX_PIXELS_ENV, _DEFAULT_MAX_PIXELS))


def output_format() -> str:
    raw = (os.environ.get(_OUTPUT_FORMAT_ENV) or _DEFAULT_OUTPUT_FORMAT).strip().lower()
    if raw not in _OUTPUT_FORMATS:
        logger.warning(
            "cohere_parse: %s=%r is not one of %s — using %r",
            _OUTPUT_FORMAT_ENV,
            raw,
            _OUTPUT_FORMATS,
            _DEFAULT_OUTPUT_FORMAT,
        )
        return _DEFAULT_OUTPUT_FORMAT
    return raw


def include_image_descriptions() -> bool:
    return _env_flag(_IMAGE_DESCRIPTIONS_ENV, False)


def pages_per_batch() -> int:
    """Pages rendered together and posted concurrently, clamped to [1, 32]."""
    return max(1, min(_MAX_BLOCK_SIZE, _env_int(_BLOCK_SIZE_ENV, _DEFAULT_BLOCK_SIZE)))


def page_concurrency() -> int:
    return max(1, _env_int(_CONCURRENCY_ENV, _DEFAULT_CONCURRENCY))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# pypdfium2 is not documented as thread-safe. Rendering is serialised here;
# the network calls that follow are what run concurrently.
_RENDER_LOCK = threading.Lock()


def _dpi_for(width_points: float, height_points: float, cap: int) -> float:
    from .page_image import dpi_for_page  # noqa: PLC0415

    return dpi_for_page(width_points, height_points, max_pixels=cap)


def _render_pages(pdf_path: str, page_numbers: Sequence[int]) -> dict[int, bytes]:
    """Render each 1-indexed page to PNG under the pixel cap; open the file once.

    Raises on a file that cannot be opened at all; a page that fails to
    render is simply absent from the mapping (logged), so one bad page does
    not sink its group.
    """
    import pypdfium2 as pdfium  # noqa: PLC0415 — heavy import, lazy

    cap = max_pixels()
    rendered: dict[int, bytes] = {}
    with _RENDER_LOCK:
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            page_count = len(pdf)
            for page_number in page_numbers:
                if not 1 <= page_number <= page_count:
                    logger.warning(
                        "cohere_parse: page %d is outside 1..%d of '%s' — skipped",
                        page_number,
                        page_count,
                        pdf_path,
                    )
                    continue
                try:
                    page = pdf[page_number - 1]
                    try:
                        width_points, height_points = page.get_size()
                        dpi = _dpi_for(width_points, height_points, cap)
                        if dpi < _DOWNSCALE_WARN_DPI:
                            logger.warning(
                                "cohere_parse: page %d of '%s' is %.0fx%.0f pt — "
                                "downscaled to %.0f DPI to fit %d px; small text "
                                "may be lost (no tiling without word polygons)",
                                page_number,
                                pdf_path,
                                width_points,
                                height_points,
                                dpi,
                                cap,
                            )
                        bitmap = page.render(
                            scale=dpi / _PDF_POINTS_PER_INCH, rotation=0
                        )
                        image = bitmap.to_pil()
                    finally:
                        with contextlib.suppress(Exception):
                            page.close()
                    # Trust but verify — PIL rounding is the one thing between
                    # our arithmetic and a vendor-side 4xx.
                    if image.width * image.height > cap:
                        shrink = math.sqrt(cap / (image.width * image.height))
                        image = image.resize(
                            (
                                max(1, int(image.width * shrink)),
                                max(1, int(image.height * shrink)),
                            )
                        )
                    buf = io.BytesIO()
                    image.save(buf, format="PNG", optimize=False)
                    rendered[page_number] = buf.getvalue()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "cohere_parse: render failed for page %d of '%s': %s",
                        page_number,
                        pdf_path,
                        exc,
                    )
        finally:
            with contextlib.suppress(Exception):
                pdf.close()
    return rendered


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

_CLIENT_LOCK = threading.Lock()
_CLIENT: Any = None
_CLIENT_TIMEOUT: float | None = None


def _http_client():
    """One pooled ``httpx.Client`` per process (parsing runs in a subprocess)."""
    global _CLIENT, _CLIENT_TIMEOUT
    import httpx  # noqa: PLC0415

    timeout = timeout_seconds()
    with _CLIENT_LOCK:
        if _CLIENT is None or timeout != _CLIENT_TIMEOUT:
            _CLIENT = httpx.Client(timeout=timeout)
            _CLIENT_TIMEOUT = timeout
        return _CLIENT


def _post(url: str, headers: dict[str, str], body: dict[str, Any]):
    """The single network seam; tests replace this."""
    return _http_client().post(url, headers=headers, json=body)


def _request_body(deployment: str, png_bytes: bytes) -> dict[str, Any]:
    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return {
        "model": deployment,
        "document": {"type": "image_url", "image_url": {"url": data_uri}},
        "output_format": output_format(),
    }


def _parse_png(png_bytes: bytes, *, log_page: int | None) -> PageOcrResult:
    """POST one rendered page; fail soft on any error except NotConfigured."""
    endpoint, key, deployment = _require_config()
    url = endpoint.rstrip("/") + _PARSE_PATH
    headers = {"api-key": key}
    body = _request_body(deployment, png_bytes)
    where = f" on page {log_page}" if log_page is not None else ""

    def _do():
        return _post(url, headers, body)

    try:
        resp = with_foundry_retry(_do, label="foundry_parse")
    except Exception as exc:  # noqa: BLE001
        status = getattr(getattr(exc, "response", None), "status_code", None)
        text = ""
        with contextlib.suppress(Exception):
            text = (getattr(exc.response, "text", "") or "")[:200]  # type: ignore[attr-defined]
        if status == 403:
            logger.error(
                "cohere_parse: HTTP 403%s — Foundry quota or key rejected for %s. "
                "Falling back to tesseract. Error: %s",
                where,
                deployment,
                text or exc,
            )
        else:
            logger.warning("cohere_parse: request failed%s: %s %s", where, exc, text)
        return PageOcrResult(
            "",
            0.0,
            request_succeeded=False,
            error=f"{status}: {text}" if status is not None else str(exc),
            confidence_reported=False,
        )

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("cohere_parse: non-JSON response%s: %s", where, exc)
        return PageOcrResult(
            "",
            0.0,
            request_succeeded=False,
            error=f"non_json_response: {exc}",
            confidence_reported=False,
        )

    _meter_pages(1)
    return _page_from_payload(payload)


def _meter_pages(count: int) -> None:
    """Best-effort billed-page metering; must never fail an OCR result."""
    with contextlib.suppress(Exception):
        from app.metrics import OCR_PAGES_TOTAL  # noqa: PLC0415

        OCR_PAGES_TOTAL.labels(engine=OCR_METHOD).inc(max(0, count))


# ---------------------------------------------------------------------------
# Response adapter
# ---------------------------------------------------------------------------


def _first(mapping: Any, *keys: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _table_markdown(grid: list[list[str]]) -> str:
    from .pdf_report import _table_to_markdown  # noqa: PLC0415 — lazy, avoids a cycle

    try:
        # The renderer accepts Optional cells (pdfplumber yields None); ours
        # are always str, and list invariance needs the copy to say so.
        return _table_to_markdown([list(row) for row in grid])
    except Exception:  # noqa: BLE001 — a renderer bug must not lose the page
        return "\n".join(" | ".join(row) for row in grid)


def _page_from_payload(payload: Any) -> PageOcrResult:
    """Turn one Parse response into the engine-neutral page result."""
    pages = _first(payload, "pages") if isinstance(payload, dict) else None
    page: Any = None
    if isinstance(pages, list) and pages:
        page = pages[0]
    elif isinstance(payload, dict) and ("blocks" in payload or "markdown" in payload):
        page = payload

    if not isinstance(page, dict):
        return PageOcrResult("", 0.0, confidence_reported=False)

    blocks = page.get("blocks")
    if isinstance(blocks, list):
        return _page_from_blocks(blocks)
    return _page_from_markdown(page.get("markdown"))


def _page_from_blocks(blocks: list[Any]) -> PageOcrResult:
    parts: list[str] = []
    tables: list[list[list[str]]] = []
    describe_images = include_image_descriptions()

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "text").strip().lower()
        if kind == "table":
            html_fragment = _first(block, "html", "content", "text")
            grid = html_table_to_grid(str(html_fragment)) if html_fragment else []
            if grid:
                tables.append(grid)
                parts.append(_table_markdown(grid))
            elif html_fragment:
                parts.append(str(html_fragment).strip())
        elif kind in {"image", "figure", "picture"}:
            if describe_images:
                description = _first(block, "description", "caption", "text")
                if description:
                    parts.append(f"[Figure: {str(description).strip()}]")
        else:
            text = _first(block, "text", "content", "markdown")
            if text:
                parts.append(str(text).strip())

    text = "\n\n".join(part for part in parts if part).strip()
    return _result(text, tables)


def _page_from_markdown(markdown: Any) -> PageOcrResult:
    content = (
        _first(markdown, "content", "text", "markdown")
        if isinstance(markdown, dict)
        else markdown
    )
    if not isinstance(content, str) or not content.strip():
        return PageOcrResult("", 0.0, confidence_reported=False)

    tables: list[list[list[str]]] = []
    text = content
    for fragment in find_table_fragments(content):
        grid = html_table_to_grid(fragment)
        if grid:
            tables.append(grid)
            text = text.replace(fragment, _table_markdown(grid), 1)

    if include_image_descriptions():
        text = _MARKDOWN_IMAGE_RE.sub(
            lambda m: (
                f"[Figure: {m.group('alt').strip()}]" if m.group("alt").strip() else ""
            ),
            text,
        )
    else:
        text = _MARKDOWN_IMAGE_RE.sub("", text)

    return _result(text.strip(), tables)


def _result(text: str, tables: list[list[list[str]]]) -> PageOcrResult:
    return PageOcrResult(
        text=text,
        mean_confidence=0.0,
        words=(),
        detected_region_count=0,
        tables=tables,
        confidence_reported=False,
    )


# ---------------------------------------------------------------------------
# Public entry points (same surface the parser used for Document Intelligence)
# ---------------------------------------------------------------------------


def ocr_page_sync(pdf_path: str, page_num: int) -> PageOcrResult:
    """OCR one page of the PDF at ``pdf_path`` via Cohere Parse.

    Fails soft (``request_succeeded=False``) on render or transport errors;
    the only exception it raises is ``CohereParseNotConfigured``, which a
    caller should surface loudly rather than swallow.
    """
    _require_config()
    try:
        rendered = _render_pages(pdf_path, [page_num])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cohere_parse: could not open '%s' for page %d: %s", pdf_path, page_num, exc
        )
        return PageOcrResult(
            "",
            0.0,
            request_succeeded=False,
            error=f"render_failed: {exc}",
            confidence_reported=False,
        )
    png = rendered.get(page_num)
    if png is None:
        return PageOcrResult(
            "",
            0.0,
            request_succeeded=False,
            error="render_failed",
            confidence_reported=False,
        )
    return _parse_png(png, log_page=page_num)


def ocr_page_block_sync(
    pdf_path: str, page_numbers: Sequence[int]
) -> dict[int, PageOcrResult]:
    """OCR a group of pages: render together, post concurrently.

    Returns ``{absolute_page_number: PageOcrResult}`` for the pages whose
    request succeeded. A page that is absent must be re-driven by the
    caller (render failed, request failed); a page that is present with
    empty text ran and came back blank, which is a different — cheaper —
    situation. Returns ``{}`` when the file cannot be opened at all.
    """
    _require_config()
    ordered = sorted(set(int(n) for n in page_numbers))
    if not ordered:
        return {}
    try:
        rendered = _render_pages(pdf_path, ordered)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cohere_parse: could not open '%s' for a %d-page group: %s",
            pdf_path,
            len(ordered),
            exc,
        )
        return {}
    if not rendered:
        return {}

    def _one(item: tuple[int, bytes]) -> tuple[int, PageOcrResult]:
        page_number, png = item
        return page_number, _parse_png(png, log_page=page_number)

    workers = max(1, min(page_concurrency(), len(rendered)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_one, sorted(rendered.items())))

    return {
        page_number: result
        for page_number, result in results
        if result.request_succeeded
    }


__all__ = [
    "DEPLOYMENT_ENV",
    "ENDPOINT_ENV",
    "ENGINE_VALUE",
    "KEY_ENV",
    "OCR_METHOD",
    "CohereParseNotConfigured",
    "OcrWord",
    "PageOcrResult",
    "is_configured",
    "is_engine_selected",
    "ocr_page_block_sync",
    "ocr_page_sync",
    "pages_per_batch",
]
