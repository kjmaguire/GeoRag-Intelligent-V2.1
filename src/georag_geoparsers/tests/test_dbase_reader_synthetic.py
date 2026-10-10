"""The dBASE / MapInfo decode rule, on bytes built in the test.

tests/test_dbase_reader.py pins the rule against the RedStar delivery, and was
skipped wholesale on every machine without that delivery -- which is every CI
run. The failure the reader exists to prevent is silent (GDAL returns these
tables 91.9% null; a decode rule that drifts keeps returning plausible floats),
so nothing was watching it.

Everything here is built with ``struct`` from the format, never from the code
under test: the bytes of a double are ``struct.pack('<d', v)``, a record is the
deletion flag then fixed-width cells, a header is 32 bytes then one 32-byte
descriptor per field then 0x0D. The expected values are the ones that went in.
Each case mirrors a trap measured in the delivery and documented in
georag_geoparsers/dbase_reader.py, and says which.

No client data, no GDAL.
"""

import struct
from pathlib import Path

import pytest

from georag_geoparsers.dbase_reader import DbaseTable, read_dbase

# --------------------------------------------------------------------------- #
# A dBASE III writer, from the format
# --------------------------------------------------------------------------- #

Field = tuple[str, str, int, int]  # name, type byte, length, decimals


def build_dbf(
    fields: list[Field],
    records: list[list[bytes]],
    *,
    deleted: tuple[int, ...] = (),
    ldid: int = 0x00,
    status_byte: dict[int, int] | None = None,
    record_len_override: int | None = None,
) -> bytes:
    """Header, field descriptors, 0x0D, then one record per row.

    ``records[i]`` is the list of raw cells, each exactly its field's length.
    ``deleted`` flags those record indexes 0x2A; ``status_byte`` overrides a
    record's flag with an arbitrary byte.
    """
    header_len = 32 + 32 * len(fields) + 1
    record_len = 1 + sum(length for _n, _t, length, _d in fields)

    head = bytearray(32)
    head[0] = 0x03                                   # dBASE III, no memo
    head[1:4] = bytes([26, 10, 10])                  # last update YY MM DD
    struct.pack_into("<IHH", head, 4, len(records), header_len, record_len_override or record_len)
    head[29] = ldid

    descriptors = b""
    for name, type_byte, length, decimals in fields:
        descriptors += (
            name.encode("ascii")[:10].ljust(11, b"\x00")   # bytes 0-10: name
            + type_byte.encode("ascii")                     # byte 11: type
            + b"\x00" * 4                                   # bytes 12-15: reserved
            + bytes([length, decimals])                     # bytes 16, 17
            + b"\x00" * 14                                  # bytes 18-31: reserved
        )

    body = b""
    for index, cells in enumerate(records):
        for cell, (_n, _t, length, _d) in zip(cells, fields, strict=True):
            assert len(cell) == length, f"cell {cell!r} is not {length} bytes"
        flag = 0x2A if index in deleted else 0x20
        flag = (status_byte or {}).get(index, flag)
        body += bytes([flag]) + b"".join(cells)

    return bytes(head) + descriptors + b"\x0d" + body


def dbl(value: float) -> bytes:
    return struct.pack("<d", value)


def i32(value: int) -> bytes:
    return struct.pack("<i", value)


def nul_text(text: str, width: int) -> bytes:
    """MapInfo pads character cells with NULs, dBASE with spaces."""
    return text.encode("latin-1").ljust(width, b"\x00")


def space_text(text: str, width: int) -> bytes:
    return text.encode("latin-1").ljust(width, b" ")


def right_justified(text: str, width: int) -> bytes:
    return text.encode("ascii").rjust(width, b" ")


def read(tmp_path: Path, blob: bytes, **kwargs) -> DbaseTable:
    target = tmp_path / "synthetic.dbf"
    target.write_bytes(blob)
    return read_dbase(target, **kwargs)


def field_of(table: DbaseTable, name: str):
    return next(f for f in table.fields if f.name == name)


def column(table: DbaseTable, name: str) -> list:
    return [row[name] for row in table.rows]


# --------------------------------------------------------------------------- #
# The builder agrees with the format notes it is built from
# --------------------------------------------------------------------------- #


def test_the_documented_byte_patterns_are_what_struct_produces():
    """dbase_reader's docstring gives three patterns; they are the test's oracle."""
    assert dbl(400807.0) == bytes.fromhex("000000009c761841")      # a round coordinate starts with NULs
    assert dbl(14.0) == bytes.fromhex("0000000000002c40")          # cu_ppm: every byte printable-or-NUL
    assert bytes.fromhex("534f494c00000000") == b"SOIL" + b"\x00" * 4  # text with NUL padding in the exponent


