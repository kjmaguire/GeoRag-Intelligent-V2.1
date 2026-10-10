"""Regenerates the legacy ``.xls`` fixtures that sit beside this file.

NOT a test and not imported by one. The three ``*.xls`` files are committed
binaries because nothing in this repository's dependency set can WRITE a
legacy workbook (xlrd only reads), and the tests that need a real BIFF8 file
(``test_xls_cell_types.py`` here, ``test_ingest_spatial_upload_regressions.py``
in src/fastapi) used to ``pytest.importorskip("xlwt")`` and so never ran in CI.

To regenerate (xlwt is a throwaway tool, deliberately NOT a project dependency):

    python -m venv /tmp/xlwt && /tmp/xlwt/bin/pip install xlwt
    /tmp/xlwt/bin/python src/georag_geoparsers/tests/fixtures/make_legacy_xls_fixtures.py

Files written next to this script:

* ``legacy_collars.xls`` - one sheet ``collars`` (hole_id, easting, northing),
  one row (TR002, 400807.0, 6117291.0).
* ``legacy_ages.xls`` - ``Ages`` (Sample, Age Ma, method / 82ASh014, 37.1,
  K-Ar) and an empty sheet ``Empty``.
* ``legacy_drill.xls`` - the cell types xlrd distinguishes, in a collar table:
  numeric hole ids (1001, 10010), an Excel ERROR cell (#N/A), real DATE cells
  (date-formatted numbers), a BOOLEAN, a TEXT hole id, and two columns with
  the same header (``Notes``).

``legacy_collars.xls`` and ``legacy_ages.xls`` are ALSO written to
``src/fastapi/tests/fixtures/xls/``, where ``test_ingest_spatial_upload_regressions.py``
reads them (FastAPI tests keep their fixtures under their own tests/fixtures).
"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

import xlwt

HERE = Path(__file__).resolve().parent
#: src/georag_geoparsers/tests/fixtures -> src/fastapi/tests/fixtures/xls
FASTAPI_COPY = HERE.parents[2] / "fastapi" / "tests" / "fixtures" / "xls"


def _collars() -> None:
    book = xlwt.Workbook()
    sheet = book.add_sheet("collars")
    for col, name in enumerate(("hole_id", "easting", "northing")):
        sheet.write(0, col, name)
    sheet.write(1, 0, "TR002")
    sheet.write(1, 1, 400807.0)
    sheet.write(1, 2, 6117291.0)
    book.save(str(HERE / "legacy_collars.xls"))


def _ages() -> None:
    book = xlwt.Workbook()
    sheet = book.add_sheet("Ages")
    for col, value in enumerate(["Sample", "Age Ma", "method"]):
        sheet.write(0, col, value)
    for col, value in enumerate(["82ASh014", 37.1, "K-Ar"]):
        sheet.write(1, col, value)
    book.add_sheet("Empty")
    book.save(str(HERE / "legacy_ages.xls"))


def _drill() -> None:
    book = xlwt.Workbook()
    sheet = book.add_sheet("Collars")
    date_style = xlwt.easyxf(num_format_str="YYYY-MM-DD")

    headers = ["HoleID", "Easting", "Northing", "Elevation", "Total_Depth",
               "DrillDate", "Notes", "Notes", "Verified"]
    for col, name in enumerate(headers):
        sheet.write(0, col, name)

    # Row 1: a NUMERIC hole id (stored as the float 1001.0), a real date, text
    # in both "Notes" columns, a boolean.
    sheet.write(1, 0, 1001)
    sheet.write(1, 1, 500000.5)
    sheet.write(1, 2, 6000000.25)
    sheet.write(1, 3, 300)
    sheet.write(1, 4, 150)
    sheet.write(1, 5, datetime.date(2023, 4, 5), date_style)
    sheet.write(1, 6, "first note")
    sheet.write(1, 7, "second note")
    sheet.write(1, 8, True)

    # Row 2: a DIFFERENT numeric hole id, 10010, whose old "10010.0" form
    # canonicalised the same as 1001's; an Excel #N/A in the elevation.
    sheet.write(2, 0, 10010)
    sheet.write(2, 1, 500100)
    sheet.write(2, 2, 6000100)
    sheet.row(2).set_cell_error(3, "#N/A!")   # xlwt's spelling of BIFF error 0x2A (42)
    sheet.write(2, 4, 160)
    sheet.write(2, 5, datetime.date(2022, 11, 30), date_style)

    # Row 3: a text hole id and a #DIV/0!.
    sheet.write(3, 0, "DH-3")
    sheet.write(3, 1, 500200)
    sheet.write(3, 2, 6000200)
    sheet.write(3, 3, 320)
    sheet.row(3).set_cell_error(4, "#DIV/0!")

    book.save(str(HERE / "legacy_drill.xls"))


if __name__ == "__main__":
    _collars()
    _ages()
    _drill()
    FASTAPI_COPY.mkdir(parents=True, exist_ok=True)
    for name in ("legacy_collars.xls", "legacy_ages.xls"):
        shutil.copyfile(HERE / name, FASTAPI_COPY / name)
    print("wrote", ", ".join(sorted(p.name for p in HERE.glob("legacy_*.xls"))))
