"""Cohere Parse 5 OCR adapter (ADR-0019; on Cohere's own API per ADR-0023).

Selected through ``OCR_ENGINE=cohere_parse``; Tesseract remains the
last-resort fallback in ``pdf_report.py``. Replaced the Azure Document
Intelligence adapter with the same public surface, so the parser's fallback
ladder, sparse-page batching and per-document page budget did not have to
change shape — only the engine behind them did. The transport has since
moved twice under that same surface and nothing else has changed with it:
Foundry → Bedrock on 2026-09-08 (ADR-0022), Bedrock → Cohere's own API on
2026-09-15 (ADR-0023). ``OCR_ENGINE=cohere_parse`` has meant Cohere Parse
throughout, because it was always the host that moved, never the model.

Why it moved off Bedrock one week after arriving there: Parse 5 is an **AWS
Marketplace** SageMaker package rather than a Bedrock model, priced at about
$2.50/hour for an endpoint that has no idle state. Even under the nightly
shutdown that is roughly $600/month for the endpoint to *exist*, before a
single page is read. Nothing was ever deployed (ADR-0023).

What Parse is
-------------
A 2.3B vision-language document parser, ``parse-v5.0`` on Cohere's API.
Input is ONE page image as a base64 data URI; output is reading-order text
with tables as HTML and image descriptions.

Wire shape
----------
::

    POST {COHERE_BASE_URL}/v2/parse
    Authorization: Bearer $COHERE_API_KEY
    {"model": "parse-v5.0",
     "document": {"type": "image_url",
                  "image_url": "data:image/png;base64,..."},
     "output_format": "blocks" | "markdown"}
    -> {"id": ..., "pages": [{"type": "blocks", "index": 0, "blocks": [
            {"type": "text",  "text":  {"content": "..."}},
            {"type": "table", "table": {"html": "<table>...", "title": ...}},
            {"type": "image", "image": {"description": "...", ...}}]}]}
       or {"pages": [{"type": "markdown", "index": 0,
                      "markdown": {"content": "...", "images": [...]}}]}

``document.image_url`` is a STRING. The first live call from this codebase
(2026-09-23, from inside the VPC) was refused with HTTP 400 "parameter
'document.image_url' is of type object but should be of type string": every
page this client had ever sent was the chat-style ``{"url": ...}`` object,
so every scanned page fell back to tesseract. The response shape above is
the Cohere Python SDK's (``cohere`` 7.1.1, ``types/parse_*.py``), where each
block nests its payload under a key named by its ``type``; no successful
call has confirmed it yet, so the flat spellings stay tolerated below.

``model`` is back in the body. On Bedrock it had moved out to ``modelId``;
here the request is Cohere's own again, which is the shape ADR-0019 first
wrote against when Foundry proxied it at
``{endpoint}/providers/cohere/v2/parse``.

STILL NOT EMPIRICALLY VERIFIED. This was true on Foundry, true on Bedrock,
and true now: no live call from this codebase has ever confirmed the
contract on any host. It is a better guess than it was — this is Cohere's
own published API rather than a Marketplace passthrough — but a better guess
is not a measurement. Run the probe with a real key and update this
docstring and ``_page_from_payload`` from its report. Until then the
response adapter stays tolerant about field names (``text`` / ``content`` /
``markdown`` for text, ``html`` / ``content`` for tables, ``description`` /
``caption`` for images), which is the right posture for an unverified
contract and the reason a wrong guess degrades rather than crashes — and
where tolerance runs out it says so loudly, because until 2026-09-15 it did
not, and an unrecognised body became a silently blank page.

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
requires credentials. The key IS required to make a call now, which is a
step back from the Bedrock task-role arrangement and the price of the cost
shape — see ADR-0023 "Negative".
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
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from . import ocr_engine
from .html_table import find_table_fragments, html_table_to_grid
from .ocr_types import OcrWord, PageOcrResult

logger = logging.getLogger("georag.ingest.cohere_parse")

ENGINE_VALUE = ocr_engine.COHERE_PARSE
#: The credential. Shared with the chat backend (`LLM_BACKEND=cohere`) —
#: one key for both capabilities, which is why it is the 11th go-live
#: secret and not the 12th.
API_KEY_ENV = "COHERE_API_KEY"
#: Cohere's own model name for Parse v5 (ADR-0019 §Context). A plain name,
#: not an endpoint ARN: there is no endpoint indirection on this host.
MODEL_ENV = "COHERE_PARSE_MODEL"
_DEFAULT_MODEL = "parse-v5.0"
BASE_URL_ENV = "COHERE_BASE_URL"
_DEFAULT_BASE_URL = "https://api.cohere.com"
#: Retired 2026-09-15 (ADR-0023). Named here only so a deployment that still
#: sets it gets told, rather than having it silently ignored while the OCR
#: bill moves to a different vendor.
_RETIRED_MODEL_ID_ENV = "BEDROCK_PARSE_MODEL_ID"
OCR_METHOD = "cohere_parse"

_TIMEOUT_ENV = "COHERE_PARSE_TIMEOUT_S"
_DEFAULT_TIMEOUT_S = 120.0

# Pixel ceiling for the rendered page. Parse takes a single page image.
# page_image._MAX_DPI (200) binds first on anything up to ~tabloid, so the
# cap only matters for plan sheets: at 20 MP an A1 sheet renders at ~158 DPI
# and an A0 at ~112 DPI, where the old 4 MP default gave A0 ~50 DPI — below
# the downscale warning. Confirmed live 2026-09-24
# (ops/validation/reports/cohere_probe_20260924T060435Z.json): Parse accepted
# a 19,996,997-pixel page, the top of the probe's ladder, so 20 MP is measured
# rather than guessed; the vendor's real ceiling may be higher. A request over
# it fails as a 4xx and the page falls back to tesseract, so a too-high value
# is loud, not silent.
_MAX_PIXELS_ENV = "COHERE_PARSE_MAX_PIXELS"
_DEFAULT_MAX_PIXELS = 20_000_000
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
    """OCR_ENGINE=cohere_parse but COHERE_API_KEY is absent.

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
    """True when the Cohere API key is present.

    The credential IS the check now. Under Bedrock it deliberately was not —
    the ECS task role supplied it and there was nothing to read from the
    environment — but on this host a missing key is a configuration error
    the operator can see before a single page is rendered, so it is worth
    checking up front rather than discovering as a 401 per page.
    """
    return bool((os.environ.get(API_KEY_ENV) or "").strip())


