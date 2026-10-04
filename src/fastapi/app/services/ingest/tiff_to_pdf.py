"""Image -> PDF normalisation (ADR-0005): pixel-exact where it can be.

Wraps the per-frame image data from a TIFF - or a standalone scanned image
(JPEG, PNG, BMP, GIF, WebP) - into a single PDF container so the standard PDF
parser and OCR provenance path can run on image-sourced documents.

What "lossless" means here (this module used to claim more than it did)
-----------------------------------------------------------------------
Every frame is DECODED and re-encoded; nothing is passed through. What the
re-encode costs depends on the page mode, and Pillow's own PDF writer is NOT
lossless for most of them:

  * mode ``1`` (bilevel, the fax / CCITT-G4 scans): written by Pillow as
    CCITT G4, which is lossless for a bilevel image.
  * ``L`` / ``RGB`` / ``CMYK``: Pillow's ``save(format="PDF")`` writes these as
    **JPEG (DCTDecode) at its default quality**, so a clean 300 dpi greyscale
    scan of 6-pt assay tables came out with JPEG ringing around every glyph -
    the exact input the OCR engine reads worst. They are therefore written
    here as **Flate (zlib) image XObjects built with pikepdf** (already a
    dependency, used for the merge), which round-trips every pixel.
  * Palette, alpha and 16-bit modes are converted first (see
    ``_normalise_frame_mode``); that conversion is the one lossy-by-nature step
    (16 -> 8 bit).

Flate output is much larger than JPEG for photographic colour pages, and the
derived PDF goes to ingest_pdf, which refuses anything over the upload ceiling
(GEORAG_MAX_UPLOAD_BYTES). So the lossless budget is bounded
(``_LOSSLESS_BUDGET_FRACTION`` of that ceiling): once the Flate pages written
so far reach it, the remaining L / RGB pages are JPEG-encoded at quality 92
with no chroma subsampling, and the result says how many
(``pages_jpeg_encoded``) so the workflow can warn. That is a degraded page the
operator is told about, not a silent one. If the Flate writer itself fails for
a page, Pillow's writer is the fallback.

Memory: one decoded frame at a time, plus the finished PDF. Each page is
written to a temp file as its own one-page PDF and released, and the pages
are then merged with pikepdf from those files. This used to decode every
frame into a list and hand it to one ``save(append_images=)`` call, so a
500-page RGB scan at 300 dpi (about 26 MB decoded per page) held roughly 13 GB
at once on a worker that runs many slots. Holding the one-page PDFs in memory
instead would still cost ~2x the compressed output (the pages plus the merged
copy), which with Flate pages is hundreds of MB, hence the spool. Two
alternatives were measured and rejected:

  * ``save(..., append=True)`` page by page (Pillow's own incremental
    append) is quadratic: 100 pages took 2.3 s and 500 pages ran past two
    minutes, and the file keeps every superseded catalog.
  * appending in chunks of seven pages still took ~25 s for 500 bilevel A4
    pages and produced a 4.7 MB file against 0.7 MB for the pikepdf merge
    (8 s end to end, including generating the test images).

Why not img2pdf: it is not installed in the fastapi image (it would need a
rebuild), and it only passes JPEG-in-TIFF through untouched; it does not
change the answer for the greyscale / LZW / bilevel scans that dominate.

Which formats are page sequences: only TIFF. A GIF or WebP "frame" is an
animation frame and an APNG's are too — turning a 300-frame animated GIF into
300 OCR pages is a bill and a pile of noise passages, not a document. For
those formats only the first frame is wrapped, and the result says how many
were left out (``frames_ignored``) so the workflow can warn.

Frame cap: ``MAX_FRAMES`` bounds pathological inputs (10k-frame satellite
stacks, fax archives). Hitting it is reported, never silent
(``truncated_at_cap`` and ``total_frames``).

Orientation: JPEG and phone-photographed logs carry an EXIF Orientation tag
instead of rotated pixels, and the §04p stack never reads EXIF — a portrait
photo taken in landscape would be OCR'd on its side and return noise. Each
frame is transposed to upright before it is wrapped.
"""
from __future__ import annotations

