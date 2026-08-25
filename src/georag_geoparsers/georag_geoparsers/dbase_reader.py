"""Raw dBASE III/IV reader that recovers MapInfo's binary-in-character fields.

## Why this exists at all

GDAL cannot read these tables. Measured against the RedStar delivery on
2026-08-24: pyogrio refuses a ``.DAT`` path outright, and the same bytes
renamed to ``.dbf`` come back 91.9% null on ``all_historical_soils_clean``.
The cause is not a driver flag — GDAL truncates a character field at the
first NUL, and MapInfo writes numbers as raw little-endian IEEE-754 doubles
into fields the dBASE header declares as type ``'C'``. Every round coordinate
starts with NUL bytes (``400807.0`` is ``00 00 00 00 9c 76 18 41``), so the
value is cut to zero length before anything downstream sees it. The bytes are
unrecoverable from GDAL's output, which is why this module reads the file
itself rather than post-processing someone else's read.

## The decode problem

A ``'C'`` field of width 8 holds either eight characters of text or one
binary double, and the header does not say which. The obvious test — "does
this cell contain non-printable bytes?" — fails in BOTH directions, and both
failures are in the corpus:

* ``cu_ppm = 14.0`` encodes as ``00 00 00 00 00 00 2c 40``. Every byte is
  printable-or-NUL, so a printability test calls it text and yields the
  string ``',@'``. A real assay value becomes punctuation.
* ``samsubtype = 'SOIL'`` encodes as ``53 4f 49 4c 00 00 00 00``. The four
  trailing NULs read as non-printable, so the same test calls it binary and
  yields ``6.323412067e-315``. A category label becomes a denormal.

Neither failure is detectable from the cell alone, so the decision is made
once per FIELD, over its non-empty cells, and every cell must agree.

## The rule

A width-8 ``'C'`` field is a double column iff every non-empty cell, unpacked
little-endian, is exactly ``0.0`` or has magnitude in ``[1e-9, 1e15]``.

The range is what separates the two cases above, and it works because of
where a double keeps its exponent. Text packed into 8 bytes puts its NUL
padding in bytes 6-7 — precisely the exponent — so it always lands on a
denormal near 1e-315 and fails the floor. Eight characters with no padding
put ASCII in the exponent instead, giving magnitudes around 1e-154 or 1e299
depending on the last byte, which fails just as clearly. Real measurements
sit in the middle of the window: coordinates near 1e6, ppm values near 1e1,
gold grades near 1e-2. Only a value outside ``[1e-9, 1e15]`` in EVERY row
would be misread, and a geochemical table has no such column.

Measured on ``all_historical_soils_clean.DAT``: of 81 width-8 ``'C'`` fields,
37 classify as doubles, 3 as text (``SOIL``, ``Stp``, ``Good``), and 41 are
empty in all 854 rows. Zero misclassifications.

## Width is not always 8

``Sitka_trD.__Key_DB`` is a ``'C'`` field of width **4** holding a
little-endian int32 row key (0, 1, 2, ...). Width 4 admits no range test:
every four-byte pattern is a valid int32, and the soils file's ``color``
field — width 4, holding text like ``'BR'`` — unpacks to a perfectly
plausible 21058. There is nothing in a single cell to choose between them.

So width 4 is decided by refutation over the whole field, which is the same
"all cells must agree" principle read from the other side. Text is the null
hypothesis; a single cell that no character field could contain — a byte
below 0x20 surviving the padding strip — condemns the field to int32.
``__Key_DB`` row 1 is ``01 00 00 00``, and 0x01 is not a character;
``color`` is ``'BR'`` in every row and never offers such a byte.

The residual ambiguity is real and deliberate: an int32 column whose every
value happened to fall in the printable ASCII range would read as text. That
cannot be resolved from the bytes, so this module reports what it can defend
rather than guessing. It does not occur in the RedStar corpus.

## What ``decoded_as`` promises

``'text'`` -> ``str``, ``'double'`` -> ``float``, ``'int32'`` -> ``int``,
with ``None`` for a numeric cell that is blank. The vocabulary is closed on
purpose so a caller can pick a column type from the field list without
sampling the rows. Genuine ASCII ``'N'``/``'F'`` fields (MiscPoints_2005.dbf
uses them) go through the same three labels, and they use the same all-agree
fallback: if one cell in an integer column will not parse, the whole column
becomes float, and if it will not parse as that either, the whole column
becomes text. A legacy ``'****'`` overflow marker therefore costs its column
its numeric type instead of costing that row its value.

## Corroboration

The rule was derived from the bytes, but MapInfo can be made to grade it.
``tr006.4-geology_gcp.TAB`` carries the schema its own writer intended for a
Discover ground-control-point table — ``ID Integer; Use Logical; Image_X
Integer; Image_Y Integer; Map_X Float; ... Description Char (100)`` — while
``tr002.3-geology_gcp.DAT`` is the same table type with its ``.TAB`` missing,
so its header declares all ten columns as ``'C'``. Classifying that ``.DAT``
blind reproduces the sidecar's ten declared types exactly. That is an oracle
this module was never tuned against, which is the closest thing to a
correctness proof available for a rule inferred from data.

That table is also the only ``'L'`` column in the corpus, and it is where the
0x00 problem shows up: MapInfo writes logicals as raw 0x01/0x00, so a reader
that treats 0x00 as padding cannot tell false from never-written. Logical
columns are therefore decoded explicitly to 1/0, reserving ``None`` for the
space and ``'?'`` the format actually defines as unknown.

NOTE: Do NOT add `from __future__ import annotations` to this file — it
follows the convention of the sibling parser modules, which Dagster Config
introspects at runtime.
"""

