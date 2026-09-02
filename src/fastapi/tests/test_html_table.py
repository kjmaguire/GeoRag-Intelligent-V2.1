"""html_table_to_grid — the bridge from Parse's HTML tables to text grids."""

from __future__ import annotations

from app.services.ingest.html_table import find_table_fragments, html_table_to_grid


def test_simple_two_by_two() -> None:
    grid = html_table_to_grid(
        "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"
    )

    assert grid == [["a", "b"], ["c", "d"]]


def test_colspan_header_propagates_into_every_covered_column() -> None:
    """The 'Inferred grade' case: the unit must reach all three grade columns."""
    html = (
        "<table>"
        "<tr><th>Category</th><th colspan='3'>Grade (g/t Au)</th></tr>"
        "<tr><td>Measured</td><td>1.1</td><td>1.2</td><td>1.3</td></tr>"
        "</table>"
    )

    grid = html_table_to_grid(html)

    assert grid[0] == ["Category", "Grade (g/t Au)", "Grade (g/t Au)", "Grade (g/t Au)"]
    assert grid[1] == ["Measured", "1.1", "1.2", "1.3"]


def test_rowspan_hangs_down_and_shifts_later_cells_right() -> None:
    html = (
        "<table>"
        "<tr><td rowspan='2'>Zone A</td><td>Measured</td><td>1.2</td></tr>"
        "<tr><td>Indicated</td><td>3.4</td></tr>"
        "</table>"
    )

    grid = html_table_to_grid(html)

    assert grid == [["Zone A", "Measured", "1.2"], ["Zone A", "Indicated", "3.4"]]


def test_an_anchor_never_overwrites_another_anchor() -> None:
    """Two cells claiming one position: the owner wins, the spill does not."""
    html = "<table>" "<tr><td colspan='2'>span</td><td>own</td></tr>" "</table>"

    grid = html_table_to_grid(html)

    assert grid == [["span", "span", "own"]]


def test_thead_tbody_br_entities_and_inline_markup_are_transparent() -> None:
    html = (
        "<table><thead><tr><th>Au<br>(g/t)</th><th>Cu &amp; Zn</th></tr></thead>"
        "<tbody><tr><td><b>2.4</b></td><td><i>0.9</i></td></tr></tbody></table>"
    )

    grid = html_table_to_grid(html)

    assert grid == [["Au (g/t)", "Cu & Zn"], ["2.4", "0.9"]]


def test_unclosed_cells_and_ragged_rows_are_padded() -> None:
    html = "<table><tr><td>a<td>b<td>c<tr><td>d</table>"

    grid = html_table_to_grid(html)

    assert grid == [["a", "b", "c"], ["d", "", ""]]


def test_nested_table_is_flattened_into_the_cell() -> None:
    html = "<table><tr><td>outer</td><td><table><tr><td>in1</td><td>in2</td></tr></table></td></tr></table>"

    grid = html_table_to_grid(html)

    assert len(grid) == 1
    assert grid[0][0] == "outer"
    assert "in1" in grid[0][1] and "in2" in grid[0][1]


def test_empty_and_cell_less_input_yield_no_grid() -> None:
    assert html_table_to_grid("") == []
    assert html_table_to_grid("no markup here") == []
    assert html_table_to_grid("<table></table>") == []


def test_find_table_fragments_returns_each_top_level_table_in_order() -> None:
    text = "intro <table><tr><td>1</td></tr></table> middle <TABLE><tr><td>2</td></tr></TABLE> end"

    fragments = find_table_fragments(text)

    assert len(fragments) == 2
    assert fragments[0].startswith("<table>") and "1" in fragments[0]
    assert "2" in fragments[1]
    assert find_table_fragments("plain") == []
