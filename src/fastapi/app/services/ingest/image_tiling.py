"""Bounded raster tiling and OCR word reconstruction.

Azure Document Intelligence rejects inputs with either side above 10,000
pixels. This module keeps a safety margin below that limit, records every
tile's source offset, remaps word polygons to source-image coordinates, and
deduplicates words observed in overlapping seams.

The Pillow decompression-bomb limit is raised only while opening trusted
internal document scans, under a process lock, and is restored immediately.
It is never disabled globally.
"""

from __future__ import annotations

import io
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image

AZURE_MAX_SIDE_PX = 10_000
DEFAULT_TILE_SIDE_PX = 9_000
DEFAULT_TILE_OVERLAP_PX = 180
MAX_TRUSTED_IMAGE_PIXELS = 1_000_000_000

_pillow_limit_lock = threading.RLock()
_NORMALIZE_WORD_RE = re.compile(r"\W+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ImageTile:
    """One tile and its placement in the original raster."""

    tile_id: str
    image: Image
    left: int
    top: int
    right: int
    bottom: int
    source_width: int
    source_height: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True, slots=True)
class TileWord:
    """One OCR word located in tile-local coordinates."""

    text: str
    confidence: float
    polygon: tuple[float, ...]
    tile_id: str


@dataclass(frozen=True, slots=True)
class ReconstructedWord:
    """One OCR word remapped into original-image coordinates."""

    text: str
    confidence: float
    polygon: tuple[float, ...]
    tile_id: str


@dataclass(frozen=True, slots=True)
class ReconstructionResult:
    """Reading-ordered, seam-deduplicated OCR words."""

    words: tuple[ReconstructedWord, ...]
    seam_duplicate_count: int

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words).strip()


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


def split_image(
    image: Image,
    *,
    tile_side_px: int = DEFAULT_TILE_SIDE_PX,
    overlap_px: int = DEFAULT_TILE_OVERLAP_PX,
) -> tuple[ImageTile, ...]:
    """Split a raster into overlapping tiles within Azure's side limit."""

    if tile_side_px <= 0 or tile_side_px > AZURE_MAX_SIDE_PX:
        raise ValueError(f"tile_side_px must be between 1 and {AZURE_MAX_SIDE_PX}")
    if overlap_px < 0 or overlap_px >= tile_side_px:
        raise ValueError("overlap_px must be non-negative and smaller than tile_side_px")

    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")

    x_starts = _tile_starts(width, tile_side_px, overlap_px)
    y_starts = _tile_starts(height, tile_side_px, overlap_px)
    tiles: list[ImageTile] = []

    for row, top in enumerate(y_starts):
        bottom = min(top + tile_side_px, height)
        for column, left in enumerate(x_starts):
            right = min(left + tile_side_px, width)
            tiles.append(
                ImageTile(
                    tile_id=f"r{row:04d}-c{column:04d}",
                    image=image.crop((left, top, right, bottom)),
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                    source_width=width,
                    source_height=height,
                )
            )

    return tuple(tiles)


def encode_tile_png(tile: ImageTile) -> bytes:
    """Encode one tile losslessly for Document Intelligence."""

    buffer = io.BytesIO()
    tile.image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def reconstruct_words(
    tiles: Sequence[ImageTile],
    words: Sequence[TileWord],
) -> ReconstructionResult:
    """Remap tile-local polygons, remove seam duplicates, preserve order."""

    tile_by_id = {tile.tile_id: tile for tile in tiles}
    remapped: list[ReconstructedWord] = []
    duplicate_count = 0

    for word in words:
        tile = tile_by_id.get(word.tile_id)
        if tile is None:
            raise ValueError(f"unknown tile_id: {word.tile_id}")
        polygon = _offset_polygon(word.polygon, tile.left, tile.top)
        candidate = ReconstructedWord(
            text=word.text.strip(),
            confidence=max(0.0, min(1.0, word.confidence)),
            polygon=polygon,
            tile_id=word.tile_id,
        )
        if not candidate.text:
            continue

        duplicate_index = _find_duplicate(remapped, candidate)
        if duplicate_index is None:
            remapped.append(candidate)
            continue

        duplicate_count += 1
        if candidate.confidence > remapped[duplicate_index].confidence:
            remapped[duplicate_index] = candidate

    remapped.sort(key=_reading_order_key)
    return ReconstructionResult(tuple(remapped), duplicate_count)