import logging
import math
import struct
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

READER_NAME = "dbase_reader"
READER_VERSION = "1.0.0"

#: Fixed-size table header, then one 32-byte descriptor per field, then 0x0D.
_TABLE_HEADER_LEN = 32
_FIELD_DESCRIPTOR_LEN = 32
_FIELD_TERMINATOR = 0x0D

#: First byte of every record: 0x20 live, 0x2A deleted. dBASE never reclaims
#: the space, so a delete-flagged record still occupies a full record slot and
#: still counts toward the header's record total.
_RECORD_LIVE = 0x20
_RECORD_DELETED = 0x2A

#: Byte 29 of the header. 0x00 means "no code page declared" — MapInfo writes
#: that on every .DAT in the corpus — and 0x57 means ANSI. It is read for the
#: log line only: latin-1 is total (it cannot raise on any byte sequence), so
#: honouring a declared-but-wrong code page would mangle bytes that latin-1
#: round-trips intact. Callers who know better pass `encoding` explicitly.
_LDID_OFFSET = 29

#: Stripped from both ends of a character cell. dBASE pads with spaces and
#: MapInfo pads with NULs; a binary double may also START with NULs, which is
#: why this strips both ends rather than just the right.
#:
#: Tab is deliberately NOT padding. Sitka_tr_Legend's ID column contains the
#: int32 value 9, whose single significant byte IS 0x09 — treating tab as pad
#: would erase that cell's only content and cost the column a vote it should
#: have cast.
_TEXT_PAD = b"\x00 "

#: Trimmed from a decoded string. Wider than _TEXT_PAD because a comment field
#: may end with the line break that terminated it — MiscPoints_2005's first
#: comment contains an interior CRLF that must survive, so this only ever
#: touches the ends.
_TEXT_TRIM = "\x00 \t\r\n"

_DOUBLE_WIDTH = 8
_INT32_WIDTH = 4

#: The window a MapInfo-written measurement falls in. See the module docstring
#: for why text can never land inside it.
_DOUBLE_MIN_MAGNITUDE = 1e-9
_DOUBLE_MAX_MAGNITUDE = 1e15

#: dBASE types whose cells are right-justified ASCII digits, not binary.
_ASCII_NUMERIC_TYPES = frozenset({"N", "F"})

#: dBASE 'L' cells. The format spec allows the ASCII letters; MapInfo writes
#: raw 0x01/0x00 instead, which is why false and "never written" have to be
#: told apart by hand below.
_LOGICAL_TRUE = frozenset(b"\x01TtYy")
_LOGICAL_FALSE = frozenset(b"\x00FfNn")
#: Only a space or '?' is genuinely unknown. 0x00 is NOT in this set: MapInfo
#: means false by it, and folding the two together would turn every false
#: into a null.
_LOGICAL_UNKNOWN = frozenset(b"? ")

DECODED_TEXT = "text"
DECODED_DOUBLE = "double"
DECODED_INT32 = "int32"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DbaseField:
    """One column, with the type we declared AND the type we actually used.

    `dbase_type` is what the file claims; `decoded_as` is what survived the
    evidence. On a MapInfo table the two disagree on most columns, and a
    caller that trusts the first will build a table of punctuation.
    """

    name: str
    dbase_type: str          # the declared type byte
    length: int
    decoded_as: str          # 'text' | 'double' | 'int32' — what we ACTUALLY used