def parse_model() -> str:
    """Cohere's model name for Parse. Defaults to ``parse-v5.0``."""
    return (os.environ.get(MODEL_ENV) or "").strip() or _DEFAULT_MODEL


def base_url() -> str:
    """Cohere API root, without a trailing slash."""
    return ((os.environ.get(BASE_URL_ENV) or "").strip() or _DEFAULT_BASE_URL).rstrip("/")


def _require_config() -> str:
    """Return the model name, or explain what is missing.

    Also rejects leftover Foundry configuration outright, and complains
    about leftover Bedrock configuration. Both are the same failure shape:
    a deployment carrying well-formed settings for a host it no longer
    talks to would otherwise fall straight through to Tesseract on every
    scanned page and extract no tables — silently, which is precisely the
    2026-08-21 failure ``ocr_engine.py`` was written to make loud.

    The Bedrock leftover is a log line rather than a raise, because unlike
    the Foundry variables it names a resource that may legitimately still
    exist: ADR-0023 took Bedrock's default, not its support, and an
    operator running the Marketplace endpoint for chat could have it set on
    purpose. It is still worth saying, because OCR is no longer billed
    through it and nothing else would reveal that.
    """
    from app.services._bedrock import assert_no_retired_foundry_env  # noqa: PLC0415

    assert_no_retired_foundry_env(context=f"{ENGINE_VALUE} OCR engine")
    if (os.environ.get(_RETIRED_MODEL_ID_ENV) or "").strip():
        logger.warning(
            "cohere_parse: %s is set but no longer used for OCR — Parse moved "
            "to Cohere's own API on 2026-09-15 (ADR-0023) and is billed per "
            "page against %s. Unset it unless the Bedrock chat backend needs it.",
            _RETIRED_MODEL_ID_ENV,
            API_KEY_ENV,
        )
    if not is_configured():
        raise CohereParseNotConfigured(
            f"{API_KEY_ENV} must be set to use the {ENGINE_VALUE} OCR engine. "
            "It is the Cohere API key, shared with LLM_BACKEND=cohere, and in "
            "production it is read from Secrets Manager (ADR-0023)."
        )
    return parse_model()


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


