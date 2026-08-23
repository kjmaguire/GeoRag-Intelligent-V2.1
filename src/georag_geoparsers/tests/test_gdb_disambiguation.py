# PORTED from src/dagster/tests/test_gdb_disambiguation.py on 2026-08-21.
#
# The original runs against a FROZEN COPY of these parsers in
# src/dagster/georag_dagster/parsers/. Those test steps were removed from
# CI on 2026-08-11 (see the comment at .github/workflows/ci.yml) and have
# not run since, so georag_geoparsers/__init__.py calling itself "the most
# thoroughly exercised code in the ingestion path" rested on tests that no
# longer execute.
#
# This copy runs against the package the Hatchet ingestion workflows
# actually import. The original is left in place deliberately -- it is
# still the only coverage the frozen tree has.
"""Geosoft GDB vs Esri FileGDB disambiguation (2026-05-23).

A user-uploaded .gdb can be EITHER:
  * Esri FileGDB — a directory; readable via pyogrio's OpenFileGDB driver
  * Geosoft GDB  — a single binary file; not openly parseable

These tests lock the disambiguation behaviour: a binary .gdb file must
fail fast with a clear NotImplementedError pointing the user at the XYZ
export workaround. A .gdb DIRECTORY still routes to FileGDB (we don't
test the full read here — that requires a real FileGDB sample).
"""
from __future__ import annotations

import pytest

# spatial_parser imports geopandas at module scope, and this file's two
# siblings (test_dxf_blocks, test_qfield_ingestion) already guard at
# module level. Without this, a machine without the geospatial stack
# gets a collection ERROR here and skips there -- same cause, two
# different-looking outcomes.
pytest.importorskip("geopandas", reason="geopandas not installed")


def test_geosoft_gdb_binary_file_raises_with_actionable_message(tmp_path):
    from georag_geoparsers.spatial_parser import parse_spatial_file

    # Geosoft GDB shape: a single binary file (with .gdb extension, not
    # a directory). Content doesn't matter — disambiguation should fire
    # before any pyogrio read attempt.
    geosoft = tmp_path / "magnetic_survey.gdb"
    geosoft.write_bytes(b"\x47\x44\x42\x00fake-geosoft-binary-content")

    with pytest.raises(NotImplementedError) as exc:
        parse_spatial_file(str(geosoft))

    msg = str(exc.value)
    # Message must (a) name the file, (b) say "Geosoft", (c) point at the
    # XYZ workaround so the user knows what to do.
    assert "magnetic_survey.gdb" in msg
    assert "Geosoft" in msg
    assert "xyz" in msg.lower() or "XYZ" in msg


def test_esri_filegdb_directory_does_not_disambiguate_as_geosoft(tmp_path):
    """A .gdb directory should still be treated as Esri FileGDB. We don't
    do a full parse here (no real FileGDB sample) — we just assert the
    NotImplementedError branch does NOT fire when the path is a directory.
    """
    from georag_geoparsers.spatial_parser import parse_spatial_file

    filegdb_dir = tmp_path / "project.gdb"
    filegdb_dir.mkdir()
    # Touch a sentinel file so the dir isn't empty.
    (filegdb_dir / "gdb").write_bytes(b"")

    # The parse will fail downstream (no real FileGDB internal structure)
    # but it must NOT fail with the NotImplementedError that's reserved
    # for the Geosoft branch.
    with pytest.raises(Exception) as exc:
        parse_spatial_file(str(filegdb_dir))

    assert not isinstance(exc.value, NotImplementedError), (
        f"Esri FileGDB directory must not trip the Geosoft disambiguation "
        f"branch; got: {type(exc.value).__name__}: {exc.value}"
    )
