"""Tables survive rendering and chunking intact (2026-08-20).

Two defects, one symptom. A geologist asks "what grade did DDH-22-001
return" and gets bare numbers under no header, or a number from the wrong
column.

  1. `_table_to_markdown` joined cells with " | " and stopped: no
     delimiter row, no leading/trailing pipes, no escaping, no column
     padding. Row-per-line text that resembles Markdown without being it,
     so a ragged row shifted values under the wrong header and a cell
     containing a pipe silently split into two columns.

  2. `_emit_windows` cut windows at raw character offsets, so a rendered
     table got cut mid-row: `| DDH-22-001 | 145.2 | 1` ends one chunk and
     `48.0 | 2.31 |` starts the next.

Neither function had any direct test coverage before this file — the
existing table tests monkeypatch `_table_to_markdown` away entirely,
which is how the format survived this long.
"""

from __future__ import annotations

import pytest

from app.services.ingest.pdf_report import (
    WINDOW_CHARS,
    WINDOW_OVERLAP_CHARS,
    WINDOW_SNAP_CHARS,
    _emit_windows,
    _is_markdown_delimiter_row,
    _split_table_markdown,
    _table_to_markdown,
)

# Fixtures are sized in WINDOWS, not in rows: WINDOW_CHARS is configurable
# (PDF_CHUNK_WINDOW_CHARS) and was raised 1500 -> 5000 on 2026-08-20. A
# fixture pinned at "400 rows" silently stops testing multi-window
# behaviour as soon as the window grows past it.
_ROW_CHARS = len("| DDH-0000 | 145.2 | 148.0 | 1.31 |\n")