# --------------------------------------------------------------------------- #
# MapInfo's binary-in-character fields
# --------------------------------------------------------------------------- #


def test_binary_doubles_declared_as_character_are_recovered(tmp_path):
    """The Sitka_trD case: every column claims 'C'; the bytes are IEEE doubles."""
    eastings = [400807.0, 400864.5, 400586.25]
    depths = [0.0, 61.5, 82.0]
    table = read(
        tmp_path,
        build_dbf(
            [("Collar", "C", 12, 0), ("Easting", "C", 8, 0), ("Depth", "C", 8, 0)],
            [[nul_text(f"TR00{i}-Sitka", 12), dbl(e), dbl(d)] for i, (e, d) in enumerate(zip(eastings, depths, strict=True), 2)],
        ),
    )

    assert {f.dbase_type for f in table.fields} == {"C"}
    assert field_of(table, "Collar").decoded_as == "text"
    assert field_of(table, "Easting").decoded_as == "double"
    assert column(table, "Collar") == ["TR002-Sitka", "TR003-Sitka", "TR004-Sitka"]
    assert column(table, "Easting") == eastings
    assert column(table, "Depth") == depths


def test_a_zero_in_a_double_column_is_a_measurement_not_a_gap(tmp_path):
    """0.0 is eight NUL bytes and MapInfo has no separate null: half a column of
    trench-start depths would vanish if all-NUL meant "missing"."""
    table = read(
        tmp_path,
        build_dbf([("Depth", "C", 8, 0)], [[dbl(0.0)], [dbl(61.5)], [dbl(0.0)], [dbl(32.0)]]),
    )

    assert field_of(table, "Depth").decoded_as == "double"
    assert column(table, "Depth") == [0.0, 61.5, 0.0, 32.0]


def test_printable_bytes_trap_14_0_is_a_number_not_punctuation(tmp_path):
    """cu_ppm = 14.0: a printability test reads its bytes as the string ',@'."""
    table = read(tmp_path, build_dbf([("cu_ppm", "C", 8, 0)], [[dbl(14.0)], [dbl(14.0)], [dbl(-1.0)]]))

    assert field_of(table, "cu_ppm").decoded_as == "double"
    assert column(table, "cu_ppm") == [14.0, 14.0, -1.0]


def test_trailing_nul_trap_text_is_not_a_denormal(tmp_path):
    """samsubtype = 'SOIL': unpacked as a double it is 6.3e-315."""
    values = ["SOIL", "TALU", "", "FROS"]
    table = read(tmp_path, build_dbf([("samsubtype", "C", 8, 0)], [[nul_text(v, 8)] for v in values]))

    assert field_of(table, "samsubtype").decoded_as == "text"
    assert column(table, "samsubtype") == values


def test_eight_unpadded_characters_are_text_too(tmp_path):
    """No padding puts ASCII in the exponent: magnitudes near 1e-154 or 1e299 fail the window."""
    table = read(tmp_path, build_dbf([("code", "C", 8, 0)], [[b"ABCDEFGH"], [b"STUVWXYZ"]]))

    assert field_of(table, "code").decoded_as == "text"
    assert column(table, "code") == ["ABCDEFGH", "STUVWXYZ"]


def test_a_column_of_all_nul_cells_declines_to_guess(tmp_path):
    """Eight NULs is both '' and 0.0; with no other evidence the field is text."""
    table = read(tmp_path, build_dbf([("Dip", "C", 8, 0)], [[b"\x00" * 8]] * 3))

    assert field_of(table, "Dip").decoded_as == "text"
    assert column(table, "Dip") == ["", "", ""]


def test_one_implausible_cell_condemns_the_whole_column_to_text(tmp_path):
    """The decision is per field, over every non-empty cell, and all must agree."""
    table = read(
        tmp_path,
        build_dbf([("mixed", "C", 8, 0)], [[dbl(14.0)], [dbl(1e300)], [dbl(22.5)]]),
    )

    assert field_of(table, "mixed").decoded_as == "text"


def test_values_at_the_edges_of_the_magnitude_window(tmp_path):
    inside = read(tmp_path, build_dbf([("v", "C", 8, 0)], [[dbl(1e-9)], [dbl(1e15)], [dbl(-1e15)], [dbl(0.173)]]))
    outside = read(tmp_path, build_dbf([("v", "C", 8, 0)], [[dbl(1e-10)], [dbl(14.0)]]))

    assert field_of(inside, "v").decoded_as == "double"
    assert field_of(outside, "v").decoded_as == "text"


