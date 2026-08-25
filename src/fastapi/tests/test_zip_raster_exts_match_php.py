"""The ZIP fan-out's idea of "is this a raster" must match the API's.

WHY THIS FILE EXISTS
    `_RASTER_EXTS` in ingest_zip_archive.py and
    `UploadController::RASTER_REPORT_EXTS` answer the same question for the
    same file arriving by two routes: directly, or as a member of a ZIP.
    They are written in different languages with no shared source, and they
    have now drifted TWICE.

      * `.rrd` was added to the PHP and missed here. Both RedStar `.rrd`
        files -- which held the only surviving copy of their image -- were
        counted `unknown`.
      * `.jpg`/`.jpeg` were added to the PHP on 2026-08-25 and missed here
        again, so a scanned legend ingested when uploaded alone and vanished
        when uploaded inside an archive.

    The failure mode is what makes this worth a test rather than a comment.
    An unrecognised ZIP member increments `counts['unknown']`, and the
    terminal status is `partial` only when `counts['errors']` is non-zero.
    Unknowns are not errors, so the archive reports COMPLETED having silently
    skipped the file. There is no warning, no failed run, and no row -- the
    geologist's only evidence is a document that is not there.

    A comment cannot hold two files together across a language boundary. This
    parses the PHP, the same way
    resources/js/lib/__tests__/uploadCategories.test.ts does to hold the
    TypeScript side.

WHAT THIS DELIBERATELY DOES NOT ASSERT
    That the two lists are byte-identical in ORDER or type. Only that they
    describe the same set of extensions.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.hatchet_workflows.ingest_zip_archive import _RASTER_EXTS

#: ``src/fastapi/tests`` -> repo root. Computed defensively: a bare
#: ``parents[3]`` raises IndexError when only ``src/fastapi`` is mounted into
#: a container, and a module-scope IndexError aborts the WHOLE collection
#: rather than skipping this one file.
_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parents[-1]
CONTROLLER = (
    REPO_ROOT / "app" / "Http" / "Controllers" / "Api" / "V1" / "UploadController.php"
)


def _php_raster_exts() -> set[str]:
    source = CONTROLLER.read_text(encoding="utf-8")
    match = re.search(
        r"private const RASTER_REPORT_EXTS = \[([^\]]*)\];", source,
    )
    if match is None:
        raise AssertionError(
            "RASTER_REPORT_EXTS not found in UploadController.php — it was "
            "renamed or restructured, and this guard must be re-aimed rather "
            "than deleted.",
        )
    return set(re.findall(r"'([a-z0-9]+)'", match.group(1)))


@pytest.mark.skipif(
    not CONTROLLER.exists(),
    reason="Laravel app not mounted (fastapi-only container)",
)
def test_zip_fan_out_recognises_exactly_the_rasters_the_api_accepts() -> None:
    php = _php_raster_exts()
    python = set(_RASTER_EXTS)

    missing_here = sorted(php - python)
    extra_here = sorted(python - php)

    assert not missing_here, (
        f"{missing_here} are raster extensions the API accepts but the ZIP "
        "fan-out does not recognise. A member with one of these extensions "
        "inside an archive is counted `unknown`, and unknowns do not make a "
        "run `partial` — so the archive reports COMPLETED having skipped the "
        "file, with no warning and no row. Add them to _RASTER_EXTS in "
        "ingest_zip_archive.py."
    )
    assert not extra_here, (
        f"{extra_here} are treated as rasters by the ZIP fan-out but are not "
        "accepted by UploadController. A direct upload of one is refused at "
        "the door while the same file inside a ZIP is dispatched to "
        "tiff_normalize — the two routes must agree."
    )


@pytest.mark.skipif(
    not CONTROLLER.exists(),
    reason="Laravel app not mounted (fastapi-only container)",
)
def test_the_formats_that_have_actually_drifted_are_present() -> None:
    # Named explicitly because each cost a real file in a real delivery:
    # .rrd held the only copy of two images, and .jpg was RedStar's map
    # legend. A set-equality test alone would pass on two empty sets if the
    # parse ever silently returned nothing.
    for ext in ("tif", "tiff", "rrd", "jpg", "jpeg"):
        assert ext in _RASTER_EXTS, f"'{ext}' missing from _RASTER_EXTS"
        assert ext in _php_raster_exts(), f"'{ext}' missing from RASTER_REPORT_EXTS"
