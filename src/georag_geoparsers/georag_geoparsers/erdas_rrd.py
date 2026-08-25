"""ERDAS Imagine HFA pyramid reader — pulls the image levels out of a `.rrd`.

## Why this exists

An `.rrd` is normally a throwaway. ERDAS Imagine writes it beside a raster to
hold the reduced-resolution pyramid its viewer pans with, and anyone who loses
one regenerates it from the parent in seconds. That makes "it's only an
overview" the default reading, and for most `.rrd` files the default reading is
right.

It is wrong for the two in the RedStar delivery. Neither names a parent that
was actually delivered — `Geologic Map Unga 1982 color utm.rrd` points at
`Geologic Map Unga 1982 color utm.tif` and `Apollo plan utm.rrd` at
`Apollo plan utm.tif`, and neither `.tif` is anywhere in the archive. The
pyramid is therefore the ONLY surviving copy of both maps: a legible 1504x2007
"GENERALIZED GEOLOGIC MAP OF UNGA ISLAND, ALASKA" with its EXPLANATION legend,
and a 364x371 UTM-warped plan of the Apollo underground workings. Skipping
`.rrd` as derived data loses two maps outright, which is why this module reads
them instead of the format's reputation.

## The container

HFA is a tree of fixed-size `Ehfa_Entry` nodes, each naming a type whose byte
layout is described by a MIF dictionary embedded in the file itself. Only four
node types matter here:

    Eimg_Layer            "Band_1" ... "Band_N"  — one per band
      Eimg_Layer_SubSample  "_ss_4_", "_ss_8_"   — one per pyramid level
        Edms_State            "RasterDMS"        — that level's block table
    Eimg_DependentFile    names the parent raster

So a level is a NAME shared across bands, not a single node: `_ss_4_` in the
Unga file is three `Edms_State` nodes (R, G, B) that assemble into one image.
Levels are collected by layer-node name and bands ordered by their parent
`Eimg_Layer` name, because sibling order in the file is not band order — both
files list their bands high-to-low (`Band_4`, `Band_3`, ... `Band_1`), so
trusting file order would silently transpose the colour channels.

## Two things the format's folklore gets wrong

**Blocks are not always 64x64.** They usually are, but the coarsest level of
each file stores the whole layer as one undersized block (46x47 in the Apollo
file, 47x63 in the Unga file). `blockWidth`/`blockHeight` are read per layer
for that reason; hardcoding 64 tiles garbage into the top of the pyramid.

**Blocks are not always uncompressed.** The Apollo file stores every block
raw, so a reader tested only against it looks finished. The Unga file — the
one carrying the geologic map — RLC-compresses 281 of its blocks and mixes
them with stored ones inside a single level, so a reader without
`_decompress_rlc` produces a torn image from the file that matters most.

## Georeferencing: there is none, stop looking

Measured across both `.rrd` files and the delivery's `.aux`: zero `Eprj_MapInfo`
and zero `Eprj_ProParameters` nodes. The pixels are all there is. Both parents
were warped to UTM before the pyramid was built — the Apollo plan sits visibly
rotated inside its frame — so the corner coordinates are gone with the `.tif`,
not hiding in a node this module declines to parse. Anything downstream that
needs a world file has to get it from a human or from the map collar.

## Scope

8-bit unsigned pixels only, which is what every level of both files stores.
Any other pixel type, and any block encoding beyond stored (0) and RLC (1),
raises and names the level rather than reinterpreting the bytes — a wrong
guess here does not fail loudly, it returns a plausible-looking black or
speckled image that a reviewer accepts.
"""

import io
import logging
import os
import re
import struct
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

PARSER_NAME = "erdas_rrd"
PARSER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Container layout
#
# Every offset below is fixed by the MIF dictionary that HFA embeds in each
# file. They are hardcoded rather than parsed out of that dictionary because
# the four types this module touches have been stable since Imagine 8.x, and a
# dictionary interpreter is a great deal of machinery to carry for types that
# do not move. The layouts were re-read out of both delivered files to confirm.
# ---------------------------------------------------------------------------

_MAGIC = b"EHFA_HEADER_TAG\0"

#: `Ehfa_File` — version, freeList, rootEntryPtr, entryHeaderLength, dictionaryPtr.
_FILE_STRUCT = struct.Struct("<iIIhI")

