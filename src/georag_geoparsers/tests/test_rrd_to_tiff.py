"""Turning an ERDAS pyramid into bytes the raster path can take.

These two `.rrd` files are NOT throwaway previews. Neither parent raster is in
the delivery, so the pyramid holds the only surviving copy of each image — a
legible colour geological map and an underground mine plan. Refusing them as
"rendering companions" loses both.
"""

import io
from pathlib import Path

import pytest

from georag_geoparsers.erdas_rrd import read_rrd_levels, rrd_to_tiff_bytes

ROOT = Path(r"C:\Users\GeoRAG\Desktop\RedStar")
UNGA = ROOT / "Unga Regional (inc)" / "Geology" / "Digital Data" / "Geologic Map Unga 1982 color utm.rrd"
APOLLO = ROOT / "Apollo Sitka" / "UG Workings" / "Apollo-Sitka maps" / "acad etc" / "Apollo plan utm.rrd"
NO_PIXELS = ROOT / "Apollo Sitka" / "UG Workings" / "Apollo-Sitka maps" / "acad etc" / "Sitka Apollo drilling utm2.aux"

pytestmark = pytest.mark.skipif(
    not UNGA.exists(), reason="RedStar delivery not present on this machine",
)


def _open(data: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(data))


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
