"""Tests for the Surpac string (.str) parser.

The structural assertions run against the REAL delivery file — the RedStar
Shumagin Main Vein level plans — rather than a fixture, because the numbers
being pinned (129 strings, 9,727 vertices, 73 levels at exact 5 m spacing,
127 closed and 2 not) are properties of that export that a hand-written
fixture would simply restate. They were measured off the file by hand before
the parser existed, so they are a genuine regression guard rather than a
recording of whatever the code happened to do.

The axis-swap test is the important one. A ``.str`` stores Y before X, and a
parser that reads them in file order produces output that is wrong in a way
nothing downstream can detect: the strings still close, the levels still
stack, the polygons still render — mirrored. ``test_points_are_east_first``
fails loudly if that swap is ever removed.

Edge conditions that the RedStar file does not contain — degenerate strings,
3D strings, malformed rows, a missing END sentinel — are synthetic, since the
only way to have one is to write one.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from pathlib import Path

import pytest

from georag_geoparsers.surpac_parser import (
    FLAT_TOLERANCE_M,
    SURPAC_EXTENSIONS,
    SurpacFile,
    SurpacString,
    read_surpac_strings,
)

# ---------------------------------------------------------------------------
# The real file
# ---------------------------------------------------------------------------

REAL_STR = Path(
    r"C:/Users/GeoRAG/Desktop/RedStar/Shumagin/Raster_Surfaces/MODELS/Main Vein"
    r"/JCG_Sections/Main Plan Sections.str"
)

requires_real_file = pytest.mark.skipif(
    not REAL_STR.exists(),
    reason=f"RedStar delivery not present at {REAL_STR}",
)

# Measured by hand off the file, 2026-08-24.
EXPECTED_STRINGS = 129
EXPECTED_VERTICES = 9727
EXPECTED_LEVELS = 73
EXPECTED_MIN_Z = -235.0
EXPECTED_MAX_Z = 125.0
EXPECTED_LEVEL_STEP = 5.0
EXPECTED_CLOSED = 127
EXPECTED_OPEN_STRING_NUMBERS = [24, 55]
EXPECTED_MIN_VERTICES_PER_STRING = 7
EXPECTED_MAX_VERTICES_PER_STRING = 213

# Line 3 of the file: ``1,6120684.44783417,399183.2878034668,125.0000249023437,``
# transposed to (x, y, z).
FIRST_VERTEX = (399183.2878034668, 6120684.44783417, 125.0000249023437)
SECOND_VERTEX = (399191.9603193359, 6120688.586986696, 125.0000249023437)
# Final vertex of string 129, the last data record in the file.
LAST_VERTEX = (399511.3835199432, 6120752.937565005, 5.000009643554677)

EXPECTED_BOUNDS = (
    399050.2907026367,     # min easting
    6120642.478197252,     # min northing
    399729.8399122314,     # max easting
    6120919.091488039,     # max northing
)


@pytest.fixture(scope="module")
def real() -> SurpacFile:
    """Parsed once — the file is 580 KB and every test below reads all of it."""
    if not REAL_STR.exists():
        pytest.skip(f"RedStar delivery not present at {REAL_STR}")
    return read_surpac_strings(REAL_STR)


@requires_real_file
class TestRedStarMainPlanSections:
    """Structural facts about the Shumagin Main Vein level-plan export."""

    def test_string_and_vertex_counts(self, real) -> None:
        assert len(real.strings) == EXPECTED_STRINGS
        assert sum(len(s.points) for s in real.strings) == EXPECTED_VERTICES

    def test_string_numbers_are_1_to_129(self, real) -> None:
        """One run per number, in file order.

        A phantom string here would mean the axis record on line 2 or the END
        sentinel on the last line had been mistaken for a terminator boundary
        with vertices behind it.
        """
        assert [s.string_number for s in real.strings] == list(range(1, 130))

    def test_points_are_east_first(self, real) -> None:
        """THE regression guard: X out of column 3, Y out of column 2.

        Read in file order these come out as (6120684, 399183) — a northing
        where the easting belongs. Nothing downstream would notice; the
        orebody would just be somewhere else.
        """
        x, y, z = real.strings[0].points[0]

        assert 399_000 < x < 400_000, f"x={x} is not an easting — Y/X swap lost?"
        assert 6_120_000 < y < 6_121_000, f"y={y} is not a northing — Y/X swap lost?"
        assert z == pytest.approx(125.0, abs=1e-3)

    def test_exact_first_and_last_vertices(self, real) -> None:
        """Full float precision, not rounded on the way through."""
        assert real.strings[0].points[0] == FIRST_VERTEX
        assert real.strings[0].points[1] == SECOND_VERTEX
        assert real.strings[-1].points[-1] == LAST_VERTEX

    def test_bounds(self, real) -> None:
        assert real.bounds == EXPECTED_BOUNDS

        minx, miny, maxx, maxy = real.bounds
        assert minx < maxx and miny < maxy
        # The extent is ~680 m east-west by ~277 m north-south. If the swap
        # were dropped these two would trade places.
        assert maxx - minx == pytest.approx(679.5, abs=1.0)
        assert maxy - miny == pytest.approx(276.6, abs=1.0)

    def test_every_string_is_flat(self, real) -> None:
        """These are level plans. A None level here means a 3D polyline crept in."""
        assert all(s.level_z is not None for s in real.strings)

    def test_levels_are_73_distinct_elevations_at_5m_spacing(self, real) -> None:
        levels = sorted({s.level_z for s in real.strings})

        assert len(levels) == EXPECTED_LEVELS
        assert levels[0] == EXPECTED_MIN_Z
        assert levels[-1] == EXPECTED_MAX_Z
        steps = {round(b - a, 9) for a, b in zip(levels, levels[1:], strict=False)}
        assert steps == {EXPECTED_LEVEL_STEP}
        # 73 levels, 5 m apart, spanning 360 m: the ladder has no gaps.
        assert len(levels) == int((EXPECTED_MAX_Z - EXPECTED_MIN_Z) / EXPECTED_LEVEL_STEP) + 1

    def test_level_z_is_the_design_elevation_not_the_float_noise(self, real) -> None:
        """FracSIS writes 125.0000249023437 and 125.0000096435547 on one level.

        Rounding to millimetres is what turns 129 near-duplicate values back
        into the 73 elevations the surveyor drew. Without it, keying a section
        index on ``level_z`` would produce one bucket per string.
        """
        raw_zs = {z for s in real.strings for _, _, z in s.points}
        assert len(raw_zs) > EXPECTED_LEVELS, "expected float noise in the raw Z column"

        assert all(s.level_z % EXPECTED_LEVEL_STEP == 0 for s in real.strings)

    def test_127_closed_2_open(self, real) -> None:
        assert sum(1 for s in real.strings if s.closed) == EXPECTED_CLOSED

        open_numbers = [s.string_number for s in real.strings if not s.closed]
        assert open_numbers == EXPECTED_OPEN_STRING_NUMBERS

    def test_open_strings_are_not_silently_closed(self, real) -> None:
        """Their endpoints are decimetres apart — a real gap, not float noise.

        Snapping them shut would invent vein outline nobody digitised, so the
        parser must leave the vertex list exactly as written.
        """
        by_number = {s.string_number: s for s in real.strings}

        for number in EXPECTED_OPEN_STRING_NUMBERS:
            string = by_number[number]
            assert string.points[0] != string.points[-1]

            gap = math.dist(string.points[0][:2], string.points[-1][:2])
            assert 0.3 < gap < 1.0, f"string {number} endpoint gap {gap} m"

        assert by_number[24].level_z == 65.0
        assert by_number[55].level_z == 5.0

    def test_vertices_per_string(self, real) -> None:
        counts = [len(s.points) for s in real.strings]

        assert min(counts) == EXPECTED_MIN_VERTICES_PER_STRING
        assert max(counts) == EXPECTED_MAX_VERTICES_PER_STRING
        assert all(c > 0 for c in counts), "a zero-vertex string is a parser artefact"

    def test_title(self, real) -> None:
        """Line 1 verbatim, minus the writer's trailing padding comma."""
        assert real.title.startswith(r"Z:\RedStar Working Folder\2015")
        assert real.title.endswith("Generated by FracSIS 5.3")
        assert "Main Plan Sections.str" in real.title
        assert not real.title.endswith(",")