#: `Ehfa_Entry` — next, prev, parent, child, data, dataSize; then a 64-byte
#: name and a 32-byte type, both NUL-terminated.
_ENTRY_STRUCT = struct.Struct("<IIIIIi")
_ENTRY_NAME_OFFSET = 24
_ENTRY_NAME_LENGTH = 64
_ENTRY_TYPE_OFFSET = 88
_ENTRY_TYPE_LENGTH = 32
#: Bytes of an entry this module actually reads. Both files declare
#: entryHeaderLength = 128 while the dictionary's Ehfa_Entry sums to 124; the
#: four-byte disagreement sits past every field used here, so bounds are
#: checked against what is read rather than against either declared size.
_ENTRY_READ_LENGTH = 124

#: `Eimg_Layer` — width, height, layerType, pixelType, blockWidth, blockHeight.
_LAYER_STRUCT = struct.Struct("<iihhii")

#: `Edms_State` opens with numVirtualBlocks, numObjectsPerBlock, nextObjectNum
#: (three int32) and a compressionType int16 — 14 bytes, none of which this
#: module needs: the per-BLOCK compression flag is what actually decides how a
#: tile is stored, and the two disagree. In the Unga file the state-level flag
#: says "RLC" for levels in which most blocks are in fact stored raw.
#:
#: The `blockinfo` array follows as a MIF pointer field: a 4-byte count and a
#: 4-byte file pointer, then the records inline. The pointer is a stale
#: absolute offset from whichever process wrote the file; only the count and
#: the inline records are read.
_DMS_COUNT_OFFSET = 14
_DMS_BLOCKS_OFFSET = 22

#: `Edms_VirtualBlockInfo` — fileCode, offset, size, logValid, compressionType.
_BLOCK_STRUCT = struct.Struct("<hIihh")

_ROOT_TYPE = "root"
_LAYER_TYPES = frozenset({"Eimg_Layer", "Eimg_Layer_SubSample"})
_DMS_TYPE = "Edms_State"
_DEPENDENT_TYPE = "Eimg_DependentFile"

#: `pixelType` enum, in dictionary order. Only u8 is implemented.
_PIXEL_TYPES = ("u1", "u2", "u4", "u8", "s8", "u16", "s16", "u32", "s32", "f32", "f64", "c64", "c128")
_PIXEL_U8 = 3

_STORED = 0
_RLC = 1

#: RLC block header — dataMin, numRuns, valueOffset, then a one-byte bit width.
_RLC_HEAD_STRUCT = struct.Struct("<iii")
_RLC_HEADER_SIZE = 13
#: numRuns == -1 means "reduced precision, no runs": the block is a straight
#: run of `pixel_count` values at the declared bit width, starting right after
#: the header. Neither delivered file uses it; see `_decompress_rlc`.
_RLC_NO_RUNS = -1
_RLC_BIT_WIDTHS = frozenset({1, 2, 4, 8, 16})

#: A run count is variable-width, keyed by the top two bits of its first byte.
_RUN_LENGTH_BYTES = (1, 2, 3, 4)

_DIGIT_RUN = re.compile(r"(\d+)")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RrdLevel:
    """One pyramid level, as it would be assembled across all of its bands."""

    name: str          # e.g. "_ss_4_"
    width: int
    height: int
    band_count: int


@dataclass(frozen=True)
class RrdFile:
    """Top-level result returned by read_rrd_levels."""

    levels: list[RrdLevel]     # finest (largest) level first
    parent_name: str | None    # the .img/.tif the header names, if any


