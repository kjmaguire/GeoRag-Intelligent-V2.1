"""Turning an ERDAS pyramid into bytes the raster path can take.

These two `.rrd` files are NOT throwaway previews. Neither parent raster is in
the delivery, so the pyramid holds the only surviving copy of each image — a
legible colour geological map and an underground mine plan. Refusing them as
"rendering companions" loses both.
"""

import io
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from georag_geoparsers import erdas_rrd
from georag_geoparsers.erdas_rrd import RrdLevel, read_rrd_levels, rrd_to_tiff_bytes

#: Where the client delivery is mounted; overridable so it need not be one
#: Windows desktop.
ROOT = Path(os.environ.get("REDSTAR_DELIVERY", r"C:\Users\GeoRAG\Desktop\RedStar"))
UNGA = ROOT / "Unga Regional (inc)" / "Geology" / "Digital Data" / "Geologic Map Unga 1982 color utm.rrd"
APOLLO = ROOT / "Apollo Sitka" / "UG Workings" / "Apollo-Sitka maps" / "acad etc" / "Apollo plan utm.rrd"
NO_PIXELS = ROOT / "Apollo Sitka" / "UG Workings" / "Apollo-Sitka maps" / "acad etc" / "Sitka Apollo drilling utm2.aux"

#: Applied to the classes that decode the delivery's pyramids, NOT to the module.
#: As a module-level pytestmark it skipped the refusal tests too, which need no
#: client data, and everything in the synthetic section at the bottom.
needs_delivery = pytest.mark.skipif(
    not UNGA.exists(),
    reason="RedStar delivery not present on this machine (set REDSTAR_DELIVERY)",
)


def _open(data: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(data))


@needs_delivery
class TestFinestLevelIsTaken:
    """Anything but the largest level discards resolution that exists nowhere else."""

    def test_unga_yields_its_finest_level(self):
        finest = max(read_rrd_levels(UNGA).levels, key=lambda lv: lv.width * lv.height)
        assert (finest.width, finest.height) == (1504, 2007)

        image = _open(rrd_to_tiff_bytes(UNGA))
        assert image.size == (1504, 2007)

    def test_apollo_yields_its_finest_level(self):
        image = _open(rrd_to_tiff_bytes(APOLLO))
        assert image.size == (364, 371)

    def test_it_is_not_a_smaller_level(self):
        # The pyramid has 6 levels; picking any other is a silent downgrade.
        levels = read_rrd_levels(UNGA).levels
        assert len(levels) > 1
        others = {(lv.width, lv.height) for lv in levels} - {(1504, 2007)}
        assert _open(rrd_to_tiff_bytes(UNGA)).size not in others


@needs_delivery
class TestTheImageIsReal:
    """A silently black image is worse than a refusal."""

    def test_unga_is_a_colour_image_with_actual_variation(self):
        image = _open(rrd_to_tiff_bytes(UNGA))
        assert image.mode == "RGB"
        extrema = image.convert("L").getextrema()
        assert extrema[0] != extrema[1], "image is a single flat value"

    def test_apollo_carries_its_alpha_band(self):
        # Measured: 4 bands. Dropping one would transpose the channels, which
        # is the failure the reader's reverse band order was written against.
        image = _open(rrd_to_tiff_bytes(APOLLO))
        assert image.mode == "RGBA"

    def test_the_output_is_a_tiff_the_raster_path_can_open(self):
        image = _open(rrd_to_tiff_bytes(APOLLO))
        assert image.format == "TIFF"


class TestRefusals:
    def test_a_file_with_no_pixel_blocks_raises_rather_than_returning_blank(self):
        # The .aux in this delivery has zero Edms_State nodes: no pixels, no
        # coordinates, nothing. A caller will hand it over by mistake.
        if not NO_PIXELS.exists():
            pytest.skip("companion .aux not present")
        with pytest.raises((ValueError, KeyError, OSError)):
            rrd_to_tiff_bytes(NO_PIXELS)

    def test_a_missing_file_raises(self):
        with pytest.raises((FileNotFoundError, OSError)):
            rrd_to_tiff_bytes(ROOT / "does-not-exist.rrd")