def _page_count(pdf_path: str) -> int:
    """Number of pages in the PDF; raises when the file cannot be opened."""
    import pypdfium2 as pdfium  # noqa: PLC0415 — heavy import, lazy

    with _RENDER_LOCK:
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            return len(pdf)
        finally:
            with contextlib.suppress(Exception):
                pdf.close()


def _render_page(pdf_path: str, page_number: int) -> bytes | None:
    """Render ONE 1-indexed page to PNG under the pixel cap; None on failure.

    Opens and closes the document per call, under the render lock. One
    page's PNG is resident per in-flight request — never a whole group —
    so the memory profile is bounded by PDF_OCR_PAGE_CONCURRENCY rather
    than OCR_PAGES_PER_BATCH (ADR-0018 is the OOM ADR; this path must not
    reintroduce a resident-set that scales with an operator knob).
    """
    import pypdfium2 as pdfium  # noqa: PLC0415 — heavy import, lazy

    cap = max_pixels()
    try:
        with _RENDER_LOCK:
            pdf = pdfium.PdfDocument(pdf_path)
            try:
                if not 1 <= page_number <= len(pdf):
                    logger.warning(
                        "cohere_parse: page %d is outside 1..%d of '%s' — skipped",
                        page_number,
                        len(pdf),
                        pdf_path,
                    )
                    return None
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
                    bitmap = page.render(scale=dpi / _PDF_POINTS_PER_INCH, rotation=0)
                    image = bitmap.to_pil()
                finally:
                    with contextlib.suppress(Exception):
                        page.close()
            finally:
                with contextlib.suppress(Exception):
                    pdf.close()
        # Trust but verify — PIL rounding is the one thing between our
        # arithmetic and a vendor-side 4xx. Encoding runs outside the lock.
        if image.width * image.height > cap:
            shrink = math.sqrt(cap / (image.width * image.height))
            image = image.resize((max(1, int(image.width * shrink)), max(1, int(image.height * shrink))))
        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=False)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cohere_parse: render failed for page %d of '%s': %s",
            page_number,
            pdf_path,
            exc,
        )
        return None


def _render_pages(pdf_path: str, page_numbers: Sequence[int]) -> dict[int, bytes]:
    """Render several pages one at a time; absent = failed. Raises if the file
    cannot be opened at all."""
    _page_count(pdf_path)
    rendered: dict[int, bytes] = {}
    for page_number in page_numbers:
        png = _render_page(pdf_path, page_number)
        if png is not None:
            rendered[page_number] = png
    return rendered


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

_CLIENT_LOCK = threading.Lock()
_CLIENT: Any = None
_CLIENT_TIMEOUT: float | None = None

#: HTTP statuses that mean the request was REJECTED rather than the service
#: being unavailable. Split out because they get different log levels: a
#: refused call is an operator problem worth an ERROR (a bad or unentitled
#: key, a model name that does not exist, an image the API will not accept),
#: a throttle or a 5xx is weather.
_REJECTED_STATUS = frozenset({400, 401, 403, 404, 413, 422})
#: Worth one more try. Under Bedrock botocore retried these before anything
#: reached this module; httpx does not, so the retries are explicit below.
#: Without them the move to this host would quietly push more pages onto
#: Tesseract — a capability regression with no error to point at.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 0.5
#: Ceiling on an honoured ``Retry-After``. A page is one unit of a bounded
#: per-document budget; waiting minutes for one of them is worse than
#: falling to Tesseract and moving on.
_MAX_RETRY_AFTER_S = 10.0


