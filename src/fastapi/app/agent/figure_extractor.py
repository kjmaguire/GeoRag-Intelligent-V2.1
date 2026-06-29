"""PDF figure extraction + VL captioning — building blocks for the figure path.

CANONICAL figure indexing lives in the ingest_pdf `persist` task: it consumes
docling's figure_manifest, uploads each figure to S3, and builds a ReportSection
per figure → silver.document_passages → the `georag_chunks` collection chat
queries (ADR-0010, RETRIEVAL_USE_DOCUMENT_PASSAGES=True). Since 2026-06-24
persist also folds a Qwen3-VL caption into that section (flag
FIGURE_VL_DESCRIPTIONS) via caption_image_with_vl below.

This module no longer indexes anything itself — it provides the reusable pieces.
(The legacy standalone indexing to the `georag_reports` collection was removed
2026-06-24 once that collection was confirmed empty/decommissioned under the
ADR-0010 canonical flip — it would only have written into a dead, un-queried
collection.)

  - extract_figures_from_pdf    — embedded RASTER images (pypdfium2, Apache-2.0;
                                  replaced the removed AGPL PyMuPDF path).
  - extract_figures_from_layout — render page + crop §04p layout figure bboxes;
                                  catches VECTOR figures too (cross-sections/maps).
  - caption_image_with_vl       — shared single-image Qwen3-VL call; used by the
                                  canonical persist path AND describe_figures_with_vl.
  - describe_figures_with_vl    — caption a batch of extracted figures (VL, with
                                  the heuristic as the per-figure fallback).
  - generate_figure_descriptions — rule-based fallback when VL is unavailable.

Follow-up: per-parser bbox-coord adapters for extract_figures_from_layout (it
assumes the docling/pdfminer points/bottom-left convention).
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Minimum image size thresholds to filter out icons/logos/bullets
MIN_IMAGE_BYTES = 5_000       # 5 KB
MIN_IMAGE_DIMENSION = 100     # 100 px width or height

# Master switch for VL figure captioning, read by the ingest_pdf persist task
# (as _FIGURE_VL_CAPTIONS) to caption docling figures with the Qwen3-VL sidecar
# instead of the docling/heuristic caption. Off by default — the VL path adds a
# per-figure inference call; opt in per deployment (FIGURE_VL_DESCRIPTIONS=1).
FIGURE_VL_DESCRIPTIONS = (os.environ.get("FIGURE_VL_DESCRIPTIONS") or "").strip().lower() in (
    "1", "true", "yes", "on",
)

# Prompt for the VL figure-captioning call. Retrieval-oriented + grounded.
_FIGURE_VL_PROMPT = (
    "You are a geological-document analyst. In 1-3 sentences, describe this figure "
    "from a mining/exploration technical report for search indexing. State the "
    "figure type (e.g. location map, geological cross-section, drill plan/collar "
    "map, grade-tonnage curve, stratigraphic column, geophysical survey, core "
    "photo), what it depicts, and any visible labels, units, place names, or hole "
    "IDs. Be specific and factual — do not invent details that are not visible."
)


def extract_figures_from_pdf(pdf_path: str) -> list[dict]:
    """Extract embedded raster images from a PDF via pypdfium2 (Apache-2.0).

    Permissive replacement for the removed PyMuPDF (AGPL) path. Walks each page's
    image objects, decodes them, and returns one dict per qualifying image.
    Icons/logos/bullets are filtered by MIN_IMAGE_BYTES / MIN_IMAGE_DIMENSION,
    and repeated images (letterheads on every page) are de-duped by SHA-256.

    Scope: this finds embedded **raster** images only. Vector figures — many
    geological cross-sections, plan maps and grade-tonnage curves are drawn as
    vector graphics — are NOT captured here; a layout-region render + VL pass is
    the richer follow-up (see module docstring).

    Returns:
        list[dict], each: {page (1-based), width, height, sha256, format,
        image_bytes}. Empty list if the PDF can't be opened or has no qualifying
        images.
    """
    import pypdfium2 as pdfium  # noqa: PLC0415 — heavy import, lazy

    try:
        pdf = pdfium.PdfDocument(pdf_path)
    except Exception:
        logger.exception("extract_figures_from_pdf: cannot open %s", pdf_path)
        return []

    figures: list[dict] = []
    seen: set[str] = set()
    try:
        for page_idx in range(len(pdf)):
            page = pdf[page_idx]
            try:
                objects = page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,))
            except Exception:
                logger.warning(
                    "extract_figures_from_pdf: get_objects failed on page %d of %s",
                    page_idx, os.path.basename(pdf_path) if pdf_path else "<unspecified>",
                )
                continue

            for obj in objects:
                try:
                    pil = obj.get_bitmap().to_pil()
                except Exception:
                    continue  # undecodable image object — skip, don't abort the page
                width, height = pil.size
                # Filter tiny glyphs/icons/bullets (both dims small).
                if width < MIN_IMAGE_DIMENSION and height < MIN_IMAGE_DIMENSION:
                    continue
                if pil.mode not in ("RGB", "L"):
                    pil = pil.convert("RGB")
                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                data = buf.getvalue()
                if len(data) < MIN_IMAGE_BYTES:
                    continue
                sha = hashlib.sha256(data).hexdigest()
                if sha in seen:
                    continue  # same image repeated across pages (e.g. letterhead)
                seen.add(sha)
                figures.append({
                    "page": page_idx + 1,
                    "width": width,
                    "height": height,
                    "sha256": sha,
                    "format": "png",
                    "image_bytes": data,
                })
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    logger.info(
        "extract_figures_from_pdf: %d figure(s) from %s",
        len(figures),
        os.path.basename(pdf_path) if pdf_path else "<unspecified>",
    )
    return figures


def extract_figures_from_layout(
    pdf_path: str,
    figure_regions: list[dict],
    *,
    render_scale: float = 2.0,
) -> list[dict]:
    """Render + crop figure regions from §04p layout bboxes (vector + raster).

    Unlike extract_figures_from_pdf (embedded RASTER images only), this renders
    the page and crops the figure's bounding box, so it captures figures drawn
    as VECTOR graphics too — most geological cross-sections, plan maps and
    grade-tonnage curves. Feed it the layout entries whose layout_label is
    "figure" from the §04p parse.

    Coordinate convention: bbox is ``[left, bottom, right, top]`` in PDF POINTS,
    BOTTOMLEFT origin — what parse_mixed (docling) / parse_native (pdfminer)
    emit via _bbox_from_prov. parse_scanned / parse_docparser_vl emit pixel-space
    bboxes; convert those before calling (a parser-specific adapter is a
    follow-up). Same output dict shape as extract_figures_from_pdf.
    """
    import pypdfium2 as pdfium  # noqa: PLC0415

    try:
        pdf = pdfium.PdfDocument(pdf_path)
    except Exception:
        logger.exception("extract_figures_from_layout: cannot open %s", pdf_path)
        return []

    # Group by page so each page is rendered once even with several figures.
    by_page: dict[int, list[dict]] = {}
    for region in figure_regions:
        try:
            by_page.setdefault(int(region["page"]), []).append(region)
        except (KeyError, TypeError, ValueError):
            continue

    figures: list[dict] = []
    seen: set[str] = set()
    try:
        for page_num, regions in sorted(by_page.items()):
            page_idx = page_num - 1
            if page_idx < 0 or page_idx >= len(pdf):
                continue
            page = pdf[page_idx]
            try:
                _w_pts, h_pts = page.get_size()
                page_img = page.render(scale=render_scale).to_pil().convert("RGB")
            except Exception:
                logger.warning("extract_figures_from_layout: render failed on page %d", page_num)
                continue

            for region in regions:
                bbox = region.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                left, bottom, right, top = (float(v) for v in bbox)
                # points/bottom-left → pixels/top-left at render_scale.
                x0 = max(0, int(round(left * render_scale)))
                x1 = min(page_img.width, int(round(right * render_scale)))
                y0 = max(0, int(round((h_pts - top) * render_scale)))
                y1 = min(page_img.height, int(round((h_pts - bottom) * render_scale)))
                width_px, height_px = x1 - x0, y1 - y0
                if width_px <= 0 or height_px <= 0:
                    continue
                if width_px < MIN_IMAGE_DIMENSION and height_px < MIN_IMAGE_DIMENSION:
                    continue

                buf = io.BytesIO()
                page_img.crop((x0, y0, x1, y1)).save(buf, format="PNG")
                data = buf.getvalue()
                if len(data) < MIN_IMAGE_BYTES:
                    continue
                sha = hashlib.sha256(data).hexdigest()
                if sha in seen:
                    continue
                seen.add(sha)
                figures.append({
                    "page": page_num,
                    "width": width_px,
                    "height": height_px,
                    "sha256": sha,
                    "format": "png",
                    "image_bytes": data,
                })
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    logger.info(
        "extract_figures_from_layout: %d figure(s) from %d region(s) in %s",
        len(figures), len(figure_regions),
        os.path.basename(pdf_path) if pdf_path else "<unspecified>",
    )
    return figures


def generate_figure_descriptions(figures: list[dict], report_title: str) -> list[dict]:
    """Generate text descriptions for extracted figures.

    Uses rule-based heuristics based on page position and image size to
    create meaningful descriptions. A vision LLM can replace this later
    for richer descriptions.

    Args:
        figures: List from extract_figures_from_pdf
        report_title: Title of the source report for context

    Returns:
        Same list with 'description' field populated.
    """
    # Common NI 43-101 figure types by page range
    section_hints = {
        (1, 3): "Location map or property overview",
        (4, 6): "Regional geology or claims map",
        (7, 10): "Property geology or deposit cross-section",
        (11, 14): "Drill plan view or collar location map",
        (15, 18): "Mineral resource block model or grade-tonnage curve",
        (19, 25): "Exploration results or geophysical survey map",
    }

    for fig in figures:
        page = fig["page"]
        w, h = fig["width"], fig["height"]
        aspect = w / max(h, 1)

        # Determine likely figure type from page position
        fig_type = "Technical figure"
        for (start, end), hint in section_hints.items():
            if start <= page <= end:
                fig_type = hint
                break

        # Refine by aspect ratio
        if aspect > 1.5:
            shape = "wide/landscape format (likely a cross-section or profile)"
        elif aspect < 0.7:
            shape = "tall/portrait format (likely a strip log or column)"
        else:
            shape = "square format (likely a map or plan view)"

        fig["description"] = (
            f"Figure from {report_title}, page {page}: "
            f"{fig_type}. Image is {w}x{h} px, {shape}. "
            f"[SHA256: {fig['sha256']}]"
        )

    return figures


async def caption_image_with_vl(
    image_bytes: bytes,
    *,
    context: str | None = None,
    http_client: Any = None,
    timeout_s: float | None = None,
) -> str | None:
    """Return a 1-3 sentence Qwen3-VL description of a figure image, or None.

    The shared single-image VL call — used by describe_figures_with_vl and the
    ingest_pdf persist figure-captioning path. NEVER raises: returns None on any
    backend failure (down, timeout, empty reply) so callers keep their own
    fallback (heuristic text / docling caption). Reuses pdf_vl's backend config
    (PDF_VL_BACKEND_URL + resolved model id, i.e. the vllm-vl sidecar).
    """
    import httpx  # noqa: PLC0415

    from app.services.pdf_vl import _DEFAULT_BACKEND_URL, _resolve_model_id  # noqa: PLC0415

    backend_url = os.environ.get("PDF_VL_BACKEND_URL", _DEFAULT_BACKEND_URL).rstrip("/")
    endpoint = f"{backend_url}/chat/completions"
    model_id = _resolve_model_id()
    if timeout_s is None:
        timeout_s = float(os.environ.get("PDF_VL_TIMEOUT_S", "120"))

    prompt = _FIGURE_VL_PROMPT + (f"\n\nSource report: {context}." if context else "")
    body = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii"),
                        },
                    },
                ],
            }
        ],
        "stream": False,
        "temperature": 0.1,
        "max_tokens": 256,
    }

    own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s)
    try:
        resp = await client.post(endpoint, json=body, timeout=timeout_s)
        resp.raise_for_status()
        text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 — caller keeps its fallback
        logger.warning("caption_image_with_vl: VL failed (%s)", type(exc).__name__)
        return None
    finally:
        if own_client:
            await client.aclose()


async def describe_figures_with_vl(
    figures: list[dict],
    report_title: str,
    *,
    http_client: Any = None,
) -> list[dict]:
    """Populate fig['description'] via the Qwen3-VL sidecar (PDF_VL_BACKEND_URL).

    Content-aware alternative to generate_figure_descriptions' page-position
    heuristics. Heuristic descriptions are computed FIRST as a guaranteed
    fallback: any VL failure keeps the heuristic text for that figure, so
    indexing is never blocked. Figures are described sequentially — the vllm-vl
    sidecar runs few concurrent sequences and figure counts per report are small.

    Args:
        figures: from extract_figures_from_pdf (each has image_bytes/page/sha256).
        report_title: source report title, woven into the prompt + description.
        http_client: optional shared httpx.AsyncClient; a per-call client
            otherwise.

    Returns:
        The same list with 'description' set — VL where it succeeded, else the
        heuristic.
    """
    import httpx  # noqa: PLC0415

    # Heuristic descriptions first — a guaranteed fallback for every figure.
    generate_figure_descriptions(figures, report_title)
    if not figures:
        return figures

    timeout_s = float(os.environ.get("PDF_VL_TIMEOUT_S", "120"))
    own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s)
    try:
        for fig in figures:
            text = await caption_image_with_vl(
                fig["image_bytes"],
                context=report_title,
                http_client=client,
                timeout_s=timeout_s,
            )
            if text:
                fig["description"] = (
                    f"Figure from {report_title}, page {fig['page']}: {text} "
                    f"[SHA256: {fig['sha256']}]"
                )
    finally:
        if own_client:
            await client.aclose()

    return figures
