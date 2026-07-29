"""Azure Document Intelligence OCR adapter (#28, 2026-07-28).

STATUS: standalone and unit-tested, but NOT wired into the live ingest
path. `pdf_report.py`'s Tesseract calls (`_ocr_single_page`,
`_attempt_ocr`) stay authoritative until a real Azure resource exists to
regression-test against a scanned NI 43-101 corpus — that rewiring is a
separate, hands-on follow-up, not something to do blind.

Why this exists now anyway: it lets the engine swap be prepped (config
seam, client wrapper, dependency, tests) without touching
`pdf_report.py`'s fragile fitz/tesseract merge logic, which the file's
own comments warn is easy to break silently (wire identifiers like
``parser_used == "fitz"`` gate the docling merge).

Gated by ``OCR_ENGINE`` (default ``"tesseract"`` — i.e. this module is
inert unless something explicitly opts in), mirroring the
``os.environ.get(...)``-based flag convention `pdf_report.py` already
uses for `PDF_PARSER_TESSERACT_FALLBACK_ENABLED` rather than routing
through `app.config.Settings`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("georag.ingest.document_intelligence")

ENDPOINT_ENV = "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
KEY_ENV = "AZURE_DOCUMENT_INTELLIGENCE_KEY"
ENGINE_ENV = "OCR_ENGINE"
_MODEL_ID = "prebuilt-read"


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
    return os.environ.get(ENGINE_ENV, "tesseract").strip().lower() == (
        "azure_document_intelligence"
    )


def is_configured() -> bool:
    """True when both endpoint and key are present in the environment."""
    return bool(os.environ.get(ENDPOINT_ENV)) and bool(os.environ.get(KEY_ENV))


@dataclass(frozen=True, slots=True)
class PageOcrResult:
    """Same (text, mean_confidence) shape as pdf_report._ocr_single_page
    with return_confidence=True, so a future caller can select an engine
    without changing its own downstream handling.
    """

    text: str
    mean_confidence: float  # 0.0-1.0, averaged over word-level confidences


def _build_client():
    # Imported lazily so `azure-ai-documentintelligence` being installed
    # doesn't force-import at module load for callers that never use it.
    from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential

    endpoint = os.environ.get(ENDPOINT_ENV)
    key = os.environ.get(KEY_ENV)
    if not endpoint or not key:
        raise DocumentIntelligenceNotConfigured(
            f"{ENDPOINT_ENV} and {KEY_ENV} must both be set to use the "
            "azure_document_intelligence OCR engine."
        )
    return DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))


async def ocr_page(pdf_bytes: bytes, page_num: int) -> PageOcrResult:
    """OCR one page of a PDF via Azure Document Intelligence's prebuilt-read model.

    Unlike the Tesseract path, this does not need local rasterisation
    (pdf2image) — Document Intelligence accepts raw PDF bytes plus a
    1-indexed `pages` selector and returns word-level text + confidence
    directly.

    Fails soft (returns an empty PageOcrResult) on any per-call error,
    matching `_ocr_single_page`'s behavior — the only exception this
    raises is `DocumentIntelligenceNotConfigured`, which is a startup/
    config error a caller should surface loudly rather than swallow.
    """
    client = _build_client()
    try:
        async with client:
            poller = await client.begin_analyze_document(
                _MODEL_ID,
                body=pdf_bytes,
                pages=str(page_num),
            )
            result = await poller.result()
    except DocumentIntelligenceNotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "document_intelligence: OCR failed on page %d: %s", page_num, exc,
        )
        return PageOcrResult("", 0.0)

    pages = getattr(result, "pages", None) or []
    words = [w for page in pages for w in (page.words or [])]
    text = " ".join(w.content for w in words if w.content)
    confidences = [w.confidence for w in words if w.confidence is not None]
    mean_confidence = (sum(confidences) / len(confidences)) if confidences else 0.0
    mean_confidence = max(0.0, min(1.0, mean_confidence))
    return PageOcrResult(text=text.strip(), mean_confidence=mean_confidence)


__all__ = [
    "ENDPOINT_ENV",
    "KEY_ENV",
    "ENGINE_ENV",
    "DocumentIntelligenceNotConfigured",
    "PageOcrResult",
    "is_engine_selected",
    "is_configured",
    "ocr_page",
]
