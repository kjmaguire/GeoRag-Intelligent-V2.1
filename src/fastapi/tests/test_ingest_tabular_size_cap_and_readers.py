"""A tabular file over the cap is refused before it is downloaded; headers and
rows are read without splitting the whole file into lines (audit findings 16, 18).

WHY THIS FILE EXISTS
    Finding 16. No limit applied to tabular files beyond the 512 MiB every
    upload is admitted under, and the CSV path holds about nine times the file
    in memory (the decoded text, the parser's copies, a ``splitlines()`` of all
    of it just to read the header) on a worker with 8 GiB.
    ``INGEST_TABULAR_MAX_BYTES`` (150 MiB) is now checked against the object's
    declared size before the download and against the file that arrived, and
    ``_csv_headers`` reads a bounded prefix.

    Finding 18. ``_read_delimited_rows`` fed ``content.splitlines()`` to
    ``csv.DictReader``, which cut a quoted cell with a line break in it at the
    break and glued the pieces to its neighbours.
"""
from __future__ import annotations

# ruff: noqa: F811 - the `env` fixture is imported, then named as a parameter
import io
from pathlib import Path
from typing import Any

import pytest
from hatchet_sdk import NonRetryableException

from app.config import settings
from app.hatchet_workflows import ingest_tabular as it
from tests.test_ingest_tabular_typed_tables import env  # noqa: F401 - a fixture

MIB = 1024 * 1024


class TestOversizeRefusal:
    def test_the_default_ceiling_is_150_mib(self) -> None:
        assert settings.INGEST_TABULAR_MAX_BYTES == 150 * MIB

    def test_a_file_at_the_limit_is_accepted(self) -> None:
        assert it._oversize_refusal(150 * MIB, filename="a.csv") is None
        assert it._oversize_refusal(1, filename="a.csv") is None

    def test_an_unknown_size_is_not_a_refusal(self) -> None:
        """HEAD may not answer; the size after the download decides."""
        assert it._oversize_refusal(None, filename="a.csv") is None

    def test_a_file_over_the_limit_is_refused_with_the_numbers_and_the_remedy(self) -> None:
        message = it._oversize_refusal(151 * MIB, filename="collars.csv")

        assert message is not None
        assert "'collars.csv'" in message and "151 MB" in message and "150 MB" in message
        assert "INGEST_TABULAR_MAX_BYTES" in message
        assert "Nothing was read" in message and "Split" in message

    def test_the_limit_is_a_setting_not_a_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "INGEST_TABULAR_MAX_BYTES", 10 * MIB)

        assert it._oversize_refusal(11 * MIB, filename="a.csv") is not None
        assert it._oversize_refusal(10 * MIB, filename="a.csv") is None


class _SizedStore:
    """A bronze store that says how big the object is, and counts downloads."""

    def __init__(self, declared: int | None, body: bytes = b"stub") -> None:
        self.declared = declared
        self.body = body
        self.downloads = 0

    def head(self, _bucket: Any, _key: str) -> dict:
        if self.declared is None:
            raise RuntimeError("HEAD not supported")
        return {"size": self.declared}

    def get_file(self, _bucket: Any, _key: str, local: str) -> None:
        self.downloads += 1
        Path(local).write_bytes(self.body)


class TestWorkflowRefusesBeforeDownloading:
    pytestmark = pytest.mark.asyncio

    async def test_an_oversize_object_is_never_downloaded(self, env, monkeypatch) -> None:
        store = _SizedStore(declared=200 * MIB)
        monkeypatch.setattr(it, "get_storage_client", lambda: store)

        with pytest.raises(ValueError, match="INGEST_TABULAR_MAX_BYTES") as refused:
            await env.run("huge.csv")

        # The same object refuses the same way on a retry, so Hatchet must not
        # schedule one, and the row is closed now rather than left open for it.
        assert isinstance(refused.value, NonRetryableException)
        assert store.downloads == 0
        assert len(env.failed) == 1
        assert "200 MB" in env.failed[0]["error"] and "150 MB" in env.failed[0]["error"]
        assert not env.completed

    async def test_a_head_that_fails_falls_back_to_the_size_that_arrived(
        self, env, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "INGEST_TABULAR_MAX_BYTES", 16)
        store = _SizedStore(declared=None, body=b"x" * 64)
        monkeypatch.setattr(it, "get_storage_client", lambda: store)

        with pytest.raises(ValueError, match="INGEST_TABULAR_MAX_BYTES"):
            await env.run("sneaky.csv")

        assert store.downloads == 1          # it had to arrive to be measured
        assert len(env.failed) == 1

    async def test_a_file_within_the_limit_runs_on(self, env, monkeypatch) -> None:
        store = _SizedStore(declared=1 * MIB)
        monkeypatch.setattr(it, "get_storage_client", lambda: store)
        monkeypatch.setattr(
            env.it, "_read_dbf_table",
            lambda _path: [{"HoleID": "TR-01", "Easting": 394240.0, "Northing": 6215000.0,
                            "Depth": 61.5}],
        )

        out = await env.run("Collars.dbf")

        assert store.downloads == 1 and not env.failed
        assert out.written["collar"]["written"] == 1