class CohereParseHttpError(RuntimeError):
    """A non-2xx from the Parse API, carrying the status for classification.

    ``httpx.HTTPStatusError`` would do, but it stringifies to a multi-line
    message with a doc URL in it, and this one ends up in a per-page log
    line. The status is what ``_parse_png`` actually branches on.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


def _http_client() -> Any:
    """One cached ``httpx.Client`` per (process, timeout).

    Parsing runs in a subprocess, so this is per-subprocess. Rebuilt when
    ``timeout_seconds()`` changes: page timeouts are configurable at runtime
    and a stale client would silently keep the old one. This is the same
    caching the Bedrock client had and, before that, the Foundry one.
    """
    global _CLIENT, _CLIENT_TIMEOUT
    import httpx  # noqa: PLC0415

    timeout = timeout_seconds()
    with _CLIENT_LOCK:
        if _CLIENT is None or timeout != _CLIENT_TIMEOUT:
            with contextlib.suppress(Exception):
                if _CLIENT is not None:
                    _CLIENT.close()
            _CLIENT = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))
            _CLIENT_TIMEOUT = timeout
        return _CLIENT


def _post(body: dict[str, Any]) -> Any:
    """One HTTP round trip. The innermost seam; retry tests replace this."""
    import json  # noqa: PLC0415

    key = (os.environ.get(API_KEY_ENV) or "").strip()
    return _http_client().post(
        f"{base_url()}/v2/parse",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        content=json.dumps(body).encode(),
    )


def _retry_after_seconds(response: Any, attempt: int) -> float:
    """Honour ``Retry-After`` when it is sane, else exponential backoff."""
    raw = ""
    with contextlib.suppress(Exception):
        raw = (response.headers.get("retry-after") or "").strip()
    if raw:
        with contextlib.suppress(TypeError, ValueError):
            return max(0.0, min(_MAX_RETRY_AFTER_S, float(raw)))
    return _BACKOFF_BASE_S * (2.0 ** (attempt - 1))


def _invoke(model: str, body: dict[str, Any]) -> bytes:
    """The single network seam; tests replace this.

    Returns the raw response body rather than a parsed dict so that a
    transport failure and an undecodable payload stay distinguishable in
    ``_parse_png``. That split has survived all three hosts and is worth
    keeping: "the API refused the call" and "the API answered with
    something that is not JSON" want different operator responses.

    ``model`` is accepted rather than read here so the caller's
    ``_require_config()`` result is what goes on the wire — the same
    arrangement the Bedrock version had with its model id, which keeps the
    test seam's signature unchanged across the move.
    """
    import httpx  # noqa: PLC0415

    body = {"model": model, **body}
    last: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = _post(body)
        except (httpx.TransportError, httpx.StreamError) as exc:
            last = exc
            if attempt == _MAX_ATTEMPTS:
                raise
            time.sleep(_BACKOFF_BASE_S * (2.0 ** (attempt - 1)))
            continue

        if response.status_code < 300:
            return cast("bytes", response.content)

        detail = ""
        with contextlib.suppress(Exception):
            detail = response.text[:200]
        if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
            logger.info(
                "cohere_parse: HTTP %d (attempt %d/%d) — retrying",
                response.status_code,
                attempt,
                _MAX_ATTEMPTS,
            )
            time.sleep(_retry_after_seconds(response, attempt))
            continue
        raise CohereParseHttpError(response.status_code, detail)

    # Unreachable: every path above either returns, raises, or continues,
    # and the final attempt cannot continue. Kept so the function has no
    # implicit None return if that ever stops being true.
    raise last or RuntimeError("cohere_parse: retry loop exited without a result")


def _request_body(png_bytes: bytes) -> dict[str, Any]:
    """Cohere's own parse body. ``model`` is added by ``_invoke``."""
    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return {
        # A bare string, not chat's {"url": ...} object — Cohere 400s the
        # object form (live call, 2026-09-23). See the module docstring.
        "document": {"type": "image_url", "image_url": data_uri},
        "output_format": output_format(),
    }