def _tile_starts(length: int, tile_side_px: int, overlap_px: int) -> tuple[int, ...]:
    if length <= tile_side_px:
        return (0,)

    stride = tile_side_px - overlap_px
    starts = [0]
    while starts[-1] + tile_side_px < length:
        starts.append(starts[-1] + stride)
    return tuple(starts)


def _offset_polygon(
    polygon: Sequence[float],
    left: int,
    top: int,
) -> tuple[float, ...]:
    if len(polygon) < 4 or len(polygon) % 2:
        raise ValueError("polygon must contain at least two x/y coordinate pairs")

    remapped: list[float] = []
    for index, coordinate in enumerate(polygon):
        remapped.append(float(coordinate) + (left if index % 2 == 0 else top))
    return tuple(remapped)


def _find_duplicate(
    accepted: Sequence[ReconstructedWord],
    candidate: ReconstructedWord,
) -> int | None:
    normalized = _normalize_word(candidate.text)
    if not normalized:
        return None
    candidate_box = _polygon_box(candidate.polygon)
    candidate_height = max(1.0, candidate_box[3] - candidate_box[1])

    for index in range(len(accepted) - 1, -1, -1):
        existing = accepted[index]
        # Repeated words within one tile are real OCR output, not seam
        # duplicates. Only observations from distinct overlapping tiles can
        # represent the same source word.
        if existing.tile_id == candidate.tile_id:
            continue
        if _normalize_word(existing.text) != normalized:
            continue
        existing_box = _polygon_box(existing.polygon)
        if _intersection_over_union(existing_box, candidate_box) >= 0.2:
            return index

        existing_height = max(1.0, existing_box[3] - existing_box[1])
        x_distance = abs(_box_center(existing_box)[0] - _box_center(candidate_box)[0])
        y_distance = abs(_box_center(existing_box)[1] - _box_center(candidate_box)[1])
        tolerance = max(existing_height, candidate_height)
        if x_distance <= tolerance and y_distance <= tolerance:
            return index
    return None


def _normalize_word(text: str) -> str:
    return _NORMALIZE_WORD_RE.sub("", text).casefold()


def _polygon_box(polygon: Sequence[float]) -> tuple[float, float, float, float]:
    xs = polygon[0::2]
    ys = polygon[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def _box_center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def _intersection_over_union(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    intersection_width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    intersection_height = max(
        0.0,
        min(first[3], second[3]) - max(first[1], second[1]),
    )
    intersection = intersection_width * intersection_height
    if intersection == 0:
        return 0.0
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    return intersection / max(first_area + second_area - intersection, 1.0)


def _reading_order_key(word: ReconstructedWord) -> tuple[int, float]:
    box = _polygon_box(word.polygon)
    height = max(1.0, box[3] - box[1])
    line_bucket = round(box[1] / max(height * 0.75, 1.0))
    return line_bucket, box[0]


__all__ = [
    "AZURE_MAX_SIDE_PX",
    "DEFAULT_TILE_OVERLAP_PX",
    "DEFAULT_TILE_SIDE_PX",
    "MAX_TRUSTED_IMAGE_PIXELS",
    "ImageTile",
    "ReconstructedWord",
    "ReconstructionResult",
    "TileWord",
    "encode_tile_png",
    "open_trusted_image",
    "reconstruct_words",
    "split_image",
    "trusted_pillow_image_limit",
]