def rrd_to_tiff_bytes(path: str | os.PathLike[str]) -> bytes:
    """The finest pyramid level of *path*, encoded as a TIFF.

    Exists so an ``.rrd`` can join the ordinary raster path instead of needing
    one of its own: the caller hands these bytes to the same TIFF handling
    every other image goes through.

    Why the FINEST level rather than a mid one: in the delivery this was
    written against, neither ``.rrd``'s parent raster was present, so the
    pyramid is not a set of previews — it is the only surviving copy of the
    image. Taking anything but the largest level would discard resolution that
    exists nowhere else. (Measured: a legible 1504x2007 colour geological map,
    and a 364x371 mine plan.)

    Raises the same errors ``extract_level`` does — a file with no pixel
    blocks, or a compression this module cannot decode. Both are loud on
    purpose: a silently black image is worse than a refusal.
    """
    from PIL import Image  # noqa: PLC0415  — deferred; Pillow is heavy

    rrd = read_rrd_levels(path)
    if not rrd.levels:
        raise ValueError(f"{os.fspath(path)!r} contains no pyramid levels to extract")

    finest = max(rrd.levels, key=lambda lv: lv.width * lv.height)
    array = extract_level(path, finest.name)

    # Pillow infers mode from shape: (h, w) -> greyscale, (h, w, 3) -> RGB.
    # A band count it cannot map is an error rather than a guess, because
    # guessing here is how channels end up transposed.
    if array.ndim == 2:
        image = Image.fromarray(array, mode="L")
    elif array.ndim == 3 and array.shape[2] == 3:
        image = Image.fromarray(array, mode="RGB")
    elif array.ndim == 3 and array.shape[2] == 4:
        image = Image.fromarray(array, mode="RGBA")
    else:
        raise ValueError(
            f"level {finest.name!r} has an unsupported shape {array.shape}; "
            "expected (h, w), (h, w, 3) or (h, w, 4)",
        )

    buffer = io.BytesIO()
    # LZW rather than raw: these are large scans and the bytes go straight
    # into object storage.
    image.save(buffer, format="TIFF", compression="tiff_lzw")
    return buffer.getvalue()


@dataclass(frozen=True)
class _Node:
    """One `Ehfa_Entry`, with its parent resolved during the walk."""

    name: str
    type_name: str
    data: int
    data_size: int
    next_offset: int
    child_offset: int
    parent: "_Node | None"


