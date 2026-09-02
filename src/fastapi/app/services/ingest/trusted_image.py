"""Bounded Pillow decoding for trusted internal document scans.

The Pillow decompression-bomb limit is raised only while opening trusted
internal uploads (WSGS / NI 43-101 scans routinely exceed the default),
under a process lock, and is restored immediately. It is never disabled
globally.

Extracted from the raster tiling module on 2026-09-02 when Azure Document
Intelligence (and with it, tiling) was retired — the TIFF normaliser still
needs this and nothing else from that module.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image

MAX_TRUSTED_IMAGE_PIXELS = 1_000_000_000

_pillow_limit_lock = threading.RLock()


@contextmanager
def trusted_pillow_image_limit(
    max_pixels: int = MAX_TRUSTED_IMAGE_PIXELS,
) -> Iterator[None]:
    """Temporarily raise Pillow's pixel limit for trusted internal scans."""

    if max_pixels <= 0:
        raise ValueError("max_pixels must be positive")

    from PIL import Image

    with _pillow_limit_lock:
        previous_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = max_pixels
        try:
            yield
        finally:
            Image.MAX_IMAGE_PIXELS = previous_limit


def open_trusted_image(
    source_bytes: bytes,
    *,
    max_pixels: int = MAX_TRUSTED_IMAGE_PIXELS,
) -> Image:
    """Open and fully decode a trusted image with an explicit pixel ceiling."""

    if not source_bytes:
        raise ValueError("source_bytes must not be empty")

    from PIL import Image

    with trusted_pillow_image_limit(max_pixels):
        image = Image.open(io.BytesIO(source_bytes))
        image.load()
        return image


__all__ = [
    "MAX_TRUSTED_IMAGE_PIXELS",
    "open_trusted_image",
    "trusted_pillow_image_limit",
]
