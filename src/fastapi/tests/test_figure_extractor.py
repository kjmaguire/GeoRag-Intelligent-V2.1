"""Tests for app.agent.figure_extractor.extract_figures_from_pdf.

Restored 2026-06-24 from the PyMuPDF (AGPL) stub to a pypdfium2 implementation.
Fast + deterministic: builds tiny PDFs in-test by embedding PIL images (no
network, no models). Guards the embedded-image contract + the size/dedup filters
so the path can't silently regress to a no-op stub again.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.agent.figure_extractor import (
    MIN_IMAGE_BYTES,
    MIN_IMAGE_DIMENSION,
    describe_figures_with_vl,
    extract_figures_from_pdf,
)


def _noise_image(width: int, height: int, seed: int = 7) -> Image.Image:
    """High-entropy RGB image — survives PNG compression (> MIN_IMAGE_BYTES)."""
    arr = (np.random.RandomState(seed).rand(height, width, 3) * 255).astype("uint8")
    return Image.fromarray(arr, "RGB")


def _pdf_from_images(images: list[Image.Image], path: Path) -> Path:
    images[0].save(
        str(path), format="PDF", save_all=True, append_images=images[1:],
    )
    return path


def test_extract_figures_finds_embedded_image(tmp_path: Path) -> None:
    pdf = _pdf_from_images([_noise_image(300, 220)], tmp_path / "one.pdf")
    figs = extract_figures_from_pdf(str(pdf))

    assert len(figs) == 1
    f = figs[0]
    assert f["page"] == 1                      # 1-based
    assert f["width"] >= MIN_IMAGE_DIMENSION
    assert f["height"] >= MIN_IMAGE_DIMENSION
    assert len(f["sha256"]) == 64
    assert f["format"] == "png"
    assert len(f["image_bytes"]) >= MIN_IMAGE_BYTES


def test_extract_figures_filters_tiny_dimension(tmp_path: Path) -> None:
    # 50x50 < MIN_IMAGE_DIMENSION on both axes → filtered out.
    pdf = _pdf_from_images([_noise_image(50, 50)], tmp_path / "tiny.pdf")
    assert extract_figures_from_pdf(str(pdf)) == []


def test_extract_figures_filters_low_byte_image(tmp_path: Path) -> None:
    # A big-but-flat solid-colour image compresses below MIN_IMAGE_BYTES.
    solid = Image.new("RGB", (300, 300), (210, 210, 210))
    pdf = _pdf_from_images([solid], tmp_path / "solid.pdf")
    assert extract_figures_from_pdf(str(pdf)) == []


def test_extract_figures_dedupes_repeated_image(tmp_path: Path) -> None:
    # Same image on two pages (e.g. a letterhead) → de-duped to one figure.
    img = _noise_image(280, 200, seed=3)
    pdf = _pdf_from_images([img, img], tmp_path / "two_same.pdf")
    figs = extract_figures_from_pdf(str(pdf))
    assert len(figs) == 1


def test_extract_figures_distinct_images_on_pages(tmp_path: Path) -> None:
    pdf = _pdf_from_images(
        [_noise_image(300, 220, seed=1), _noise_image(300, 220, seed=2)],
        tmp_path / "two_diff.pdf",
    )
    figs = extract_figures_from_pdf(str(pdf))
    assert len(figs) == 2
    assert {f["page"] for f in figs} == {1, 2}


def test_extract_figures_missing_file_returns_empty() -> None:
    # Bad path must degrade to [] (callers run it opportunistically), not raise.
    assert extract_figures_from_pdf("/nonexistent/does-not-exist.pdf") == []


# --- describe_figures_with_vl (Qwen3-VL captioning, mocked backend) ---------

class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._p = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._p


class _FakeClient:
    """Stand-in for httpx.AsyncClient — returns a canned reply or raises."""

    def __init__(self, payload: dict | None = None, raise_exc: Exception | None = None) -> None:
        self._payload = payload
        self._raise = raise_exc
        self.calls = 0

    async def post(self, url, json=None, timeout=None):  # noqa: ANN001
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return _FakeResp(self._payload or {})

    async def aclose(self) -> None:
        return None


def _figure(page: int = 1) -> dict:
    return {
        "page": page, "width": 300, "height": 200,
        "sha256": "a" * 64, "format": "png", "image_bytes": b"\x89PNG-bytes",
    }


def test_describe_figures_with_vl_uses_vl_text() -> None:
    payload = {"choices": [{"message": {
        "content": "A drill collar location plan showing 12 holes over the PLS conductor."
    }}]}
    client = _FakeClient(payload=payload)
    out = asyncio.run(describe_figures_with_vl([_figure(2)], "Test Report", http_client=client))

    assert client.calls == 1
    desc = out[0]["description"]
    assert "drill collar location plan" in desc   # VL text used
    assert "Test Report" in desc and "page 2" in desc


def test_describe_figures_with_vl_falls_back_to_heuristic_on_failure() -> None:
    client = _FakeClient(raise_exc=RuntimeError("vllm-vl unreachable"))
    out = asyncio.run(describe_figures_with_vl([_figure(1)], "Test Report", http_client=client))

    # VL failed → the heuristic description (computed first) is retained,
    # indexing is never blocked.
    desc = out[0]["description"]
    assert desc
    assert "Image is 300x200 px" in desc   # heuristic signature, not VL text


def test_describe_figures_with_vl_empty_reply_keeps_heuristic() -> None:
    # 200 but empty content → keep the heuristic, don't write a blank description.
    client = _FakeClient(payload={"choices": [{"message": {"content": "   "}}]})
    out = asyncio.run(describe_figures_with_vl([_figure(1)], "Test Report", http_client=client))
    assert "Image is 300x200 px" in out[0]["description"]
