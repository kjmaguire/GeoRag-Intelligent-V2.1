"""A re-upload of the same spatial file must replace its features, not add.

Seen on the 2026-09-02 RedStar batch: every shapefile ZIP was ingested
twice and ``silver.spatial_features`` held two copies of every polygon.
The replace-on-re-upload in ``ingest_spatial`` keyed on
``source_file = <storage basename>`` — and the storage basename carries the
timestamp the upload controllers prepend (``20260902_143012_geology_poly.zip``,
plus a microsecond component when the ZIP fan-out re-keys a member). Two
uploads of one file never shared it, so the delete matched nothing.

The workflow now keys on the prefix-stripped name — the same rule the
Ingestion Runs page uses for its display name — and the delete predicate
strips that prefix from the stored value on the way through, so rows
written under the old shape are found too. These tests pin both halves
without a database; test_ingest_spatial_reupload_replaces_pg.py runs the
same regex through PostgreSQL.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.hatchet_workflows import _progress
from app.hatchet_workflows import ingest_spatial as module

_SRC = (
    pathlib.Path(__file__).parents[1]
    / "app"
    / "hatchet_workflows"
    / "ingest_spatial.py"
).read_text(encoding="utf-8")


def _run_body() -> str:
    tree = ast.parse(_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_ingest_spatial":
            return ast.get_source_segment(_SRC, node) or ""
    raise AssertionError("run_ingest_spatial not found in ingest_spatial.py")


# ---------------------------------------------------------------------------
# The workflow keys on the stripped name, on both the write and the delete
# ---------------------------------------------------------------------------


def test_source_file_is_the_prefix_stripped_name() -> None:
    body = _run_body()
    assert "source_file = _progress._filename_from_key(input.minio_key)" in body, (
        "run_ingest_spatial must derive source_file through _filename_from_key; "
        "the raw storage basename carries the upload timestamp and never "
        "matches a previous upload of the same file"
    )
    assert "source_file=source_file," in body, (
        "_write_features must persist the stripped name, or the next "
        "re-upload cannot find these rows either"
    )


def test_delete_uses_the_prefix_aware_predicate() -> None:
    body = _run_body()
    assert "_REPLACE_SQL," in body
    assert "input.project_id, source_file, _LEGACY_SOURCE_FILE_PREFIX," in body
    assert "source_file = $2" not in body, (
        "a raw equality on source_file cannot reach rows written before "
        "2026-09-03, which stored the timestamped basename"
    )
    assert "regexp_replace(source_file, $3, '') = $2" in module._REPLACE_SQL
    assert "project_id = $1::uuid" in module._REPLACE_SQL


def test_the_sql_prefix_is_the_display_name_prefix() -> None:
    """One rule for 'what the user called this file', on both sides."""
    assert _progress._GENERATED_PREFIX.pattern == module._LEGACY_SOURCE_FILE_PREFIX


def test_the_prefix_regex_stays_inside_postgres_are_syntax() -> None:
    """The pattern is handed to regexp_replace verbatim.

    PostgreSQL's ARE dialect shares ``\\d``, ``{n}`` and ``(?:...)`` with
    Python but has no named groups, no ``\\A``/``\\Z`` and no lookbehind. A
    future edit that reaches for one of those would silently match nothing
    on the database side while every Python test stayed green.
    """
    pattern = module._LEGACY_SOURCE_FILE_PREFIX
    for forbidden in ("(?P<", "(?<", "\\A", "\\Z", "(?i)", "(?x)"):
        assert forbidden not in pattern, f"{forbidden!r} is not PostgreSQL ARE"


# ---------------------------------------------------------------------------
# The regex semantics both sides rely on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "stable"),
    [
        # Direct upload: UploadController's `{Ymd_His}_{name}`.
        ("20260902_143012_geology_poly.zip", "geology_poly.zip"),
        # ZIP fan-out: ingest_zip_archive's `%Y%m%d_%H%M%S_%f_{name}`.
        ("20260902_143012_123456_geology_poly.zip", "geology_poly.zip"),
        # DrillUploadController's 8-hex digest variant.
        ("20260902_143012_deadbeef_geology_poly.zip", "geology_poly.zip"),
        # Already in the new shape: a fixed point.
        ("geology_poly.zip", "geology_poly.zip"),
        # A file whose own name starts with a date: the rule cannot tell the
        # 8-digit date from DrillUploadController's 8-hex digest, so the
        # optional group eats it too. That is the display name's existing
        # behaviour, and the property that matters here is only that the
        # stored value and the fresh upload strip to the SAME string.
        (
            "20260902_143012_20240101_120000_survey.zip",
            "120000_survey.zip",
        ),
    ],
)
def test_legacy_and_new_source_file_shapes_collapse_to_one_identity(
    stored: str,
    stable: str,
) -> None:
    stripped = re.sub(module._LEGACY_SOURCE_FILE_PREFIX, "", stored, count=1)
    assert stripped == stable
    # What the workflow would compute for a fresh upload of the same file.
    assert _progress._filename_from_key(f"spatial/proj/{stored}") == stable


def test_a_different_file_is_not_swept_up() -> None:
    other = re.sub(
        module._LEGACY_SOURCE_FILE_PREFIX,
        "",
        "20260902_143012_geology_line.zip",
        count=1,
    )
    assert other != "geology_poly.zip"


def test_a_shapefile_bundled_by_the_zip_fan_out_keeps_its_stem() -> None:
    """The fan-out re-zips `faults.shp` + sidecars as `faults.zip`.

    A geologist who uploads the loose shapefile set as `faults.zip` next
    time must replace what the archive ingest wrote, not sit beside it.
    """
    from_archive = _progress._filename_from_key(
        "spatial/proj/20260902_143012_123456_faults.zip",
    )
    direct = _progress._filename_from_key("spatial/proj/20260903_090000_faults.zip")
    assert from_archive == direct == "faults.zip"
