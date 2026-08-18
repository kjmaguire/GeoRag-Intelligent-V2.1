"""Page-image rendering for Cohere Embed v4 multimodal indexing (2026-08-18).

Why this exists
---------------
Maps, cross-sections, plan views and drill-log plates carry their meaning in
the *picture*, not in text. OCR on such a page yields a handful of legend
scraps, so the page is effectively invisible to search today. Cohere Embed v4
embeds a page image into the SAME 1024-dim space as text, so a rendered page
becomes a retrievable point in the existing `georag_chunks` collection and a
plain text question ("cross-section through the eastern fault zone") matches
it directly.

The hard constraint this module owns
------------------------------------
Embed v4 rejects images above **2 million pixels**. This is the single most
likely way the feature breaks in production, because every other rendering
path in this repo targets OCR resolution:

    250 DPI letter page = 2125 x 2750 = 5.84M px   -> REJECTED
    200 DPI letter page = 1700 x 2200 = 3.74M px   -> REJECTED
    150 DPI letter page = 1275 x 1650 = 2.10M px   -> REJECTED (just barely)
    144 DPI letter page = 1224 x 1584 = 1.94M px   -> ok

Rather than hard-code a DPI that happens to work for US Letter and silently
fails on A0 mine plans (which are routinely 10x that area), `render_page_png`
computes the DPI from the page's own dimensions so the result always lands
just under the cap regardless of sheet size. Callers must not pass their own
DPI — that is exactly the mistake this module exists to prevent.

Deliberately NOT reusing PdfRenderService
-----------------------------------------
`app.services.pdf_render.PdfRenderService` renders via a ProcessPoolExecutor.
Ingest already runs inside a parse subprocess (see `_run_parser_subprocess` in
hatchet_workflows/ingest_pdf.py), and nesting a process pool there is the
documented cause of the 2026-06-24 /dev/shm exhaustion (loky leak). We call
that module's *module-level worker function* directly instead — same renderer,
same pypdfium2 code path, no extra processes.
"""

from __future__ import annotations

import io
import logging
import math
import os

logger = logging.getLogger("georag.ingest.page_image")

# Embed v4's documented ceiling (learn.microsoft.com, `embed-v-4-0` model card,
# checked 2026-08-18: "images (2MM pixels)"). The safety margin absorbs PIL's
# rounding when scale * page_points is not an integer — without it a page can
# land a few hundred pixels over the line and 400 for a reason that looks
# random.
EMBED_V4_MAX_PIXELS = 2_000_000
_PIXEL_SAFETY_MARGIN = 0.97

# Below this the render is too coarse for the model to resolve anything useful
# (a 2 m x 1.5 m mine plan at its cap-derived DPI is already marginal). Pages
# needing less than this are still rendered — a coarse vector beats no vector —
# but the caller gets a warning it can surface in ingestion diagnostics.
_MIN_USEFUL_DPI = 40.0

# Ceiling for small pages. Without it a business-card-sized page would render
# at absurd DPI to "fill" the pixel budget, wasting bytes on upscaled noise.
_MAX_DPI = 200.0

_PDF_POINTS_PER_INCH = 72.0


class PageImageTooLarge(RuntimeError):
    """Raised when a rendered page exceeds the model's pixel cap.

    Should be unreachable — `render_page_png` sizes the render to fit — so it
    fires only if pypdfium2's output diverges from the requested scale. Kept
    as a hard failure rather than a silent downscale so that divergence is
    caught in tests instead of becoming a mystery 400 in production.
    """


def dpi_for_page(width_points: float, height_points: float) -> float:
    """Return the highest DPI that keeps this page under the pixel cap.

    Derived from the page's own geometry, so an A0 plan sheet and a US Letter
    page both land just under the cap instead of one of them being rejected.
    """
    if width_points <= 0 or height_points <= 0:
        # Degenerate page box — fall back to the letter-safe DPI rather than
        # dividing by zero. The render will still be pixel-checked below.
        return 144.0

    area_sq_inches = (width_points / _PDF_POINTS_PER_INCH) * (
        height_points / _PDF_POINTS_PER_INCH
    )
    budget = EMBED_V4_MAX_PIXELS * _PIXEL_SAFETY_MARGIN
    return min(_MAX_DPI, math.sqrt(budget / area_sq_inches))


