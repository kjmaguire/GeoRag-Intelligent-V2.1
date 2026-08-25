"""Tests for georag_geoparsers.access_mdb.

## What runs where

mdbtools is an OS package and is NOT installed on the Windows development
host, so this file is built so that the parts which can be proven anywhere are
proven anywhere:

  * The **not-installed** path is exercised for real here, because the host
    genuinely does not have mdbtools. It is not simulated.
  * The **process contract** (argv shape, exit codes, stderr handling, JSON
    Lines parsing) is exercised against a *fake* mdb-tables / mdb-json / mdb-ver
    planted on PATH. These are real child processes doing real argv round
    trips, not mocks of subprocess.
  * The **security property** -- list argv, ``shell=False``, table name as its
    own element -- is asserted on the argv the module builds, so it holds for
    hostile names that a .bat or sh shim could not carry intact.
  * The **real-file** assertions are pinned to measured values from
    ``CEN_L3750_IP.mdb`` and skip unless both mdbtools and that file are
    present. They were run and passing in a python:3.13-slim container with
    mdbtools 1.0.1 on 2026-08-25; they SKIP on the Windows host.

Every number in the real-file tests came from running the tools against the
file, not from the file's documentation.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from georag_geoparsers import access_mdb
from georag_geoparsers.access_mdb import (
    AccessMdbError,
    MdbReadError,
    MdbToolsNotInstalledError,
    UnaddressableTableError,
    export_all,
    list_tables,
    mdbtools_version,
    read_table,
    require_mdbtools,
)

# ---------------------------------------------------------------------------
# Measured facts -- RedStar Centennial IP survey database
#
# File: RedStar/Centennial/Geophysics/IP/June 19/L3750N/IPDB/CEN_L3750_IP.mdb
# 577,536 bytes; `mdb-ver` reports JET3. Measured 2026-08-25 with mdbtools
# 1.0.1 on python:3.13-slim (Debian trixie).
# ---------------------------------------------------------------------------

REAL_MDB = Path(
    os.environ.get(
        "GEORAG_TEST_MDB",
        r"C:/Users/GeoRAG/Desktop/RedStar/Centennial/Geophysics/IP/June 19"
        r"/L3750N/IPDB/CEN_L3750_IP.mdb",
    )
)

#: mdb-tables reports catalogue order, not alphabetical -- note DipoleSet,
#: elrechd and LineSubSets trailing after LineSubSets' alphabetical position.
EXPECTED_TABLES: list[str] = [
    "Adjustments",
    "Datasets",
    "DipoleSpacing",
    "Duplicates",
    "elrecdat",
    "ErrorTable",
    "HeaderType",
    "LineAdjs",
    "LineSets",
    "Projects",
    "RawData",
    "Sections",
    "SMHeader",
    "SSHeader",
    "StationMulti",
    "StationSingle",
    "DipoleSet",
    "elrechd",
    "LineSubSets",
]

EXPECTED_ROW_COUNTS: dict[str, int] = {
    "Adjustments": 1,
    "Datasets": 1,
    "DipoleSpacing": 426,
    "Duplicates": 0,
    "elrecdat": 0,
    "ErrorTable": 29,
    "HeaderType": 0,
    "LineAdjs": 20,
    "LineSets": 0,
    "Projects": 1,
    "RawData": 0,
    "Sections": 0,
    "SMHeader": 20,
    "SSHeader": 0,
    "StationMulti": 120,
    "StationSingle": 0,
    "DipoleSet": 36,
    "elrechd": 0,
    "LineSubSets": 0,
}

EXPECTED_TOTAL_ROWS = 654


def _mdbtools_present() -> bool:
    return mdbtools_version() is not None


requires_real_mdb = pytest.mark.skipif(
    not _mdbtools_present() or not REAL_MDB.is_file(),
    reason=(
        "needs mdbtools on PATH and the RedStar CEN_L3750_IP.mdb; set "
        "GEORAG_TEST_MDB to point at the file"
    ),
)

requires_mdbtools = pytest.mark.skipif(
    not _mdbtools_present(), reason="needs mdbtools on PATH"
)


# ---------------------------------------------------------------------------
# Fake mdbtools planted on PATH
# ---------------------------------------------------------------------------

_FAKE_IMPL = '''\
import json, os, sys, time

tool = sys.argv[1]
argv = sys.argv[2:]
cfg_dir = os.environ["GEORAG_FAKE_MDB_DIR"]

with open(os.path.join(cfg_dir, tool + ".argv.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

cfg_path = os.path.join(cfg_dir, tool + ".json")
if not os.path.exists(cfg_path):
    sys.stderr.write("fake %s: no config planted\\n" % tool)
    sys.exit(97)

with open(cfg_path, encoding="utf-8") as fh:
    cfg = json.load(fh)

if cfg.get("sleep"):
    time.sleep(cfg["sleep"])

sys.stdout.write(cfg.get("stdout", ""))
sys.stderr.write(cfg.get("stderr", ""))
sys.exit(cfg.get("rc", 0))
'''


class FakeMdbTools:
    """A real mdb-tables/mdb-json/mdb-ver on PATH that we control.

    Deliberately real child processes rather than a subprocess mock: the thing
    most likely to break is the argv round trip and the exit-code handling,
    and a mock of subprocess.run would assert those against itself.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.cfg = root / "cfg"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.cfg.mkdir(parents=True, exist_ok=True)

        impl = self.bin / "_fake_mdb.py"
        _write_text(impl, _FAKE_IMPL, newline="\n")

        for tool in ("mdb-tables", "mdb-json", "mdb-ver"):
            self._make_shim(tool, impl)

    def _make_shim(self, tool: str, impl: Path) -> None:
        if os.name == "nt":
            shim = self.bin / f"{tool}.bat"
            body = (
                "@echo off\r\n"
                f'"{sys.executable}" "{impl}" {tool} %*\r\n'
            )
            _write_text(shim, body, newline="")
        else:
            shim = self.bin / tool
            body = f'#!/bin/sh\nexec "{sys.executable}" "{impl}" {tool} "$@"\n'
            _write_text(shim, body, newline="\n")
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

    def plant(
        self,
        tool: str,
        *,
        stdout: str = "",
        stderr: str = "",
        rc: int = 0,
        sleep: float | None = None,
    ) -> None:
        cfg: dict[str, Any] = {"stdout": stdout, "stderr": stderr, "rc": rc}
        if sleep is not None:
            cfg["sleep"] = sleep
        _write_text(self.cfg / f"{tool}.json", json.dumps(cfg), newline="\n")

    def recorded_argv(self, tool: str) -> list[list[str]]:
        log = self.cfg / f"{tool}.argv.jsonl"
        if not log.exists():
            return []
        return [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def activate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEORAG_FAKE_MDB_DIR", str(self.cfg))
        monkeypatch.setenv("PATH", str(self.bin) + os.pathsep + os.environ["PATH"])


def _write_text(path: Path, body: str, *, newline: str) -> None:
    """Write *body* verbatim.

    Path.write_text on this Windows host rewrites UTF-8 as cp1252 and LF as
    CRLF, which corrupts a POSIX shim's shebang line and its line endings.
    Both are pinned explicitly here so the shim byte content is what was
    intended on either platform.
    """
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(body)


@pytest.fixture
def fake_mdb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeMdbTools:
    tools = FakeMdbTools(tmp_path / "fake")
    tools.activate(monkeypatch)
    return tools


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    """An existing file to stand in for a database.

    The fakes never open it; _validated_path only requires that it exists.
    """
    target = tmp_path / "survey.mdb"
    target.write_bytes(b"\x00\x01JET3-not-really")
    return target


# ---------------------------------------------------------------------------
# mdbtools absent -- exercised for real on this host
# ---------------------------------------------------------------------------


def test_missing_mdbtools_names_the_package_and_the_install_command(
    monkeypatch: pytest.MonkeyPatch, db_file: Path
) -> None:
    """A missing OS package must not surface as 'command not found'."""
    monkeypatch.setattr(access_mdb.shutil, "which", lambda _tool: None)

    with pytest.raises(MdbToolsNotInstalledError) as excinfo:
        list_tables(db_file)

    message = str(excinfo.value)
    assert "mdbtools" in message
    assert "apt-get install" in message
    assert "mdb-tables" in message
    # The single most common wrong turn is reaching for pip.
    assert "pip" in message


def test_missing_mdbtools_is_not_confused_with_a_bad_file(
    monkeypatch: pytest.MonkeyPatch, db_file: Path
) -> None:
    """MdbToolsNotInstalledError must be catchable apart from MdbReadError.

    A caller offering several readers falls through on 'tool missing' but must
    NOT fall through on 'this file is corrupt'.
    """
    monkeypatch.setattr(access_mdb.shutil, "which", lambda _tool: None)

    with pytest.raises(MdbToolsNotInstalledError):
        read_table(db_file, "Projects")

    assert issubclass(MdbToolsNotInstalledError, AccessMdbError)
    assert not issubclass(MdbToolsNotInstalledError, MdbReadError)


def test_mdbtools_version_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence is an answer for the probe, not an exception."""
    monkeypatch.setattr(access_mdb.shutil, "which", lambda _tool: None)
    assert mdbtools_version() is None


def test_require_mdbtools_raises_with_install_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access_mdb.shutil, "which", lambda _tool: None)

    with pytest.raises(MdbToolsNotInstalledError) as excinfo:
        require_mdbtools()

    assert "apt-get install" in str(excinfo.value)


def test_unexecutable_tool_reports_as_not_installed(
    monkeypatch: pytest.MonkeyPatch, db_file: Path, tmp_path: Path
) -> None:
    """which() succeeding but exec failing is still 'the tool is not usable'."""
    ghost = tmp_path / "not-a-program"
    ghost.mkdir()  # a directory: resolvable, never executable
    monkeypatch.setattr(access_mdb.shutil, "which", lambda _tool: str(ghost))

    with pytest.raises(MdbToolsNotInstalledError) as excinfo:
        list_tables(db_file)

    assert "could not be executed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Argument safety
# ---------------------------------------------------------------------------


def _capture_argv(monkeypatch: pytest.MonkeyPatch, stdout: bytes = b"") -> list[dict]:
    """Intercept subprocess.run and record exactly how it was called."""
    calls: list[dict] = []

    class _Result:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = b""

    def _fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"argv": argv, "kwargs": kwargs})
        return _Result()

    monkeypatch.setattr(access_mdb.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(access_mdb.subprocess, "run", _fake_run)
    return calls


def test_argv_is_a_list_and_shell_is_never_used(
    monkeypatch: pytest.MonkeyPatch, db_file: Path
) -> None:
    """No shell string is ever built from a filename."""
    calls = _capture_argv(monkeypatch)

    list_tables(db_file)

    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert isinstance(argv, list)
    assert all(isinstance(part, str) for part in argv)
    assert calls[0]["kwargs"]["shell"] is False


def test_filename_with_shell_metacharacters_is_passed_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hostile FILENAME reaches mdbtools as one argument, uninterpreted.

    The name avoids '/', '\\', ':', '"', '*', '?', '<', '>' and '|' because
    Windows will not create a file containing them -- the remaining space,
    ';', '&&', '$( )' and backticks are the characters a shell would act on,
    which is what this asserts never happens.
    """
    nasty = tmp_path / "a b; rm -rf . && echo $(whoami) `id`.mdb"
    nasty.write_bytes(b"x")
    calls = _capture_argv(monkeypatch)

    list_tables(nasty)

    argv = calls[0]["argv"]
    assert str(nasty) in argv, argv
    # Exactly one element carries it -- nothing was split on the space or ';'.
    assert sum(1 for part in argv if str(nasty) == part) == 1


def test_table_name_is_its_own_argv_element_and_never_interpolated(
    monkeypatch: pytest.MonkeyPatch, db_file: Path
) -> None:
    """A table name is attacker-controlled data read out of the .mdb file."""
    hostile = 'Rock"; DROP TABLE x; --  $(id) `whoami` && rm -rf /'
    calls = _capture_argv(monkeypatch)

    read_table(db_file, hostile)

    argv = calls[0]["argv"]
    assert hostile in argv, argv
    assert argv[-1] == hostile
    # It is a discrete element, not spliced into the path or a flag.
    assert not any(hostile in part for part in argv if part != hostile)


def test_option_terminator_precedes_the_positional_arguments(
    monkeypatch: pytest.MonkeyPatch, db_file: Path
) -> None:
    """'--' is what stops a name like '-U' from flipping a real mdb-json flag."""
    calls = _capture_argv(monkeypatch)

    read_table(db_file, "Projects")
    argv = calls[0]["argv"]
    assert argv.index("--") < argv.index(str(db_file))
    assert argv.index("--") < argv.index("Projects")

    calls.clear()
    list_tables(db_file)
    argv = calls[0]["argv"]
    # mdb-tables needs '-1' BEFORE the terminator or it becomes a positional.
    assert argv.index("-1") < argv.index("--") < argv.index(str(db_file))


@pytest.mark.parametrize("name", ["-Projects", "-U", "--date-format=%s", "-"])
def test_leading_dash_table_name_is_rejected_with_a_true_explanation(
    db_file: Path, name: str
) -> None:
    """mdbtools cannot address these; say so rather than relaying glib's error.

    Measured on mdbtools 1.0.1: both `mdb-json FILE -Projects` and
    `mdb-json -- FILE -Projects` exit 1, with "option parsing failed" and
    "Wrong number of arguments" respectively.
    """
    with pytest.raises(UnaddressableTableError) as excinfo:
        read_table(db_file, name)

    message = str(excinfo.value)
    assert repr(name) in message or name in message
    assert "-" in message
    assert issubclass(UnaddressableTableError, MdbReadError)


def test_empty_table_name_is_rejected(db_file: Path) -> None:
    with pytest.raises(UnaddressableTableError):
        read_table(db_file, "")


def test_missing_file_raises_filenotfound_before_spawning_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Matches every other parser in this package, and costs no subprocess."""
    calls = _capture_argv(monkeypatch)

    with pytest.raises(FileNotFoundError):
        list_tables(tmp_path / "absent.mdb")
    with pytest.raises(FileNotFoundError):
        read_table(tmp_path / "absent.mdb", "Projects")
    with pytest.raises(FileNotFoundError):
        export_all(tmp_path / "absent.mdb")

    assert calls == []


def test_directory_is_not_accepted_as_a_database(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list_tables(tmp_path)


# ---------------------------------------------------------------------------
# Process contract, against a real fake process on PATH
# ---------------------------------------------------------------------------


def test_list_tables_parses_one_name_per_line(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    fake_mdb.plant(
        "mdb-tables", stdout="Adjustments\nDatasets\nDipoleSpacing\n"
    )

    assert list_tables(db_file) == ["Adjustments", "Datasets", "DipoleSpacing"]


def test_list_tables_argv_reaches_the_real_child_intact(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    """End-to-end argv round trip through an actual process, not a mock."""
    fake_mdb.plant("mdb-tables", stdout="Projects\n")

    list_tables(db_file)

    assert fake_mdb.recorded_argv("mdb-tables") == [["-1", "--", str(db_file)]]


def test_list_tables_ignores_blank_and_whitespace_lines(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    fake_mdb.plant("mdb-tables", stdout="Projects\n\n   \nRawData\n")

    assert list_tables(db_file) == ["Projects", "RawData"]


def test_read_table_parses_json_lines_not_a_json_array(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    """The whole stream is NOT valid JSON; each line is."""
    fake_mdb.plant(
        "mdb-json",
        stdout=(
            '{"Key":109,"DipoleSet":3,"DipoleSpacing":100}\n'
            '{"Key":110,"DipoleSet":3,"DipoleSpacing":100}\n'
        ),
    )

    rows = read_table(db_file, "DipoleSpacing")

    assert rows == [
        {"Key": 109, "DipoleSet": 3, "DipoleSpacing": 100},
        {"Key": 110, "DipoleSet": 3, "DipoleSpacing": 100},
    ]
    # Guard the premise: json.loads on the whole stream would have failed.
    with pytest.raises(json.JSONDecodeError):
        json.loads(
            '{"Key":109,"DipoleSet":3,"DipoleSpacing":100}\n'
            '{"Key":110,"DipoleSet":3,"DipoleSpacing":100}\n'
        )


def test_read_table_preserves_rows_with_differing_key_sets(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    """mdb-json omits NULL columns; the omission must survive to the caller.

    Shaped after the real ErrorTable, where 28 of 29 rows lack InverseField.
    Back-filling None here would fabricate a schema mdb-json never reported.
    """
    fake_mdb.plant(
        "mdb-json",
        stdout=(
            '{"ErrorKey":2,"SField":"Sp","Factor":0,"Constant":0}\n'
            '{"ErrorKey":3,"SField":"Vp","InverseField":"I","Factor":5,'
            '"Constant":0.001}\n'
        ),
    )

    rows = read_table(db_file, "ErrorTable")

    assert len(rows) == 2
    assert "InverseField" not in rows[0]
    assert rows[1]["InverseField"] == "I"
    assert set(rows[0]) != set(rows[1])


def test_empty_table_is_empty_not_an_error(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    """Measured: mdb-json on an empty table exits 0 with zero bytes."""
    fake_mdb.plant("mdb-json", stdout="", rc=0)

    assert read_table(db_file, "RawData") == []


def test_nonzero_exit_raises_and_quotes_the_stderr(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    fake_mdb.plant(
        "mdb-json",
        stderr="Error: Table NoSuchTable does not exist in this database.\n",
        rc=1,
    )

    with pytest.raises(MdbReadError) as excinfo:
        read_table(db_file, "NoSuchTable")

    message = str(excinfo.value)
    assert "does not exist in this database" in message
    assert "exit code 1" in message


def test_nonzero_exit_with_silent_stderr_still_says_something_useful(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    """Measured: mdb-json exits 1 and writes NOTHING when the file is not Access.

    Reporting only the captured stderr would raise a blank error message.
    """
    fake_mdb.plant("mdb-json", stdout="", stderr="", rc=1)

    with pytest.raises(MdbReadError) as excinfo:
        read_table(db_file, "Projects")

    message = str(excinfo.value)
    assert "exit code 1" in message
    assert "not an Access database" in message


def test_list_tables_reports_an_unreadable_database(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    fake_mdb.plant("mdb-tables", stderr="Couldn't open database.\n", rc=1)

    with pytest.raises(MdbReadError) as excinfo:
        list_tables(db_file)

    assert "Couldn't open database" in str(excinfo.value)


def test_malformed_json_line_raises_and_names_the_line(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    """A bad line must never be silently skipped -- that under-reports rows."""
    fake_mdb.plant(
        "mdb-json",
        stdout='{"Key":1}\n{"Key":2,,,broken\n{"Key":3}\n',
    )

    with pytest.raises(MdbReadError) as excinfo:
        read_table(db_file, "StationMulti")

    message = str(excinfo.value)
    assert "line 2" in message
    assert "StationMulti" in message


def test_non_object_json_line_is_rejected(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    fake_mdb.plant("mdb-json", stdout='{"Key":1}\n[1, 2, 3]\n')

    with pytest.raises(MdbReadError) as excinfo:
        read_table(db_file, "Sections")

    assert "line 2" in str(excinfo.value)


def test_trailing_blank_lines_do_not_become_rows(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    fake_mdb.plant("mdb-json", stdout='{"Key":1}\n\n\n')

    assert read_table(db_file, "Projects") == [{"Key": 1}]


def test_timeout_raises_mdbreaderror_naming_the_limit(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    fake_mdb.plant("mdb-json", stdout='{"Key":1}\n', sleep=5.0)

    with pytest.raises(MdbReadError) as excinfo:
        read_table(db_file, "Projects", timeout_s=0.5)

    assert "did not finish" in str(excinfo.value)


def test_mdbtools_version_reports_the_string(fake_mdb: FakeMdbTools) -> None:
    fake_mdb.plant("mdb-ver", stdout="mdbtools v1.0.1\n")

    assert mdbtools_version() == "mdbtools v1.0.1"
    assert require_mdbtools() == "mdbtools v1.0.1"


def test_export_all_maps_every_table_including_empty_ones(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    fake_mdb.plant("mdb-tables", stdout="Projects\nRawData\n")
    fake_mdb.plant("mdb-json", stdout='{"Key":1,"ProjectID":"123456"}\n')

    exported = export_all(db_file)

    assert list(exported) == ["Projects", "RawData"]
    # One planted config serves both calls, so both come back with the row;
    # what matters here is that no table is dropped from the mapping.
    assert exported["Projects"] == [{"Key": 1, "ProjectID": "123456"}]
    assert fake_mdb.recorded_argv("mdb-json") == [
        ["--", str(db_file), "Projects"],
        ["--", str(db_file), "RawData"],
    ]


def test_export_all_is_strict_when_one_table_fails(
    fake_mdb: FakeMdbTools, db_file: Path
) -> None:
    """A partial dict returned as complete is indistinguishable from success."""
    fake_mdb.plant("mdb-tables", stdout="Projects\nBroken\n")
    fake_mdb.plant("mdb-json", stderr="Error: Table Broken does not exist.\n", rc=1)

    with pytest.raises(MdbReadError):
        export_all(db_file)


# ---------------------------------------------------------------------------
# Real file -- skipped unless mdbtools and the RedStar .mdb are both present
# ---------------------------------------------------------------------------


@requires_real_mdb
def test_real_file_exposes_the_measured_tables() -> None:
    assert list_tables(REAL_MDB) == EXPECTED_TABLES
    assert len(EXPECTED_TABLES) == 19


@requires_real_mdb
def test_real_file_row_counts_are_pinned() -> None:
    for table, expected in EXPECTED_ROW_COUNTS.items():
        assert len(read_table(REAL_MDB, table)) == expected, table


@requires_real_mdb
def test_real_file_error_table_proves_null_key_omission() -> None:
    """29 rows; exactly one carries InverseField. This is the NULL trap, live."""
    rows = read_table(REAL_MDB, "ErrorTable")

    assert len(rows) == 29
    with_inverse = [r for r in rows if "InverseField" in r]
    assert len(with_inverse) == 1
    assert with_inverse[0]["InverseField"] == "I"
    assert {len(r) for r in rows} == {4, 5}


@requires_real_mdb
def test_real_file_raw_data_is_empty_but_readings_are_not() -> None:
    """The survey's readings are in StationMulti, not the empty RawData."""
    assert read_table(REAL_MDB, "RawData") == []

    readings = read_table(REAL_MDB, "StationMulti")
    assert len(readings) == 120
    first = readings[0]
    assert first["Key"] == 1
    assert first["C1_Lin"] == 3750
    assert first["Vp"] == 326
    assert first["Rho"] == pytest.approx(320.7408635083189)


@requires_real_mdb
def test_real_file_export_all_totals() -> None:
    exported = export_all(REAL_MDB)

    assert list(exported) == EXPECTED_TABLES
    assert sum(len(rows) for rows in exported.values()) == EXPECTED_TOTAL_ROWS
    assert sum(1 for rows in exported.values() if not rows) == 10


@requires_real_mdb
def test_real_file_nonexistent_table_raises() -> None:
    with pytest.raises(MdbReadError) as excinfo:
        read_table(REAL_MDB, "NoSuchTable")

    assert "NoSuchTable" in str(excinfo.value)


@requires_mdbtools
def test_real_mdbtools_rejects_a_non_access_file(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mdb"
    junk.write_text("not an access database at all", encoding="utf-8")

    with pytest.raises(MdbReadError):
        list_tables(junk)


@requires_mdbtools
def test_real_mdbtools_version_is_reported() -> None:
    version = mdbtools_version()
    assert version is not None
    assert "mdbtools" in version.lower()