def test_width_four_int32_is_decided_by_refutation_of_text(tmp_path):
    """Sitka_trD.__Key_DB: 0, 1, 2 ... row 1 is 01 00 00 00, and 0x01 is not a character."""
    keys = list(range(10))
    table = read(tmp_path, build_dbf([("__Key_DB", "C", 4, 0)], [[i32(k)] for k in keys]))

    assert field_of(table, "__Key_DB").decoded_as == "int32"
    assert column(table, "__Key_DB") == keys


def test_width_four_text_that_would_unpack_to_a_plausible_int_stays_text(tmp_path):
    """`color` = 'BR' unpacks to 21058 as an int32; width 4 cannot be decided per cell."""
    assert struct.unpack("<i", b"BR\x00\x00")[0] == 21058
    table = read(tmp_path, build_dbf([("color", "C", 4, 0)], [[nul_text("BR", 4)], [nul_text("GR", 4)], [b"    "]]))

    assert field_of(table, "color").decoded_as == "text"
    assert column(table, "color") == ["BR", "GR", ""]


def test_an_int32_column_of_printable_bytes_reads_as_text_by_design(tmp_path):
    """The documented residual ambiguity: the module reports what it can defend."""
    table = read(tmp_path, build_dbf([("odd", "C", 4, 0)], [[i32(0x41424344)], [i32(0x45464748)]]))

    assert field_of(table, "odd").decoded_as == "text"


@pytest.mark.parametrize("width", [1, 2, 3, 5, 6, 7, 9, 12, 16])
def test_only_widths_four_and_eight_are_ever_binary(tmp_path, width):
    """A 6-byte field cannot hold a double or an int32; testing it would only manufacture false positives."""
    cell = (dbl(14.0) + b"\x01" * 8)[:width]
    table = read(tmp_path, build_dbf([("x", "C", width, 0)], [[cell], [cell]]))

    assert field_of(table, "x").decoded_as == "text"


# --------------------------------------------------------------------------- #
# Deleted records
# --------------------------------------------------------------------------- #


def test_deleted_records_are_counted_but_not_returned(tmp_path):
    """23 declared / 7 deleted / 16 live is the Sitka_tr_Legend shape: all three numbers matter."""
    ids = list(range(1, 24))
    table = read(
        tmp_path,
        build_dbf([("ID", "N", 4, 0)], [[right_justified(str(i), 4)] for i in ids], deleted=tuple(range(7))),
    )

    assert table.record_count == 23
    assert table.deleted_count == 7
    assert len(table.rows) == 16
    assert column(table, "ID") == list(range(8, 24))


def test_an_unrecognised_status_byte_keeps_the_row(tmp_path, caplog):
    """An unknown byte is not evidence of a deletion; dropping the row would lose data."""
    table = read(
        tmp_path,
        build_dbf([("ID", "N", 3, 0)], [[b"  1"], [b"  2"], [b"  3"]], status_byte={1: 0x41}),
    )

    assert column(table, "ID") == [1, 2, 3]
    assert table.deleted_count == 0
    assert "unrecognised status byte 0x41" in caplog.text


# --------------------------------------------------------------------------- #
# Genuine ASCII dBASE numerics -- the no-regression guard
# --------------------------------------------------------------------------- #


def test_ascii_numerics_stay_numeric(tmp_path):
    table = read(
        tmp_path,
        build_dbf(
            [("OBJECTID", "N", 10, 0), ("AREA", "N", 18, 11), ("Type", "C", 12, 0)],
            [
                [right_justified("93", 10), right_justified("29024.30078125", 18), space_text("Misc", 12)],
                [right_justified("94", 10), right_justified("1.5", 18), space_text("HandSample", 12)],
            ],
        ),
    )

    assert (field_of(table, "OBJECTID").dbase_type, field_of(table, "OBJECTID").decoded_as) == ("N", "int32")
    assert field_of(table, "AREA").decoded_as == "double"
    assert column(table, "OBJECTID") == [93, 94]
    assert column(table, "AREA") == [29024.30078125, 1.5]
    assert column(table, "Type") == ["Misc", "HandSample"]


def test_a_blank_numeric_is_none_not_zero(tmp_path):
    """Writing 0 would put a fabricated assay result into a geochemistry table."""
    table = read(
        tmp_path,
        build_dbf([("au_ppm", "N", 8, 2)], [[right_justified("0.25", 8)], [b" " * 8], [right_justified("3.10", 8)]]),
    )

    assert column(table, "au_ppm") == [0.25, None, 3.10]