@dataclass(frozen=True)
class DbaseTable:
    """A fully decoded dBASE table.

    `record_count` is the header's own total and `rows` excludes the
    delete-flagged records, so the two differ by `deleted_count`. They are
    reported separately because "23 records" and "16 rows" are both true of
    Sitka_tr_Legend.DAT, and a loader that reconciles them against a sidecar
    needs to know which number it is holding.
    """

    fields: list[DbaseField]
    rows: list[dict[str, object]]
    record_count: int        # header count, INCLUDING deleted
    deleted_count: int


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def _parse_field_descriptors(raw: bytes, header_len: int, source: Path) -> list[tuple[str, str, int, int]]:
    """Walk the 32-byte descriptors up to the 0x0D terminator.

    The terminator is scanned for rather than derived from `header_len`. The
    two agree on every file in the corpus, but a writer that pads the header
    (dBASE IV appends a backlink block) makes the arithmetic invent fields out
    of padding, and an invented field shifts every subsequent column's offset.
    """
    descriptors: list[tuple[str, str, int, int]] = []
    offset = _TABLE_HEADER_LEN

    while offset < header_len:
        if raw[offset] == _FIELD_TERMINATOR:
            break
        if offset + _FIELD_DESCRIPTOR_LEN > header_len:
            raise ValueError(
                f"dBASE file '{source}' has a malformed header: a field descriptor at offset "
                f"{offset} runs past the declared header length of {header_len} bytes."
            )
        block = raw[offset:offset + _FIELD_DESCRIPTOR_LEN]
        name = block[:11].split(b"\x00")[0].decode("ascii", errors="replace").strip()
        descriptors.append((name, chr(block[11]), block[16], block[17]))
        offset += _FIELD_DESCRIPTOR_LEN
    else:
        raise ValueError(
            f"dBASE file '{source}' has a malformed header: no 0x0D field terminator in the "
            f"{header_len} bytes the header claims to occupy."
        )

    if not descriptors:
        raise ValueError(f"dBASE file '{source}' declares no fields.")
    return descriptors


def _disambiguate(names: list[str], source: Path) -> list[str]:
    """Make every column name unique so none is lost to the row dicts.

    Rows are dicts keyed by field name, so two columns sharing a name would
    silently collapse into one and the second would overwrite the first. A
    suffix keeps both — refusing the file would be worse, since recovering
    junk tables is the entire point of this reader.
    """
    seen: dict[str, int] = {}
    unique: list[str] = []
    for name in names:
        base = name or "field"
        if base in seen:
            seen[base] += 1
            renamed = f"{base}_{seen[base]}"
            logger.warning(
                "dbase_reader: '%s' declares field '%s' more than once — the later copy is exposed as '%s'.",
                source, base, renamed,
            )
            unique.append(renamed)
        else:
            seen[base] = 1
            unique.append(base)
    return unique


# ---------------------------------------------------------------------------
# Field classification — see the module docstring for the reasoning
# ---------------------------------------------------------------------------

def _is_present(cell: bytes) -> bool:
    """True when anything survives the padding, i.e. the cell carries evidence.

    An all-NUL width-8 cell is simultaneously a valid empty string and a valid
    0.0, so it votes for neither and is excluded from classification. It is
    still DECODED once the field's type is settled — inside a double column
    those eight NULs are a real measured zero, not a missing value.
    """
    return bool(cell.strip(_TEXT_PAD))


def _decodes_as_double(cells: list[bytes]) -> bool:
    """Every non-empty cell is a plausible IEEE-754 measurement."""
    saw_evidence = False
    for cell in cells:
        if not _is_present(cell):
            continue
        saw_evidence = True
        (value,) = struct.unpack("<d", cell)
        if value == 0.0:
            continue
        if not math.isfinite(value) or not (_DOUBLE_MIN_MAGNITUDE <= abs(value) <= _DOUBLE_MAX_MAGNITUDE):
            return False
    return saw_evidence


def _decodes_as_int32(cells: list[bytes]) -> bool:
    """Some non-empty cell holds a byte that no character field could contain.

    Width 4 has no range test to run, so text is refuted rather than a number
    being confirmed. One control byte anywhere in the column settles it for
    every row, because a dBASE column is homogeneous by construction.
    """
    for cell in cells:
        body = cell.strip(_TEXT_PAD)
        if not body:
            continue
        if any(byte < 0x20 or byte == 0x7F for byte in body):
            return True
    return False


