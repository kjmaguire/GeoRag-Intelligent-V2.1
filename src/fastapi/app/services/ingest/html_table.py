"""HTML ``<table>`` → row-major text grid, for OCR engines that emit HTML.

Cohere Parse returns every table it detects as an HTML fragment (ADR-0019).
The rest of the pipeline — ``_table_to_markdown``, the resource-table
classifier, the scanned-table sections — works on ``grid[row][col]`` of
stripped cell text, which is also what the Document Intelligence adapter
produced from its ``cells`` collection. This is the bridge.

Span handling is deliberately identical to the old adapter: a spanning cell
is written into EVERY position it covers, and an anchor never overwrites
another anchor. A scanned resource table typically has a two-row header
where ``Grade (g/t Au)`` spans the three category columns; read
anchor-only, two of the three grade columns end up with a blank header and a
question about the Inferred grade retrieves a column with no name and no
unit. Propagating the span is what keeps the unit attached.

Stdlib only (``html.parser``): no new dependency for one bridge function.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_WS_RE = re.compile(r"\s+")


class _TableGridParser(HTMLParser):
    """Collect (row_index, col_index, rowspan, colspan, text) for one table.

    Nested tables are flattened into the enclosing cell's text: the outer
    table is the one the engine drew a box around, and a grid of grids is
    not something the markdown renderer can express anyway.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[tuple[int, int, int, int, str]] = []
        self._depth = 0
        self._row = -1
        self._in_cell = False
        self._cell_rowspan = 1
        self._cell_colspan = 1
        self._buffer: list[str] = []
        # Per-row occupancy carried forward for rowspans, so the column a
        # new cell lands in accounts for cells hanging down from above.
        self._occupied: dict[int, set[int]] = {}
        self._col = 0

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _span(attrs: list[tuple[str, str | None]], name: str) -> int:
        for key, value in attrs:
            if key.lower() == name and value:
                try:
                    return max(1, int(value.strip()))
                except ValueError:
                    return 1
        return 1

    def _next_free_col(self) -> int:
        taken = self._occupied.setdefault(self._row, set())
        col = self._col
        while col in taken:
            col += 1
        return col

    def _close_cell(self) -> None:
        if not self._in_cell:
            return
        text = _WS_RE.sub(" ", "".join(self._buffer)).strip()
        col = self._next_free_col()
        self.cells.append(
            (self._row, col, self._cell_rowspan, self._cell_colspan, text)
        )
        for r in range(self._row, self._row + self._cell_rowspan):
            taken = self._occupied.setdefault(r, set())
            taken.update(range(col, col + self._cell_colspan))
        self._col = col + self._cell_colspan
        self._in_cell = False
        self._buffer = []

    # -- HTMLParser hooks -------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._depth += 1
            if self._depth > 1:
                # Nested: keep its text inside the current cell.
                return
            return
        if self._depth > 1:
            if tag in {"tr", "td", "th"} and self._in_cell:
                self._buffer.append(" ")
            return
        if tag == "tr":
            self._close_cell()
            self._row += 1
            self._col = 0
        elif tag in {"td", "th"}:
            self._close_cell()
            if self._row < 0:
                self._row = 0
            self._in_cell = True
            self._cell_rowspan = self._span(attrs, "rowspan")
            self._cell_colspan = self._span(attrs, "colspan")
            self._buffer = []
        elif tag == "br" and self._in_cell:
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            if self._depth == 1:
                self._close_cell()
            self._depth = max(0, self._depth - 1)
            return
        if self._depth > 1:
            return
        if tag in {"td", "th"} or tag == "tr":
            self._close_cell()

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._buffer.append(data)

    def close(self) -> None:  # pragma: no cover — defensive flush
        super().close()
        self._close_cell()


def html_table_to_grid(fragment: str) -> list[list[str]]:
    """Convert one HTML table fragment into ``grid[row][col]`` of cell text.

    Returns ``[]`` when the fragment holds no cells. Ragged rows are padded
    to the widest row so downstream renderers never index past a row's end.
    Entities are unescaped, ``<br>`` becomes a space, and inline markup
    (``<b>``, ``<i>``, ``<span>``) contributes only its text.
    """
    if not fragment or "<" not in fragment:
        return []

    parser = _TableGridParser()
    parser.feed(fragment)
    parser.close()

    if not parser.cells:
        return []

    row_count = max(r + rs for r, _c, rs, _cs, _t in parser.cells)
    col_count = max(c + cs for _r, c, _rs, cs, _t in parser.cells)
    grid = [["" for _ in range(col_count)] for _ in range(row_count)]

    for row, col, rowspan, colspan, text in parser.cells:
        if not text:
            continue
        for r in range(row, min(row + rowspan, row_count)):
            for c in range(col, min(col + colspan, col_count)):
                if not grid[r][c]:
                    grid[r][c] = text

    return grid


_TABLE_FRAGMENT_RE = re.compile(r"<table\b.*?</table\s*>", re.IGNORECASE | re.DOTALL)


def find_table_fragments(text: str) -> list[str]:
    """Every top-level ``<table>…</table>`` fragment in ``text``, in order."""
    if not text or "<table" not in text.lower():
        return []
    return _TABLE_FRAGMENT_RE.findall(text)


__all__ = ["find_table_fragments", "html_table_to_grid"]
