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
    import logging as _logging  # noqa: PLC0415
    use_gpu = paddleocr_use_gpu()
    # 2026-06-23 sweep — PaddleOCR 3.x migration (ADR-0016):
    #   use_angle_cls   -> use_textline_orientation
    #   use_gpu         -> device="gpu:0" | "cpu"
    #   show_log        -> dropped (logging module instead)
    #   det_model_dir   -> text_detection_model_dir
    #   rec_model_dir   -> text_recognition_model_dir
    #   cls_model_dir   -> textline_orientation_model_dir
    # The settings_used dict downstream still records `use_angle_cls`
    # (key kept stable for the silver schema) — only the call surface
    # changes here, not the persisted column name.
    _logging.getLogger("paddleocr").setLevel(_logging.WARNING)
    ocr = PaddleOCR(
        use_textline_orientation=effective_settings["use_angle_cls"],
        lang=lang,
        device="gpu:0" if use_gpu else "cpu",
        text_detection_model_dir=_model_dir("det", lang),
        text_recognition_model_dir=_model_dir("rec", lang),
        textline_orientation_model_dir=_model_dir("cls", lang),
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
                # 2026-06-23 sweep — PaddleOCR 3.x migration (ADR-0016):
                # `ocr.ocr(arr)` -> `ocr.predict(arr)`.
                result = ocr.predict(arr)
            except Exception as exc:  # PaddleOCR can throw on degenerate pages
                # Record the failure as a zero-confidence page; the
                # caller (quality_graph) will route to retry/review.
                per_page_ocr_confidence.append(0.0)
                per_page_text_line_counts.append(0)
                per_page_deskew_applied.append(effective_settings["use_angle_cls"])
                per_page_rotation_applied.append(0.0)
                per_page_retry_counts.append(0)
                continue

            # PaddleOCR 3.x return shape: list[OCRResult]. Each result
            # has rec_texts/rec_scores/rec_boxes (axis-aligned already
            # in pixel coords). The _flatten_paddleocr_result helper that
            # normalised the 2.x nested-tuple shape is no longer needed —
            # kept in the module for reference + the smoke-test path.
            page_result = result[0] if (result and result[0] is not None) else None
            if page_result is None:
                per_page_ocr_confidence.append(0.0)
                per_page_text_line_counts.append(0)
                per_page_deskew_applied.append(effective_settings["use_angle_cls"])
                per_page_rotation_applied.append(0.0)
                per_page_retry_counts.append(0)
                continue

            texts = getattr(page_result, "rec_texts", []) or []
            scores = getattr(page_result, "rec_scores", []) or []
            boxes = getattr(page_result, "rec_boxes", None)

            confidences: list[float] = []
            region_idx = 0
            for idx, (text, conf) in enumerate(zip(texts, scores)):
                if not text:
                    continue
                # rec_boxes is already axis-aligned [x_min, y_min, x_max,
                # y_max] in pixel coords — no min/max-over-corners needed.
                if boxes is not None and idx < len(boxes):
                    bbox = [
                        round(float(boxes[idx][0]), 3),
                        round(float(boxes[idx][1]), 3),
                        round(float(boxes[idx][2]), 3),
                        round(float(boxes[idx][3]), 3),
                    ]
                else:
                    bbox = [0.0, 0.0, 0.0, 0.0]
                passages.append({
                    "page": page_idx,
                    "region": region_idx,
                    "bbox": bbox,
                    "source_method": "paddleocr_pp_ocrv5",
                    "extraction_confidence": float(conf),
                    "text_content": str(text),
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
    """LEGACY (PaddleOCR 2.x): normalize the nested ``[[bbox, (text, conf)], ...]``
    return shape into a flat list. Retained for two reasons:
      1. The ocr_cpu_smoke validation script (ops/validation/ocr_cpu_smoke.py)
         still references it to assert the 2.x→3.x cutover happened.
      2. Anyone hitting a 2.x model output (e.g. via a third-party PaddleOCR
         build, or a stubbed test fixture) can still parse it.

    The main code path was rewritten in the 2026-06-23 PaddleOCR 3.x migration
    (ADR-0016) to consume `result[0].rec_texts / rec_scores / rec_boxes`
    directly — no flattening required.
    """
    if not result:
        return []
    # 3.x guard: if the first element looks like a PaddleOCR 3.x OCRResult
    # (has rec_texts/rec_scores attributes), the legacy helper can't parse
    # it and the caller should be using the attribute access pattern instead.
    if result and hasattr(result[0], "rec_texts"):
        return []
    # Unwrap a one-element batch list if present
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], list):
        candidate = result[0]
        if candidate and isinstance(candidate[0], list) and len(candidate[0]) == 2:
            return candidate
    if isinstance(result, list) and result and isinstance(result[0], list) and len(result[0]) == 2:
        return result
    return []
