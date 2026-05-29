"""Sprint 4b tests for DXF block extraction via ezdxf.

Covers:
  - _extract_dxf_blocks: block name, entity count, entity types, base_point.
  - Blocks starting with '*' (MODEL_SPACE etc.) are excluded.
  - Block insertions are captured with correct location/rotation/scale.
  - Integration through parse_spatial_file: result.dxf_blocks is populated and
    "dxf_blocks" is removed from deferred_capabilities when ezdxf works.
  - ezdxf.readfile failure emits {"code": "dxf_block_extraction_failed"} warning.

Run with:  pytest tests/test_dxf_blocks.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

ezdxf = pytest.importorskip("ezdxf", reason="ezdxf not installed")
geopandas = pytest.importorskip("geopandas", reason="geopandas not installed")

from georag_dagster.parsers.spatial_parser import (  # noqa: E402
    SpatialParseResult,
    _extract_dxf_blocks,
    parse_spatial_file,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def simple_dxf(tmp_path):
    """DXF with one named block containing a POINT and TEXT entity."""
    doc = ezdxf.new()
    blk = doc.blocks.new(name="BoreholeSymbol")
    blk.add_point((0, 0))
    blk.add_text("HOLE_ID")
    path = tmp_path / "test.dxf"
    doc.saveas(str(path))
    return str(path)


@pytest.fixture()
def dxf_with_insertion(tmp_path):
    """DXF with one named block and one INSERT in modelspace."""
    doc = ezdxf.new()
    blk = doc.blocks.new(name="BoreholeSymbol")
    blk.add_point((0, 0))
    blk.add_text("HOLE_ID")
    msp = doc.modelspace()
    msp.add_blockref("BoreholeSymbol", (100, 200))
    path = tmp_path / "with_insert.dxf"
    doc.saveas(str(path))
    return str(path)


@pytest.fixture()
def multi_block_dxf(tmp_path):
    """DXF with two named blocks."""
    doc = ezdxf.new()
    blk1 = doc.blocks.new(name="CollarMarker")
    blk1.add_circle((0, 0), radius=5)
    blk2 = doc.blocks.new(name="SampleInterval")
    blk2.add_line((0, 0), (0, 10))
    blk2.add_text("Au")
    path = tmp_path / "multi_block.dxf"
    doc.saveas(str(path))
    return str(path)


# ---------------------------------------------------------------------------
# _extract_dxf_blocks — unit tests
# ---------------------------------------------------------------------------

class TestExtractDxfBlocks:
    def test_returns_list(self, simple_dxf):
        result = _extract_dxf_blocks(simple_dxf)
        assert isinstance(result, list)

    def test_single_block_name(self, simple_dxf):
        result = _extract_dxf_blocks(simple_dxf)
        names = [b["name"] for b in result]
        assert "BoreholeSymbol" in names

    def test_star_blocks_excluded(self, simple_dxf):
        """Blocks starting with '*' (MODEL_SPACE, PAPER_SPACE) must be excluded."""
        result = _extract_dxf_blocks(simple_dxf)
        names = [b["name"] for b in result]
        for name in names:
            assert not name.startswith("*"), f"Star block leaked into results: {name!r}"

    def test_entity_count(self, simple_dxf):
        result = _extract_dxf_blocks(simple_dxf)
        block = next(b for b in result if b["name"] == "BoreholeSymbol")
        assert block["entity_count"] == 2

    def test_entity_types(self, simple_dxf):
        result = _extract_dxf_blocks(simple_dxf)
        block = next(b for b in result if b["name"] == "BoreholeSymbol")
        assert block["entity_types"] == {"POINT": 1, "TEXT": 1}

    def test_base_point_is_list(self, simple_dxf):
        result = _extract_dxf_blocks(simple_dxf)
        block = next(b for b in result if b["name"] == "BoreholeSymbol")
        assert isinstance(block["base_point"], list)
        assert len(block["base_point"]) == 3

    def test_base_point_values(self, simple_dxf):
        """Block was created with default base point (0,0,0)."""
        result = _extract_dxf_blocks(simple_dxf)
        block = next(b for b in result if b["name"] == "BoreholeSymbol")
        assert block["base_point"] == [0.0, 0.0, 0.0]

    def test_no_insertions_when_none_exist(self, simple_dxf):
        """Block with no INSERT references in modelspace → empty insertions list."""
        result = _extract_dxf_blocks(simple_dxf)
        block = next(b for b in result if b["name"] == "BoreholeSymbol")
        assert block["insertions"] == []

    def test_insertion_location(self, dxf_with_insertion):
        """INSERT at (100, 200) → location == [100.0, 200.0, 0.0]."""
        result = _extract_dxf_blocks(dxf_with_insertion)
        block = next(b for b in result if b["name"] == "BoreholeSymbol")
        assert len(block["insertions"]) == 1
        assert block["insertions"][0]["location"] == [100.0, 200.0, 0.0]

    def test_insertion_rotation_default_zero(self, dxf_with_insertion):
        result = _extract_dxf_blocks(dxf_with_insertion)
        block = next(b for b in result if b["name"] == "BoreholeSymbol")
        ins = block["insertions"][0]
        assert ins["rotation"] == 0.0

    def test_insertion_scale_default_one(self, dxf_with_insertion):
        result = _extract_dxf_blocks(dxf_with_insertion)
        block = next(b for b in result if b["name"] == "BoreholeSymbol")
        ins = block["insertions"][0]
        assert ins["xscale"] == 1.0
        assert ins["yscale"] == 1.0

    def test_multiple_blocks_returned(self, multi_block_dxf):
        result = _extract_dxf_blocks(multi_block_dxf)
        names = {b["name"] for b in result}
        assert "CollarMarker" in names
        assert "SampleInterval" in names

    def test_attributes_list_empty_when_none(self, simple_dxf):
        result = _extract_dxf_blocks(simple_dxf)
        block = next(b for b in result if b["name"] == "BoreholeSymbol")
        assert isinstance(block["attributes"], list)
        assert block["attributes"] == []


# ---------------------------------------------------------------------------
# Integration through parse_spatial_file
# ---------------------------------------------------------------------------

class TestDxfBlocksIntegration:
    def test_dxf_blocks_populated_on_result(self, dxf_with_insertion):
        """parse_spatial_file should populate result.dxf_blocks for DXF files."""
        result = parse_spatial_file(str(dxf_with_insertion))
        assert isinstance(result, SpatialParseResult)
        assert hasattr(result, "dxf_blocks")
        assert isinstance(result.dxf_blocks, list)
        names = [b["name"] for b in result.dxf_blocks]
        assert "BoreholeSymbol" in names

    def test_dxf_blocks_removed_from_deferred(self, dxf_with_insertion):
        """When ezdxf extraction succeeds, 'dxf_blocks' is removed from deferred_capabilities."""
        result = parse_spatial_file(str(dxf_with_insertion))
        assert "dxf_blocks" not in result.deferred_capabilities, (
            f"Expected 'dxf_blocks' removed from deferred; got: {result.deferred_capabilities}"
        )

    def test_dxf_block_extraction_failed_warning_on_readfile_error(self, tmp_path):
        """If _extract_dxf_blocks raises, a dxf_block_extraction_failed warning is emitted.

        The DXF must have an INSERT in modelspace so pyogrio returns non-empty
        features — otherwise the parser hits the early-return path before the
        ezdxf block extraction code runs.
        """
        # Create a DXF with a block AND an INSERT in modelspace so pyogrio
        # returns at least one feature and we reach the ezdxf extraction branch.
        doc = ezdxf.new()
        blk = doc.blocks.new(name="TestBlock")
        blk.add_point((0, 0))
        msp = doc.modelspace()
        msp.add_blockref("TestBlock", (10, 20))
        dxf_path = tmp_path / "mock_fail.dxf"
        doc.saveas(str(dxf_path))

        with patch("georag_dagster.parsers.spatial_parser._extract_dxf_blocks") as mock_extract:
            mock_extract.side_effect = RuntimeError("simulated readfile failure")
            result = parse_spatial_file(str(dxf_path))

        codes = [w["code"] for w in result.warnings]
        assert "dxf_block_extraction_failed" in codes, (
            f"Expected dxf_block_extraction_failed warning; got: {codes}"
        )
        assert result.dxf_blocks == []
        assert result.dxf_blocks == []