def _parse_png(png_bytes: bytes, *, log_page: int | None) -> PageOcrResult:
    """Parse one rendered page; fail soft on any error except NotConfigured."""
    model = _require_config()
    body = _request_body(png_bytes)
    where = f" on page {log_page}" if log_page is not None else ""

    try:
        raw = _invoke(model, body)
    except Exception as exc:  # noqa: BLE001 — every failure falls back to tesseract
        status = getattr(exc, "status_code", None)
        message = getattr(exc, "message", "") or ""
        if status in _REJECTED_STATUS:
            # This branch has earned its ERROR level on every host. On
            # Foundry it was an HTTP 403, and Foundry blocked 1,421 of 2,524
            # calls on 2026-08-17 with nothing noticing. On Bedrock the same
            # condition at least also showed up in InvocationClientErrors,
            # which was alarmed. On Cohere's API there is NO AWS metric
            # behind it at all — CloudWatch cannot see a call that never
            # went to AWS — so this log line is the whole signal, and the
            # `cohere-parse-rejected` alarm marker is what pages on it.
            logger.error(
                "COHERE_PARSE_REJECTED: HTTP %s%s for model %s. Falling back "
                "to tesseract, which extracts no tables. Check that %s is "
                "valid and entitled to Parse. Detail: %s",
                status,
                where,
                model,
                API_KEY_ENV,
                message or exc,
            )
        else:
            logger.warning("cohere_parse: request failed%s: %s", where, exc)
        return PageOcrResult(
            "",
            0.0,
            request_succeeded=False,
            error=f"http_{status}: {message}" if status is not None else str(exc),
            confidence_reported=False,
        )

    try:
        import json  # noqa: PLC0415

        payload = json.loads(raw)
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


def _first_str(mapping: Any, *keys: str) -> str | None:
    """Like ``_first``, but only a non-empty STRING counts.

    ``_first`` returns whatever is there, and a block's ``text`` is an
    OBJECT in the SDK's shape (``{"content": ...}``) — ``str()`` of it put
    ``{'content': '...'}`` into the page text instead of the text.
    """
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _block_fields(block: dict[str, Any], kind: str) -> dict[str, Any]:
    """The dict a block's content fields live in.

    The SDK nests them under a key named by the block's type —
    ``{"type": "text", "text": {"content": ...}}``, ``{"type": "table",
    "table": {"html": ...}}``. The flat spelling (fields on the block
    itself) is what this adapter assumed before 2026-09-23 and stays
    accepted: neither has been confirmed by a successful live call.
    """
    nested = block.get(kind)
    return nested if isinstance(nested, dict) else block


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

    if isinstance(page, dict):
        blocks = page.get("blocks")
        if isinstance(blocks, list):
            return _page_from_blocks(blocks)
        if "markdown" in page:
            return _page_from_markdown(page.get("markdown"))

    # Fixed 2026-09-15: every path above used to fall through to
    # `_page_from_markdown(page.get("markdown"))`, which returns an empty
    # PageOcrResult — and request_succeeded defaults to TRUE
    # (ocr_types.py:45). The caller drops to tesseract only `if not
    # result.request_succeeded` — pdf_report.py:2665, whose own comment reads
    # "NOT merely empty text". So a response whose SHAPE we do not recognise
    # produced a page that was billed (_meter_pages ran before this), yielded
    # no text and no tables, did NOT fall back to tesseract, and left no log
    # line. Indistinguishable from a blank sheet, at scale.
    #
    # This is the failure this deployment is most likely to actually hit.
    # Cohere Parse's wire shape has never been verified empirically on ANY
    # host, Foundry included (ADR-0022, Verification), so "HTTP 200 with a
    # body we do not recognise" is precisely the shape of being wrong about
    # it — and it was the one case the old code scored as success.
    #
    # An empty page is still success, and deliberately so: a genuinely blank
    # scan arrives as a RECOGNISED shape — `blocks` a list (possibly empty),
    # or a `markdown` key present — and returns above. Only an unrecognised
    # shape reaches here.
    #
    # Only key names are logged, never values: a Parse body carries the
    # document's text, and this line goes to CloudWatch.
    inspected = page if isinstance(page, dict) else payload
    logger.error(
        "COHERE_PARSE_UNRECOGNISED_RESPONSE: no recognisable page in the "
        "Parse body (keys=%s). Falling back to tesseract, which extracts no "
        "tables. Run ops/validation/cohere_probe.py and correct the response "
        "adapter from its report.",
        sorted(inspected)[:10] if isinstance(inspected, dict) else type(inspected).__name__,
    )
    return PageOcrResult(
        "",
        0.0,
        request_succeeded=False,
        error="unrecognised_response_shape",
        confidence_reported=False,
    )