def _rows_for_windows(windows: float) -> int:
    return max(3, int((WINDOW_CHARS * windows) // _ROW_CHARS))


def _parse_markdown_table(md: str) -> list[list[str]]:
    """Parse a GFM table back into rows, asserting the shape as it goes."""
    rows: list[list[str]] = []
    for line in md.splitlines():
        assert line.startswith("| "), f"row does not open with a pipe: {line!r}"
        assert line.endswith(" |"), f"row does not close with a pipe: {line!r}"
        rows.append([cell.strip() for cell in line[1:-1].split(" | ")])
    return rows


class TestTableToMarkdown:
    def test_emits_a_delimiter_row(self) -> None:
        md = _table_to_markdown([["Hole", "Au g/t"], ["DDH-1", "1.2"], ["DDH-2", "0.4"]])
        lines = md.splitlines()
        assert lines[0] == "| Hole | Au g/t |"
        assert _is_markdown_delimiter_row(lines[1])
        assert lines[2] == "| DDH-1 | 1.2 |"

    def test_ragged_rows_are_padded_not_shifted(self) -> None:
        """The one that produces wrong answers.

        Without padding, a row missing its middle cell renders with its
        last value one column to the left — so the depth reads as the
        grade.
        """
        md = _table_to_markdown([
            ["Hole", "From", "Au g/t"],
            ["DDH-1", "145.2", "1.31"],
            ["DDH-2", "88.0"],
        ])
        rows = _parse_markdown_table(md)

        assert all(len(row) == 3 for row in rows), "every row must have 3 columns"
        assert rows[3] == ["DDH-2", "88.0", ""]

    def test_a_long_row_widens_the_table_rather_than_losing_cells(self) -> None:
        md = _table_to_markdown([["A"], ["x", "y", "z"], ["p", "q"]])
        rows = _parse_markdown_table(md)
        assert all(len(row) == 3 for row in rows)
        assert rows[2] == ["x", "y", "z"]

    def test_pipe_in_a_cell_is_escaped(self) -> None:
        """An unescaped pipe adds a column and shifts everything after it."""
        md = _table_to_markdown([
            ["Unit", "Range"],
            ["ppm", "10|20"],
            ["ppm", "30"],
        ])
        rows = _parse_markdown_table(md)
        assert all(len(row) == 2 for row in rows)
        assert rows[2][1] == r"10\|20"

    def test_newline_in_a_cell_does_not_end_the_row(self) -> None:
        md = _table_to_markdown([
            ["Note", "Value"],
            ["wrapped\ncell", "1"],
            ["b", "2"],
        ])
        assert len(md.splitlines()) == 4  # header + delimiter + 2 data rows
        assert "| wrapped cell | 1 |" in md

    def test_whitespace_inside_cells_is_collapsed(self) -> None:
        md = _table_to_markdown([["A", "B"], ["  lots   of\t space ", "x"], ["c", "d"]])
        assert "| lots of space | x |" in md

    def test_empty_inputs(self) -> None:
        assert _table_to_markdown([]) == ""
        assert _table_to_markdown([[None, None], ["", ""]]) == ""

    def test_none_cells_become_empty_not_the_string_none(self) -> None:
        md = _table_to_markdown([["A", "B"], ["x", None], ["y", "z"]])
        assert "None" not in md
        assert "| x |  |" in md


class TestDelimiterRowDetection:
    @pytest.mark.parametrize("line", ["| --- | --- |", "|---|---|", "| :--- | ---: |"])
    def test_recognised(self, line: str) -> None:
        assert _is_markdown_delimiter_row(line)

    @pytest.mark.parametrize("line", ["| Hole | Au |", "", "| |", "| - a - |"])
    def test_rejected(self, line: str) -> None:
        assert not _is_markdown_delimiter_row(line)


class TestSplitTableMarkdown:
    def _big_table(self, data_rows: int) -> str:
        return _table_to_markdown(
            [["Hole", "From", "To", "Au g/t"]]
            + [[f"DDH-{n:04d}", "145.2", "148.0", "1.31"] for n in range(data_rows)]
        )

    def test_every_part_repeats_header_and_delimiter(self) -> None:
        parts = _split_table_markdown(self._big_table(_rows_for_windows(6)))
        assert len(parts) > 1, "a six-window table must split"
        for part in parts:
            lines = part.splitlines()
            assert lines[0] == "| Hole | From | To | Au g/t |"
            assert _is_markdown_delimiter_row(lines[1]), (
                "a part without its delimiter row is a headerless run of "
                "pipe-separated lines, not a table"
            )

    def test_parts_stay_within_the_window(self) -> None:
        for part in _split_table_markdown(self._big_table(_rows_for_windows(6))):
            assert len(part) <= WINDOW_CHARS

    def test_no_data_row_is_lost_or_duplicated(self) -> None:
        rows = _rows_for_windows(2)
        parts = _split_table_markdown(self._big_table(rows))
        data_rows = [
            line
            for part in parts
            for line in part.splitlines()[2:]
        ]
        assert len(data_rows) == rows
        assert len(set(data_rows)) == rows

    def test_small_table_is_returned_whole(self) -> None:
        md = self._big_table(3)
        assert _split_table_markdown(md) == [md]

    def test_tolerates_legacy_markdown_without_a_delimiter_row(self) -> None:
        """Stored values from before this change still split sensibly."""
        legacy = "\n".join(
            ["Hole | Au"]
            + [f"DDH-{n} | 1.2" for n in range(_rows_for_windows(6))]
        )
        parts = _split_table_markdown(legacy)
        assert len(parts) > 1
        assert all(part.splitlines()[0] == "Hole | Au" for part in parts)


class TestWindowSnapping:
    def _windows(self, text: str):
        page_index = [(1, 0, len(text))]
        return _emit_windows(text, 0, len(text), None, "Body", page_index)

    def _table_text(self, rows: int) -> str:
        return _table_to_markdown(
            [["Hole", "From", "To", "Au g/t"]]
            + [[f"DDH-{n:04d}", f"{n}.2", f"{n}.9", "1.31"] for n in range(rows)]
        )

    def test_no_chunk_starts_or_ends_mid_row(self) -> None:
        """The headline defect."""
        text = self._table_text(_rows_for_windows(6))
        assert len(text) > WINDOW_CHARS * 3

        chunks = self._windows(text)

        assert len(chunks) > 3
        for chunk in chunks:
            for line in chunk.text.splitlines():
                assert line.startswith("|"), f"row cut open: {line!r}"
                assert line.endswith("|"), f"row cut short: {line!r}"

    def test_every_row_survives_somewhere(self) -> None:
        """Snapping must not skip content between windows."""
        text = self._table_text(_rows_for_windows(3))
        emitted = {
            line
            for chunk in self._windows(text)
            for line in chunk.text.splitlines()
        }
        assert set(text.splitlines()) <= emitted

    def test_windows_still_respect_the_size_bound(self) -> None:
        for chunk in self._windows(self._table_text(_rows_for_windows(6))):
            assert len(chunk.text) <= WINDOW_CHARS

    def test_windows_do_not_shrink_below_the_overlap(self) -> None:
        """Termination guard: a window at or under the overlap would make
        the walk stall (or emit thousands of near-identical chunks)."""
        chunks = self._windows(self._table_text(_rows_for_windows(6)))
        for chunk in chunks[:-1]:
            assert len(chunk.text) > WINDOW_OVERLAP_CHARS

    def test_snapping_costs_at_most_the_snap_budget(self) -> None:
        chunks = self._windows(self._table_text(_rows_for_windows(6)))
        for chunk in chunks[:-1]:
            assert len(chunk.text) >= WINDOW_CHARS - WINDOW_SNAP_CHARS - 1

    def test_adjacent_windows_still_overlap(self) -> None:
        """Snapping must not cost the overlap that makes split sentences
        retrievable."""
        chunks = self._windows(self._table_text(_rows_for_windows(4)))
        for prev, curr in zip(chunks, chunks[1:], strict=False):
            assert curr.text.splitlines()[0] in prev.text.splitlines(), (
                "the next window should open on a row the previous one also "
                "carried"
            )

    def test_a_line_longer_than_the_snap_budget_falls_back_to_a_hard_cut(self) -> None:
        """OCR word-soup has no newline to snap to; the walk must not stall."""
        text = "X" * (WINDOW_CHARS * 3)
        chunks = self._windows(text)
        assert len(chunks) >= 3
        assert all(len(c.text) <= WINDOW_CHARS for c in chunks)

    def test_prose_is_unaffected_in_shape(self) -> None:
        text = ("The Patterson Lake property lies in the Athabasca Basin. " * 200)
        chunks = self._windows(text)
        assert len(chunks) > 1
        assert "".join(c.text for c in chunks).replace(" ", "").startswith(
            "ThePattersonLake"
        )

    def test_short_segment_is_one_chunk(self) -> None:
        chunks = self._windows("| A | B |\n| --- | --- |\n| 1 | 2 |")
        assert len(chunks) == 1
        assert chunks[0].text.endswith("| 1 | 2 |")
