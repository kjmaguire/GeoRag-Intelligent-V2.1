"""Regression guard for the ERDAS HFA pyramid reader.

Every number here was measured by hand against the RedStar delivery on
2026-08-24 and then confirmed by rendering the result and looking at it — the
Unga level really does read "GENERALIZED GEOLOGIC MAP OF UNGA ISLAND, ALASKA"
with its EXPLANATION legend, and the Apollo level really is the underground
plan, sitting visibly rotated inside its frame from the UTM warp.

That eyes-on step is not decoration. Every realistic way this decoder breaks
produces an ARRAY, not an exception: a wrong block stride tiles the image,
a wrong band order swaps the colour channels, a missed RLC branch tears
individual 64x64 squares, and a file whose blocks are all marked unwritten
decodes to a flawless black rectangle. None of those raise. So the assertions
below deliberately go past "it returned something of the right shape":

* the exact byte content of both levels, as a digest;
* a saturated yellow pixel, which no channel transposition survives;
* the per-band means in order, for the same reason;
* the unique-value count, which collapses to 1 the moment the image goes flat.

Two file properties are pinned as well, because they are what make the other
assertions meaningful and they live in the fixtures rather than in the code:
that the Unga file genuinely contains RLC-compressed blocks (otherwise the
digest stops testing the decompressor at all), and that neither file's named
parent raster was ever delivered (otherwise this module has no reason to
exist and callers should read the .tif instead).
"""

import hashlib
import struct
from pathlib import Path

import numpy as np
import pytest

from georag_geoparsers.erdas_rrd import (
    RrdLevel,
    _Block,
    _block_table,
    _collect_levels,
    _decode_block,
    _decompress_rlc,
    _read_container,
    _walk,
    extract_level,
    read_rrd_levels,
)

REDSTAR = Path("C:/Users/GeoRAG/Desktop/RedStar")

UNGA = REDSTAR / "Unga Regional (inc)" / "Geology" / "Digital Data" / "Geologic Map Unga 1982 color utm.rrd"
_APOLLO_MAPS = REDSTAR / "Apollo Sitka" / "UG Workings" / "Apollo-Sitka maps" / "acad etc"
APOLLO = _APOLLO_MAPS / "Apollo plan utm.rrd"
#: Same container format, zero Edms_State nodes — statistics and histograms
#: only. A caller will hand this over by mistake; it must be refused clearly.
STATS_ONLY = _APOLLO_MAPS / "Sitka Apollo drilling utm2.aux"

needs_delivery = pytest.mark.skipif(
    not REDSTAR.is_dir(),
    reason="RedStar delivery not mounted on this machine",
)


def _digest(array: np.ndarray) -> str:
    return hashlib.sha256(array.tobytes()).hexdigest()


def _band_means(array: np.ndarray) -> list[float]:
    return [round(float(array[:, :, band].mean()), 3) for band in range(array.shape[2])]


@pytest.fixture(scope="module")
def unga_ss4() -> np.ndarray:
    return extract_level(UNGA, "_ss_4_")


@pytest.fixture(scope="module")
def apollo_ss4() -> np.ndarray:
    return extract_level(APOLLO, "_ss_4_")


# ---------------------------------------------------------------------------
# The Unga geologic map — the compressed file, and the one that matters most
# ---------------------------------------------------------------------------