def _page_from_blocks(blocks: list[Any]) -> PageOcrResult:
    parts: list[str] = []
    tables: list[list[list[str]]] = []
    describe_images = include_image_descriptions()

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "text").strip().lower()
        fields = _block_fields(block, kind)
        if kind == "table":
            html_fragment = _first_str(fields, "html", "content", "text")
            grid = html_table_to_grid(html_fragment) if html_fragment else []
            if grid:
                tables.append(grid)
                parts.append(_table_markdown(grid))
            elif html_fragment:
                parts.append(html_fragment.strip())
        elif kind in {"image", "figure", "picture"}:
            if describe_images:
                description = _first_str(fields, "description", "caption", "text")
                if description:
                    parts.append(f"[Figure: {description.strip()}]")
        else:
            text = _first_str(fields, "content", "text", "markdown")
            if text:
                parts.append(text.strip())

    text = "\n\n".join(part for part in parts if part).strip()
    return _result(text, tables)


def _page_from_markdown(markdown: Any) -> PageOcrResult:
    content = _first_str(markdown, "content", "text", "markdown") if isinstance(markdown, dict) else markdown
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
            lambda m: f"[Figure: {m.group('alt').strip()}]" if m.group("alt").strip() else "",
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
    png = _render_page(pdf_path, page_num)
    if png is None:
        return PageOcrResult(
            "",
            0.0,
            request_succeeded=False,
            error="render_failed",
            confidence_reported=False,
        )
    return _parse_png(png, log_page=page_num)


def ocr_page_block_sync(pdf_path: str, page_numbers: Sequence[int]) -> dict[int, PageOcrResult]:
    """OCR a group of pages: each worker renders its page, then posts it.

    Returns ``{absolute_page_number: PageOcrResult}`` for the pages whose
    request succeeded. A page that is absent must be re-driven by the
    caller (render failed, request failed); a page that is present with
    empty text ran and came back blank, which is a different — cheaper —
    situation. Returns ``{}`` when the file cannot be opened at all.

    Rendering happens inside the worker so at most PDF_OCR_PAGE_CONCURRENCY
    page PNGs are resident, whatever OCR_PAGES_PER_BATCH is set to.
    """
    _require_config()
    ordered = sorted(set(int(n) for n in page_numbers))
    if not ordered:
        return {}
    try:
        _page_count(pdf_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cohere_parse: could not open '%s' for a %d-page group: %s",
            pdf_path,
            len(ordered),
            exc,
        )
        return {}

    def _one(page_number: int) -> tuple[int, PageOcrResult | None]:
        png = _render_page(pdf_path, page_number)
        if png is None:
            return page_number, None
        return page_number, _parse_png(png, log_page=page_number)

    workers = max(1, min(page_concurrency(), len(ordered)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_one, ordered))

    return {page_number: result for page_number, result in results if result is not None and result.request_succeeded}


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "ENGINE_VALUE",
    "MODEL_ENV",
    "OCR_METHOD",
    "CohereParseHttpError",
    "CohereParseNotConfigured",
    "OcrWord",
    "PageOcrResult",
    "base_url",
    "is_configured",
    "is_engine_selected",
    "ocr_page_block_sync",
    "ocr_page_sync",
    "pages_per_batch",
    "parse_model",
]
