"""Lossless multi-page TIFF → PDF normalisation (ADR-0005).

Wraps the per-frame image data from a TIFF into a single multi-page PDF
container so the §04p PDF stack (docling, PaddleOCR, tesseract psm=3,
preprocessing, ocr_confidence capture, figure linking, p04p_dual_write)
can run on TIFF-sourced documents.

Why PIL not img2pdf:
  * img2pdf has tighter JPEG-in-TIFF passthrough but isn't installed in
    the running fastapi image — would need a rebuild.
  * PIL.Image.save(format="PDF", save_all=True, append_images=...) ships
    in Pillow which is already in the image. It DOES decode and re-wrap
    each frame, but the wrap is to PDF/Flate so the pixel data round-trips
    losslessly for the formats we see in practice (B&W CCITT, grayscale
    LZW, RGB scans).
  * Quality budget: the downstream §04p stack is what determines OCR
    quality; the wrap only needs to preserve every pixel, which PIL does.

Multi-page handling: PIL.ImageSequence walks every frame; we cap at
``MAX_FRAMES`` to bound memory on pathological inputs (10k-frame
satellite stacks, fax archives). Default 500 matches a generous
practical NI 43-101 scan budget.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

log = logging.getLogger("georag.ingest.tiff_to_pdf")

# Bound the in-memory wrap. The old tiff_ocr_ingester had a 50-page cap
# that silently dropped pages past 50; this is a much higher safety
# ceiling, intended to protect against pathological inputs rather than
# constrain legitimate documents.
MAX_FRAMES = 500

# Cap raw input size at 2 GB to match the Laravel upload ceiling
# (see [[upload-size-stack-2026-05-21]]). Larger files belong on the
# silver_raster path, not document OCR.
MAX_TIFF_BYTES = 2 * 1024 * 1024 * 1024


@dataclass
class TiffNormalizeResult:
    pdf_bytes: bytes
    page_count: int
    source_bytes: int
    truncated_at_cap: bool  # True if input had more frames than MAX_FRAMES


class TiffNormalizeError(Exception):
    """Raised when img → PDF wrap fails for a recoverable reason.

    Distinct from arbitrary exceptions so the Hatchet workflow can route
    these to the IngestQuality admin surface (manual triage) rather than
    retry forever.
    """


def tiff_to_pdf(source_bytes: bytes) -> TiffNormalizeResult:
    """Convert multi-page TIFF bytes to multi-page PDF bytes (lossless).

    Pillow walks every frame of the input TIFF; the first frame becomes
    the cover page and the rest are appended. Each frame is converted to
    RGB if it's in a mode PIL's PDF writer can't embed directly
    (palette-mode 'P', LA, CMYK without ICC) — preserves all pixel data.

    Returns
    -------
    TiffNormalizeResult
        pdf_bytes : the wrapped multi-page PDF (uncompressed Flate inside)
        page_count : number of frames actually written
        source_bytes : input size
        truncated_at_cap : True iff the input had more than MAX_FRAMES
            frames and we stopped early. Caller should log a warning.

    Raises
    ------
    TiffNormalizeError
        on oversized input or on a Pillow decode failure that PIL
        surfaces as ``UnidentifiedImageError`` / generic IOError.
    """
    if not source_bytes:
        raise TiffNormalizeError("empty input")
    if len(source_bytes) > MAX_TIFF_BYTES:
        raise TiffNormalizeError(
            f"input exceeds {MAX_TIFF_BYTES} bytes ({len(source_bytes)})"
        )

    # Pillow only — no img2pdf dependency. Import locally so test code
    # without Pillow installed can still import this module.
    try:
        from PIL import Image, ImageSequence  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover — PIL is in fastapi image
        raise TiffNormalizeError(f"Pillow not available: {exc}") from exc

    # Lift PIL's default decompression-bomb threshold; WSGS / NI 43-101
    # scans routinely exceed it. We've already capped the *byte* size
    # above; pixel-count guard is delegated to MAX_FRAMES + per-frame
    # PIL behaviour (drops to None == "no limit").
    Image.MAX_IMAGE_PIXELS = None

    try:
        src = Image.open(io.BytesIO(source_bytes))
    except Exception as exc:
        raise TiffNormalizeError(f"PIL.Image.open failed: {exc}") from exc

    frames: list[Image.Image] = []
    truncated = False
    for i, frame in enumerate(ImageSequence.Iterator(src)):
        if i >= MAX_FRAMES:
            truncated = True
            break
        frames.append(_normalise_frame_mode(frame))

    if not frames:
        raise TiffNormalizeError("no frames in TIFF")

    pdf_buf = io.BytesIO()
    cover, *rest = frames
    try:
        cover.save(
            pdf_buf,
            format="PDF",
            save_all=True,
            append_images=rest,
            # Resolution metadata — best-effort from the TIFF; the §04p
            # stack uses pdf2image at fixed DPI so this is informational.
            resolution=_effective_dpi(src),
        )
    except Exception as exc:
        raise TiffNormalizeError(f"PIL PDF write failed: {exc}") from exc

    pdf_bytes = pdf_buf.getvalue()
    log.info(
        "tiff_to_pdf.ok frames=%d truncated=%s in_bytes=%d out_bytes=%d",
        len(frames), truncated, len(source_bytes), len(pdf_bytes),
    )
    return TiffNormalizeResult(
        pdf_bytes=pdf_bytes,
        page_count=len(frames),
        source_bytes=len(source_bytes),
        truncated_at_cap=truncated,
    )


def _normalise_frame_mode(frame):
    """Convert frames to a mode PIL's PDF writer accepts cleanly.

    PIL's PDF writer handles 1, L, RGB, CMYK natively. Palette ('P'),
    'LA', 'RGBA', and 'I;16' need conversion. We pick the smallest mode
    that preserves the data:
      * 1-bit ('1') stays as-is — bilevel scans (CCITT/G4) are common
        in fax-grade TIFFs and round-trip without colour blow-up.
      * 'L' (grayscale) stays as-is.
      * 'P' (palette) → RGB to preserve colour.
      * 'LA' → 'L' (drop alpha; documents don't need transparency).
      * 'RGBA' → 'RGB' (drop alpha).
      * 'I;16' (16-bit grayscale, rare for documents) → 'L' (8-bit).
      * Anything else (CMYK, YCbCr, etc.) → RGB.
    """
    mode = frame.mode
    if mode in ("1", "L", "RGB", "CMYK"):
        return frame.copy()
    if mode == "LA":
        return frame.convert("L")
    if mode == "RGBA":
        return frame.convert("RGB")
    if mode == "I;16":
        return frame.convert("L")
    if mode == "P":
        return frame.convert("RGB")
    return frame.convert("RGB")


def _effective_dpi(src) -> float:
    """Pull a reasonable DPI from the TIFF metadata, defaulting to 300.

    NI 43-101 scans are typically 200-300 DPI; the §04p stack re-renders
    via pdf2image at 250 DPI for OCR regardless, so this metadata is for
    downstream-tool consumption only (e.g. an IngestQuality preview).
    """
    info = getattr(src, "info", {}) or {}
    dpi = info.get("dpi")
    if isinstance(dpi, tuple) and dpi:
        try:
            return float(dpi[0])
        except (TypeError, ValueError):
            pass
    return 300.0


__all__ = [
    "tiff_to_pdf",
    "TiffNormalizeResult",
    "TiffNormalizeError",
    "MAX_FRAMES",
    "MAX_TIFF_BYTES",
]