@needs_delivery
class TestUngaGeologicMap:
    def test_the_pyramid_is_not_empty(self):
        assert read_rrd_levels(UNGA).levels

    def test_it_lists_all_six_levels_finest_first(self):
        levels = read_rrd_levels(UNGA).levels
        assert [level.name for level in levels] == [
            "_ss_4_", "_ss_8_", "_ss_16_", "_ss_32_", "_ss_64_", "_ss_128_",
        ]

    def test_ss_4_is_the_measured_size(self):
        levels = read_rrd_levels(UNGA).levels
        assert levels[0] == RrdLevel(name="_ss_4_", width=1504, height=2007, band_count=3)

    def test_it_names_a_parent_that_was_never_delivered(self):
        """The whole reason to read an .rrd rather than skip it as derived data."""
        parent = read_rrd_levels(UNGA).parent_name
        assert parent == "Geologic Map Unga 1982 color utm.tif"
        assert not (UNGA.parent / parent).exists()

    def test_ss_4_extracts_to_the_measured_dimensions(self, unga_ss4):
        assert unga_ss4.shape == (2007, 1504, 3)
        assert unga_ss4.dtype == np.uint8

    def test_the_image_is_not_silently_black(self, unga_ss4):
        assert unga_ss4.any()
        assert len(np.unique(unga_ss4)) == 248
        assert float((unga_ss4 != 0).mean()) == pytest.approx(0.9166, abs=1e-3)

    def test_the_bands_are_in_file_order_not_node_order(self, unga_ss4):
        """Both files list their bands Band_3 -> Band_1, so node order is reversed.

        A saturated map unit is the sharpest available check: the yellow
        polygon at (879, 889) has almost no blue in it, and there is no
        channel permutation that leaves the 2 where a correct read puts it.
        """
        assert unga_ss4[879, 889].tolist() == [239, 208, 2]
        assert _band_means(unga_ss4) == [195.214, 197.303, 184.838]

    def test_the_level_is_byte_for_byte_what_was_measured(self, unga_ss4):
        assert _digest(unga_ss4) == "74d55123d77758756eacb54cb6787642a9f82e58854d301dcd0959ab8eabd167"


@needs_delivery
class TestUngaReallyIsCompressed:
    """Pins the fixture property the digest test silently depends on.

    The Apollo file stores every block raw, so a reader with no RLC support
    passes every Apollo assertion in this file. If the Unga file ever stopped
    containing compressed blocks, the digest above would keep passing while
    testing nothing about `_decompress_rlc`.
    """

    def test_the_ss_4_level_mixes_compressed_and_stored_blocks(self):
        blob, label = _read_container(UNGA)
        levels = _collect_levels(_walk(blob, label), label)
        blocks = [
            block
            for _band, _layer, state in levels["_ss_4_"]
            for block in _block_table(blob, state, "_ss_4_", label)
        ]
        assert len(blocks) == 768 * 3
        assert sum(block.compression == 1 for block in blocks) == 281
        assert sum(block.compression == 0 for block in blocks) == 768 * 3 - 281


# ---------------------------------------------------------------------------
# The Apollo mine plan — every block stored, and an undersized top level
# ---------------------------------------------------------------------------

@needs_delivery
class TestApolloMinePlan:
    def test_the_pyramid_is_not_empty(self):
        assert read_rrd_levels(APOLLO).levels

    def test_ss_4_is_the_measured_size(self):
        levels = read_rrd_levels(APOLLO).levels
        assert levels[0] == RrdLevel(name="_ss_4_", width=364, height=371, band_count=4)

    def test_it_names_a_parent_that_was_never_delivered(self):
        parent = read_rrd_levels(APOLLO).parent_name
        assert parent == "Apollo plan utm.tif"
        assert not (APOLLO.parent / parent).exists()

    def test_ss_4_extracts_to_the_measured_dimensions(self, apollo_ss4):
        assert apollo_ss4.shape == (371, 364, 4)
        assert apollo_ss4.dtype == np.uint8

    def test_the_image_is_not_silently_black(self, apollo_ss4):
        assert apollo_ss4.any()
        assert len(np.unique(apollo_ss4)) == 207

    def test_the_bands_are_in_file_order_not_node_order(self, apollo_ss4):
        assert apollo_ss4[185, 182].tolist() == [155, 149, 104, 255]
        assert _band_means(apollo_ss4) == [116.732, 116.641, 116.584, 122.103]

    def test_the_level_is_byte_for_byte_what_was_measured(self, apollo_ss4):
        assert _digest(apollo_ss4) == "191540cbdf95cb943d65215732acc30cfadc2311eb68493cf9290e668bb95b35"

    def test_the_coarsest_level_stores_one_undersized_block(self):
        """`_ss_32_` is 46x47 with a 46x47 block, not the 64x64 the format's
        folklore promises. Assembling it on a 64x64 grid puts a 46-pixel-wide
        image into a 64-pixel stride and shears it."""
        coarsest = extract_level(APOLLO, "_ss_32_")
        assert coarsest.shape == (47, 46, 4)
        assert len(np.unique(coarsest)) == 192