def test_an_overflow_marker_costs_the_column_its_type_not_the_row_its_value(tmp_path):
    """Legacy '****' markers: int -> float -> text, and the cell is kept verbatim."""
    table = read(
        tmp_path,
        build_dbf([("depth", "N", 6, 0)], [[right_justified("12", 6)], [b"  ****"], [right_justified("7", 6)]]),
    )

    assert field_of(table, "depth").decoded_as == "text"
    assert column(table, "depth") == ["12", "****", "7"]


def test_an_integer_column_with_one_decimal_value_demotes_to_float_not_text(tmp_path):
    table = read(
        tmp_path,
        build_dbf([("n", "N", 6, 0)], [[right_justified("12", 6)], [right_justified("7.5", 6)]]),
    )

    assert field_of(table, "n").decoded_as == "double"
    assert column(table, "n") == [12.0, 7.5]


# --------------------------------------------------------------------------- #
# Logical columns
# --------------------------------------------------------------------------- #


def test_mapinfo_logicals_are_zero_and_one_with_unknown_as_none(tmp_path):
    """MapInfo writes raw 0x01/0x00, so NUL is a recorded FALSE here and not padding."""
    table = read(
        tmp_path,
        build_dbf([("Use", "L", 1, 0)], [[b"\x01"], [b"\x00"], [b" "], [b"?"], [b"T"], [b"F"]]),
    )

    assert field_of(table, "Use").decoded_as == "int32"
    assert column(table, "Use") == [1, 0, None, None, 1, 0]


def test_an_undefined_logical_byte_demotes_the_column_to_text(tmp_path):
    """An unrecognised flag costs the column its type, never its content."""
    table = read(tmp_path, build_dbf([("Use", "L", 1, 0)], [[b"\x01"], [b"Z"]]))

    assert field_of(table, "Use").decoded_as == "text"
    assert column(table, "Use")[1] == "Z"


# --------------------------------------------------------------------------- #
# Text handling
# --------------------------------------------------------------------------- #


def test_trailing_padding_is_stripped_but_an_interior_line_break_survives(tmp_path):
    comment = b"check if bm sampled along this\r\nzone." + b" " * 10
    table = read(tmp_path, build_dbf([("Comments", "C", len(comment), 0)], [[comment]]))

    assert column(table, "Comments") == ["check if bm sampled along this\r\nzone."]


def test_a_trailing_line_break_is_trimmed_from_the_end_only(tmp_path):
    table = read(tmp_path, build_dbf([("c", "C", 12, 0)], [[b"ab\r\ncd\r\n    "]]))

    assert column(table, "c") == ["ab\r\ncd"]


def test_the_codec_is_the_callers_and_latin_1_is_total(tmp_path):
    """Neither a code page of 0x00 nor 0x57 drives the codec; `encoding` does."""
    cell = bytes([0x63, 0x61, 0x66, 0xE9, 0x80]).ljust(8, b" ")           # 'caf' + e-acute + 0x80
    blob_00 = build_dbf([("n", "C", 8, 0)], [[cell]], ldid=0x00)
    blob_57 = build_dbf([("n", "C", 8, 0)], [[cell]], ldid=0x57)

    default_00 = read(tmp_path, blob_00)
    default_57 = read(tmp_path, blob_57)
    cp1252 = read(tmp_path, blob_00, encoding="cp1252")

    assert column(default_00, "n") == column(default_57, "n") == ["café\x80"]
    assert column(cp1252, "n") == ["café€"]


def test_duplicate_field_names_are_both_kept(tmp_path, caplog):
    table = read(
        tmp_path,
        build_dbf([("X", "N", 3, 0), ("X", "N", 3, 0)], [[b"  1", b"  2"]]),
    )

    assert [f.name for f in table.fields] == ["X", "X_2"]
    assert table.rows == [{"X": 1, "X_2": 2}]
    assert "declares field 'X' more than once" in caplog.text


# --------------------------------------------------------------------------- #
# A realistic mixed table, and the contract on what `decoded_as` promises
# --------------------------------------------------------------------------- #