class _PrefixOnlyStream:
    """A decoded-file stream that fails if anything reads ALL of it."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.reads: list[int | None] = []

    def read(self, n: int | None = -1) -> str:
        self.reads.append(n)
        if n is None or n < 0:
            raise AssertionError("the whole decoded file was read to find a header")
        return self.text[:n]


class TestCsvHeaders:
    def _headers(self, monkeypatch, text: str) -> tuple[list[str], _PrefixOnlyStream]:
        import georag_geoparsers._csv_io as csv_io

        stream = _PrefixOnlyStream(text)
        monkeypatch.setattr(
            csv_io, "open_csv_with_encoding", lambda _p: (stream, "utf-8", "sha", len(text)),
        )
        return it._csv_headers("ignored.csv"), stream

    def test_only_a_prefix_of_a_large_file_is_read(self, monkeypatch) -> None:
        body = "HoleID,Easting,Northing\n" + "A-1,500000,6000000\n" * 200_000

        headers, stream = self._headers(monkeypatch, body)

        assert headers == ["HoleID", "Easting", "Northing"]
        assert stream.reads == [it._HEADER_SCAN_CHARS]

    def test_blank_leading_lines_are_skipped(self, monkeypatch) -> None:
        headers, _ = self._headers(monkeypatch, "\n  \nHole;From;To\nA;0;1\n")

        assert headers == ["Hole", "From", "To"]

    def test_a_quoted_header_with_a_line_break_stays_one_cell(self, monkeypatch) -> None:
        headers, _ = self._headers(monkeypatch, 'HoleID,"Total\nDepth",Dip\nA,1,2\n')

        assert headers == ["HoleID", "Total\nDepth", "Dip"]

    def test_an_empty_file_has_no_headers(self, monkeypatch) -> None:
        assert self._headers(monkeypatch, "")[0] == []


class TestReadDelimitedRows:
    def _rows(self, tmp_path: Path, text: str, *, name: str = "t.csv") -> list[dict]:
        path = tmp_path / name
        path.write_bytes(text.encode("utf-8"))
        return it._read_delimited_rows(str(path))

    def test_a_quoted_cell_with_a_line_break_is_one_cell(self, tmp_path: Path) -> None:
        rows = self._rows(
            tmp_path, 'Hole,Note,Depth\nA-1,"first line\nsecond line",10\nA-2,plain,20\n',
        )

        assert rows == [
            {"Hole": "A-1", "Note": "first line\nsecond line", "Depth": "10"},
            {"Hole": "A-2", "Note": "plain", "Depth": "20"},
        ]

    def test_crlf_files_and_cr_inside_a_cell_survive(self, tmp_path: Path) -> None:
        rows = self._rows(tmp_path, 'Hole,Note\r\nA-1,"a\r\nb"\r\nA-2,c\r\n')

        assert [r["Hole"] for r in rows] == ["A-1", "A-2"]
        assert rows[0]["Note"] == "a\r\nb"

    def test_a_semicolon_table_still_splits_on_its_delimiter(self, tmp_path: Path) -> None:
        rows = self._rows(tmp_path, "Hole;Au\nA-1;0,5\n")

        assert rows == [{"Hole": "A-1", "Au": "0,5"}]

    def test_the_stream_helper_still_decodes_latin_1(self, tmp_path: Path) -> None:
        path = tmp_path / "latin.csv"
        path.write_bytes("Hole,Note\nA-1,R\xf8dberg\n".encode("latin-1"))

        assert it._read_delimited_rows(str(path)) == [{"Hole": "A-1", "Note": "Rødberg"}]


def test_the_module_imports_io_for_stringio() -> None:
    """Guard: the readers above rely on ``io.StringIO(newline="")``."""
    assert it.io is io