def render_page_png(pdf_bytes: bytes, page_number: int) -> tuple[bytes, int, int, float]:
    """Render ONE page to PNG sized to fit Embed v4's pixel cap.

    Parameters
    ----------
    pdf_bytes:
        Raw bytes of the PDF.
    page_number:
        1-indexed page number, matching `silver.document_passages.page_number`
        and every other page reference in the ingest path. Converted to
        pypdfium2's 0-indexed convention internally — the off-by-one here
        would silently embed the wrong page, so it is done in exactly one
        place.

    Returns
    -------
    ``(png_bytes, width_px, height_px, dpi_used)``
    """
    import pypdfium2 as pdfium  # noqa: PLC0415 — heavy import, lazy

    from app.services.pdf_render import _render_full_page_worker  # noqa: PLC0415

    # Measure the page before rendering it — we need its dimensions to pick a
    # DPI, and opening the document twice is far cheaper than rendering at the
    # wrong size and discarding the result.
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        page = pdf[page_number - 1]
        width_points, height_points = page.get_size()
    finally:
        pdf.close()

    dpi = dpi_for_page(width_points, height_points)
    if dpi < _MIN_USEFUL_DPI:
        logger.warning(
            "page_image: page %d is %.0fx%.0f pt — capping at %.0f DPI to fit "
            "the %d px embed limit; the render may be too coarse to be useful",
            page_number, width_points, height_points, dpi, EMBED_V4_MAX_PIXELS,
        )

    png = _render_full_page_worker(pdf_bytes, page_number - 1, int(dpi))

    # Trust but verify: the pixel cap is a hard API boundary, and PIL's
    # rounding is the one thing between our arithmetic and a 400.
    from PIL import Image  # noqa: PLC0415

    with Image.open(io.BytesIO(png)) as img:
        width_px, height_px = img.size

    if width_px * height_px > EMBED_V4_MAX_PIXELS:
        raise PageImageTooLarge(
            f"page {page_number} rendered to {width_px}x{height_px} "
            f"({width_px * height_px} px) at {dpi:.0f} DPI, over the "
            f"{EMBED_V4_MAX_PIXELS} px Embed v4 cap"
        )

    return png, width_px, height_px, dpi


# ---------------------------------------------------------------------------
# Which pages get an image vector
# ---------------------------------------------------------------------------
# Kyle's call 2026-08-18: default to every page. The knob exists so the scope
# can be dialled back from config after measuring, without a code change —
# "all" roughly doubles the points in georag_chunks and adds a render + a
# network embed call per page, which is real ingestion latency.
#
#   all      — every page (default; Kyle's choice)
#   figures  — only pages the parser could not read as text, i.e. the maps and
#              plates that are invisible today. ~2% of pages on the live
#              corpus, and the highest value-per-call setting.
#   off      — disable image embedding entirely
_SCOPE_ENV = "IMAGE_EMBED_PAGE_SCOPE"
_VALID_SCOPES = ("all", "figures", "off")


def image_embed_scope() -> str:
    raw = (os.environ.get(_SCOPE_ENV) or "all").strip().lower()
    if raw not in _VALID_SCOPES:
        logger.warning(
            "page_image: %s=%r is not one of %s — falling back to 'all'",
            _SCOPE_ENV, raw, _VALID_SCOPES,
        )
        return "all"
    return raw


def should_embed_page(page_number: int, text_pages: set[int]) -> bool:
    """True when this page should get an image vector under the active scope.

    `text_pages` is the set of page numbers the parser read successfully as
    native text — i.e. NOT the image-only pages. Under scope="figures" those
    are precisely the pages we skip.
    """
    scope = image_embed_scope()
    if scope == "off":
        return False
    if scope == "figures":
        return page_number not in text_pages
    return True


def text_pages_from_sections(sections: list[dict]) -> set[int]:
    """Page numbers the parser read from a real text layer.

    Used only by scope="figures". A page counts as text if ANY section
    spanning it came from a native extractor — `document_intelligence` and
    `tesseract` deliberately do NOT count, because a page that needed OCR is
    exactly the kind of page (map, plate, scanned insert) whose picture
    carries meaning the text does not.
    """
    native = {"fitz_native", "pdfplumber_native"}
    pages: set[int] = set()
    for section in sections or []:
        if (section.get("ocr_method") or "fitz_native") not in native:
            continue
        first = section.get("page_first")
        last = section.get("page_last") or first
        if first is None:
            continue
        pages.update(range(int(first), int(last) + 1))
    return pages


