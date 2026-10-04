"""Spreadsheet passages keep their column header and say where they came from.

Parts 2..N of a long assay sheet used to be bare numbers (the only repeated
header was ``[Sheet: name]``), and the passage insert stored no page or row
range, so a citation could not point at the sheet. The report row was also
SELECT-then-INSERT with a random id and no unique key.
"""
from __future__ import annotations

import re

import pytest

from app.services.ingest import xlsx_ingester
from app.services.ingest.xlsx_ingester import (
    _SHEET_PASSAGE_CHARS,
    _sheet_passages,
    land_sheets_as_text,
)

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_HEADER = "hole_id\tfrom_m\tto_m\tAu_ppm\tCu_ppm"


def _assay_sheet(rows: int) -> str:
    lines = [_HEADER]
    lines += [f"DDH-{i:05d}\t{i}.0\t{i + 1}.0\t0.{i % 97:02d}\t1{i % 13}" for i in range(rows)]
    return "\n".join(lines)


class TestColumnHeaderRepeatedPerPart:
    def test_every_part_after_the_first_starts_with_the_column_row(self) -> None:
        passages = _sheet_passages([("Assays", _assay_sheet(12_000))])

        assert len(passages) > 10
        for sp in passages:
            body = sp.text.split("\n", 1)[1]
            assert body.split("\n", 1)[0] == _HEADER, sp.ordinal

    def test_the_first_part_is_not_given_a_second_copy(self) -> None:
        first = _sheet_passages([("Assays", _assay_sheet(12_000))])[0]
        assert first.text.count(_HEADER) == 1

    def test_no_data_row_is_lost_or_duplicated(self) -> None:
        passages = _sheet_passages([("Assays", _assay_sheet(3_000))])
        data_rows: list[str] = []
        for sp in passages:
            body = sp.text.split("\n")[1:]  # drop the [Sheet: ...] label
            data_rows.extend(r for r in body if r and r != _HEADER)
        assert len(data_rows) == 3_000
        assert len(set(data_rows)) == 3_000

    def test_parts_stay_within_the_window(self) -> None:
        for sp in _sheet_passages([("Assays", _assay_sheet(5_000))]):
            body = sp.text.split("\n", 1)[1]
            assert len(body) <= _SHEET_PASSAGE_CHARS + 1

    def test_a_single_part_sheet_is_unlabelled_and_unchanged(self) -> None:
        (only,) = _sheet_passages([("Small", f"{_HEADER}\nDDH-1\t0\t1\t0.1\t5")])
        assert only.text == f"[Sheet: Small]\n{_HEADER}\nDDH-1\t0\t1\t0.1\t5"
        assert (only.page, only.row_first, only.row_last) == (1, 1, 2)

    def test_an_oversized_first_row_is_not_repeated(self) -> None:
        """A first row over half the window is a paragraph, not column
        headings; repeating it would eat the window of every part."""
        title = "x" * (_SHEET_PASSAGE_CHARS // 2 + 10)
        passages = _sheet_passages(
            [("Notes", title + "\n" + "\n".join(f"row {i}" * 40 for i in range(200)))]
        )
        assert len(passages) > 1
        assert all(sp.text.count(title) == (1 if sp.ordinal == 0 else 0) for sp in passages)

    def test_a_row_longer_than_the_window_does_not_make_a_header_only_part(
        self,
    ) -> None:
        huge = "y" * (_SHEET_PASSAGE_CHARS + 500)
        passages = _sheet_passages([("S", f"{_HEADER}\n{'a' * 4000}\n{huge}\nlast\trow")])
        for sp in passages:
            body = [r for r in sp.text.split("\n")[1:] if r != _HEADER]
            assert body, f"part {sp.ordinal} holds only the repeated header"


class TestRowRangeAndPage:
    def test_row_ranges_tile_the_sheet_and_appear_in_the_label(self) -> None:
        text = _assay_sheet(4_000)
        passages = _sheet_passages([("Assays", text)])
        n_rows = len(text.split("\n"))

        assert passages[0].row_first == 1
        assert passages[-1].row_last == n_rows
        for prev, nxt in zip(passages, passages[1:], strict=False):
            assert nxt.row_first == prev.row_last + 1

        for sp in passages:
            m = re.match(
                r"\[Sheet: Assays\] \(part (\d+) of (\d+), rows (\d+)-(\d+) of (\d+)\)",
                sp.text,
            )
            assert m, sp.text[:80]
            assert int(m.group(3)) == sp.row_first
            assert int(m.group(4)) == sp.row_last
            assert int(m.group(5)) == n_rows

    def test_page_is_the_one_based_sheet_index(self) -> None:
        passages = _sheet_passages(
            [("A", "h\n1"), ("B", _assay_sheet(3_000)), ("C", "h\n2")]
        )
        pages = [(sp.page) for sp in passages]
        assert pages[0] == 1
        assert pages[-1] == 3
        assert {sp.page for sp in passages if "[Sheet: B]" in sp.text} == {2}
        assert min(pages) >= 1  # document_passages_page_range_positive


class _Conn:
    def __init__(self) -> None:
        self.passage_args: list[tuple] = []
        self.report_args: tuple | None = None

    async def fetchrow(self, sql: str, *args):
        flat = " ".join(sql.split())
        if "INSERT INTO silver.reports" in flat:
            self.report_args = args
            return {"report_id": args[0]}
        if "INSERT INTO silver.document_passages" in flat:
            assert "page_first" in flat and "page_last" in flat
            self.passage_args.append(args)
            return {"passage_id": "p"}
        raise AssertionError(flat[:100])


class TestPassageInsertCarriesPage:
    @pytest.mark.asyncio
    async def test_page_first_and_last_are_written(self, tmp_path) -> None:
        f = tmp_path / "a.csv"
        f.write_bytes(b"x")
        conn = _Conn()

        await land_sheets_as_text(
            conn, path=f,
            sheet_texts=[("One", "h\n1"), ("Two", _assay_sheet(3_000))],
            total_rows=3_002, workspace_id=_WS, project_id=_PJ,
            parser_used="openpyxl",
        )

        # args: document_id, workspace_id, text, hash, ordinal, page_first, page_last
        by_sheet = {a[2].split("\n", 1)[0].split(" (")[0]: (a[5], a[6]) for a in conn.passage_args}
        assert by_sheet["[Sheet: One]"] == (1, 1)
        assert by_sheet["[Sheet: Two]"] == (2, 2)
        assert [a[4] for a in conn.passage_args] == list(range(len(conn.passage_args)))

    @pytest.mark.asyncio
    async def test_report_insert_is_an_upsert_on_a_stable_id(self, tmp_path) -> None:
        f = tmp_path / "a.csv"
        f.write_bytes(b"x")
        ids = []
        for _ in range(2):
            c = _Conn()
            r = await land_sheets_as_text(
                c, path=f, sheet_texts=[("S", "h\n1")], total_rows=2,
                workspace_id=_WS, project_id=_PJ, parser_used="csv-text",
            )
            ids.append(r.document_id)
        assert ids[0] == ids[1]
        assert xlsx_ingester._stable_report_id(
            workspace_id=_WS, project_id=_PJ, source_identity="s",
        ) == xlsx_ingester._stable_report_id(
            workspace_id=_WS, project_id=_PJ, source_identity="s",
        )
