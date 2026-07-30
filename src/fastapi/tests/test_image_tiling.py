"""Unit tests for bounded image tiling and seam reconstruction."""

from __future__ import annotations

from PIL import Image

from app.services.ingest.image_tiling import (
    TileWord,
    reconstruct_words,
    split_image,
)


def test_small_image_produces_one_identity_tile() -> None:
    image = Image.new("L", (2_550, 8_000))

    tiles = split_image(image)

    assert len(tiles) == 1
    assert (tiles[0].left, tiles[0].top, tiles[0].right, tiles[0].bottom) == (
        0,
        0,
        2_550,
        8_000,
    )


def test_well_log_height_splits_into_overlapping_vertical_bands() -> None:
    image = Image.new("L", (2_550, 16_269))

    tiles = split_image(image, tile_side_px=9_000, overlap_px=180)

    assert len(tiles) == 2
    assert all(tile.width <= 9_000 and tile.height <= 9_000 for tile in tiles)
    assert tiles[0].top == 0
    assert tiles[0].bottom == 9_000
    assert tiles[1].top == 8_820
    assert tiles[1].bottom == 16_269
    assert tiles[0].bottom - tiles[1].top == 180


def test_both_axes_are_tiled_when_both_exceed_azure_limit() -> None:
    image = Image.new("L", (18_100, 18_100))

    tiles = split_image(image, tile_side_px=9_000, overlap_px=180)

    assert len(tiles) == 9
    assert {tile.tile_id for tile in tiles} == {f"r{row:04d}-c{column:04d}" for row in range(3) for column in range(3)}
    assert all(tile.width <= 9_000 and tile.height <= 9_000 for tile in tiles)


def test_exact_multiple_and_one_pixel_over_limit_never_create_empty_tiles() -> None:
    exact = split_image(
        Image.new("L", (9_000, 18_000)),
        tile_side_px=9_000,
        overlap_px=180,
    )
    barely_oversized = split_image(
        Image.new("L", (9_001, 100)),
        tile_side_px=9_000,
        overlap_px=180,
    )

    assert len(exact) == 3
    assert len(barely_oversized) == 2
    assert all(tile.width > 0 and tile.height > 0 for tile in (*exact, *barely_oversized))
    assert all(tile.width <= 9_000 and tile.height <= 9_000 for tile in (*exact, *barely_oversized))
    assert barely_oversized[-1].right == 9_001


def test_reconstruction_offsets_polygons_and_deduplicates_overlap() -> None:
    image = Image.new("L", (500, 900))
    tiles = split_image(image, tile_side_px=600, overlap_px=300)
    assert len(tiles) == 2

    words = [
        TileWord("Top", 0.9, (10, 10, 50, 10, 50, 30, 10, 30), tiles[0].tile_id),
        TileWord(
            "Seam",
            0.8,
            (20, 490, 70, 490, 70, 510, 20, 510),
            tiles[0].tile_id,
        ),
        TileWord(
            "Seam",
            0.95,
            (20, 190, 70, 190, 70, 210, 20, 210),
            tiles[1].tile_id,
        ),
        TileWord(
            "Bottom",
            0.9,
            (10, 500, 70, 500, 70, 520, 10, 520),
            tiles[1].tile_id,
        ),
    ]

    result = reconstruct_words(tiles, words)

    assert result.text == "Top Seam Bottom"
    assert result.seam_duplicate_count == 1
    seam = next(word for word in result.words if word.text == "Seam")
    assert seam.confidence == 0.95
    assert seam.polygon[1] == 490


def test_reconstruction_preserves_legitimate_repeated_words_in_one_tile() -> None:
    image = Image.new("L", (300, 100))
    tiles = split_image(image)
    words = [
        TileWord("very", 0.9, (10, 10, 40, 10, 40, 30, 10, 30), tiles[0].tile_id),
        TileWord("very", 0.9, (42, 10, 72, 10, 72, 30, 42, 30), tiles[0].tile_id),
    ]

    result = reconstruct_words(tiles, words)

    assert result.text == "very very"
    assert result.seam_duplicate_count == 0


def test_reconstruction_rejects_unknown_tile_id() -> None:
    image = Image.new("L", (100, 100))
    tiles = split_image(image)
    words = [TileWord("bad", 0.5, (0, 0, 1, 0, 1, 1, 0, 1), "missing")]

    try:
        reconstruct_words(tiles, words)
    except ValueError as exc:
        assert "unknown tile_id" in str(exc)
    else:
        raise AssertionError("unknown tile id was accepted")
