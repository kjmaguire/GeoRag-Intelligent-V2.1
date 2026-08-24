"""Deskew, and the rest of the preprocessing that was only ever a docstring.

`_preprocess_image_for_ocr` listed five steps and implemented two. It
converted to grayscale, upscaled narrow images, computed a `binary` array
that was then discarded, carried a comment about denoising with no denoise
code, and applied a single SHARPEN. There was no deskew anywhere in the
stack, and tesseract is invoked at every call site with `--psm 3`, which
assumes upright text.

A 1970s plan sheet fed through a flatbed routinely lands 2-5 degrees out.
Uncorrected, every line straddles two text rows and the output is fragmented
tokens -- which `_assess_ocr_result` correctly tiers `mandatory_review`, and
which (before 2026-08-21) was then embedded and retrieved anyway.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.ingest.pdf_report import (
    _estimate_skew_degrees,
    _preprocess_image_for_ocr,
)


def _scanned_page(skew_degrees: float) -> Image.Image:
    """A synthetic scan: twenty lines of text as dark horizontal bars."""
    img = Image.new("L", (1200, 900), 255)
    draw = ImageDraw.Draw(img)
    for i in range(20):
        top = 40 + i * 42
        draw.rectangle([80, top, 1120, top + 12], fill=30)

    return img.rotate(-skew_degrees, resample=Image.BICUBIC, fillcolor=255)


def _binarize(img: Image.Image) -> np.ndarray:
    arr = np.array(img.convert("L"))
    return ((arr < arr.mean() * 0.85) * 255).astype(np.uint8)


class TestSkewEstimation:
    @pytest.mark.parametrize("skew", [0.0, 1.0, 2.5, -3.0, 4.0, -5.5])
    def test_a_known_skew_is_recovered(self, skew: float) -> None:
        estimated = _estimate_skew_degrees(_binarize(_scanned_page(skew)))

        # 0.5 degrees is the search step, so that is the achievable accuracy.
        assert abs(estimated - skew) <= 0.5

    def test_a_tiny_image_is_left_alone(self) -> None:
        """Below the size where a projection profile means anything."""
        assert _estimate_skew_degrees(np.zeros((10, 10), dtype=np.uint8)) == 0.0

    def test_a_blank_page_reports_no_skew(self) -> None:
        blank = np.zeros((900, 1200), dtype=np.uint8)

        assert _estimate_skew_degrees(blank) == 0.0


class TestPreprocessing:
    def test_it_returns_an_image_for_a_skewed_page(self) -> None:
        out = _preprocess_image_for_ocr(_scanned_page(4.0))

        assert isinstance(out, Image.Image)
        assert out.mode == "L"

    def test_a_narrow_page_is_upscaled_past_the_tesseract_dpi_floor(self) -> None:
        narrow = Image.new("L", (800, 600), 255)
        ImageDraw.Draw(narrow).rectangle([50, 50, 750, 70], fill=30)

        out = _preprocess_image_for_ocr(narrow)

        assert out.size[0] >= 2000

    def test_correcting_a_skewed_page_raises_its_profile_variance(self) -> None:
        """The property deskew exists to restore: horizontal text lines.

        A tilted page smears its lines across rows, flattening the row-sum
        profile. Straightening it puts the peaks back.
        """
        skewed = _scanned_page(5.0)
        corrected = _preprocess_image_for_ocr(skewed)

        before = np.array(skewed).sum(axis=1, dtype=np.float64).var()
        after = np.array(corrected).sum(axis=1, dtype=np.float64).var()

        assert after > before
