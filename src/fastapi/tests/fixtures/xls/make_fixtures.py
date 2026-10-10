"""Regenerate the legacy .xls fixtures in this directory.

NOT a test (no test_ prefix; pytest never imports it) and NOT a dependency:
xlwt is deliberately absent from pyproject.toml and the lockfiles. The tests
read the committed .xls bytes directly, so they run on every CI job; this
script only exists to say where those bytes came from and to rebuild them.

    python3 -m venv /tmp/xlwt-venv && /tmp/xlwt-venv/bin/pip install xlwt
    /tmp/xlwt-venv/bin/python make_fixtures.py          # writes next to this file

Only xlwt 1.3.0 is needed. The files are tiny OLE2/BIFF8 workbooks (about 6 KB
each) written the way Excel 97-2003 writes them.

Cell types are plain on purpose, and they are the ones a decades-old drill
archive actually contains: a text hole id, an integer-looking number (stored by
the format as a float, which is exactly why it surfaces as "1001.0"), a float,
and a DATE cell (a float with a date number format).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import xlwt

HERE = Path(__file__).resolve().parent


def collars_legacy() -> xlwt.Workbook:
    """tests/test_ingest_spatial_upload_regressions.py::test_xlrd_still_supports_xls."""
    book = xlwt.Workbook()
    sheet = book.add_sheet("collars")
    date_style = xlwt.easyxf(num_format_str="YYYY-MM-DD")

    for col, name in enumerate(("hole_id", "easting", "northing", "hole_no", "depth_m", "logged")):
        sheet.write(0, col, name)

    sheet.write(1, 0, "TR002")                       # text id
    sheet.write(1, 1, 400807.0)                      # float (UTM easting)
    sheet.write(1, 2, 6117291.0)                     # float (UTM northing)
    sheet.write(1, 3, 1001)                          # integer-looking id
    sheet.write(1, 4, 152.75)                        # float with a fraction
    sheet.write(1, 5, dt.date(2023, 7, 14), date_style)  # DATE cell
    return book


def ages_legacy() -> xlwt.Workbook:
    """tests/test_ingest_spatial_upload_regressions.py::test_it_reads_a_real_xls."""
    book = xlwt.Workbook()
    sheet = book.add_sheet("Ages")
    for col, value in enumerate(["Sample", "Age Ma", "method"]):
        sheet.write(0, col, value)
    for col, value in enumerate(["82ASh014", 37.1, "K-Ar"]):
        sheet.write(1, col, value)
    book.add_sheet("Empty")          # an empty sheet must add nothing downstream
    return book


if __name__ == "__main__":
    collars_legacy().save(str(HERE / "collars_legacy.xls"))
    ages_legacy().save(str(HERE / "ages_legacy.xls"))
    print("wrote collars_legacy.xls and ages_legacy.xls to", HERE)
