"""ADR-0012 — tests for the structured-to-NL summary synthesizers.

Tests the pure template + ID derivation logic — no DB I/O. The asset
materialise paths are tested separately in the integration suite.
"""
import importlib
import uuid

import pytest


# ---------------------------------------------------------------------------
# Lazy import: the module imports psycopg2 + dagster at module level.
# A pytest collection environment without those should not break test
# discovery, so wrap the import in a fixture.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def m():
    return importlib.import_module(
        "georag_dagster.assets.silver_nl_summaries"
    )


# ---------------------------------------------------------------------------
# _derive_passage_id — same input always produces same UUID
# ---------------------------------------------------------------------------


def test_derive_passage_id_is_deterministic(m):
    rid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    a = m._derive_passage_id("silver.assays_v2", rid)
    b = m._derive_passage_id("silver.assays_v2", rid)
    assert a == b


def test_derive_passage_id_differs_per_source_table(m):
    """Same row_id but different source_table → different passage_id."""
    rid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    a = m._derive_passage_id("silver.assays_v2", rid)
    b = m._derive_passage_id("silver.lithology", rid)
    assert a != b


def test_derive_passage_id_uses_uuid5(m):
    """Derivation must be uuid5 (deterministic) — not uuid4 (random)."""
    rid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    derived = m._derive_passage_id("silver.assays_v2", rid)
    assert derived.version == 5


# ---------------------------------------------------------------------------
# _text_hash — sha256 truncated to 64 chars
# ---------------------------------------------------------------------------


def test_text_hash_length_matches_silver_text_hash_column(m):
    h = m._text_hash("any text")
    assert len(h) == 64
    # sha256 produces hex chars only.
    assert all(c in "0123456789abcdef" for c in h)


def test_text_hash_is_deterministic(m):
    assert m._text_hash("hello world") == m._text_hash("hello world")


# ---------------------------------------------------------------------------
# _render_assay_passage — template covers the inline-context join
# ---------------------------------------------------------------------------


def _assay_row(**overrides):
    """Minimal valid assay-group row matching the GROUP BY in the asset SQL."""
    base = dict(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        collar_id="b0000000-0000-0000-0000-000000000001",
        sample_id="PLS-2024-1247",
        from_depth=142.3,
        to_depth=145.6,
        lab_name="SRC Geoanalytical",
        certificate_ref="2024-08-15",
        analysis_method="ICP-MS",
        instrument="Agilent 7900",
        qaqc_flag="pass",
        elements={
            "U3O8": {"value": 0.45, "unit": "wt%", "value_ppm": 4500,
                     "over_detection": False, "under_detection": False},
            "Mo":   {"value": 12,   "unit": "ppm", "value_ppm": 12,
                     "over_detection": False, "under_detection": False},
        },
        hole_id="MAC-22-11",
        project_name="Patterson Lake South",
        rock_code="PG-GR",
        rock_name="graphitic pelitic gneiss",
    )
    base.update(overrides)
    return base


def test_render_assay_passage_includes_hole_and_project(m):
    row = _assay_row()
    text = m._render_assay_passage(row)
    assert "MAC-22-11" in text
    assert "Patterson Lake South" in text


def test_render_assay_passage_lists_all_elements_alphabetically(m):
    row = _assay_row()
    text = m._render_assay_passage(row)
    # Both elements present.
    assert "U3O8 0.45 wt%" in text
    assert "Mo 12 ppm" in text
    # Alphabetical ordering — Mo before U3O8.
    assert text.index("Mo 12") < text.index("U3O8 0.45")


def test_render_assay_passage_marks_below_detection(m):
    row = _assay_row(elements={
        "Au": {"value": 0.005, "unit": "ppm", "value_ppm": 0.005,
               "over_detection": False, "under_detection": True},
    })
    text = m._render_assay_passage(row)
    assert "below detection" in text


def test_render_assay_passage_falls_back_for_missing_hole(m):
    row = _assay_row(hole_id=None, project_name=None)
    text = m._render_assay_passage(row)
    assert "unknown hole" in text
    assert "unknown project" in text


def test_render_assay_passage_includes_lithology_context(m):
    row = _assay_row()
    text = m._render_assay_passage(row)
    # Lithology join produces an inline "Host rock at interval:" line.
    assert "Host rock" in text
    assert "graphitic pelitic gneiss" in text


def test_render_assay_passage_includes_lab_and_certificate(m):
    row = _assay_row()
    text = m._render_assay_passage(row)
    assert "SRC Geoanalytical" in text
    assert "2024-08-15" in text


# ---------------------------------------------------------------------------
# _render_lithology_passage
# ---------------------------------------------------------------------------


