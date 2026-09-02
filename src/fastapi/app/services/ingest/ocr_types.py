"""Engine-neutral OCR result types shared by every remote OCR adapter.

`PageOcrResult` is the shape `pdf_report._ocr_single_page` and the batched
page paths consume. It was born inside the Azure Document Intelligence
adapter (2026-07-28) and moved here when the engine behind it changed
(Cohere Parse v5, ADR-0019): the parser must not import a vendor module
just to name the dataclass its fallback ladder passes around.

Confidence semantics
--------------------
`mean_confidence` is a float for API stability, but not every engine
reports one. Document Intelligence and Tesseract average per-word
confidences; Cohere Parse is a vision-language model that returns text
with no per-token confidence at all. `confidence_reported=False` tells the
quality router (`ocr_quality`) to skip the confidence-based bands and
judge the page on its content signals only, and tells the persist path to
store `ocr_confidence` as NULL rather than a fabricated 0.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One OCR word with page-local pixel coordinates (engines that report them)."""

    text: str
    confidence: float
    polygon: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class PageOcrResult:
    """Same (text, mean_confidence) shape as pdf_report._ocr_single_page
    with return_confidence=True, so a caller can select an engine without
    changing its own downstream handling.
    """

    text: str
    mean_confidence: float  # 0.0-1.0; meaningless when confidence_reported is False
    words: tuple[OcrWord, ...] = ()
    detected_region_count: int = 0
    request_succeeded: bool = True
    error: str | None = None
    # Row-major text grids: tables[t][row][col]. [] when the engine returned
    # no table structure and on the failure sentinels.
    tables: list[list[list[str]]] = field(default_factory=list)
    #: False for engines that return no per-word confidence (Cohere Parse).
    confidence_reported: bool = True


__all__ = ["OcrWord", "PageOcrResult"]
