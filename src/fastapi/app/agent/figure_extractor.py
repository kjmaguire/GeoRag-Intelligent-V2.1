"""PDF figure extraction and vision-based indexing.

`extract_figures_from_pdf` extracts embedded raster images via pypdfium2
(Apache-2.0) — the permissive replacement for the original PyMuPDF (`fitz`,
AGPL-3.0) path, which was on the permanent-reject list and removed from
pyproject.toml (2026-05-27 CC-1 audit). Restored 2026-06-24.

Pipeline: extract_figures_from_pdf → generate_figure_descriptions →
index_figures_to_qdrant (collection `georag_reports`). `extract_and_index_figures`
runs all three. Today the only caller is the standalone scripts/index_figures.py;
the §04p ingest workflow does NOT call it yet.

Remaining follow-ups (each a separate piece of work):
  1. **Vector figures.** Embedded-image extraction misses figures drawn as
     vector graphics (many geological cross-sections / plan maps). A
     layout-region render (use the §04p `layouts` figure bboxes + pypdfium2
     page render + crop) would capture those too.
  2. **VL descriptions.** generate_figure_descriptions is still rule-based
     (page-position + aspect-ratio heuristics). Wiring it to the Qwen3-VL
     sidecar (app.services.pdf_vl, served via vllm-vl) would give real
     content-aware descriptions — that's where the local VL serving pays off.
  3. **Auto-ingest wiring.** Call extract_and_index_figures from the ingest_pdf
     workflow so figures are indexed during normal ingest, not only via the
     manual script.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Minimum image size thresholds to filter out icons/logos/bullets
MIN_IMAGE_BYTES = 5_000       # 5 KB
MIN_IMAGE_DIMENSION = 100     # 100 px width or height


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


async def index_figures_to_qdrant(
    figures: list[dict],
    report_id: str,
    project_id: str,
    report_title: str,
    qdrant_client: Any,
    embedding_model: Any,
    collection: str = "georag_reports",
) -> int:
    """Embed figure descriptions and upsert into Qdrant.

    Returns the number of points upserted.
    """
    if not figures:
        return 0

    texts = [f["description"] for f in figures]
    vectors = embedding_model.encode(texts).tolist()

    from qdrant_client.models import PointStruct

    points = []
    for fig, vec in zip(figures, vectors):
        point_id = str(uuid.uuid4())
        points.append(
            PointStruct(
                id=point_id,
                vector=vec,
                payload={
                    "text": fig["description"],
                    "document_title": report_title,
                    "document_type": "NI43",
                    "report_id": report_id,
                    "project_id": project_id,
                    "section_number": f"Figure (page {fig['page']})",
                    "section_title": f"Extracted figure — page {fig['page']}",
                    "page": fig["page"],
                    "content_type": "figure_description",
                    "figure_sha256": fig["sha256"],
                    "figure_width": fig["width"],
                    "figure_height": fig["height"],
                },
            )
        )

    await qdrant_client.upsert(collection_name=collection, points=points)

    logger.info(
        "figure_extractor: indexed %d figure descriptions into %s",
        len(points),
        collection,
    )

    return len(points)


async def extract_and_index_figures(
    pdf_path: str,
    report_id: str,
    project_id: str,
    report_title: str,
    qdrant_client: Any,
    embedding_model: Any,
) -> int:
    """Full pipeline: extract → describe → embed → index.

    Returns the number of figures indexed.
    """
    figures = extract_figures_from_pdf(pdf_path)
    if not figures:
        return 0

    figures = generate_figure_descriptions(figures, report_title)
    count = await index_figures_to_qdrant(
        figures, report_id, project_id, report_title,
        qdrant_client, embedding_model,
    )
    return count
