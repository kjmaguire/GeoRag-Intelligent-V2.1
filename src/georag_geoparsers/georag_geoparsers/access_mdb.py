"""Read tables out of a Microsoft Access ``.mdb`` database via mdbtools.

## Why this exists

Geosoft's IP/resistivity acquisition software ships its survey database as a
JET3 Access file, and that is how it arrives in a delivery. Measured against
the RedStar corpus on 2026-08-25,
``Centennial/Geophysics/IP/June 19/L3750N/IPDB/CEN_L3750_IP.mdb`` (577 KB,
``mdb-ver`` reports ``JET3``) holds 19 tables and 654 rows. There is no pure
Python reader for JET3 in the image and none worth adding: the format is a
1990s B-tree with per-page compression, and the one maintained C
implementation is mdbtools.

GDAL is already present and its ODBC/PGeo drivers are compiled in, so reaching
for those first is the obvious move. It does not pay off — PGeo needs unixODBC
plus a system Access ODBC driver, and on Linux the only such driver is the one
mdbtools itself provides. That route installs mdbtools anyway and adds a
driver manager and a DSN on top, so this module talks to the CLI directly.

## What "shells out" costs, measured

``mdbtools`` is an apt package, not a Python one, so nothing here touches
``pyproject.toml`` and no dependency gate applies. Its footprint was measured
on 2026-08-25 rather than taken from the package description, and the answer
depends entirely on which baseline you measure against:

  ===================  =========  ==================================
  package              installed  in bare python:3.13-slim?
  ===================  =========  ==================================
  mdbtools                268 KB   no
  libmdb3t64              156 KB   no
  libmdbsql3t64            71 KB   no
  libglib2.0-0t64        4448 KB   no
  libatomic1               45 KB   no (pulled in by libglib)
  libreadline8t64         485 KB   yes, preinstalled
  ===================  =========  ==================================

Added to a *bare* ``python:3.13-slim`` the growth measured over ``/usr`` and
``/lib`` is **~4.5 MB**, essentially all of it libglib2.0.

That is not our baseline. The FastAPI runtime stage already installs
``libglib2.0-0`` (it is in the same apt block as gdal-bin and poppler-utils,
for pango/cairo), and ``libreadline8t64`` ships in the base image. Measured
against an image that already has libglib, adding mdbtools costs
**460 KB** -- only the three mdb-* packages. That is the number that applies
to ``docker/fastapi.Dockerfile``.

Both figures are recorded because the difference is a factor of ten and the
cheap one is only true while that libglib line stays in the runtime stage. If
it is ever removed, this reader silently becomes a 4.5 MB dependency.

## The output contract, and the trap in it

``mdb-json`` emits **one JSON object per line** -- it is JSON Lines, not a
JSON array, so ``json.loads`` on the whole stream fails. That much is
documented. What is not documented is the part that changes the shape of the
data:

**``mdb-json`` omits a key entirely when the column is NULL.** Rows from one
table therefore do not all carry the same keys. This is not hypothetical and
not rare enough to ignore -- in ``CEN_L3750_IP.mdb`` the ``ErrorTable`` has 29
rows, of which 28 carry four keys and exactly one also carries
``InverseField``. A caller that reads ``row["InverseField"]`` positionally, or
builds a dataframe from the first row's keys, loses that row's data or crashes
on the other 28.

This module does **not** paper over that by back-filling ``None``. The set of
columns is not recoverable from ``mdb-json`` output alone -- a column that is
NULL in every row never appears at all -- so a back-fill would be a guess
presented as a schema. Callers get the rows as mdbtools reported them and are
expected to use ``.get()``. ``list_tables`` plus a caller-side union of keys is
the honest way to get a column list.

A second, smaller surprise from the same file: mdbtools writes some empty text
fields as the four-character string ``"null"``, not as JSON ``null``. Those
arrive as the Python ``str`` ``"null"``. Coercing them to ``None`` here would
be inventing a semantic the file does not state -- ``"null"`` is also a legal
value for a text column -- so they are passed through unchanged.

## Empty is not an error

Ten of the 19 tables in the reference file are empty, including ``RawData``.
``mdb-json`` on an empty table exits **0** with zero bytes of output, which is
indistinguishable from success on a table that has rows, and must be: the IP
survey's readings live in ``StationMulti`` (120 rows) and ``DipoleSpacing``
(426 rows), while ``RawData`` is genuinely unused. Treating empty output as a
failure would reject a valid database. Only a non-zero exit is an error.

## Why the table name is passed as its own argv element

A table name is data read out of an attacker-controlled file, so it is never
interpolated into a command string -- every call here builds a list argv and
runs with ``shell=False``. Argument *position* is not enough on its own,
because mdbtools parses options with glib and a name beginning with ``-`` is
taken as a flag. Measured both ways on the real binary:

  * ``mdb-json FILE -Projects``     -> exit 1, "option parsing failed"
  * ``mdb-json -- FILE -Projects``  -> exit 1, "Wrong number of arguments"

Both fail loudly, so there is no silent-wrong-data hazard in either form --
but note that the ``--`` separator does **not** rescue such a name either,
because glib consumes it regardless. mdbtools simply cannot address a table
whose name starts with ``-``. This module still passes ``--`` (it is what
stops a name like ``-U`` from silently flipping ``--no-unprintable`` and
altering the values), and rejects a leading-dash name up front with a message
that says what is actually wrong, instead of surfacing "Wrong number of
arguments" from two layers down.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Final

logger = logging.getLogger(__name__)

READER_NAME: Final[str] = "access_mdb"
READER_VERSION: Final[str] = "1.0.0"

#: The apt package that provides every binary this module calls. Named in the
#: not-installed error so the message is actionable rather than diagnostic.
APT_PACKAGE: Final[str] = "mdbtools"

_TABLES_TOOL: Final[str] = "mdb-tables"
_JSON_TOOL: Final[str] = "mdb-json"
_VERSION_TOOL: Final[str] = "mdb-ver"

#: Generous, but bounded. A hung child would otherwise stall an ingestion
#: worker forever; 577 KB of JET3 exports in well under a second.
DEFAULT_TIMEOUT_S: Final[float] = 300.0

#: How much of a tool's stderr to quote back in an exception message.
_STDERR_QUOTE_LIMIT: Final[int] = 500


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AccessMdbError(Exception):
    """Base class for every failure raised by this module."""


class MdbToolsNotInstalledError(AccessMdbError):
    """mdbtools is not on PATH.

    Separate from MdbReadError because the remedy is completely different:
    nothing about the .mdb file is wrong, the host is missing an OS package.
    A caller that supports several readers can catch this to fall through to
    another one, which it could not do if this were indistinguishable from a
    corrupt database.
    """


class MdbReadError(AccessMdbError):
    """mdbtools ran but could not deliver the requested table.

    Covers a corrupt or non-Access file, a table that does not exist, a
    timeout, and output that is not the JSON Lines the contract promises.
    """


class UnaddressableTableError(MdbReadError):
    """The table exists in the file but mdbtools cannot be asked for it.

    Raised for a name beginning with ``-``, which glib's option parser claims
    before mdbtools sees it as a positional argument. See the module docstring
    for the measured behaviour with and without a ``--`` separator.
    """


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------


def _resolve_tool(tool: str) -> str:
    """Locate *tool* on PATH, or raise a message that says how to install it.

    Prevents the failure mode where a missing OS package surfaces as a bare
    ``FileNotFoundError: [Errno 2] No such file or directory: 'mdb-json'``,
    which reads like the .mdb file is missing and sends the reader looking in
    the wrong place entirely.
    """
    resolved = shutil.which(tool)
    if resolved is None:
        raise MdbToolsNotInstalledError(
            f"{tool!r} was not found on PATH, so Access .mdb files cannot be read. "
            f"This is an OS package, not a Python one -- pip install will not help. "
            f"Install it with `apt-get install -y --no-install-recommends {APT_PACKAGE}` "
            f"on Debian/Ubuntu (460 KB on an image that already has libglib2.0), or "
            f"`brew install {APT_PACKAGE}` on macOS. The package provides "
            f"{_TABLES_TOOL}, {_JSON_TOOL} and {_VERSION_TOOL}."
        )
    return resolved


def _decode(raw: bytes, *, tool: str, stream: str) -> str:
    """Decode tool output as UTF-8, degrading loudly rather than crashing.

    mdb-json escapes unprintable bytes as ``\\u00XX`` so its own output is
    ASCII, but a table NAME comes from the file's catalogue and can carry any
    bytes at all. Losing a whole export to a UnicodeDecodeError on one odd
    table name is worse than substituting a replacement character, so long as
    the substitution is not silent.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        logger.warning(
            "access_mdb: %s %s is not valid UTF-8 (%s); decoding with replacement "
            "characters -- table names or values containing the bad bytes will be "
            "mangled",
            tool,
            stream,
            exc,
        )
        return raw.decode("utf-8", errors="replace")


