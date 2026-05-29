"""PDF figure extraction and vision-based indexing.

⚠️ TEMPORARILY STUBBED — CC-1 audit follow-up (2026-05-27)

PyMuPDF (`fitz`) is AGPL-3.0 and was on the project's permanent-reject
list per plan §"What is deliberately not in this plan". The dependency
has been REMOVED from pyproject.toml in favour of permissive
alternatives (pypdfium2 — Apache 2.0; pypdf — BSD-3).

This module's two functions (`extract_figures_from_pdf` and
`extract_and_index_figures`) now return [] / 0 respectively. They
preserve their public signatures so callers (e.g. ingest_pdf workflow
steps that call `extract_and_index_figures` opportunistically) keep
working without code changes; the result is that figure descriptions
are not indexed into Qdrant until a permissive replacement lands.

Follow-up work (planned, not in this session):

  1. Rewrite `extract_figures_from_pdf` against pypdfium2's
     `PdfDocument` + `PdfPage.get_objects(filter=...)` API. The
     equivalent fitz calls were:
        fitz.open(path)             → pdfium.PdfDocument(path)
        len(doc)                    → len(pdf)
        doc[page_num]               → pdf[page_num]
        page.get_images(full=True)  → page.get_objects() filtered by
                                       type == OBJECT_IMAGE
        doc.extract_image(xref)     → image_obj.get_bitmap().to_pil()
                                       → io.BytesIO buffer
        doc.close()                 → pdf.close()

  2. Verify against a real NI 43-101 PDF that figure counts + bytes +
     dimensions match (within tolerance) what the old fitz path
     produced. Memory `project_overnight_run_2026_05_22` references
     figure_extractor + minio presigning — preserve that contract.

  3. Re-enable indexing into the `georag_reports` Qdrant collection;
     the embedding + payload shape is unchanged.

For now, callers see a no-op. Figure-description Qdrant points
written before this change remain indexed — only new ingest runs
miss figure descriptions until the rewrite lands.
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
    """STUB — CC-1 audit follow-up. PyMuPDF removed (AGPL).

    See module docstring for rewrite plan. Returns [] so callers keep
    working without code changes; ingest pipelines just don't get
    figure-description Qdrant entries until the pypdfium2 rewrite
    lands.

    Returns:
        Always an empty list.
    """
    logger.info(
        "figure_extractor.extract_figures_from_pdf: stub — PyMuPDF (AGPL) "
        "removed pending pypdfium2 rewrite. Path skipped: %s",
        os.path.basename(pdf_path) if pdf_path else "<unspecified>",
    )
    return []


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