import io
import logging
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from app.services.ingest.upload_limits import max_upload_bytes

log = logging.getLogger("georag.ingest.tiff_to_pdf")

# Bound the wrap. The old tiff_ocr_ingester had a 50-page cap that silently
# dropped pages past 50; this is a much higher safety ceiling, intended to
# protect against pathological inputs rather than constrain legitimate
# documents.
MAX_FRAMES = 500

# Cap raw input size at the upload ceiling, GEORAG_MAX_UPLOAD_BYTES (512 MiB
# by default; see upload_limits and [[upload-size-stack-2026-05-21]]). This was
# a hard-coded 2 GB "to match" a Laravel ceiling that had since been lowered.
# Larger files belong on the silver_raster path, not document OCR.
MAX_TIFF_BYTES = max_upload_bytes()

#: Image formats (``PIL.Image.format``) whose frames are PAGES. Every other
#: format contributes its first frame only.
_PAGE_SEQUENCE_FORMATS = frozenset({"TIFF"})

#: Fraction of the upload ceiling the Flate (lossless) pages may add up to
#: before the remaining L / RGB pages are JPEG-encoded instead. The derived
#: PDF must itself pass ingest_pdf's size check, so the lossless pages cannot
#: be allowed all of it.
_LOSSLESS_BUDGET_FRACTION = 0.75

#: JPEG quality for pages past the lossless budget. High, and 4:4:4 (no chroma
#: subsampling), because thin coloured linework and small print are what the
#: default settings smear.
_FALLBACK_JPEG_QUALITY = 92

#: Below this a 16-bit frame is treated as a 12-bit (or lower) scan stored in
#: 16-bit words, and scaled by its own maximum. See ``_to_eight_bit``.
_TWELVE_BIT_LIMIT = 4096

#: EXIF Orientation tag id.
_EXIF_ORIENTATION = 0x0112

#: DPI outside this range is a corrupt or placeholder header (0, or a
#: ``pHYs`` of millions), and a zero would divide by zero inside Pillow's PDF
#: writer when it computes the page size.
_DPI_RANGE = (10.0, 4800.0)
_DEFAULT_DPI = 300.0


@dataclass
class TiffNormalizeResult:
    pdf_bytes: bytes
    page_count: int
    source_bytes: int
    truncated_at_cap: bool  # True if input had more frames than MAX_FRAMES
    #: Frames the input holds (1 for a single image). With
    #: ``truncated_at_cap`` this says how many were dropped.
    total_frames: int = 1
    #: Frames deliberately not wrapped because the format's frames are not
    #: pages (an animated GIF / WebP / APNG). 0 for TIFF and still images.
    frames_ignored: int = 0
    #: Pages that carried an EXIF orientation and were rotated upright.
    pages_reoriented: int = 0
    #: L / RGB pages written as JPEG because the lossless (Flate) budget was
    #: spent. 0 for any normal-sized scan; > 0 means those pages carry JPEG
    #: artefacts the OCR engine may read worse.
    pages_jpeg_encoded: int = 0


class TiffNormalizeError(Exception):
    """Raised when img → PDF wrap fails for a recoverable reason.

    Distinct from arbitrary exceptions so the Hatchet workflow can route
    these to the IngestQuality admin surface (manual triage) rather than
    retry forever.
    """


