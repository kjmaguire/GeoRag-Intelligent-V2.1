"""Geosoft XYZ parsing (ING-19, 2026-09-29).

The layouts below are the ones Oasis montaj actually writes. The default
export has NO line column: lines are delimited by ``Line <id>`` / ``Tie <id>``
marker rows, which the previous parser read as data rows (X = "Line"),
losing every line. These tests pin the shapes that fail silently when wrong:
the marker grouping, FID not being a line number, dummies becoming None, and
a short row being skipped rather than padded into misalignment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from georag_geoparsers.xyz_parser import (
    CODE_COORD_NOT_NUMERIC,
    CODE_FIELD_COUNT,
    iter_xyz_lines,
    parse_xyz_file,
    scan_xyz_header,
)

GEOSOFT_DEFAULT = """\
/ ------------------------------------------------
/ XYZ EXPORT [03/15/2019]
/ DATABASE  [C:\\Jobs\\Sitka\\mag.gdb]
/ ------------------------------------------------
/
/        X            Y          FID     MAG_TMI     MAG_RESID
/
Line 1010
   495000.0    6220000.0         1     55432.1        -12.3
   495010.5    6220005.2         2     55431.9         *
   495020.5    6220010.2         3     -1.0E32         -8.7
   495030.5    garbage           4     55431.9         -8.7
   495040.5    6220020.2         5     55431.9
Tie 9010
   495000.0    6220100.0         1     55432.5        -2.3
   495100.0    6220100.0         2     55433.5        -1.3
"""


def _write(tmp_path: Path, text: str, name: str = "survey.xyz") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_line_and_tie_markers_define_the_lines(tmp_path: Path) -> None:
    result = parse_xyz_file(_write(tmp_path, GEOSOFT_DEFAULT))
    assert [(ln.line_id, ln.line_type) for ln in result.lines] == [
        ("1010", "line"), ("9010", "tie"),
    ]
    first = result.lines[0]
    # Rows 12 (non-numeric Y) and 13 (short) are not in the line.
    assert first.source_rows == [9, 10, 11]
    assert first.x == [495000.0, 495010.5, 495020.5]


def test_fid_is_a_channel_not_the_line_number(tmp_path: Path) -> None:
    header = scan_xyz_header(_write(tmp_path, GEOSOFT_DEFAULT))
    assert header.line_column is None
    assert "FID" in header.channel_columns


def test_dummies_become_none(tmp_path: Path) -> None:
    result = parse_xyz_file(_write(tmp_path, GEOSOFT_DEFAULT))
    channels = result.lines[0].channels
    assert channels["MAG_RESID"][1] is None      # '*'
    assert channels["MAG_TMI"][2] is None        # -1.0E32


def test_bad_rows_are_skipped_with_a_reason_not_padded(tmp_path: Path) -> None:
    result = parse_xyz_file(_write(tmp_path, GEOSOFT_DEFAULT))
    by_row = {issue.row: issue.code for issue in result.skipped_rows}
    assert by_row == {12: CODE_COORD_NOT_NUMERIC, 13: CODE_FIELD_COUNT}
    # Every channel stays aligned with the coordinates.
    for line in result.lines:
        for values in line.channels.values():
            assert len(values) == line.point_count


def test_a_line_column_splits_rows_when_there_are_no_markers(tmp_path: Path) -> None:
    text = (
        "/ EASTING NORTHING LINE_NO GRAV_BOUGUER\n"
        "400000 6100000 100 -12.5\n"
        "400010 6100000 100.0 -12.4\n"
        "400000 6100200 200 -11.9\n"
    )
    result = parse_xyz_file(_write(tmp_path, text))
    assert [(ln.line_id, ln.point_count) for ln in result.lines] == [("100", 2), ("200", 1)]
    assert result.lines[0].channels == {"GRAV_BOUGUER": [-12.5, -12.4]}


def test_an_uncommented_header_row_is_accepted(tmp_path: Path) -> None:
    text = "X Y K_PCT U_PPM TH_PPM\n500000 6000000 1.2 2.1 8.5\n"
    header = scan_xyz_header(_write(tmp_path, text))
    assert header.header_source == "first_row"
    result = parse_xyz_file(_write(tmp_path, text))
    assert result.point_count == 1
    assert result.lines[0].line_type == "points"


def test_projected_pair_wins_over_lat_long(tmp_path: Path) -> None:
    text = "/ LONG LAT X Y MAG\nLine 1\n-129.1 56.1 495000 6220000 55000\n"
    header = scan_xyz_header(_write(tmp_path, text))
    assert (header.easting_column, header.northing_column) == ("X", "Y")
    assert header.axis_family == "projected"
    assert set(header.other_axis_columns) == {"LONG", "LAT"}


def test_a_header_with_no_coordinate_pair_is_refused(tmp_path: Path) -> None:
    # The old parser fell back to "column 0 is X, column 1 is Y".
    with pytest.raises(ValueError, match="no complete coordinate pair"):
        scan_xyz_header(_write(tmp_path, "/ X FID MAG\n1 2 3\n"))


def test_a_long_line_is_split_into_segments(tmp_path: Path) -> None:
    rows = "\n".join(f"{400000 + i} 6100000 {i}" for i in range(5))
    path = _write(tmp_path, f"/ X Y MAG\nLine 7\n{rows}\n")
    blocks = list(iter_xyz_lines(path, max_points=2))
    assert [(b.line_id, b.segment, b.point_count) for b in blocks] == [
        ("7", 1, 2), ("7", 2, 2), ("7", 3, 1),
    ]


def test_provenance_carries_the_file_hash(tmp_path: Path) -> None:
    result = parse_xyz_file(_write(tmp_path, GEOSOFT_DEFAULT))
    assert len(result.provenance["source_file_sha256"]) == 64
    assert result.provenance["parser_name"] == "xyz_parser"
