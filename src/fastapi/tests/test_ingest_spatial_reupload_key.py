"""A corrected re-upload of a spatial file replaces it, not doubles it (ING-7).

Audit 2026-09-29: ingest_spatial scoped its replace to ``source_file =
filename``, and the filename is the bronze key's last segment, which carries
the upload timestamp (``20260929_081500_geology.zip``; the ZIP fan-out adds
microseconds). Every upload has a new timestamp, so the delete only ever
matched a Hatchet retry of the SAME key and a geologist's corrected
re-upload drew every polygon twice.

The live-database half (the regexp in SQL matches the Python one, and two
different keys for one name replace) is in
test_ingest_constraint_rows_integration.py.
"""
from __future__ import annotations

import re

import pytest


def _mod():
    from app.hatchet_workflows import ingest_spatial

    return ingest_spatial


@pytest.mark.parametrize(("filename", "logical"), [
    ("20260929_081500_geology.zip", "geology.zip"),              # UploadController
    ("20260929_081500_123456_geology.zip", "geology.zip"),       # ZIP fan-out
    ("geology.zip", "geology.zip"),                               # pre-prefix rows
    ("2026_geology.zip", "2026_geology.zip"),                     # not a stamp
    ("20260929_081500_", "20260929_081500_"),                     # nothing left
])
def test_logical_name_drops_only_the_upload_stamp(filename: str, logical: str) -> None:
    assert _mod()._logical_source_name(filename) == logical


def test_python_and_sql_patterns_agree() -> None:
    """The SQL arm must strip exactly what the Python helper strips."""
    mod = _mod()
    as_python = mod._UPLOAD_STAMP_SQL.replace("(_", "(?:_")
    assert as_python == mod._UPLOAD_STAMP_RE.pattern


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql: str, *args):
        self.calls.append((sql, args))
        return 7


async def test_two_uploads_of_one_file_share_a_replace_key() -> None:
    mod = _mod()
    conn = _Conn()
    first = await mod._replace_previous_upload(
        conn, project_id="p", filename="20260901_100000_geology.zip",
    )
    await mod._replace_previous_upload(
        conn, project_id="p", filename="20260929_081500_123456_geology.zip",
    )
    assert first == 7
    (_sql1, args1), (sql2, args2) = conn.calls
    assert args1[2] == args2[2] == "geology.zip"
    assert re.search(r"regexp_replace\(source_file, \$4, ''\) = \$3", sql2)
    # The stored source_file is still the exact bronze object name.
    assert args2[1] == "20260929_081500_123456_geology.zip"
