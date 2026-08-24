"""A continuation page must not label its assay data as column names.

Tables are extracted PER PAGE. `_table_to_markdown` used to emit `padded[0]`
as the header row followed by a `| --- |` delimiter, unconditionally. On
page 2+ of a table that spans a page break there is no header row, so the
first DATA row became the column header.

A 6-page assay table starting on page 88 with
`Hole ID | From (m) | To (m) | Au (g/t)` rendered pages 89-93 as::

    | DDH-22-041 | 145.20 | 148.00 | 2.31 |
    | --- | --- | --- | --- |

telling the reader and the LLM that "DDH-22-041" and "2.31" are column
names — and consuming a real assay row as a label. Every answer drawn from
those pages carried unlabelled numbers with no units.
"""

from __future__ import annotations

import pytest

from app.services.ingest.pdf_report import (
    _cell_is_numeric,
    _first_row_is_data,
    _table_to_markdown,
)

HEADED_PAGE = [
    ["Hole ID", "From (m)", "To (m)", "Au (g/t)"],
    ["DDH-22-040", "132.50", "136.00", "1.02"],
    ["DDH-22-041", "145.20", "148.00", "2.31"],
]

CONTINUATION_PAGE = [
    ["DDH-22-041", "145.20", "148.00", "2.31"],
    ["DDH-22-042", "151.00", "154.00", "0.87"],
    ["DDH-22-043", "203.10", "209.00", "4.02"],
]

LITHOLOGY_TABLE = [
    ["Unit", "Lithology", "Alteration"],
    ["1", "Granodiorite", "Chlorite"],
    ["2", "Pelitic gneiss", "Hematite"],
]


class TestHeaderDetection:
    def test_a_real_header_is_recognised(self) -> None:
        assert _first_row_is_data(HEADED_PAGE) is False

    def test_a_continuation_page_is_recognised_as_data(self) -> None:
        assert _first_row_is_data(CONTINUATION_PAGE) is True

    def test_a_text_table_keeps_its_header(self) -> None:
        """Conservative by design: demote only on positive evidence."""
        assert _first_row_is_data(LITHOLOGY_TABLE) is False

    def test_a_table_with_no_numbers_anywhere_keeps_its_header(self) -> None:
        assert _first_row_is_data(
            [["Massive sulphide", "Strong"], ["Semi-massive", "Moderate"]],
        ) is False

    def test_a_header_whose_labels_carry_units_is_not_demoted(self) -> None:
        """"From (m)" and "Au (g/t)" contain digits but are not numbers."""
        assert _first_row_is_data(
            [["Hole ID", "From (m)", "To (m)", "Au (g/t)"],
             ["DDH-1", "1.0", "2.0", "3.0"]],
        ) is False

    def test_a_numeric_first_row_over_a_text_body_is_not_demoted(self) -> None:
        assert _first_row_is_data(
            [["1", "2", "Name", "Note"], ["a", "b", "c", "d"], ["e", "f", "g", "h"]],
        ) is False

    def test_a_single_row_table_keeps_the_historical_reading(self) -> None:
        assert _first_row_is_data([["A", "B"]]) is False


class TestRendering:
    def test_the_headed_page_renders_its_header(self) -> None:
        first = _table_to_markdown(HEADED_PAGE).splitlines()[0]

        assert first == "| Hole ID | From (m) | To (m) | Au (g/t) |"

    def test_the_continuation_page_emits_an_empty_header(self) -> None:
        lines = _table_to_markdown(CONTINUATION_PAGE).splitlines()

        assert lines[0] == "|  |  |  |  |"
        assert set(lines[1].replace("|", "").split()) == {"---"}

    def test_no_data_row_is_consumed_by_the_header(self) -> None:
        """The bug ate a real assay row as well as mislabelling it."""
        lines = _table_to_markdown(CONTINUATION_PAGE).splitlines()
        body = lines[2:]

        assert len(body) == len(CONTINUATION_PAGE)
        assert "DDH-22-041" in body[0]
        assert "DDH-22-043" in body[-1]

    def test_still_valid_gfm(self) -> None:
        """Every row carries the delimiter row's column count."""
        for table in (HEADED_PAGE, CONTINUATION_PAGE, LITHOLOGY_TABLE):
            lines = _table_to_markdown(table).splitlines()
            widths = {line.count("|") for line in lines}

            assert len(widths) == 1, table

    @pytest.mark.parametrize("table", [[], [[None, None]]])
    def test_degenerate_tables_render_empty(self, table) -> None:
        assert _table_to_markdown(table) == ""


class TestExplicitOverride:
    """Document Intelligence reports `cell.kind == "columnHeader"`.

    When an extractor knows, its answer beats the heuristic.
    """

    def test_forcing_a_header_on(self) -> None:
        out = _table_to_markdown(CONTINUATION_PAGE, has_header=True)

        assert out.splitlines()[0].startswith("| DDH-22-041")

    def test_forcing_a_header_off(self) -> None:
        out = _table_to_markdown(HEADED_PAGE, has_header=False)

        assert out.splitlines()[0] == "|  |  |  |  |"
        assert "| Hole ID | From (m) | To (m) | Au (g/t) |" in out


class TestNumericCellReader:
    @pytest.mark.parametrize(
        "cell",
        ["1,250", "2.31%", "<0.01", "145.20 m", "(3.4)", "2.31 g/t", "-0.5", "0"],
    )
    def test_measurements_read_as_numeric(self, cell: str) -> None:
        assert _cell_is_numeric(cell) is True

    @pytest.mark.parametrize(
        "cell",
        ["Hole ID", "DDH-22-041", "", "   ", "Au (g/t)", "n/a", "granodiorite"],
    )
    def test_labels_do_not(self, cell: str) -> None:
        assert _cell_is_numeric(cell) is False