def _classify_character(cells: list[bytes], width: int) -> str:
    """Decide what a 'C' column really holds.

    Only widths 8 and 4 are candidates for binary: those are the sizes MapInfo
    writes a double and an int32 into. Every other width is text, and testing
    it would only manufacture false positives — a 6-byte field cannot hold
    either type, so there is nothing to detect.
    """
    if width == _DOUBLE_WIDTH and _decodes_as_double(cells):
        return DECODED_DOUBLE
    if width == _INT32_WIDTH and _decodes_as_int32(cells):
        return DECODED_INT32
    return DECODED_TEXT


def _classify_logical(cells: list[bytes]) -> str:
    """A 'L' column is 0/1 integers as long as every cell is a legal flag.

    Exposing a boolean as 0/1 keeps `decoded_as` a closed three-value
    vocabulary, and 0/1 is lossless and sorts the way a caller expects. A
    column carrying anything the format does not define falls back to text
    rather than having its odd bytes rounded to a truth value.
    """
    for cell in cells:
        byte = cell[0] if cell else 0x20
        if byte not in _LOGICAL_TRUE and byte not in _LOGICAL_FALSE and byte not in _LOGICAL_UNKNOWN:
            return DECODED_TEXT
    return DECODED_INT32


def _decode_logical(cell: bytes) -> object:
    byte = cell[0] if cell else 0x20
    if byte in _LOGICAL_TRUE:
        return 1
    if byte in _LOGICAL_FALSE:
        return 0
    return None


def _classify_ascii_numeric(cells: list[bytes], decimals: int, encoding: str) -> str:
    """Decide an 'N'/'F' column's type, demoting rather than dropping.

    A column is only int if EVERY populated cell parses as one; one cell that
    does not costs the column its integer type, then its float type, and never
    costs that cell its content. Legacy exports carry '****' overflow markers
    and typed-in notes in numeric columns, and losing those rows to silent
    nulls is the failure this ordering exists to prevent.
    """
    texts = [cell.decode(encoding, errors="replace").strip(_TEXT_TRIM) for cell in cells]
    populated = [t for t in texts if t]
    if not populated:
        return DECODED_TEXT

    if decimals == 0:
        try:
            for text in populated:
                int(text)
            return DECODED_INT32
        except ValueError:
            pass

    try:
        for text in populated:
            float(text)
        return DECODED_DOUBLE
    except ValueError:
        return DECODED_TEXT


# ---------------------------------------------------------------------------
# Cell decoding
# ---------------------------------------------------------------------------