def tiff_to_pdf(source_bytes: bytes) -> TiffNormalizeResult:
    """Convert image bytes (multi-page TIFF or a single image) to PDF bytes.

    Pillow walks the frames of a TIFF; every other format contributes its
    first frame. Each frame is rotated upright from its EXIF orientation and
    converted to a mode PIL's PDF writer can embed (palette, alpha and 16-bit
    modes are flattened), and written one page at a time so memory is bounded
    by a single frame.

    Returns
    -------
    TiffNormalizeResult
        pdf_bytes : the wrapped PDF
        page_count : number of frames actually written
        source_bytes : input size
        truncated_at_cap : True iff the input had more than MAX_FRAMES
            frames and we stopped early. ``total_frames`` has the real count.
        frames_ignored : animation frames not wrapped (non-TIFF formats)
        pages_reoriented : pages rotated by their EXIF orientation

    Raises
    ------
    TiffNormalizeError
        on oversized input, an undecodable image, or a frame that fails
        part-way (a PDF missing pages is worse than no PDF).
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

    from .trusted_image import trusted_pillow_image_limit

    # One-page PDFs are spooled to disk (see the module docstring, "Memory").
    spool = tempfile.TemporaryDirectory(prefix="tiff_to_pdf_")
    page_paths: list[Path] = []
    reoriented = 0
    budget = _LosslessBudget(int(MAX_TIFF_BYTES * _LOSSLESS_BUDGET_FRACTION))

    # WSGS / NI 43-101 scans routinely exceed Pillow's default threshold.
    # Raise it to a finite, explicit ceiling only while decoding this trusted
    # internal upload, then restore the process-wide Pillow setting. Frames
    # decode lazily, so the WHOLE loop runs inside the context.
    with trusted_pillow_image_limit():
        try:
            src = Image.open(io.BytesIO(source_bytes))
        except Exception as exc:
            raise TiffNormalizeError(f"PIL.Image.open failed: {exc}") from exc

        is_sequence = (src.format or "").upper() in _PAGE_SEQUENCE_FORMATS
        total_frames = _frame_count(src)
        limit = MAX_FRAMES if is_sequence else 1

        try:
            for index, frame in enumerate(ImageSequence.Iterator(src)):
                if index >= limit:
                    break
                dpi = _effective_dpi(frame)
                page, rotated = _prepare_frame(frame)
                reoriented += int(rotated)
                try:
                    page_pdf = _frame_to_pdf(page, dpi, budget)
                finally:
                    page.close()
                page_path = Path(spool.name) / f"p{len(page_paths):05d}.pdf"
                page_path.write_bytes(page_pdf)
                del page_pdf
                page_paths.append(page_path)
        except Exception as exc:
            spool.cleanup()
            # Includes a frame that fails to DECODE part-way through the
            # sequence: a PDF silently missing its tail pages is worse than
            # no PDF, so the whole wrap is refused and names the frame.
            raise TiffNormalizeError(
                f"frame {len(page_paths)} could not be read or wrapped: {exc}"
            ) from exc

    try:
        if not page_paths:
            raise TiffNormalizeError("no frames in TIFF")

        truncated = is_sequence and total_frames > MAX_FRAMES
        ignored = 0 if is_sequence else max(total_frames - 1, 0)

        pdf_bytes = _merge_pages(page_paths, Path(spool.name) / "merged.pdf")
    finally:
        spool.cleanup()
    log.info(
        "tiff_to_pdf.ok frames=%d total=%d truncated=%s ignored=%d "
        "reoriented=%d jpeg_pages=%d in_bytes=%d out_bytes=%d",
        len(page_paths), total_frames, truncated, ignored, reoriented,
        budget.jpeg_pages, len(source_bytes), len(pdf_bytes),
    )
    if budget.jpeg_pages:
        log.warning(
            "tiff_to_pdf: %d page(s) JPEG-encoded - the lossless budget (%d "
            "bytes) was spent; the OCR engine may read them worse",
            budget.jpeg_pages, budget.limit,
        )
    return TiffNormalizeResult(
        pdf_bytes=pdf_bytes,
        page_count=len(page_paths),
        source_bytes=len(source_bytes),
        truncated_at_cap=truncated,
        total_frames=total_frames,
        frames_ignored=ignored,
        pages_reoriented=reoriented,
        pages_jpeg_encoded=budget.jpeg_pages,
    )


def _frame_count(src) -> int:
    """Frames in the source, 1 when the format does not say."""
    try:
        return max(int(getattr(src, "n_frames", 1) or 1), 1)
    except Exception:  # noqa: BLE001 — a damaged IFD chain; the loop reports it
        log.debug("tiff_to_pdf: n_frames unreadable, assuming one frame", exc_info=True)
        return 1


class _LosslessBudget:
    """Running total of Flate page bytes against the lossless ceiling."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spent = 0
        self.jpeg_pages = 0

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit


#: PDF colour space per PIL mode, for the modes written by ``_image_page_pdf``.
_COLOR_SPACES = {"L": "/DeviceGray", "RGB": "/DeviceRGB", "CMYK": "/DeviceCMYK"}


def _frame_to_pdf(page, dpi: float, budget: _LosslessBudget | None = None) -> bytes:
    """One prepared frame as a one-page PDF (see the module docstring).

    ``1`` -> Pillow's CCITT G4 (lossless). ``L`` / ``RGB`` / ``CMYK`` -> a Flate
    image XObject built with pikepdf (lossless) while the lossless budget
    lasts, then JPEG q92 4:4:4 for L / RGB (CMYK falls to Pillow's writer).
    Any failure of the pikepdf path falls back to Pillow's writer, so a page
    is never lost to the better encoder.
    """
    budget = budget if budget is not None else _LosslessBudget(1 << 62)
    if page.mode in _COLOR_SPACES:
        lossless = not budget.exhausted
        if lossless or page.mode != "CMYK":
            try:
                pdf = _image_page_pdf(page, dpi, lossless=lossless)
            except Exception as exc:  # noqa: BLE001 - the Pillow writer is the fallback
                log.warning(
                    "tiff_to_pdf: pikepdf page writer failed (%s); using Pillow's "
                    "PDF writer for this page", exc,
                )
            else:
                if lossless:
                    budget.spent += len(pdf)
                else:
                    budget.jpeg_pages += 1
                return pdf
    buf = io.BytesIO()
    # Resolution metadata - best-effort from the source; the §04p stack uses
    # pdf2image at fixed DPI so this is informational.
    page.save(buf, format="PDF", resolution=dpi)
    if page.mode in _COLOR_SPACES:
        # Pillow wrote this L / RGB / CMYK page as JPEG.
        budget.jpeg_pages += 1
    return buf.getvalue()