@dataclass(frozen=True)
class _Block:
    """One `Edms_VirtualBlockInfo` — where a single tile's bytes live."""

    offset: int
    size: int
    log_valid: bool
    compression: int


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _natural_key(name: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key that orders "Band_10" after "Band_9" and "_ss_8_" before "_ss_128_".

    Each part is widened to a uniform 3-tuple so a digit run never has to be
    compared against a text run — plain `(int | str)` tuples raise TypeError
    the moment two names differ in shape, which is exactly when a sort of
    node names taken from an untrusted file is most likely to happen.
    """
    return tuple(
        (1, int(part), "") if part.isdigit() else (0, 0, part)
        for part in _DIGIT_RUN.split(name)
        if part
    )


def _cstring(blob: bytes, offset: int, length: int) -> str:
    """Decode a fixed-width NUL-terminated node name or type.

    latin-1 because it is total: HFA node names are ASCII in practice, and a
    stray high byte in a corrupt header should surface as a strange name in an
    error message, not as a UnicodeDecodeError from inside the tree walk.
    """
    return blob[offset:offset + length].split(b"\0")[0].decode("latin-1")


# ---------------------------------------------------------------------------
# Container walk
# ---------------------------------------------------------------------------

def _read_container(path: str | os.PathLike[str]) -> tuple[bytes, str]:
    """Read the whole file and confirm it is an HFA container.

    Read whole rather than mmap'd: an `.rrd` is a pyramid and is bounded by
    construction at roughly a third of a parent this module has already
    established is missing. The largest in the delivery is 11.8 MB.
    """
    label = os.path.basename(os.fspath(path))
    with open(path, "rb") as handle:
        blob = handle.read()

    if len(blob) < len(_MAGIC) + 4 or not blob.startswith(_MAGIC):
        raise ValueError(
            f"'{label}' is not an ERDAS HFA file: it does not start with {_MAGIC!r}. "
            f"The .rrd/.aux/.img family all share this tag, so a file without it was "
            f"never written by Imagine."
        )
    return blob, label


def _walk(blob: bytes, label: str) -> list[_Node]:
    """Return every node in the entry tree, each carrying its parent."""
    header_offset = struct.unpack_from("<I", blob, len(_MAGIC))[0]
    if header_offset + _FILE_STRUCT.size > len(blob):
        raise ValueError(f"'{label}' has an Ehfa_File pointer ({header_offset}) past the end of the file.")

    _version, _free_list, root_offset, _entry_header_length, _dictionary = _FILE_STRUCT.unpack_from(blob, header_offset)

    nodes: list[_Node] = []
    seen: set[int] = set()
    # Explicit stack, and `seen` is global rather than per-sibling-ring: a
    # corrupt file whose `next` pointers form a cycle would otherwise spin
    # here forever instead of raising.
    stack: list[tuple[int, _Node | None]] = [(root_offset, None)]

    while stack:
        offset, parent = stack.pop()
        if not offset or offset in seen:
            continue
        if offset + _ENTRY_READ_LENGTH > len(blob):
            raise ValueError(f"'{label}' has an entry pointer ({offset}) past the end of the file.")
        seen.add(offset)

        next_offset, _prev, _parent, child_offset, data, data_size = _ENTRY_STRUCT.unpack_from(blob, offset)
        node = _Node(
            name=_cstring(blob, offset + _ENTRY_NAME_OFFSET, _ENTRY_NAME_LENGTH),
            type_name=_cstring(blob, offset + _ENTRY_TYPE_OFFSET, _ENTRY_TYPE_LENGTH),
            data=data,
            data_size=data_size,
            next_offset=next_offset,
            child_offset=child_offset,
            parent=parent,
        )
        nodes.append(node)
        stack.append((node.next_offset, parent))
        stack.append((node.child_offset, node))

    return nodes


def _parent_name(blob: bytes, nodes: list[_Node]) -> str | None:
    """The raster this pyramid was built from, per `Eimg_DependentFile`.

    Stored as a MIF string: a 4-byte character count, a 4-byte pointer, then
    the characters. The count includes the terminating NUL.
    """
    node = next((n for n in nodes if n.type_name == _DEPENDENT_TYPE), None)
    if node is None or node.data_size <= 8:
        return None

    count = struct.unpack_from("<i", blob, node.data)[0]
    if count <= 1:
        return None
    start = node.data + 8
    stop = min(start + count, node.data + node.data_size, len(blob))
    text = blob[start:stop].split(b"\0")[0].decode("latin-1").strip()
    return text or None


def _layer_geometry(blob: bytes, layer: _Node, level_name: str, label: str) -> tuple[int, int, int, int, int]:
    """(width, height, pixel_type, block_width, block_height) for one band's layer."""
    if layer.data_size < _LAYER_STRUCT.size or layer.data + _LAYER_STRUCT.size > len(blob):
        raise ValueError(
            f"'{label}' level '{level_name}': node '{layer.name}' carries no readable Eimg_Layer "
            f"struct (dataSize={layer.data_size}), so the level has no geometry to assemble against."
        )

    width, height, _layer_type, pixel_type, block_width, block_height = _LAYER_STRUCT.unpack_from(blob, layer.data)
    if min(width, height, block_width, block_height) <= 0:
        raise ValueError(
            f"'{label}' level '{level_name}': non-positive geometry "
            f"({width}x{height}, blocks {block_width}x{block_height})."
        )
    return width, height, pixel_type, block_width, block_height


def _collect_levels(nodes: list[_Node], label: str) -> dict[str, list[tuple[str, _Node, _Node]]]:
    """Group every `Edms_State` by pyramid level, bands in natural name order.

    Raises when the file has none. That is not a hypothetical: ERDAS writes a
    statistics-and-histogram sidecar with the very same container format and
    the very same `Eimg_Layer` nodes, so an `.aux` looks like a readable
    pyramid right up to the point where you ask it for pixels.
    """
    states = [node for node in nodes if node.type_name == _DMS_TYPE]
    if not states:
        present = sorted({node.type_name for node in nodes if node.type_name != _ROOT_TYPE})
        raise ValueError(
            f"'{label}' contains no Edms_State node, so it holds no image pyramid — "
            f"it carries {', '.join(present) or 'nothing'}. An ERDAS .aux sidecar has this "
            f"shape: statistics, histograms and layer descriptions, but no pixels. "
            f"The pixel data, if it survives at all, is in the matching .rrd or .img."
        )

    levels: dict[str, list[tuple[str, _Node, _Node]]] = {}
    for state in states:
        layer = state.parent
        if layer is None or layer.type_name not in _LAYER_TYPES:
            raise ValueError(
                f"'{label}': Edms_State '{state.name}' hangs off "
                f"'{layer.type_name if layer else 'nothing'}' rather than an image layer."
            )
        band = layer.parent
        # In an .rrd the layer is a level under a band; in a full .img the
        # band's own layer holds the full-resolution blocks and its parent is
        # the tree root. Fall back to the layer's own name in that case so the
        # level is still identifiable rather than named "root".
        band_name = band.name if band is not None and band.type_name in _LAYER_TYPES else layer.name
        levels.setdefault(layer.name, []).append((band_name, layer, state))

    for bands in levels.values():
        bands.sort(key=lambda entry: _natural_key(entry[0]))
    return levels


# ---------------------------------------------------------------------------
# Block decoding
# ---------------------------------------------------------------------------

def _block_table(blob: bytes, state: _Node, level_name: str, label: str) -> list[_Block]:
    """Read one `Edms_State`'s array of block descriptors."""
    if state.data_size < _DMS_BLOCKS_OFFSET or state.data + _DMS_BLOCKS_OFFSET > len(blob):
        raise ValueError(f"'{label}' level '{level_name}': truncated Edms_State (dataSize={state.data_size}).")

    count = struct.unpack_from("<i", blob, state.data + _DMS_COUNT_OFFSET)[0]
    needed = _DMS_BLOCKS_OFFSET + count * _BLOCK_STRUCT.size
    if count < 0 or needed > state.data_size:
        raise ValueError(
            f"'{label}' level '{level_name}': block table declares {count} blocks, which needs "
            f"{needed} bytes of a {state.data_size}-byte Edms_State."
        )

    blocks = []
    for index in range(count):
        offset = state.data + _DMS_BLOCKS_OFFSET + index * _BLOCK_STRUCT.size
        _file_code, block_offset, size, log_valid, compression = _BLOCK_STRUCT.unpack_from(blob, offset)
        blocks.append(_Block(offset=block_offset, size=size, log_valid=bool(log_valid), compression=compression))
    return blocks


def _unpack_values(raw: bytes, offset: int, count: int, bit_width: int, level_name: str, label: str) -> np.ndarray:
    """Read `count` little-endian, LSB-first values of `bit_width` bits each."""
    needed = _ceil_div(count * bit_width, 8)
    if offset < 0 or offset + needed > len(raw):
        raise ValueError(
            f"'{label}' level '{level_name}': RLC value stream wants {needed} bytes at {offset} "
            f"in a {len(raw)}-byte block."
        )
    if bit_width == 8:
        return np.frombuffer(raw, np.uint8, count, offset).astype(np.int64)
    if bit_width == 16:
        return np.frombuffer(raw, "<u2", count, offset).astype(np.int64)

    # Sub-byte widths pack low bits first, so value i lives at bit i*width of
    # byte (i*width)//8 — the same table for 1, 2 and 4.
    per_byte = 8 // bit_width
    packed = np.frombuffer(raw, np.uint8, needed, offset)
    shifts = (np.arange(per_byte, dtype=np.uint8) * bit_width).astype(np.uint8)
    widened = (packed[:, None] >> shifts) & np.uint8((1 << bit_width) - 1)
    return widened.reshape(-1)[:count].astype(np.int64)


def _to_uint8(values: np.ndarray, level_name: str, label: str) -> np.ndarray:
    """Narrow decoded values to uint8, refusing to wrap them into range.

    A decoder that has misread `dataMin` or the bit width produces values just
    outside 0-255; masking them would turn that into a picture rather than an
    error, and a picture is what a reviewer signs off on.
    """
    if values.size and (int(values.min()) < 0 or int(values.max()) > 255):
        raise ValueError(
            f"'{label}' level '{level_name}': decoded RLC values span "
            f"{int(values.min())}..{int(values.max())}, outside the 0-255 the layer declares."
        )
    return values.astype(np.uint8)


def _decode_run_counts(raw: bytes, run_count: int, level_name: str, label: str) -> np.ndarray:
    """Decode the variable-width run-length counters that follow the RLC header.

    The top two bits of the first byte give the counter's total width in
    bytes; the remaining six are its most significant bits. Masking with 0x3f
    is safe even in the one-byte form, where those two bits are zero anyway.
    """
    counts = np.empty(run_count, dtype=np.int64)
    position = _RLC_HEADER_SIZE

    for index in range(run_count):
        if position >= len(raw):
            raise ValueError(
                f"'{label}' level '{level_name}': RLC counters end after {index} of {run_count} runs."
            )
        lead = raw[position]
        width = _RUN_LENGTH_BYTES[lead >> 6]
        if position + width > len(raw):
            raise ValueError(
                f"'{label}' level '{level_name}': a {width}-byte RLC counter runs past the block end."
            )
        value = lead & 0x3F
        for extra in range(1, width):
            value = (value << 8) | raw[position + extra]
        counts[index] = value
        position += width

    return counts


def _decompress_rlc(raw: bytes, pixel_count: int, level_name: str, label: str) -> np.ndarray:
    """Expand one ERDAS RLC block to `pixel_count` uint8 pixels."""
    if len(raw) < _RLC_HEADER_SIZE:
        raise ValueError(f"'{label}' level '{level_name}': RLC block is {len(raw)} bytes, too short for a header.")

    data_min, run_count, value_offset = _RLC_HEAD_STRUCT.unpack_from(raw, 0)
    bit_width = raw[12]
    if bit_width not in _RLC_BIT_WIDTHS:
        raise NotImplementedError(
            f"'{label}' level '{level_name}': RLC block declares a {bit_width}-bit value width, "
            f"which is not implemented. Refusing rather than reinterpreting the block."
        )

    if run_count == _RLC_NO_RUNS:
        # Reduced precision without runs. Neither delivered file uses this
        # branch, so it is guarded by the same length check as the run path
        # and will raise rather than emit a plausible image if the layout
        # assumption is wrong.
        values = _unpack_values(raw, _RLC_HEADER_SIZE, pixel_count, bit_width, level_name, label)
        return _to_uint8(values + data_min, level_name, label)

    if run_count < 0 or value_offset < _RLC_HEADER_SIZE or value_offset > len(raw):
        raise ValueError(
            f"'{label}' level '{level_name}': RLC block declares {run_count} runs with values at "
            f"{value_offset} in {len(raw)} bytes."
        )

    counts = _decode_run_counts(raw, run_count, level_name, label)
    total = int(counts.sum())
    if total != pixel_count:
        # The strongest check available on this decoder: the runs must tile
        # the block exactly. A misread counter almost never lands back on the
        # block size, so this catches drift instead of shipping a torn tile.
        raise ValueError(
            f"'{label}' level '{level_name}': RLC runs expand to {total} pixels, "
            f"but the block holds {pixel_count}."
        )

    values = _unpack_values(raw, value_offset, run_count, bit_width, level_name, label)
    return np.repeat(_to_uint8(values + data_min, level_name, label), counts)


def _decode_block(blob: bytes, block: _Block, pixel_count: int, level_name: str, label: str) -> np.ndarray:
    """Return one tile's `pixel_count` uint8 pixels, whatever its encoding."""
    if block.offset < 0 or block.size < 0 or block.offset + block.size > len(blob):
        raise ValueError(
            f"'{label}' level '{level_name}': block at {block.offset} (+{block.size}) "
            f"lies outside the {len(blob)}-byte file."
        )
    raw = blob[block.offset:block.offset + block.size]

    if block.compression == _STORED:
        if len(raw) != pixel_count:
            raise ValueError(
                f"'{label}' level '{level_name}': stored block holds {len(raw)} bytes "
                f"for a {pixel_count}-pixel tile."
            )
        return np.frombuffer(raw, dtype=np.uint8)

    if block.compression == _RLC:
        return _decompress_rlc(raw, pixel_count, level_name, label)

    raise NotImplementedError(
        f"'{label}' level '{level_name}': block encoding {block.compression} is not implemented "
        f"(only 0 = stored and 1 = RLC are). Refusing rather than returning uninterpreted bytes as pixels."
    )


def _decode_band(blob: bytes, band_name: str, layer: _Node, state: _Node, level_name: str, label: str) -> np.ndarray:
    """Assemble one band of one level into a (height, width) uint8 array."""
    width, height, pixel_type, block_width, block_height = _layer_geometry(blob, layer, level_name, label)
    if pixel_type != _PIXEL_U8:
        declared = _PIXEL_TYPES[pixel_type] if 0 <= pixel_type < len(_PIXEL_TYPES) else f"code {pixel_type}"
        raise NotImplementedError(
            f"'{label}' level '{level_name}' band '{band_name}' stores {declared} pixels; only u8 "
            f"is implemented. Refusing rather than reinterpreting the bytes as 8-bit."
        )

    blocks = _block_table(blob, state, level_name, label)
    columns = _ceil_div(width, block_width)
    rows = _ceil_div(height, block_height)
    if len(blocks) != rows * columns:
        raise ValueError(
            f"'{label}' level '{level_name}' band '{band_name}': {len(blocks)} blocks for a "
            f"{columns}x{rows} grid over {width}x{height} pixels."
        )

    # Assemble on a whole-block canvas and crop once. Edge tiles are stored
    # full-size with their overhang padded, so cropping per block would be the
    # same arithmetic done len(blocks) times.
    canvas = np.zeros((rows * block_height, columns * block_width), dtype=np.uint8)
    written = 0
    for index, block in enumerate(blocks):
        if not block.log_valid:
            continue
        pixels = _decode_block(blob, block, block_width * block_height, level_name, label)
        row, column = divmod(index, columns)
        top, left = row * block_height, column * block_width
        canvas[top:top + block_height, left:left + block_width] = pixels.reshape(block_height, block_width)
        written += 1

    if written == 0:
        raise ValueError(
            f"'{label}' level '{level_name}' band '{band_name}': all {len(blocks)} blocks are marked "
            f"unwritten, so the level would decode to a uniformly black image. Refusing to return one."
        )
    if written != len(blocks):
        logger.warning(
            "ERDAS RRD: '%s' level '%s' band '%s' — %d of %d blocks are marked unwritten and "
            "were left at zero.",
            label, level_name, band_name, len(blocks) - written, len(blocks),
        )

    return canvas[:height, :width]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_rrd_levels(path: str | os.PathLike[str]) -> RrdFile:
    """List the pyramid levels an ERDAS `.rrd` (or `.img`) holds.

    Args:
        path: Path to the .rrd file on the local filesystem.

    Returns:
        RrdFile with one RrdLevel per level, finest (largest) first, and the
        parent raster's name if the header records one. The parent is very
        often absent from the delivery that contains the .rrd — that is the
        case this module exists for — so a name here is not a promise the file
        can be found.

    Raises:
        FileNotFoundError: if there is no file at *path*.
        ValueError: if the file is not an HFA container, or contains no
            Edms_State node at all. An ERDAS .aux statistics sidecar is
            exactly that second case and is a normal thing for a caller to
            hand over by mistake.
    """
    blob, label = _read_container(path)
    nodes = _walk(blob, label)
    grouped = _collect_levels(nodes, label)

    levels = []
    for level_name, bands in grouped.items():
        _band_name, layer, _state = bands[0]
        width, height, _pixel_type, _block_width, _block_height = _layer_geometry(blob, layer, level_name, label)
        levels.append(RrdLevel(name=level_name, width=width, height=height, band_count=len(bands)))

    levels.sort(key=lambda level: (-(level.width * level.height), _natural_key(level.name)))
    parent = _parent_name(blob, nodes)

    logger.info(
        "ERDAS RRD: '%s' holds %d level(s) — %s; parent raster '%s'",
        label,
        len(levels),
        ", ".join(f"{level.name} {level.width}x{level.height}x{level.band_count}" for level in levels),
        parent or "<unnamed>",
    )
    return RrdFile(levels=levels, parent_name=parent)


def extract_level(path: str | os.PathLike[str], level_name: str) -> np.ndarray:
    """Decode one pyramid level to a numpy array.

    Args:
        path: Path to the .rrd file on the local filesystem.
        level_name: A name from `read_rrd_levels(path).levels`, e.g. "_ss_4_".

    Returns:
        A uint8 array shaped (height, width) for a single-band level, or
        (height, width, band_count) for a multi-band one — band 1 first,
        ordered by the layer names in the file rather than by the order the
        nodes appear in, which is reversed in both delivered files.

    Raises:
        FileNotFoundError: if there is no file at *path*.
        ValueError: if the file holds no pyramid, has no level by that name,
            or decodes inconsistently (bands disagreeing on size, RLC runs not
            tiling a block, every block marked unwritten).
        NotImplementedError: if the level's pixel type or block encoding is
            not implemented. This is deliberately noisy: the failure mode of
            guessing is a plausible-looking image, not a crash.
    """
    blob, label = _read_container(path)
    nodes = _walk(blob, label)
    grouped = _collect_levels(nodes, label)

    if level_name not in grouped:
        available = sorted(grouped, key=_natural_key)
        raise ValueError(
            f"'{label}' has no pyramid level named '{level_name}'. It holds: {', '.join(available)}."
        )

    planes = [
        _decode_band(blob, band_name, layer, state, level_name, label)
        for band_name, layer, state in grouped[level_name]
    ]
    shapes = {plane.shape for plane in planes}
    if len(shapes) != 1:
        raise ValueError(
            f"'{label}' level '{level_name}': bands disagree on size ({sorted(shapes)}), "
            f"so they cannot be stacked into one image."
        )

    logger.info(
        "ERDAS RRD: '%s' level '%s' decoded to %dx%d over %d band(s)",
        label, level_name, planes[0].shape[1], planes[0].shape[0], len(planes),
    )
    return planes[0] if len(planes) == 1 else np.stack(planes, axis=-1)