def _decode_column(cells: list[bytes], decoded_as: str, dbase_type: str, encoding: str) -> list[object]:
    """Turn one column's raw cells into Python values of the promised type."""
    if dbase_type == "L" and decoded_as == DECODED_INT32:
        return [_decode_logical(cell) for cell in cells]

    if dbase_type not in _ASCII_NUMERIC_TYPES:
        if decoded_as == DECODED_DOUBLE:
            return [struct.unpack("<d", cell)[0] for cell in cells]
        if decoded_as == DECODED_INT32:
            return [struct.unpack("<i", cell)[0] for cell in cells]

    texts = [cell.decode(encoding, errors="replace").strip(_TEXT_TRIM) for cell in cells]

    if decoded_as == DECODED_INT32:
        # A blank ASCII numeric is missing, not zero. Writing 0 here would
        # put a fabricated assay result into a geochemistry table.
        return [int(t) if t else None for t in texts]
    if decoded_as == DECODED_DOUBLE:
        return [float(t) if t else None for t in texts]
    return list(texts)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def read_dbase(path: str | Path, *, encoding: str = "latin-1") -> DbaseTable:
    """Read a dBASE III/IV table, decoding MapInfo's binary 'C' fields.

    Args:
        path: Path to a ``.dbf`` or MapInfo ``.DAT`` file.
        encoding: Codec for character data. The default is total — latin-1
            maps all 256 byte values — so a mislabelled file degrades to
            mojibake that can still be re-decoded downstream, rather than
            raising or dropping rows.

    Returns:
        DbaseTable. ``rows`` holds the live records only; ``record_count`` is
        the header's total and ``deleted_count`` says how many of those carry
        the 0x2A delete flag.

    Raises:
        FileNotFoundError: no file at *path*.
        ValueError: the header is malformed, or the file is shorter than the
            records it declares. A truncated file is refused outright — half
            a table that looks complete is worse than no table, because the
            missing rows leave no trace for anyone downstream to notice.
    """
    source = Path(path)
    raw = source.read_bytes()

    if len(raw) < _TABLE_HEADER_LEN:
        raise ValueError(
            f"dBASE file '{source}' is {len(raw)} bytes — too short to hold even the "
            f"{_TABLE_HEADER_LEN}-byte table header."
        )

    version = raw[0]
    record_count, header_len, record_len = struct.unpack("<IHH", raw[4:12])
    language_driver = raw[_LDID_OFFSET]

    if header_len < _TABLE_HEADER_LEN + 1 or header_len > len(raw):
        raise ValueError(
            f"dBASE file '{source}' declares a {header_len}-byte header, which does not fit in "
            f"the {len(raw)}-byte file."
        )
    if record_len < 1:
        raise ValueError(f"dBASE file '{source}' declares a record length of {record_len} bytes.")

    descriptors = _parse_field_descriptors(raw, header_len, source)
    names = _disambiguate([d[0] for d in descriptors], source)

    declared = 1 + sum(d[2] for d in descriptors)   # +1 for the deletion flag
    if declared > record_len:
        raise ValueError(
            f"dBASE file '{source}' has an inconsistent header: its {len(descriptors)} fields need "
            f"{declared} bytes per record (including the deletion flag) but the header declares "
            f"{record_len}. Reading it would run every column past its own cell."
        )

    needed = header_len + record_count * record_len
    if len(raw) < needed:
        short = needed - len(raw)
        raise ValueError(
            f"dBASE file '{source}' is truncated: the header declares {record_count} records of "
            f"{record_len} bytes beginning at offset {header_len}, needing {needed} bytes, but the "
            f"file holds {len(raw)}. It is {short} bytes ({short / record_len:.1f} records) short."
        )

    logger.info(
        "dbase_reader: opening '%s' — version=0x%02X ldid=0x%02X fields=%d records=%d record_len=%d",
        source, version, language_driver, len(descriptors), record_count, record_len,
    )

    # ------------------------------------------------------------------
    # Split into columns. Classification needs a whole column at once, so
    # the file is transposed before anything is decoded.
    # ------------------------------------------------------------------
    columns: list[list[bytes]] = [[] for _ in descriptors]
    deleted_count = 0

    for index in range(record_count):
        start = header_len + index * record_len
        record = raw[start:start + record_len]
        flag = record[0]
        if flag == _RECORD_DELETED:
            deleted_count += 1
            continue
        if flag != _RECORD_LIVE:
            # Not a flag this format defines. Keeping the row is the safe
            # call: an unknown byte is not evidence of a deletion, and
            # dropping it would lose data the file still contains.
            logger.warning(
                "dbase_reader: '%s' record %d carries an unrecognised status byte 0x%02X — kept as live.",
                source, index, flag,
            )
        offset = 1
        for column, (_name, _type, length, _dec) in zip(columns, descriptors, strict=True):
            column.append(record[offset:offset + length])
            offset += length

    # ------------------------------------------------------------------
    # Classify, then decode.
    # ------------------------------------------------------------------
    fields: list[DbaseField] = []
    decoded_columns: list[list[object]] = []

    for name, (_raw_name, dbase_type, length, decimals), cells in zip(names, descriptors, columns, strict=True):
        if dbase_type in _ASCII_NUMERIC_TYPES:
            decoded_as = _classify_ascii_numeric(cells, decimals, encoding)
        elif dbase_type == "C":
            decoded_as = _classify_character(cells, length)
        elif dbase_type == "L":
            decoded_as = _classify_logical(cells)
        else:
            # 'D', 'M' and anything else are handed back as trimmed text. No
            # file in the corpus exercises them, and converting a date on the
            # strength of the spec alone would ship a code path that has never
            # seen a real byte. The bytes are preserved either way.
            decoded_as = DECODED_TEXT

        fields.append(DbaseField(name=name, dbase_type=dbase_type, length=length, decoded_as=decoded_as))
        decoded_columns.append(_decode_column(cells, decoded_as, dbase_type, encoding))

    rows: list[dict[str, object]] = [
        dict(zip(names, values, strict=True)) for values in zip(*decoded_columns, strict=True)
    ] if decoded_columns else []

    binary_fields = [f.name for f in fields if f.dbase_type == "C" and f.decoded_as != DECODED_TEXT]
    logger.info(
        "dbase_reader: '%s' — %d rows (%d deleted of %d records), %d fields, "
        "%d character field(s) decoded as binary: %s",
        source, len(rows), deleted_count, record_count, len(fields),
        len(binary_fields), ", ".join(binary_fields) or "none",
    )

    return DbaseTable(
        fields=fields,
        rows=rows,
        record_count=record_count,
        deleted_count=deleted_count,
    )