def _image_page_pdf(page, dpi: float, *, lossless: bool) -> bytes:
    """A one-page PDF whose single image XObject is Flate (lossless) or JPEG."""
    import pikepdf  # noqa: PLC0415
    from pikepdf import Name  # noqa: PLC0415

    width, height = page.size
    if lossless:
        data = zlib.compress(page.tobytes(), 6)
        filter_name = Name.FlateDecode
    else:
        jpeg = io.BytesIO()
        page.save(
            jpeg, format="JPEG", quality=_FALLBACK_JPEG_QUALITY,
            subsampling=0, optimize=True,
        )
        data = jpeg.getvalue()
        filter_name = Name.DCTDecode

    pdf = pikepdf.Pdf.new()
    image = pikepdf.Stream(pdf, b"")
    image.write(data, filter=filter_name)
    image.Type = Name.XObject
    image.Subtype = Name.Image
    image.Width = width
    image.Height = height
    image.ColorSpace = Name(_COLOR_SPACES[page.mode])
    image.BitsPerComponent = 8

    # Page size in points from the pixel size and the resolution. Built as a
    # plain page dictionary rather than with ``add_blank_page``, which refuses
    # a page outside 3..14400 units (a thumbnail, or a 60,000-px survey sheet at
    # 300 dpi - both legitimate here).
    width_pt = width * 72.0 / dpi
    height_pt = height * 72.0 / dpi
    content = pdf.make_stream(
        f"q {width_pt:.4f} 0 0 {height_pt:.4f} 0 0 cm /Im0 Do Q".encode("ascii")
    )
    page_obj = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=Name.Page,
            MediaBox=[0, 0, width_pt, height_pt],
            Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image)),
            Contents=content,
        )
    )
    pdf.pages.append(pikepdf.Page(page_obj))
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _merge_pages(page_paths: list[Path], merged_path: Path) -> bytes:
    """Join the one-page PDFs (spooled on disk) into one document.

    A single page is returned as it is. The sources are opened from their
    paths, so pikepdf reads page content lazily from disk rather than holding
    every page in memory, and the merged document is saved to disk before it is
    read back as the result.
    """
    if len(page_paths) == 1:
        return page_paths[0].read_bytes()

    try:
        import pikepdf  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - pikepdf is in the image
        raise TiffNormalizeError(f"pikepdf not available: {exc}") from exc

    sources = []
    try:
        merged = pikepdf.Pdf.new()
        # The sources must stay open until the merged document is saved.
        sources = [pikepdf.Pdf.open(path) for path in page_paths]
        for src_pdf in sources:
            merged.pages.extend(src_pdf.pages)
        merged.save(merged_path)
        merged.close()
    except Exception as exc:
        raise TiffNormalizeError(f"PDF page merge failed: {exc}") from exc
    finally:
        for src_pdf in sources:
            src_pdf.close()
    return merged_path.read_bytes()


def _prepare_frame(frame):
    """Upright, PDF-embeddable copy of *frame*, and whether it was rotated."""
    upright, rotated = _apply_exif_orientation(frame)
    return _normalise_frame_mode(upright, copy=False), rotated


def _apply_exif_orientation(frame):
    """Return ``(image, rotated)`` with the EXIF Orientation applied.

    Always returns an image the caller owns. A damaged EXIF block is not a
    reason to lose the page: it is logged and the pixels go through as
    stored.
    """
    from PIL import ImageOps  # noqa: PLC0415

    try:
        orientation = frame.getexif().get(_EXIF_ORIENTATION, 1)
    except Exception as exc:  # noqa: BLE001
        log.debug("tiff_to_pdf: unreadable EXIF, leaving orientation: %s", exc)
        return frame.copy(), False

    if orientation in (None, 0, 1):
        return frame.copy(), False
    try:
        return ImageOps.exif_transpose(frame), True
    except Exception as exc:  # noqa: BLE001
        log.debug("tiff_to_pdf: exif_transpose failed, leaving orientation: %s", exc)
        return frame.copy(), False


def _flatten_on_white(frame, *, grey: bool):
    """Composite a transparent image onto white.

    Dropping the alpha channel instead (``convert("RGB")``) exposes whatever
    colour sits under fully transparent pixels — usually black — so black
    text on a transparent PNG background comes out black on black and OCR
    reads nothing.
    """
    from PIL import Image  # noqa: PLC0415

    rgba = frame.convert("RGBA")
    flat = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    rgba.close()
    return flat.convert("L" if grey else "RGB")