# ---------------------------------------------------------------------------
# Synthetic edge cases
# ---------------------------------------------------------------------------

TITLE = "synthetic.str,,Generated by test,"
AXIS = "0,0.000,0.000,0.000,0.000,1.000,0.000"
TERMINATOR = "0,0.000,0.000,0.000"
END = "0,0.000,0.000,0.000,END"


def write_str(
    tmp_path: Path,
    body: list[str],
    *,
    name: str = "synthetic.str",
    header: list[str] | None = None,
    encoding: str = "utf-8",
    newline: str = "\r\n",
    trailing_newline: bool = True,
) -> Path:
    """Write a .str as BYTES.

    Not ``Path.write_text``: on this Windows host it re-encodes to cp1252 and
    translates line endings, which would silently rewrite the very things
    several of these tests exist to check.
    """
    lines = (header if header is not None else [TITLE, AXIS]) + body
    blob = newline.join(lines) + (newline if trailing_newline else "")
    path = tmp_path / name
    path.write_bytes(blob.encode(encoding))
    return path


class TestRecordLayout:
    def test_y_x_z_columns_are_transposed(self, tmp_path: Path) -> None:
        path = write_str(tmp_path, ["1,6120000.5,399000.25,100.0,", TERMINATOR, END])

        (point,) = read_surpac_strings(path).strings[0].points

        assert point == (399000.25, 6120000.5, 100.0)

    def test_descriptors_absent_empty_and_populated(self, tmp_path: Path) -> None:
        """Three shapes of the same record. The coordinates must survive all of them."""
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,10.0",              # no descriptor field
                "1,6120001.0,399001.0,10.0,",             # empty descriptor field
                "1,6120002.0,399002.0,10.0,ORE,HW,,3",    # four of them, one empty
                TERMINATOR,
                END,
            ],
        )

        points = read_surpac_strings(path).strings[0].points

        assert points == [
            (399000.0, 6120000.0, 10.0),
            (399001.0, 6120001.0, 10.0),
            (399002.0, 6120002.0, 10.0),
        ]

    def test_blank_lines_and_no_trailing_newline(self, tmp_path: Path) -> None:
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,10.0,",
                "",
                "   ",
                "1,6120001.0,399001.0,10.0,",
                TERMINATOR,
                END,
            ],
            trailing_newline=False,
        )

        result = read_surpac_strings(path)

        assert len(result.strings) == 1
        assert len(result.strings[0].points) == 2

    def test_lf_line_endings(self, tmp_path: Path) -> None:
        """Surpac writes CRLF, but a file that has been through git or scp may not."""
        path = write_str(
            tmp_path, ["1,6120000.0,399000.0,10.0,", TERMINATOR, END], newline="\n"
        )

        assert len(read_surpac_strings(path).strings[0].points) == 1

    def test_axis_record_does_not_become_a_phantom_string(self, tmp_path: Path) -> None:
        """Line 2 opens with string number 0, exactly as a terminator does.

        Told apart by position only. A uniform terminator rule would emit an
        empty string ahead of the real first one.
        """
        path = write_str(tmp_path, ["7,6120000.0,399000.0,10.0,", TERMINATOR, END])

        result = read_surpac_strings(path)

        assert len(result.strings) == 1
        assert result.strings[0].string_number == 7

    def test_missing_axis_record_keeps_the_data_row(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A line 2 that carries a real string number is data, not a header."""
        path = write_str(
            tmp_path,
            ["1,6120000.0,399000.0,10.0,", TERMINATOR, END],
            header=[TITLE],
        )

        with caplog.at_level(logging.WARNING, logger="georag_geoparsers.surpac_parser"):
            result = read_surpac_strings(path)

        assert len(result.strings[0].points) == 1
        assert "not an axis record" in caplog.text

    def test_title_keeps_interior_commas(self, tmp_path: Path) -> None:
        """Windows paths may contain commas, so the title is never field-split."""
        path = write_str(
            tmp_path,
            ["1,6120000.0,399000.0,10.0,", TERMINATOR, END],
            header=[r"C:\Sections, 2015\plan.str,,Generated by FracSIS 5.3,,", AXIS],
        )

        assert read_surpac_strings(path).title == r"C:\Sections, 2015\plan.str,,Generated by FracSIS 5.3"

    def test_cp1252_title_falls_back(self, tmp_path: Path) -> None:
        """One unmappable byte in a folder name must not cost the coordinates."""
        path = write_str(
            tmp_path,
            ["1,6120000.0,399000.0,10.0,", TERMINATOR, END],
            header=[r"Z:\Levé topographique\plan.str,,FracSIS,", AXIS],
            encoding="cp1252",
        )

        result = read_surpac_strings(path)

        assert "Levé topographique" in result.title
        assert result.strings[0].points == [(399000.0, 6120000.0, 10.0)]


class TestClosure:
    def test_repeated_first_vertex_is_closed(self, tmp_path: Path) -> None:
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,10.0,",
                "1,6120010.0,399000.0,10.0,",
                "1,6120010.0,399010.0,10.0,",
                "1,6120000.0,399000.0,10.0,",
                TERMINATOR,
                END,
            ],
        )

        assert read_surpac_strings(path).strings[0].closed is True

    def test_near_miss_is_not_closed(self, tmp_path: Path) -> None:
        """One millimetre short is still open. The parser does not snap."""
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,10.0,",
                "1,6120010.0,399000.0,10.0,",
                "1,6120010.0,399010.0,10.0,",
                "1,6120000.001,399000.0,10.0,",
                TERMINATOR,
                END,
            ],
        )

        assert read_surpac_strings(path).strings[0].closed is False

    def test_single_vertex_is_not_closed(self, tmp_path: Path) -> None:
        path = write_str(tmp_path, ["1,6120000.0,399000.0,10.0,", TERMINATOR, END])

        string = read_surpac_strings(path).strings[0]

        assert string.closed is False
        assert string.points == [(399000.0, 6120000.0, 10.0)]
        assert string.level_z == 10.0

    def test_all_identical_vertices_report_closed(self, tmp_path: Path) -> None:
        """Documented behaviour, pinned so it stays deliberate.

        ``closed`` says the vertex list repeats its first point and nothing
        more. This one does, and encloses no area — a caller building polygons
        still has to reject rings with fewer than three distinct vertices.
        """
        row = "1,6120000.0,399000.0,10.0,"
        path = write_str(tmp_path, [row, row, row, TERMINATOR, END])

        string = read_surpac_strings(path).strings[0]

        assert string.closed is True
        assert len(set(string.points)) == 1


class TestLevelZ:
    def test_float_noise_within_tolerance_is_still_flat(self, tmp_path: Path) -> None:
        """The RedStar spread, reproduced: 125.0000249… and 125.0000096…."""
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,125.0000249023437,",
                "1,6120010.0,399010.0,125.0000096435547,",
                TERMINATOR,
                END,
            ],
        )

        assert read_surpac_strings(path).strings[0].level_z == 125.0

    def test_a_real_3d_string_has_no_level(self, tmp_path: Path) -> None:
        """A drillhole trace or wireframe edge is not a level plan.

        None means "this string is genuinely 3D", never "the elevation was
        missing" — the caller must not paper over it with a default.
        """
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,100.0,",
                "1,6120010.0,399010.0,95.0,",
                TERMINATOR,
                END,
            ],
        )

        assert read_surpac_strings(path).strings[0].level_z is None

    def test_tolerance_boundary(self, tmp_path: Path) -> None:
        """Just inside is flat, an order of magnitude outside is not."""
        inside = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,50.0,",
                f"1,6120010.0,399010.0,{50.0 + FLAT_TOLERANCE_M / 2},",
                TERMINATOR,
                END,
            ],
            name="inside.str",
        )
        outside = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,50.0,",
                f"1,6120010.0,399010.0,{50.0 + FLAT_TOLERANCE_M * 10},",
                TERMINATOR,
                END,
            ],
            name="outside.str",
        )

        assert read_surpac_strings(inside).strings[0].level_z == 50.0
        assert read_surpac_strings(outside).strings[0].level_z is None

    def test_negative_levels(self, tmp_path: Path) -> None:
        """Underground sections run below datum; -235 m is the RedStar floor."""
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,-235.0000249023437,",
                "1,6120010.0,399010.0,-235.0000096435547,",
                TERMINATOR,
                END,
            ],
        )

        assert read_surpac_strings(path).strings[0].level_z == -235.0


class TestSegmentation:
    def test_same_number_twice_stays_two_strings(self, tmp_path: Path) -> None:
        """Two lenses on one level share a string number and are not connected.

        Merging them would draw a segment across the gap between them.
        """
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,10.0,",
                "1,6120010.0,399010.0,10.0,",
                TERMINATOR,
                "1,6120500.0,399500.0,10.0,",
                "1,6120510.0,399510.0,10.0,",
                TERMINATOR,
                END,
            ],
        )

        strings = read_surpac_strings(path).strings

        assert len(strings) == 2
        assert [s.string_number for s in strings] == [1, 1]

    def test_number_change_without_terminator_splits(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,10.0,",
                "2,6120500.0,399500.0,15.0,",
                TERMINATOR,
                END,
            ],
        )

        with caplog.at_level(logging.WARNING, logger="georag_geoparsers.surpac_parser"):
            strings = read_surpac_strings(path).strings

        assert [s.string_number for s in strings] == [1, 2]
        assert [len(s.points) for s in strings] == [1, 1]
        assert "is still open" in caplog.text

    def test_unterminated_final_string_is_kept(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A truncated file has lost records; it has not lost the ones it still has."""
        path = write_str(tmp_path, ["1,6120000.0,399000.0,10.0,"])

        with caplog.at_level(logging.WARNING, logger="georag_geoparsers.surpac_parser"):
            result = read_surpac_strings(path)

        assert len(result.strings) == 1
        assert "no terminator record" in caplog.text
        assert "may be truncated" in caplog.text

    def test_missing_end_sentinel_is_reported(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = write_str(tmp_path, ["1,6120000.0,399000.0,10.0,", TERMINATOR])

        with caplog.at_level(logging.WARNING, logger="georag_geoparsers.surpac_parser"):
            read_surpac_strings(path)

        assert "may be truncated" in caplog.text

    def test_end_sentinel_present_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = write_str(tmp_path, ["1,6120000.0,399000.0,10.0,", TERMINATOR, END])

        with caplog.at_level(logging.WARNING, logger="georag_geoparsers.surpac_parser"):
            read_surpac_strings(path)

        assert "may be truncated" not in caplog.text


class TestMalformedInput:
    def test_bad_row_is_skipped_and_the_rest_survive(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One unreadable line must not cost the other 9,726 coordinates."""
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,10.0,",
                "1,6120001.0,NORTHING,10.0,",   # non-numeric coordinate
                "1,6120002.0,399002.0,",        # Z field missing
                "not a record at all",
                "1,6120003.0,399003.0,10.0,",
                TERMINATOR,
                END,
            ],
        )

        with caplog.at_level(logging.WARNING, logger="georag_geoparsers.surpac_parser"):
            result = read_surpac_strings(path)

        assert result.strings[0].points == [
            (399000.0, 6120000.0, 10.0),
            (399003.0, 6120003.0, 10.0),
        ]
        assert "3 row(s) skipped as malformed" in caplog.text

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.str"
        path.write_bytes(b"")

        with pytest.raises(ValueError, match="no title line"):
            read_surpac_strings(path)

    def test_header_only_file_raises(self, tmp_path: Path) -> None:
        """Refused rather than returned with a fabricated bounding box.

        There is no honest extent for an empty set, and a caller reading
        ``(0, 0, 0, 0)`` as one would place the file at the CRS origin.
        """
        path = write_str(tmp_path, [END])

        with pytest.raises(ValueError, match="no vertex records"):
            read_surpac_strings(path)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_surpac_strings(tmp_path / "nope.str")


class TestBounds:
    def test_bounds_span_every_string(self, tmp_path: Path) -> None:
        path = write_str(
            tmp_path,
            [
                "1,6120000.0,399000.0,10.0,",
                "1,6120100.0,399100.0,10.0,",
                TERMINATOR,
                "2,6119000.0,398000.0,20.0,",
                "2,6121000.0,400000.0,20.0,",
                TERMINATOR,
                END,
            ],
        )

        assert read_surpac_strings(path).bounds == (
            398000.0, 6119000.0, 400000.0, 6121000.0,
        )

    def test_bounds_are_minx_miny_maxx_maxy(self, tmp_path: Path) -> None:
        """Ordering pinned explicitly — (minx, miny, maxx, maxy), not (x, x, y, y)."""
        path = write_str(
            tmp_path,
            ["1,6120000.0,399000.0,10.0,", "1,6120050.0,399200.0,10.0,", TERMINATOR, END],
        )

        minx, miny, maxx, maxy = read_surpac_strings(path).bounds

        assert (minx, maxx) == (399000.0, 399200.0)
        assert (miny, maxy) == (6120000.0, 6120050.0)


class TestModuleContract:
    def test_claims_the_str_extension(self) -> None:
        assert ".str" in SURPAC_EXTENSIONS

    def test_nothing_returned_claims_a_crs(self, tmp_path: Path) -> None:
        """A .str declares no CRS, so this module must not invent one.

        The magnitudes look like UTM zone 3N, but that inference belongs to
        the caller who knows where the project is, not to the reader that
        knows the bytes. Asserted on the returned surface rather than by
        grepping the module for "EPSG", which would only ever match the
        comment explaining why there is no EPSG.
        """
        from georag_geoparsers import surpac_parser

        names = {
            f.name.lower()
            for cls in (SurpacFile, SurpacString)
            for f in dataclasses.fields(cls)
        } | {n.lower() for n in vars(surpac_parser)}

        assert not any("crs" in n or "epsg" in n or "srid" in n for n in names)

        # And no CRS smuggled onto the instance either.
        path = write_str(tmp_path, ["1,6120000.0,399000.0,10.0,", TERMINATOR, END])
        result = read_surpac_strings(path)

        assert not any(
            "crs" in n.lower() or "epsg" in n.lower()
            for n in dir(result) + dir(result.strings[0])
        )

    def test_accepts_str_and_path(self, tmp_path: Path) -> None:
        path = write_str(tmp_path, ["1,6120000.0,399000.0,10.0,", TERMINATOR, END])

        assert read_surpac_strings(str(path)).bounds == read_surpac_strings(path).bounds