def _lith_row(**overrides):
    base = dict(
        id="c0000000-0000-0000-0000-000000000001",
        workspace_id="a0000000-0000-0000-0000-000000000001",
        collar_id="b0000000-0000-0000-0000-000000000001",
        from_depth=142.3,
        to_depth=145.6,
        rock_code="PG-GR-CHL",
        rock_name="graphitic pelitic gneiss",
        description=(
            "Fine-grained dark grey gneiss with abundant graphite and "
            "chloritic alteration along fractures. Visible uranium oxide "
            "staining on fracture surfaces."
        ),
        colour="dark grey",
        grain_size="fine",
        texture="banded",
        weathering="fresh",
        hardness="hard",
        logged_by="J. Smith",
        logged_date="2024-08-15",
        hole_id="MAC-22-11",
        project_name="Patterson Lake South",
    )
    base.update(overrides)
    return base


def test_render_lithology_passage_includes_hole_interval_and_rock(m):
    row = _lith_row()
    text = m._render_lithology_passage(row)
    assert "MAC-22-11" in text
    assert "142.3" in text and "145.6" in text
    assert "graphitic pelitic gneiss" in text
    assert "PG-GR-CHL" in text


def test_render_lithology_passage_includes_logger_and_date(m):
    row = _lith_row()
    text = m._render_lithology_passage(row)
    assert "J. Smith" in text
    assert "2024-08-15" in text


def test_render_lithology_passage_truncates_long_description(m):
    row = _lith_row(description="X" * 500)
    text = m._render_lithology_passage(row)
    # 280-char clamp + … marker per the template logic.
    assert "…" in text


def test_render_lithology_passage_skips_missing_attributes(m):
    """Empty attribute strings should not appear as 'colour None'."""
    row = _lith_row(colour=None, grain_size=None, texture=None,
                    weathering=None, hardness=None)
    text = m._render_lithology_passage(row)
    assert "None" not in text
    assert "Attributes" not in text  # The whole clause skipped.


# ---------------------------------------------------------------------------
# _render_collar_passage
# ---------------------------------------------------------------------------


def _collar_row(**overrides):
    base = dict(
        collar_id="b0000000-0000-0000-0000-000000000001",
        workspace_id="a0000000-0000-0000-0000-000000000001",
        hole_id="MAC-22-11",
        easting=612345.0,
        northing=5734567.0,
        elevation=415.0,
        total_depth=187.5,
        hole_type="surface diamond drill",
        drill_type="DDH",
        azimuth=45.0,
        dip=-75.0,
        drill_date="2024-08-15",
        status="completed",
        hole_status="completed",
        purpose="resource definition",
        driller="Boart Longyear",
        geologist="J. Smith",
        project_name="Patterson Lake South",
    )
    base.update(overrides)
    return base


def test_render_collar_passage_includes_orientation(m):
    row = _collar_row()
    text = m._render_collar_passage(row)
    assert "Azimuth 45.0°" in text
    assert "dip -75.0°" in text.lower()


def test_render_collar_passage_includes_coords(m):
    row = _collar_row()
    text = m._render_collar_passage(row)
    assert "612345" in text
    assert "5734567" in text
    assert "415" in text


def test_render_collar_passage_includes_crew(m):
    row = _collar_row()
    text = m._render_collar_passage(row)
    assert "Boart Longyear" in text
    assert "J. Smith" in text


def test_render_collar_passage_handles_missing_orientation(m):
    row = _collar_row(azimuth=None, dip=None)
    text = m._render_collar_passage(row)
    assert "Azimuth" not in text
    assert "Dip" not in text


# ---------------------------------------------------------------------------
# Module-level constants — pin contract for downstream consumers
# ---------------------------------------------------------------------------


def test_chunk_kind_constant_value(m):
    """The chunk_kind discriminator is part of the chat-pipeline filter
    contract. UI / training code reads this to separate prose chunks
    from structured-summary chunks. Pin the literal value."""
    assert m.CHUNK_KIND_STRUCTURED == "structured_summary"


def test_parser_used_constant_value(m):
    assert m.PARSER_USED == "structured_summary_v1"


def test_stub_synthesizers_raise_not_implemented(m):
    """The five stubs are tracked for follow-up PRs. Calling them
    should NotImplementedError loudly, not silently no-op."""
    for stub in (
        m.silver_samples_nl_summary_TODO,
        m.silver_structures_nl_summary_TODO,
        m.silver_las_curves_nl_summary_TODO,
        m.silver_review_queue_nl_summary_TODO,
        m.silver_public_geo_nl_summary_TODO,
    ):
        with pytest.raises(NotImplementedError):
            stub()