# ---------------------------------------------------------------------------
# Every level, both files
# ---------------------------------------------------------------------------

@needs_delivery
@pytest.mark.parametrize("path", [UNGA, APOLLO], ids=["unga", "apollo"])
class TestEveryLevelDecodes:
    def test_each_level_matches_its_declared_geometry(self, path):
        for level in read_rrd_levels(path).levels:
            array = extract_level(path, level.name)
            assert array.shape == (level.height, level.width, level.band_count), level.name
            assert array.dtype == np.uint8, level.name

    def test_no_level_decodes_to_a_single_flat_value(self, path):
        for level in read_rrd_levels(path).levels:
            array = extract_level(path, level.name)
            assert len(np.unique(array)) > 1, level.name


# ---------------------------------------------------------------------------
# Files that hold no pyramid
# ---------------------------------------------------------------------------

@needs_delivery
class TestFilesWithNoPyramid:
    def test_the_statistics_sidecar_is_refused_by_name(self):
        with pytest.raises(ValueError) as err:
            read_rrd_levels(STATS_ONLY)
        message = str(err.value)
        assert "Sitka Apollo drilling utm2.aux" in message
        assert "no Edms_State" in message
        # It must say where the pixels went, not just that it failed.
        assert ".rrd" in message

    def test_extracting_from_it_is_refused_too(self):
        """It has Eimg_Layer nodes, so a shape-only check would let it through."""
        with pytest.raises(ValueError, match="no Edms_State"):
            extract_level(STATS_ONLY, "_ss_4_")


@needs_delivery
class TestLevelSelection:
    def test_an_unknown_level_lists_the_ones_that_exist(self):
        with pytest.raises(ValueError) as err:
            extract_level(APOLLO, "_ss_2_")
        message = str(err.value)
        assert "_ss_2_" in message
        assert "_ss_4_" in message and "_ss_32_" in message


class TestUnreadableInput:
    def test_a_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_rrd_levels(tmp_path / "nope.rrd")

    def test_a_file_without_the_hfa_tag_is_named_in_the_error(self, tmp_path):
        path = tmp_path / "not_really.rrd"
        path.write_bytes(b"II*\x00" + b"\x00" * 512)
        with pytest.raises(ValueError) as err:
            read_rrd_levels(path)
        assert "not_really.rrd" in str(err.value)
        assert "EHFA_HEADER_TAG" in str(err.value)


# ---------------------------------------------------------------------------
# The RLC decompressor, against blocks built by hand from the format
#
# These are the only assertions in the file whose expected values do not come
# from the delivery. That is the point: checking the decompressor against
# output it produced itself proves only that it is deterministic.
# ---------------------------------------------------------------------------

#: dataMin, numRuns, valueOffset, then a one-byte value width.
_RLC_HEADER_SIZE = 13


def _rlc_block(data_min: int, counters: bytes, values: bytes, bit_width: int = 8) -> bytes:
    """Assemble one RLC block: header, run counters, then the value stream."""
    return (
        struct.pack("<iii", data_min, len(_split_counters(counters)), _RLC_HEADER_SIZE + len(counters))
        + bytes([bit_width])
        + counters
        + values
    )


def _split_counters(counters: bytes) -> list[int]:
    """Count how many runs a counter stream encodes (test-side, independent)."""
    widths = {0x00: 1, 0x40: 2, 0x80: 3, 0xC0: 4}
    runs, position = [], 0
    while position < len(counters):
        width = widths[counters[position] & 0xC0]
        value = counters[position] & 0x3F
        for extra in range(1, width):
            value = (value << 8) | counters[position + extra]
        runs.append(value)
        position += width
    return runs