def _to_eight_bit(frame):
    """16/32-bit greyscale to 8-bit by scaling, not by clipping.

    ``convert("L")`` on an ``I;16`` image saturates: every sample above 255
    becomes 255, so a normal 16-bit grey scan (values up to 65535) comes out
    as a solid white page.

    The scale depends on the data, not on the container. A true 16-bit scan
    uses the whole 0..65535 range, so dividing by 256 is right. A 12-bit scan
    stored in 16-bit words (common from document scanners and medical-style
    capture; every value <= 4095) divided by 256 comes out at 0..15 - a
    near-black page that OCR reads as nothing. So when the frame's actual
    maximum is below ``_TWELVE_BIT_LIMIT`` the frame is scaled by that maximum
    (the darkest-to-brightest range maps to 0..255); otherwise by 1/256.
    A constant frame (maximum 0) stays black.
    """
    try:
        _low, high = frame.getextrema()
        high = float(high)
    except Exception:  # noqa: BLE001 - an unreadable extrema: fall back to the container scale
        log.debug("tiff_to_pdf: no extrema for mode %s, scaling by 1/256", frame.mode, exc_info=True)
        high = float(_TWELVE_BIT_LIMIT)
    scale = 255.0 / high if 0 < high < _TWELVE_BIT_LIMIT else 1.0 / 256.0
    try:
        # point() on I / I;16 applies the scale but KEEPS the 16/32-bit mode
        # whatever the mode argument says, so the narrowing is a second step.
        scaled = frame.point(lambda i: i * scale)
        return scaled.convert("L")
    except Exception:  # noqa: BLE001 - a mode point() cannot scale
        log.debug("tiff_to_pdf: 16-bit scale failed for mode %s, plain convert", frame.mode, exc_info=True)
        return frame.convert("L")


def _normalise_frame_mode(frame, *, copy: bool = True):
    """Convert frames to a mode PIL's PDF writer accepts cleanly.

    PIL's PDF writer handles 1, L, RGB, CMYK natively. Palette ('P'),
    'LA', 'RGBA' and 16-bit modes need conversion. We pick the smallest mode
    that preserves the data:
      * 1-bit ('1') stays as-is — bilevel scans (CCITT/G4) are common
        in fax-grade TIFFs and round-trip without colour blow-up.
      * 'L' (grayscale) stays as-is.
      * 'P' (palette) → RGB to preserve colour; a palette with a
        transparent index is flattened onto white first.
      * 'LA' → 'L', 'RGBA' / 'PA' → 'RGB', flattened onto WHITE (not
        dropped — see ``_flatten_on_white``).
      * 'I;16' / 'I' (16/32-bit grayscale) → 'L', scaled to 8 bits.
      * Anything else (YCbCr, LAB, HSV, etc.) → RGB.

    ``copy=False`` is for a frame the caller already owns (the result of
    ``_apply_exif_orientation``), so a pass-through mode is not copied twice.
    """
    mode = frame.mode
    if mode in ("1", "L", "RGB", "CMYK"):
        return frame.copy() if copy else frame
    if mode == "LA":
        return _flatten_on_white(frame, grey=True)
    if mode in ("RGBA", "PA"):
        return _flatten_on_white(frame, grey=False)
    if mode.startswith("I;16") or mode == "I":
        return _to_eight_bit(frame)
    if mode == "P":
        if "transparency" in frame.info:
            return _flatten_on_white(frame, grey=False)
        return frame.convert("RGB")
    return frame.convert("RGB")


def _effective_dpi(src) -> float:
    """Pull a reasonable DPI from the image metadata, defaulting to 300.

    NI 43-101 scans are typically 200-300 DPI; the §04p stack re-renders
    via pdf2image at 250 DPI for OCR regardless, so this metadata is for
    downstream-tool consumption only (e.g. an IngestQuality preview). A
    value outside ``_DPI_RANGE`` is a placeholder, not a resolution.
    """
    info = getattr(src, "info", {}) or {}
    dpi = info.get("dpi")
    if isinstance(dpi, tuple) and dpi:
        try:
            value = float(dpi[0])
        except (TypeError, ValueError):
            log.debug("tiff_to_pdf: unreadable dpi %r, using %s", dpi, _DEFAULT_DPI, exc_info=True)
            return _DEFAULT_DPI
        if _DPI_RANGE[0] <= value <= _DPI_RANGE[1]:
            return value
    return _DEFAULT_DPI


__all__ = [
    "tiff_to_pdf",
    "TiffNormalizeResult",
    "TiffNormalizeError",
    "MAX_FRAMES",
    "MAX_TIFF_BYTES",
]