def _run(tool: str, args: list[str], *, timeout_s: float) -> str:
    """Run *tool* with *args* and return its stdout.

    argv is always a list and ``shell=False`` is explicit, so neither a
    filename nor a table name can reach a shell for interpretation. Both are
    attacker-controlled: the path comes from an upload, the table name from
    inside the uploaded file.

    Raises:
        MdbToolsNotInstalledError: tool absent from PATH, or not executable.
        MdbReadError: tool exited non-zero, or exceeded *timeout_s*.
    """
    executable = _resolve_tool(tool)
    argv = [executable, *args]
    logger.debug("access_mdb: exec %r (timeout %.0fs)", argv, timeout_s)

    try:
        completed = subprocess.run(  # noqa: S603 - list argv, shell=False, no shell involved
            argv,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MdbReadError(
            f"{tool} did not finish within {timeout_s:.0f}s for argv {args!r}. "
            f"The file may be far larger than expected or the child may be wedged; "
            f"raise timeout_s if the database is genuinely large."
        ) from exc
    except OSError as exc:
        # which() found it a moment ago, so this is a race, a permissions
        # problem, or a broken interpreter line -- all "the tool is not usable
        # here", which is what MdbToolsNotInstalledError means.
        raise MdbToolsNotInstalledError(
            f"{tool} was found at {executable!r} but could not be executed: {exc}"
        ) from exc

    if completed.returncode != 0:
        stderr = _decode(completed.stderr, tool=tool, stream="stderr").strip()
        # Measured 2026-08-25: mdb-json exits 1 with a COMPLETELY EMPTY stderr
        # when handed a file that is not an Access database. Reporting only the
        # captured stderr would produce a blank error message, so say so.
        detail = (
            stderr[:_STDERR_QUOTE_LIMIT]
            if stderr
            else "(the tool wrote nothing to stderr -- it does this when the file "
            "is not an Access database at all)"
        )
        raise MdbReadError(
            f"{tool} failed with exit code {completed.returncode} for argv {args!r}: "
            f"{detail}"
        )

    return _decode(completed.stdout, tool=tool, stream="stdout")


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def mdbtools_version() -> str | None:
    """Return the mdbtools version string, or None if it is unavailable.

    A probe, not an assertion -- callers use it to decide whether to offer
    .mdb support at all, so "not installed" is an ANSWER here rather than an
    error. ``mdb-ver --version`` prints e.g. ``mdbtools v1.0.1``.
    """
    try:
        raw = _run(_VERSION_TOOL, ["--version"], timeout_s=30.0).strip()
    except AccessMdbError as exc:
        # Control-flow probe: absence is the expected negative result, so this
        # is not an error to report upward. Logged at debug WITH the reason so
        # a "why is .mdb support missing?" investigation has something to read.
        logger.debug("access_mdb: mdbtools probe failed: %s", exc)
        return None
    return raw or None


def require_mdbtools() -> str:
    """Assert mdbtools is usable, returning its version string.

    Use at the top of an ingestion run to fail with an actionable message
    immediately, rather than part-way through a batch on the first .mdb.
    """
    version = mdbtools_version()
    if version is None:
        # Re-run the resolution so the raised message is the full install
        # instruction rather than a summary of it.
        _resolve_tool(_VERSION_TOOL)
        raise MdbToolsNotInstalledError(
            f"{_VERSION_TOOL} is on PATH but did not report a version; the "
            f"{APT_PACKAGE} installation looks broken."
        )
    return version


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _validated_path(path: str | os.PathLike[str]) -> str:
    """Turn *path* into a str and confirm it is an existing file.

    mdbtools reports a missing file as "File not found / Couldn't open
    database." on stderr with exit 1, which is indistinguishable from a
    corrupt file. Checking first lets a missing input raise FileNotFoundError,
    matching every other parser in this package.
    """
    db = os.fspath(path)
    if not os.path.isfile(db):
        raise FileNotFoundError(f"Access database not found: {db!r}")
    return db


def _reject_unaddressable(name: str) -> None:
    """Refuse a table name mdbtools has no way to receive.

    See the module docstring: glib claims a leading-dash argument as an option
    whether or not a ``--`` separator precedes it, so such a table is
    unreachable. Both failure modes exit non-zero, so nothing silently returns
    wrong rows -- this only replaces an opaque message with a true one.
    """
    if not name:
        raise UnaddressableTableError(
            "Table name is empty. Pass a name from list_tables()."
        )
    if name.startswith("-"):
        raise UnaddressableTableError(
            f"Table name {name!r} begins with '-', which mdbtools cannot accept: "
            f"its glib option parser claims the argument as a flag even after a "
            f"'--' separator. The table is unreadable through the mdbtools CLI."
        )


def _parse_json_lines(
    stdout: str, *, table: str, source: str
) -> list[dict[str, Any]]:
    """Parse mdb-json's JSON Lines output into one dict per row.

    Blank lines are skipped (trailing newline produces one). A line that is
    not a JSON object is an error, never a skipped row: silently dropping it
    would under-report the table's contents with no way for the caller to
    notice.
    """
    rows: list[dict[str, Any]] = []

    for lineno, line in enumerate(stdout.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise MdbReadError(
                f"{_JSON_TOOL} produced a line that is not valid JSON while reading "
                f"table {table!r} from {source!r} (line {lineno}): {exc}. "
                f"Line began: {stripped[:200]!r}"
            ) from exc

        if not isinstance(parsed, dict):
            raise MdbReadError(
                f"{_JSON_TOOL} produced a {type(parsed).__name__} rather than an "
                f"object on line {lineno} of table {table!r} in {source!r}. "
                f"Each line is expected to be one row."
            )

        rows.append(parsed)

    return rows


def list_tables(
    path: str | os.PathLike[str], *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> list[str]:
    """List the user tables in the Access database at *path*.

    ``mdb-tables -1`` prints one name per line. System tables (``MSys*``) are
    excluded by mdbtools itself and are not requested here.

    Args:
        path: Path to the ``.mdb`` file.
        timeout_s: Wall-clock limit for the child process.

    Returns:
        Table names in the order mdbtools reports them, which is catalogue
        order rather than alphabetical.

    Raises:
        FileNotFoundError: *path* is not an existing file.
        MdbToolsNotInstalledError: mdbtools is not installed.
        MdbReadError: the file is not a readable Access database.
    """
    db = _validated_path(path)

    # '-1' (one per line) must precede '--'; the separator ends option parsing.
    stdout = _run(_TABLES_TOOL, ["-1", "--", db], timeout_s=timeout_s)

    tables = [line.strip() for line in stdout.splitlines() if line.strip()]
    logger.info("access_mdb: %r exposes %d table(s)", db, len(tables))
    return tables


def read_table(
    path: str | os.PathLike[str],
    name: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Read every row of table *name* from the database at *path*.

    Args:
        path: Path to the ``.mdb`` file.
        name: Table name, normally one returned by :func:`list_tables`.
        timeout_s: Wall-clock limit for the child process.

    Returns:
        One dict per row. **Keys vary between rows**: mdb-json omits a key
        whose column is NULL in that row, and this function deliberately does
        not back-fill them -- see the module docstring for why that cannot be
        done correctly. Use ``.get()``. An empty list means the table is
        genuinely empty, which is common and is not an error.

    Raises:
        FileNotFoundError: *path* is not an existing file.
        UnaddressableTableError: *name* is empty or begins with '-'.
        MdbToolsNotInstalledError: mdbtools is not installed.
        MdbReadError: no such table, unreadable file, or malformed output.
    """
    db = _validated_path(path)
    _reject_unaddressable(name)

    stdout = _run(_JSON_TOOL, ["--", db, name], timeout_s=timeout_s)
    rows = _parse_json_lines(stdout, table=name, source=db)

    logger.debug("access_mdb: table %r in %r yielded %d row(s)", name, db, len(rows))
    return rows


def export_all(
    path: str | os.PathLike[str], *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> dict[str, list[dict[str, Any]]]:
    """Read every table in the database at *path*.

    Strict by design: if any single table fails to read, the whole call
    raises. A partial dict returned as if it were complete is the failure
    mode this package keeps finding in its own history -- the caller cannot
    tell a genuinely empty table from a skipped one, because both look like
    an empty list. A caller that wants best-effort behaviour should loop over
    :func:`list_tables` itself and decide, per table, what a failure means.

    Args:
        path: Path to the ``.mdb`` file.
        timeout_s: Wall-clock limit applied to EACH child process, not to the
            export as a whole.

    Returns:
        Mapping of table name to its rows, in the order :func:`list_tables`
        reports. Empty tables map to empty lists and are included.

    Raises:
        FileNotFoundError: *path* is not an existing file.
        MdbToolsNotInstalledError: mdbtools is not installed.
        MdbReadError: any table could not be read.
    """
    db = _validated_path(path)
    tables = list_tables(db, timeout_s=timeout_s)

    exported: dict[str, list[dict[str, Any]]] = {}
    for table in tables:
        exported[table] = read_table(db, table, timeout_s=timeout_s)

    total_rows = sum(len(rows) for rows in exported.values())
    empty = [name for name, rows in exported.items() if not rows]
    logger.info(
        "access_mdb: exported %d table(s) / %d row(s) from %r (%d empty: %s)",
        len(exported),
        total_rows,
        db,
        len(empty),
        ", ".join(empty) if empty else "none",
    )
    return exported