# ---------------------------------------------------------------------------
# The seam's own logic, with the pyramid decoder replaced by a hand-made one
#
# `rrd_to_tiff_bytes` does three things of its own on top of the decoder: it
# picks the finest level, maps the band count to an image mode, and refuses
# what it cannot map. The decoder (read_rrd_levels / extract_level) is pinned
# against the delivery in test_erdas_rrd.py; here it is stubbed, so these run
# anywhere and a regression in the seam cannot hide behind a missing delivery.
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_pyramid(monkeypatch):
    """Install levels and per-level arrays; returns the list of levels extracted."""

    def install(levels, arrays):
        requested: list[str] = []
        monkeypatch.setattr(erdas_rrd, "read_rrd_levels", lambda path: SimpleNamespace(levels=levels))

        def extract(path, level_name):
            requested.append(level_name)
            return arrays[level_name]

        monkeypatch.setattr(erdas_rrd, "extract_level", extract)
        return requested

    return install


def _gradient(height, width, bands=None):
    """A non-flat uint8 image, so a swapped or dropped channel is visible."""
    base = (np.arange(height * width, dtype=np.uint32) % 251).astype(np.uint8).reshape(height, width)
    if bands is None:
        return base
    return np.stack([(base + 40 * b).astype(np.uint8) for b in range(bands)], axis=-1)


class TestTheSeamOnAHandMadePyramid:
    def test_it_takes_the_largest_level_by_area_not_the_first_or_the_last(self, stub_pyramid):
        levels = [
            RrdLevel("_ss_2_", 20, 10, 3),
            RrdLevel("_ss_4_", 60, 40, 3),      # the finest: 2400 px
            RrdLevel("_ss_8_", 30, 20, 3),
        ]
        arrays = {lv.name: _gradient(lv.height, lv.width, 3) for lv in levels}
        requested = stub_pyramid(levels, arrays)

        image = _open(rrd_to_tiff_bytes("pyramid.rrd"))

        assert requested == ["_ss_4_"], "extracting any other level silently discards resolution"
        assert image.size == (60, 40)

    def test_a_wide_shallow_level_can_be_finer_than_a_square_one(self, stub_pyramid):
        levels = [RrdLevel("_ss_2_", 50, 50, 3), RrdLevel("_ss_4_", 200, 20, 3)]    # 2500 vs 4000
        stub_pyramid(levels, {lv.name: _gradient(lv.height, lv.width, 3) for lv in levels})

        assert _open(rrd_to_tiff_bytes("pyramid.rrd")).size == (200, 20)

    @pytest.mark.parametrize(
        ("bands", "mode"),
        [(None, "L"), (3, "RGB"), (4, "RGBA")],
    )
    def test_the_band_count_decides_the_image_mode(self, stub_pyramid, bands, mode):
        level = RrdLevel("_ss_4_", 16, 12, bands or 1)
        stub_pyramid([level], {"_ss_4_": _gradient(12, 16, bands)})

        image = _open(rrd_to_tiff_bytes("pyramid.rrd"))

        assert image.mode == mode
        assert image.size == (16, 12)

    def test_the_channels_come_out_in_the_order_they_went_in(self, stub_pyramid):
        # Every realistic breakage here yields an array, not an exception: a
        # transposed channel is a plausible-looking wrong picture.
        array = _gradient(12, 16, 3)
        stub_pyramid([RrdLevel("_ss_4_", 16, 12, 3)], {"_ss_4_": array})

        image = _open(rrd_to_tiff_bytes("pyramid.rrd"))

        assert np.array_equal(np.asarray(image), array)

    def test_the_output_is_a_tiff_that_round_trips_lossless(self, stub_pyramid):
        array = _gradient(12, 16, 4)
        stub_pyramid([RrdLevel("_ss_4_", 16, 12, 4)], {"_ss_4_": array})

        data = rrd_to_tiff_bytes("pyramid.rrd")
        image = _open(data)

        assert data[:4] in (b"II*\x00", b"MM\x00*")
        assert image.format == "TIFF"
        assert np.array_equal(np.asarray(image), array)

    def test_a_pyramid_with_no_levels_is_refused(self, stub_pyramid):
        stub_pyramid([], {})

        with pytest.raises(ValueError, match="no pyramid levels"):
            rrd_to_tiff_bytes("empty.rrd")

    @pytest.mark.parametrize("bands", [2, 5])
    def test_a_band_count_it_cannot_map_is_refused_not_guessed(self, stub_pyramid, bands):
        stub_pyramid([RrdLevel("_ss_4_", 16, 12, bands)], {"_ss_4_": _gradient(12, 16, bands)})

        with pytest.raises(ValueError, match="unsupported shape"):
            rrd_to_tiff_bytes("odd.rrd")