# ---------------------------------------------------------------------------
# Staging (parse task) — render pages and park them under a _pending key
# ---------------------------------------------------------------------------
# Mirrors the figure-manifest convention already in ingest_pdf.py: the parse
# task does not know the report_id yet, so renders are uploaded under
# `page-images/_pending/{sha256}/...` and the persist task renames them to
# `page-images/{report_id}/...` once the row exists. Same reason, same shape —
# see the figure_manifest handling around ingest_pdf.py:1218.

PENDING_PREFIX = "page-images/_pending"
FINAL_PREFIX = "page-images"


def pending_key(sha256: str, page_number: int) -> str:
    return f"{PENDING_PREFIX}/{sha256}/page_{page_number:05d}.png"


def final_key(report_id: str, page_number: int) -> str:
    return f"{FINAL_PREFIX}/{report_id}/page_{page_number:05d}.png"


def stage_page_images(
    pdf_path: str,
    sha256: str,
    sections: list[dict],
    *,
    max_pages: int | None = None,
) -> list[dict]:
    """Render in-scope pages and upload them under their pending keys.

    Returns a manifest of ``{page_number, pending_key, width, height, dpi}``
    for the persist task to finalise. Runs in the parse task, where the PDF is
    already on local disk.

    Fail-soft by design: a page that fails to render or upload is logged and
    omitted from the manifest. A missing page image degrades search coverage
    for that page; it must never fail the whole document's ingestion, which
    would trade a working text pipeline for a nice-to-have one.
    """
    scope = image_embed_scope()
    if scope == "off":
        return []

    from georag_object_storage import Bucket, get_storage_client  # noqa: PLC0415

    try:
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
    except OSError as exc:
        logger.warning("page_image: cannot read %s for staging: %s", pdf_path, exc)
        return []

    import pypdfium2 as pdfium  # noqa: PLC0415

    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        total_pages = len(pdf)
    finally:
        pdf.close()

    text_pages = text_pages_from_sections(sections) if scope == "figures" else set()
    targets = [
        n for n in range(1, total_pages + 1) if should_embed_page(n, text_pages)
    ]

    # Cap mirrors AZURE_DI_MAX_PAGES_PER_DOC's rationale: one pathological
    # document must not be able to issue unbounded renders + embed calls. The
    # drop is logged rather than silent — a truncated manifest that looked
    # like full coverage is how "we indexed everything" becomes false.
    if max_pages is None:
        max_pages = int(os.environ.get("IMAGE_EMBED_MAX_PAGES_PER_DOC", "1000"))
    if len(targets) > max_pages:
        logger.warning(
            "page_image: %d pages in scope for %s but IMAGE_EMBED_MAX_PAGES_PER_DOC=%d "
            "— indexing the first %d, dropping %d",
            len(targets), pdf_path, max_pages, max_pages, len(targets) - max_pages,
        )
        targets = targets[:max_pages]

    storage = get_storage_client()
    manifest: list[dict] = []
    for page_number in targets:
        try:
            png, width, height, dpi = render_page_png(pdf_bytes, page_number)
            key = pending_key(sha256, page_number)
            storage.put_bytes(
                Bucket.BRONZE_RASTER, key, png, content_type="image/png",
            )
        except Exception as exc:  # noqa: BLE001 — see fail-soft note above
            logger.warning(
                "page_image: staging failed for page %d of %s: %s",
                page_number, pdf_path, exc,
            )
            continue
        manifest.append({
            "page_number": page_number,
            "pending_key": key,
            "width": width,
            "height": height,
            "dpi": round(dpi, 1),
        })

    if manifest:
        logger.info(
            "page_image: staged %d/%d page renders for %s (scope=%s)",
            len(manifest), total_pages, sha256, scope,
        )
    return manifest


def placeholder_text(page_number: int, report_title: str | None) -> str:
    """Passage text for an image page that has not been verbalized yet.

    Carries the page number so it stays unique under
    UNIQUE (document_id, revision_number, text_hash), and reads as an honest
    label in a citation rather than pretending to be quoted document text.
    Replaced wholesale once a vision model verbalizes the page.
    """
    title = (report_title or "").strip()
    suffix = f" of {title}" if title else ""
    return f"[Page {page_number}{suffix} — page image, not yet described]"