def test_a_soils_shaped_table_classifies_every_width_eight_column(tmp_path):
    """Doubles, text and all-empty side by side, as in all_historical_soils_clean."""
    rows = 5
    fields = [
        ("easting", "C", 8, 0), ("cu_ppm", "C", 8, 0), ("sb_ppm", "C", 8, 0),
        ("samsubtype", "C", 8, 0), ("quality", "C", 8, 0),
        ("empty_a", "C", 8, 0), ("empty_b", "C", 8, 0),
    ]
    records = [
        [
            dbl(383954.0 + i), dbl(14.0), dbl(-1.0 if i == 0 else 0.173),
            nul_text("SOIL" if i % 2 == 0 else "TALU", 8), nul_text("Good", 8),
            b"\x00" * 8, b"\x00" * 8,
        ]
        for i in range(rows)
    ]
    table = read(tmp_path, build_dbf(fields, records))

    doubles = [f.name for f in table.fields if f.decoded_as == "double"]
    texts = [f.name for f in table.fields if f.decoded_as == "text"]
    populated_text = [n for n in texts if any(column(table, n))]

    assert doubles == ["easting", "cu_ppm", "sb_ppm"]
    assert texts == ["samsubtype", "quality", "empty_a", "empty_b"]
    assert populated_text == ["samsubtype", "quality"]
    assert column(table, "sb_ppm")[0] == -1.0
    assert column(table, "samsubtype")[:2] == ["SOIL", "TALU"]


def test_decoded_as_predicts_the_python_type_of_every_value(tmp_path):
    """text -> str, double -> float, int32 -> int, None only for a blank numeric."""
    fields = [
        ("t", "C", 6, 0), ("d", "C", 8, 0), ("k", "C", 4, 0),
        ("n", "N", 5, 0), ("f", "N", 8, 2), ("u", "L", 1, 0),
    ]
    records = [
        [space_text("a", 6), dbl(2.5), i32(1), b"    1", right_justified("1.5", 8), b"\x01"],
        [space_text("b", 6), dbl(3.5), i32(2), b"     ", b" " * 8, b" "],
    ]
    table = read(tmp_path, build_dbf(fields, records))

    expected = {"text": str, "double": float, "int32": int}
    assert {f.name: f.decoded_as for f in table.fields} == {
        "t": "text", "d": "double", "k": "int32", "n": "int32", "f": "double", "u": "int32",
    }
    for field in table.fields:
        for row in table.rows:
            value = row[field.name]
            if value is not None:
                assert isinstance(value, expected[field.decoded_as]), (field.name, value)


# --------------------------------------------------------------------------- #
# Refusals -- half a table that looks whole is the worst return value
# --------------------------------------------------------------------------- #


def test_a_truncated_file_is_refused_by_name_with_the_shortfall(tmp_path):
    blob = build_dbf([("v", "C", 8, 0)], [[dbl(float(i))] for i in range(1, 11)])
    target = tmp_path / "Sitka_trD_cut.DAT"
    target.write_bytes(blob[:-20])

    with pytest.raises(ValueError) as excinfo:
        read_dbase(target)

    message = str(excinfo.value)
    assert "Sitka_trD_cut.DAT" in message
    assert "truncated" in message
    assert "10 records" in message
    assert "20 bytes" in message


def test_a_header_that_cannot_hold_its_own_fields_is_refused(tmp_path):
    blob = build_dbf([("a", "C", 8, 0), ("b", "C", 8, 0)], [[dbl(1.0), dbl(2.0)]], record_len_override=9)

    with pytest.raises(ValueError, match="inconsistent header"):
        read(tmp_path, blob)


def test_a_header_with_no_room_for_the_field_terminator_is_refused(tmp_path):
    blob = bytearray(build_dbf([("a", "C", 8, 0)], []))
    struct.pack_into("<H", blob, 8, 32 + 32)         # header ends where the terminator should be

    with pytest.raises(ValueError, match="no 0x0D field terminator"):
        read(tmp_path, bytes(blob))


def test_a_table_that_declares_no_fields_is_refused(tmp_path):
    with pytest.raises(ValueError, match="declares no fields"):
        read(tmp_path, build_dbf([], []))


def test_a_header_longer_than_the_file_is_refused(tmp_path):
    blob = bytearray(build_dbf([("a", "C", 8, 0)], [[dbl(1.0)]]))
    struct.pack_into("<H", blob, 8, 5000)

    with pytest.raises(ValueError, match="does not fit"):
        read(tmp_path, bytes(blob))


def test_a_zero_record_length_is_refused(tmp_path):
    blob = bytearray(build_dbf([("a", "C", 8, 0)], [[dbl(1.0)]]))
    struct.pack_into("<H", blob, 10, 0)

    with pytest.raises(ValueError, match="record length of 0"):
        read(tmp_path, bytes(blob))


def test_a_table_with_fields_and_no_records_reads_as_empty(tmp_path):
    table = read(tmp_path, build_dbf([("a", "C", 8, 0), ("b", "N", 4, 0)], []))

    assert [f.name for f in table.fields] == ["a", "b"]
    assert table.rows == []
    assert (table.record_count, table.deleted_count) == (0, 0)