class TestRlcDecompression:
    def test_all_three_counter_widths(self):
        """Run lengths 5, 300 and 70000 use the 1-, 2- and 3-byte forms."""
        counters = bytes([0x05, 0x41, 0x2C, 0x81, 0x11, 0x70])
        assert _split_counters(counters) == [5, 300, 70000]

        block = _rlc_block(data_min=10, counters=counters, values=bytes([0, 5, 245]))
        pixels = _decompress_rlc(block, 5 + 300 + 70000, "_ss_4_", "synthetic.rrd")

        assert pixels.dtype == np.uint8
        assert np.array_equal(np.unique(pixels), [10, 15, 255])
        assert [int((pixels == v).sum()) for v in (10, 15, 255)] == [5, 300, 70000]
        # dataMin is an offset applied to every value, not a floor.
        assert pixels[0] == 10 and pixels[-1] == 255

    def test_four_bit_values_unpack_low_nibble_first(self):
        block = _rlc_block(
            data_min=0,
            counters=bytes([1, 2, 3, 4]),
            values=bytes([(2 << 4) | 1, (15 << 4) | 3]),
            bit_width=4,
        )
        pixels = _decompress_rlc(block, 10, "_ss_4_", "synthetic.rrd")
        assert pixels.tolist() == [1, 2, 2, 3, 3, 3, 15, 15, 15, 15]

    def test_runs_that_do_not_tile_the_block_are_refused(self):
        """The strongest structural check the format allows, so it must fire."""
        block = _rlc_block(data_min=0, counters=bytes([5]), values=bytes([1]))
        with pytest.raises(ValueError) as err:
            _decompress_rlc(block, 4096, "_ss_8_", "torn.rrd")
        assert "expand to 5 pixels" in str(err.value)
        assert "_ss_8_" in str(err.value)

    def test_an_unimplemented_bit_width_names_the_level(self):
        block = _rlc_block(data_min=0, counters=bytes([4]), values=bytes([1]), bit_width=3)
        with pytest.raises(NotImplementedError) as err:
            _decompress_rlc(block, 4, "_ss_16_", "odd.rrd")
        assert "3-bit" in str(err.value)
        assert "_ss_16_" in str(err.value)

    def test_values_are_not_wrapped_into_range(self):
        """dataMin + value overflowing a byte means the decode drifted.

        Masking it back to 0-255 would turn a decoder bug into a picture, and
        a picture is what gets signed off.
        """
        block = _rlc_block(data_min=200, counters=bytes([4]), values=bytes([100]))
        with pytest.raises(ValueError) as err:
            _decompress_rlc(block, 4, "_ss_4_", "drifted.rrd")
        assert "300" in str(err.value)


class TestBlockEncodings:
    def test_an_unknown_encoding_is_refused_rather_than_guessed(self):
        block = _Block(offset=0, size=4, log_valid=True, compression=7)
        with pytest.raises(NotImplementedError) as err:
            _decode_block(b"\x00" * 16, block, 4, "_ss_4_", "future.rrd")
        assert "encoding 7" in str(err.value)
        assert "_ss_4_" in str(err.value)

    def test_a_stored_block_of_the_wrong_length_is_refused(self):
        """Short-reading a stored block would pad the tile with whatever
        `np.zeros` left there — a black wedge, not an error."""
        block = _Block(offset=0, size=10, log_valid=True, compression=0)
        with pytest.raises(ValueError, match="stored block holds 10 bytes"):
            _decode_block(b"\x00" * 16, block, 4096, "_ss_4_", "short.rrd")

    def test_a_block_pointing_outside_the_file_is_refused(self):
        block = _Block(offset=8, size=4096, log_valid=True, compression=0)
        with pytest.raises(ValueError, match="outside the 16-byte file"):
            _decode_block(b"\x00" * 16, block, 4096, "_ss_4_", "truncated.rrd")
