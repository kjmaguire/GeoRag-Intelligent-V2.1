"""§04p scanned parser — PaddleOCR PP-OCRv5 CPU on pre-rendered page images.

**Master-plan §9.3 / §9.4 reference.** Path for PDFs classified as
``scanned`` — no text layer, OCR required. Per ADR-0002, runs on CPU
only.

**Status:** Step 4 implementation (doc-phase 52).

**Why image-input not PDF-input:** PaddleOCR's PDF-input path requires
PyMuPDF (``fitz``) which is NOT installed in the FastAPI image (AGPL
license clashes with our MIT/BSD/Apache 2.0 rule). We pre-render each
page to a numpy array via pypdfium2 and pass the array to
``PaddleOCR.ocr()`` — the image-input path is fitz-free. Validated
by the smoke-bench at ``ops/validation/ocr_cpu_smoke.py``.

Measured CPU latency baseline (2026-05-12, image-input path,
Threadripper 5955WX, 6-CPU WSL2 container):
- Cold: ~8.5 sec/page (first PaddleOCR instantiation in a worker process)
- Warm: ~6.1 sec/page

Retry policy (per kickoff Step 4): the parse_scanned function does
ONE OCR pass per call. The retry loop lives in
``app.ocr.quality_graph`` (Step 6) which decides whether to call
parse_scanned again with different `settings` based on confidence
thresholds.

Output schema (locked here):
    {
        "passages": [
            {
                "page": int,
                "region": int,
                "bbox": [x0, y0, x1, y1],
                "source_method": "paddleocr_pp_ocrv5",
                "extraction_confidence": float,
                "text_content": str,
            },
            ...
        ],
        "parser_used": "scanned_paddleocr",
        "page_count": int,
        "per_page_ocr_confidence": list[float],        # mean per-page confidence
        "per_page_text_line_counts": list[int],
        "per_page_deskew_applied": list[bool],
        "per_page_rotation_applied": list[float],     # degrees
        "per_page_retry_counts": list[int],            # always 0 here; quality_graph increments
        "settings_used": dict,
    }
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Sequence


# PaddleOCR model cache root. The default ($HOME/.paddleocr) is not
# writable for the FastAPI container's www-data user. Use /tmp so OCR
# works uniformly across containers regardless of which user the process
# runs as. Operators who want a persistent (not /tmp) cache should
# set PADDLEOCR_HOME at container startup.
import os as _os
_PADDLEOCR_HOME = _os.environ.get("PADDLEOCR_HOME", "/tmp/.paddleocr")


# Default PaddleOCR settings. Step 6's quality_graph may override via
# the `settings` argument for retry passes.
DEFAULT_SCANNED_SETTINGS: dict[str, Any] = {
    "use_angle_cls": True,    # auto-rotation detection
    "lang": "en",
    "render_scale": 2.0,       # pypdfium2 render scale (~144 DPI)
}


def _model_dir(category: str, lang: str) -> str:
    """Build the explicit PaddleOCR model dir path for a category.

    PaddleOCR's path convention is:
        {root}/whl/{category}/{lang_or_global}/{model_name}/
    where:
      - det → per-language
      - rec → per-language
      - cls → global (no lang subdir; PaddleOCR uses a fixed name)
    """
    base = f"{_PADDLEOCR_HOME}/whl/{category}"
    if category == "cls":
        return f"{base}/ch_ppocr_mobile_v2.0_cls_infer"
    # Per-language det + rec
    name_lang = "en" if lang == "en" else lang
    if category == "det":
        return f"{base}/{name_lang}/en_PP-OCRv3_det_infer"
    if category == "rec":
        return f"{base}/{name_lang}/en_PP-OCRv4_rec_infer"
    return base


async def parse_scanned(
    pdf_path: Path,
    pages: Sequence[int] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """OCR a scanned-profile PDF via PaddleOCR PP-OCRv5 CPU image-input.

    Args:
        pdf_path: Local filesystem path. Caller pre-validates via
            preflight + profile.
        pages: 0-indexed page numbers to OCR. ``None`` = all pages.
        settings: optional overrides for DEFAULT_SCANNED_SETTINGS.
            Step 6's quality_graph uses this for retry passes with
            escalating engine settings (e.g. binarization threshold,
            language hint).

    Returns:
        Parse result dict (see module docstring for schema).
    """
    return await asyncio.to_thread(_parse_scanned_sync, pdf_path, pages, settings)


def _parse_scanned_sync(
    pdf_path: Path,
    pages: Sequence[int] | None,
    settings: dict[str, Any] | None,
) -> dict[str, Any]:
    """Synchronous implementation; called via asyncio.to_thread.

    PaddleOCR is instantiated per call. The first call in a worker
    process pays the ~3 sec cold-start; subsequent calls in the same
    process are warm because PaddleOCR caches model weights internally.
    """
    import numpy as np
    import pypdfium2 as pdfium
    from paddleocr import PaddleOCR

    effective_settings = {**DEFAULT_SCANNED_SETTINGS, **(settings or {})}
    render_scale = float(effective_settings.get("render_scale", 2.0))

    lang = effective_settings["lang"]
    # Ensure the cache root exists + is writable. PaddleOCR will create
    # subdirs under it as needed when it downloads.
    import os as _os_local
    _os_local.makedirs(_PADDLEOCR_HOME, exist_ok=True)

    # Phase 9 (2026-05-22) — env-gated GPU acceleration. Same helper
    # used by services/pdf_ocr.py so routing is consistent across both
    # PaddleOCR call sites.
    from app.ocr._paddleocr_gpu import paddleocr_use_gpu  # noqa: PLC0415
    use_gpu = paddleocr_use_gpu()
    ocr = PaddleOCR(
        use_angle_cls=effective_settings["use_angle_cls"],
        lang=lang,
        use_gpu=use_gpu,
        show_log=False,
        det_model_dir=_model_dir("det", lang),
        rec_model_dir=_model_dir("rec", lang),
        cls_model_dir=_model_dir("cls", lang),
    )

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        if pages is None:
            page_indices = list(range(len(pdf)))
        else:
            page_indices = [p for p in pages if 0 <= p < len(pdf)]

        passages: list[dict[str, Any]] = []
        per_page_ocr_confidence: list[float] = []
        per_page_text_line_counts: list[int] = []
        per_page_deskew_applied: list[bool] = []
        per_page_rotation_applied: list[float] = []
        per_page_retry_counts: list[int] = []

        for page_idx in page_indices:
            bitmap = pdf[page_idx].render(scale=render_scale)
            arr = np.asarray(bitmap.to_pil().convert("RGB"))

            try:
                result = ocr.ocr(arr)
            except Exception as exc:  # PaddleOCR can throw on degenerate pages
                # Record the failure as a zero-confidence page; the
                # caller (quality_graph) will route to retry/review.
                per_page_ocr_confidence.append(0.0)
                per_page_text_line_counts.append(0)
                per_page_deskew_applied.append(effective_settings["use_angle_cls"])
                per_page_rotation_applied.append(0.0)
                per_page_retry_counts.append(0)
                continue

            page_lines = _flatten_paddleocr_result(result)
            confidences: list[float] = []
            region_idx = 0
            for line in page_lines:
                bbox_4pt, text_conf = line
                if not text_conf or len(text_conf) < 2:
                    continue
                text, conf = text_conf[0], text_conf[1]
                if not text:
                    continue
                # Convert the 4-corner-point box to a (x0,y0,x1,y1) bbox.
                xs = [float(p[0]) for p in bbox_4pt]
                ys = [float(p[1]) for p in bbox_4pt]
                bbox = [
                    round(min(xs), 3),
                    round(min(ys), 3),
                    round(max(xs), 3),
                    round(max(ys), 3),
                ]
                passages.append({
                    "page": page_idx,
                    "region": region_idx,
                    "bbox": bbox,
                    "source_method": "paddleocr_pp_ocrv5",
                    "extraction_confidence": float(conf),
                    "text_content": text,
                })
                confidences.append(float(conf))
                region_idx += 1

            mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
            per_page_ocr_confidence.append(round(mean_conf, 4))
            per_page_text_line_counts.append(len(confidences))
            per_page_deskew_applied.append(effective_settings["use_angle_cls"])
            per_page_rotation_applied.append(0.0)  # use_angle_cls handles internally
            per_page_retry_counts.append(0)

        return {
            "passages": passages,
            "parser_used": "scanned_paddleocr",
            "page_count": len(page_indices),
            "per_page_ocr_confidence": per_page_ocr_confidence,
            "per_page_text_line_counts": per_page_text_line_counts,
            "per_page_deskew_applied": per_page_deskew_applied,
            "per_page_rotation_applied": per_page_rotation_applied,
            "per_page_retry_counts": per_page_retry_counts,
            "settings_used": effective_settings,
        }
    finally:
        try:
            pdf.close()
        except Exception:
            pass


def _flatten_paddleocr_result(result: Any) -> list[tuple[Any, Any]]:
    """Normalize PaddleOCR's nested result shape into a flat list of
    ``(bbox_4pt, (text, confidence))`` tuples.

    PaddleOCR's `ocr()` return shape varies by version:
    - 2.6+: ``[[ [bbox, (text, conf)], ... ]]`` (outer list = batch of 1 image)
    - older: ``[ [bbox, (text, conf)], ... ]``
    This helper handles both.
    """
    if not result:
        return []
    # Unwrap a one-element batch list if present
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], list):
        candidate = result[0]
        if candidate and isinstance(candidate[0], list) and len(candidate[0]) == 2:
            return candidate
    if isinstance(result, list) and result and isinstance(result[0], list) and len(result[0]) == 2:
        return result
    return []
