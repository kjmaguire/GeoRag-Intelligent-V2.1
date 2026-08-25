"""UBC-GIF DCIP2D inversion output parser.

DCIP2D is the UBC Geophysical Inversion Facility's 2-D DC-resistivity and
induced-polarisation inversion code. A finished run leaves a directory of
plain-ASCII files that fall into three kinds, all of which this module reads:

  * **observed data** (``.rdt`` and its ``.rdtm*`` splits) — a two-line header
    followed by one row per measurement;
  * **2-D models** (``dcinv2d.NNN``, ``ipinv2d.NNN``, ``ipinv2d.chg``) — a
    ``nx nz`` header followed by ``nx * nz`` cell values;
  * **the run manifest** (``IP.inp``) — one ``value ! comment`` line per
    control parameter.

## The trap: "XYZ" in the filename is not x/y/z

The observed-data files in a Geosoft-driven workflow are named things like
``CEN_L3750_Mx_XYZ.rdtmd``. That ``XYZ`` is Geosoft Oasis montaj's word for
"channel export" — it is the name of the *export format*, not a description of
the columns. The five columns are::

    C1   C2   P1   P2   value

which is four ELECTRODE POSITIONS along a single 1-D chainage, plus one
measurement. There is no Y column and no Z column anywhere in the file. The
whole survey is one straight line; its real-world position lives in the
survey's grid definition, not here.

Reading these as easting/northing/elevation is the failure this module exists
to prevent: five numeric columns parse cleanly, nothing raises, and every
reading lands at a coordinate that has nothing to do with where the crew
stood. On the L3750N export, column 1 ranges 4,596–5,494 and column 2 is
IDENTICAL to it in all 96 rows — a coordinate importer would have produced a
diagonal line of points through the origin quadrant of whatever CRS it was
handed, and it would have looked like data.

``C1 == C2`` in every row is not a quirk either. It is what "Pole-Dipole"
(line 2 of the header) means: the second current electrode is at infinity, so
the writer repeats C1 rather than leaving a column blank.

## Chargeability and conductivity share one mesh

``read_dcip2d_model`` returns the air mask alongside the values because the
two model families flag air differently but agree on *which* cells are air —
see ``_air_mask``. On the L3750N export both families mark the same 98 of
1,100 cells, which is the evidence that a dcinv2d resistivity section and an
ipinv2d chargeability section can be overlaid cell-for-cell without
resampling.

## Stubs are a legitimate outcome

A DCIP2D export splits observed data across several files by survey geometry
(``C1 < P1`` vs ``C1 > P1``, current line on or off the dipole line). A
geometry the survey never produced still gets a file — header written, zero
rows. Three of the four observed-data files in the L3750N export are exactly
that. ``read_dcip2d_data`` returns an empty ``records`` list for them and does
NOT raise: an empty split is information about the survey, not a parse error,
and treating it as one would fail a delivery that is entirely intact.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

PARSER_NAME = "dcip2d_parser"
PARSER_VERSION = "1.0.0"

#: The explicit no-data flag UBC writes for cells above topography in the
#: chargeability models. Held as a named constant because it is a magic
#: number that MUST NOT reach a colour scale or a mean.
AIR_FLAG = -1.0e30

#: Anything at or below this is the flag above. A tolerance rather than an
#: equality test: the flag is written as text ("-1.00000E+30") and re-parsed,
#: and no real chargeability or conductivity is within 30 orders of magnitude
#: of it, so a loose comparison costs nothing and survives a reformat.
_AIR_FLAG_CUTOFF = -1.0e29

#: UBC's "parameter not supplied, use the default" token in a .inp manifest.
#: Kept verbatim in the returned dict — six of IP.inp's ten lines are this,
#: and mapping it to None would erase the difference between "defaulted" and
#: "line missing".
INP_UNSET = "NULL"

#: Columns in one observed-data row. Named here so the count check below reads
#: as a contract rather than a bare 5, and so the docstring trap above has
#: something to point at.
DATA_COLUMNS = ("C1", "C2", "P1", "P2", "value")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Dcip2dData:
    """One observed-data file: its two header lines and its measurements.

    ``records`` may legitimately be empty — see the module docstring on stubs.
    """

    title: str          # line 1, free text written by the survey contractor
    array_type: str     # line 2, e.g. "Pole-Dipole"
    records: list[tuple[float, float, float, float, float]]  # C1,C2,P1,P2,value


@dataclass(frozen=True, eq=False)
class Dcip2dModel:
    """One 2-D model: the mesh shape, the cell values, and the air mask.

    ``values`` holds the file's numbers verbatim, air cells included, so that
    nothing is dropped on the way in. Every consumer computing a statistic or
    a colour range must index with ``values[~air_mask]`` first; the air
    padding is ``-1e30`` in the ip models and a near-zero conductivity in the
    dc models, and either one wrecks a range silently.

    ``eq=False`` is deliberate. The dataclass-generated ``__eq__`` would
    compare two numpy arrays with ``==``, which yields an array and then
    raises "truth value of an array is ambiguous" the moment anyone writes
    ``model_a == model_b``; the generated ``__hash__`` would raise TypeError
    on the same arrays. Identity comparison is the honest behaviour here.
    """

    nx: int                  # cells along the survey line
    nz: int                  # cells down
    values: np.ndarray       # shape (nz, nx), SURFACE ROW FIRST
    air_mask: np.ndarray     # bool, same shape; True where the cell is padding


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _read_lines(path: str | Path) -> list[str]:
    """Read *path* as text and split it into lines.

    All twelve files in the reference export are byte-scanned pure ASCII with
    CRLF endings, which is what a Fortran formatted write on Windows produces.
    Decoding is still attempted as UTF-8 first with a cp1252 fallback, because
    line 1 of an observed-data file is free text a contractor typed, and
    contractor text in this ingestion path arrives as cp1252 far more often
    than as UTF-8. The fallback uses ``errors="replace"`` so that a mangled
    title degrades to a title with a replacement character in it rather than
    taking the whole file's numbers down with it.
    """
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("DCIP2D parser: '%s' is not UTF-8 — decoding as cp1252", path)
        text = raw.decode("cp1252", errors="replace")
    return text.splitlines()


def _air_mask(values: np.ndarray) -> np.ndarray:
    """Flag the cells that are air above topography rather than measurements.

    The two model families flag air differently, and a reader that knows only
    one of them silently corrupts the other:

      * **ipinv2d** writes the explicit UBC no-data flag ``-1.0E+30``.
      * **dcinv2d** writes no flag at all. Its air cells hold the lower
        conductivity bound the inversion pins them to — measured as
        2.51485E-11 S/m at iteration 011, 2.37633E-11 at 016 and 2.27286E-11
        at 030 of the L3750N run. The number MOVES between iterations of the
        SAME inversion, so a hard-coded sentinel would match one file of a
        family and quietly stop matching the next.

    What is stable for dcinv2d is that the padding value is the array minimum
    and that it repeats exactly. A smooth inversion result does not produce
    two bit-identical floats by accident, let alone ninety-eight of them.

    The agreement between the two rules is the check that both are right.
    Measured on the L3750N export, they select the SAME 98 of 1,100 cells in
    all seven models, and every selected cell sits in an unbroken run from the
    top of its column — the mask is a topographic surface, not scattered
    nulls. Two independently-derived masks landing on one topography is also
    the evidence that the dc and ip inversions ran on a shared mesh and are
    co-registered.

    A model whose minimum happens to be unique gets an all-False mask. One
    spurious "air" cell punched into the middle of a section is worse than no
    mask at all, and the caller can still see the raw values.
    """
    flagged = values <= _AIR_FLAG_CUTOFF
    if flagged.any():
        return flagged

    lowest = values.min()
    pinned = values == lowest
    if pinned.sum() > 1:
        return pinned

    return np.zeros(values.shape, dtype=bool)


# ---------------------------------------------------------------------------
# Observed data  (.rdt / .rdtmd / .rdtmm / .rdtmp)
# ---------------------------------------------------------------------------

def read_dcip2d_data(path: str | Path) -> Dcip2dData:
    """Parse a DCIP2D observed-data file.

    Args:
        path: Path to the ``.rdt``-family file.

    Returns:
        Dcip2dData. ``records`` holds one ``(C1, C2, P1, P2, value)`` tuple per
        data row — four electrode chainages and one measurement, NOT x/y/z.
        See the module docstring. An empty list means the file is a
        geometry-split stub, which is a normal export, not a failure.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ValueError: if the two header lines are missing, or if a data row does
            not carry exactly five numeric fields. Both are corruption, not a
            variant, and a partial row cannot be repaired by guessing which
            electrode it lost.
    """
    lines = _read_lines(path)
    if len(lines) < 2:
        raise ValueError(
            f"DCIP2D data file '{path}' has {len(lines)} line(s); "
            "the format requires a title line and an array-type line."
        )

    title = lines[0].strip()
    array_type = lines[1].strip()

    records: list[tuple[float, float, float, float, float]] = []
    for offset, line in enumerate(lines[2:], start=3):
        if not line.strip():
            continue

        # The rows are written fixed-width, but splitting on whitespace is
        # what actually survives them: the value field is right-padded with
        # spaces to fill its column ("0.174    "), and every field is
        # non-empty in the reference export, so a column-slice buys nothing
        # and breaks the moment a chainage needs one more digit.
        fields = line.split()
        if len(fields) != len(DATA_COLUMNS):
            raise ValueError(
                f"DCIP2D data file '{path}' line {offset}: expected "
                f"{len(DATA_COLUMNS)} fields {DATA_COLUMNS}, got {len(fields)}: {line!r}"
            )

        try:
            c1, c2, p1, p2, value = (float(field) for field in fields)
        except ValueError as exc:
            raise ValueError(
                f"DCIP2D data file '{path}' line {offset}: non-numeric field in {line!r}"
            ) from exc

        records.append((c1, c2, p1, p2, value))

    if not records:
        logger.info(
            "DCIP2D parser: '%s' is a header-only stub (%s / %s) — "
            "this geometry produced no readings",
            path, title, array_type,
        )
    else:
        logger.info(
            "DCIP2D parser: '%s' — %d readings, array '%s'",
            path, len(records), array_type,
        )

    return Dcip2dData(title=title, array_type=array_type, records=records)


# ---------------------------------------------------------------------------
# 2-D models  (dcinv2d.NNN / ipinv2d.NNN / ipinv2d.chg)
# ---------------------------------------------------------------------------

def read_dcip2d_model(path: str | Path) -> Dcip2dModel:
    """Parse a DCIP2D 2-D model file into a (nz, nx) array plus its air mask.

    The file is a ``nx nz`` header followed by ``nx * nz`` whitespace-separated
    values (five to a line, though nothing depends on that — the parser reads
    the whole tail as one token stream).

    STORAGE ORDER IS ROW-MAJOR WITH THE SURFACE ROW FIRST: ``nz`` rows of
    ``nx`` columns. That is measured, not assumed. Reshaped row-major, all 98
    air cells of the L3750N models fall into unbroken runs from the top of
    their columns — a topographic air layer, 0 to 3 cells thick, thickest
    where the ground is highest. Reshaped column-major the same 98 cells
    scatter through all 20 depths, including isolated "air" pockets under
    rock, which no topography can produce.

    Getting this backwards raises nothing. It transposes the section and
    returns a plausible-looking picture of nothing, which is why the order is
    documented here and asserted in the tests rather than left to a comment.

    Args:
        path: Path to the model file.

    Returns:
        Dcip2dModel with ``values`` verbatim (air padding included) and
        ``air_mask`` marking the padding. Units are whatever the producing
        code writes: S/m for dcinv2d (invert for ohm-m), mV/V for ipinv2d.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ValueError: if the header is missing or non-integer, or if the value
            count does not equal ``nx * nz``. A short file must not be padded
            and a long one must not be truncated — either would shift every
            cell after the discrepancy into the wrong place on the section.
    """
    tokens = " ".join(_read_lines(path)).split()
    if len(tokens) < 2:
        raise ValueError(f"DCIP2D model file '{path}' is empty or has no 'nx nz' header.")

    try:
        nx, nz = int(tokens[0]), int(tokens[1])
    except ValueError as exc:
        raise ValueError(
            f"DCIP2D model file '{path}': header is '{tokens[0]} {tokens[1]}', "
            "expected two integers 'nx nz'."
        ) from exc

    if nx <= 0 or nz <= 0:
        raise ValueError(f"DCIP2D model file '{path}': non-positive mesh shape nx={nx}, nz={nz}.")

    body = tokens[2:]
    expected = nx * nz
    if len(body) != expected:
        raise ValueError(
            f"DCIP2D model file '{path}': header declares nx={nx} nz={nz} "
            f"({expected} cells) but the file holds {len(body)} values."
        )

    try:
        flat = np.array([float(token) for token in body], dtype=float)
    except ValueError as exc:
        raise ValueError(f"DCIP2D model file '{path}': non-numeric cell value.") from exc

    values = flat.reshape(nz, nx)
    air_mask = _air_mask(values)

    logger.info(
        "DCIP2D parser: '%s' — %dx%d mesh, %d air cells, earth range %.6g..%.6g",
        path, nz, nx, int(air_mask.sum()),
        values[~air_mask].min() if (~air_mask).any() else float("nan"),
        values[~air_mask].max() if (~air_mask).any() else float("nan"),
    )

    return Dcip2dModel(nx=nx, nz=nz, values=values, air_mask=air_mask)


# ---------------------------------------------------------------------------
# Run manifest  (IP.inp / *.inp)
# ---------------------------------------------------------------------------

def read_inp(path: str | Path) -> dict[str, str]:
    """Parse a DCIP2D control file into ``{comment_label: value}``.

    Each line is ``value ! comment``. The value may itself contain spaces —
    line 1 of IP.inp is ``0 15`` for the pair ``niter, irest`` — so the split
    is on the first ``!`` only, never on whitespace.

    The ORDER is the real contract: UBC's reader consumes these lines
    positionally and the ``!`` text is a human annotation a different vintage
    of the code is free to spell differently. This returns a dict for
    convenience, and dicts preserve insertion order, so a caller who needs the
    positional reading can take ``list(result.values())`` and still be correct.

    A line with no ``!`` is keyed ``line_N`` on its 1-based line number, so it
    is still returned rather than dropped. A REPEATED label raises instead of
    overwriting: a control file with two "obs file" lines is not a control
    file we understand, and silently returning nine entries where the operator
    wrote ten is the kind of quiet loss this pipeline is not allowed to have.

    Values are returned verbatim, ``NULL`` included (see ``INP_UNSET``), and
    Windows paths keep their backslashes — the manifest records where the run
    was performed, which is provenance, not a path this process should follow.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ValueError: on a duplicated comment label.
    """
    manifest: dict[str, str] = {}

    for lineno, line in enumerate(_read_lines(path), start=1):
        if not line.strip():
            continue

        value, sep, comment = line.partition("!")
        key = comment.strip() if sep else ""
        if not key:
            key = f"line_{lineno}"

        if key in manifest:
            raise ValueError(
                f"DCIP2D control file '{path}' line {lineno}: duplicate label "
                f"{key!r} (already set to {manifest[key]!r}); refusing to overwrite it."
            )

        manifest[key] = value.strip()

    logger.info("DCIP2D parser: '%s' — %d control entries", path, len(manifest))
    return manifest
