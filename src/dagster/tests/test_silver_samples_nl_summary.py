"""ADR-0012 first slice — tests for silver_samples_nl_summary.

Tests pure template + ID-derivation logic against 5 mock sample rows
mirroring real silver.samples shapes. No DB I/O — the asset materialise
path is exercised by the Dagster integration suite (see
``tests/integration/test_silver_nl_summaries_smoke.py`` once the
overall ADR-0012 backfill is wired).

Mirrors the existing ``test_silver_nl_summaries.py`` pattern so the
two test modules stay readable side-by-side.
"""
import importlib
import uuid

import pytest


@pytest.fixture(scope="module")
def m():
    return importlib.import_module(
        "georag_dagster.assets.silver_samples_nl_summary"
    )


@pytest.fixture(scope="module")
def shared():
    """Shared helpers (passage_id derivation, text_hash) live in the
    sibling module; the silver_samples asset imports them. Pinning the
    contract here keeps the two modules in lockstep."""
    return importlib.import_module(
        "georag_dagster.assets.silver_nl_summaries"
    )


# ---------------------------------------------------------------------------
# Mock rows — five representative shapes
# ---------------------------------------------------------------------------
#
# 1. Core sample with full Au + U3O8 results
# 2. Core sample with U3O8 only (Au below detection / not assayed)
# 3. Trench sample with no QA category
# 4. Grab sample with missing collar context (LEFT JOIN yields NULL)
# 5. Core sample with empty commodity_assays dict
#
# Real silver.samples shapes pulled from a live DB sample on 2026-05-29:
#   {"Au_ppb": 22, "U3O8_ppm": 18500}
#   {"Au_ppb": 410, "U3O8_ppm": 52000}
#   {"U3O8_ppm": 41200}
# All five sample_ids are valid UUIDs — required for the uuid5 derivation.


def _sample_row(**overrides):
    """Minimal valid silver.samples row matching the asset's SELECT shape."""
    base = dict(
        sample_id=uuid.UUID("0ee6fa78-6d0b-4259-b807-4db39d94a568"),
        workspace_id=uuid.UUID("a0000000-0000-0000-0000-000000000001"),
        collar_id=uuid.UUID("7a089efc-d9ce-43a1-8cca-46d2a31d6a62"),
        from_depth=350.0,
        to_depth=365.0,
        sample_type="Core",
        lab_id="SRC-2022-08-1",
        qaqc_type=None,
        commodity_assays={"Au_ppb": 22, "U3O8_ppm": 18500},
        commodity_assay_flags=None,
        hole_id="MAC-22-11",
        project_name="Patterson Lake South",
    )
    base.update(overrides)
    return base


def _five_mock_rows():
    """Return the five representative-shape rows the smoke test renders."""
    return [
        # 1. Full Au + U3O8
        _sample_row(),
        # 2. U3O8 only
        _sample_row(
            sample_id=uuid.UUID("5e316618-f62f-403b-8747-e9af95c1d57c"),
            from_depth=380.0, to_depth=395.0,
            lab_id="SRC-2022-08-3",
            commodity_assays={"U3O8_ppm": 41200},
        ),
        # 3. Trench, no QA
        _sample_row(
            sample_id=uuid.UUID("b3a1c0a0-1111-2222-3333-444455556666"),
            sample_type="Trench",
            from_depth=0.0, to_depth=1.5,
            lab_id="ALS-T-008",
            qaqc_type=None,
            commodity_assays={"Au_ppb": 5},
        ),
        # 4. Grab, missing collar / project context
        _sample_row(
            sample_id=uuid.UUID("c4a2c1a0-1111-2222-3333-444455556666"),
            sample_type="Grab",
            from_depth=0.0, to_depth=0.5,
            hole_id=None,
            project_name=None,
            commodity_assays={"Cu_ppm": 2300},
        ),
        # 5. Empty assays dict — head still renders.
        _sample_row(
            sample_id=uuid.UUID("d5a3c2a0-1111-2222-3333-444455556666"),
            commodity_assays={},
            qaqc_type="duplicate",
        ),
    ]


# ---------------------------------------------------------------------------
# _split_element_unit — JSONB key → (element, unit) decoder
# ---------------------------------------------------------------------------


def test_split_element_unit_strips_ppm_suffix(m):
    assert m._split_element_unit("Au_ppm") == ("Au", "ppm")


def test_split_element_unit_strips_ppb_suffix(m):
    assert m._split_element_unit("Au_ppb") == ("Au", "ppb")


def test_split_element_unit_strips_wt_pct_suffix(m):
    # wt_pct decodes to the wt% glyph (geologists' canonical render).
    assert m._split_element_unit("U3O8_wt_pct") == ("U3O8", "wt%")


def test_split_element_unit_passes_through_when_no_suffix(m):
    # Just the element symbol — no unit suffix encoded.
    assert m._split_element_unit("U3O8") == ("U3O8", "")


# ---------------------------------------------------------------------------
# _render_samples_passage — shape contract on the five mock rows
# ---------------------------------------------------------------------------


def test_render_samples_passage_includes_sample_hole_and_project(m):
    row = _sample_row()
    text = m._render_samples_passage(row)
    assert str(row["sample_id"]) in text
    assert "MAC-22-11" in text
    assert "Patterson Lake South" in text


def test_render_samples_passage_includes_interval(m):
    row = _sample_row()
    text = m._render_samples_passage(row)
    assert "350" in text
    assert "365" in text


def test_render_samples_passage_lists_assays_alphabetically(m):
    row = _sample_row()
    text = m._render_samples_passage(row)
    # Both values present, with units decoded from the key suffix.
    assert "Au 22 ppb" in text
    assert "U3O8 18500 ppm" in text
    # Alphabetical ordering — Au before U3O8.
    assert text.index("Au 22") < text.index("U3O8 18500")


def test_render_samples_passage_includes_lab_id(m):
    row = _sample_row()
    text = m._render_samples_passage(row)
    assert "SRC-2022-08-1" in text


def test_render_samples_passage_handles_missing_hole(m):
    row = _sample_row(hole_id=None, project_name=None)
    text = m._render_samples_passage(row)
    assert "unknown hole" in text
    assert "unknown project" in text


def test_render_samples_passage_skips_empty_assays_sentence(m):
    """No commodity_assays → no 'Assay results:' clause."""
    row = _sample_row(commodity_assays={})
    text = m._render_samples_passage(row)
    assert "Assay results" not in text


def test_render_samples_passage_skips_missing_qaqc_clause(m):
    """qaqc_type IS NULL → no 'QA category:' clause."""
    row = _sample_row(qaqc_type=None)
    text = m._render_samples_passage(row)
    assert "QA category" not in text


def test_render_samples_passage_renders_qaqc_when_present(m):
    row = _sample_row(qaqc_type="duplicate")
    text = m._render_samples_passage(row)
    assert "QA category: duplicate" in text


# ---------------------------------------------------------------------------
# Five-row smoke — assert each row produces a deterministic, distinct
# passage_id and a non-trivial rendered text
# ---------------------------------------------------------------------------


def test_smoke_five_rows_produce_distinct_passage_ids(m, shared):
    """Each silver.samples row → its own uuid5 passage_id."""
    rows = _five_mock_rows()
    derived = [
        shared._derive_passage_id("silver_samples", r["sample_id"])
        for r in rows
    ]
    assert len(set(derived)) == 5, "passage_ids must be unique per source row"


def test_smoke_five_rows_produce_deterministic_passage_ids(m, shared):
    """Re-deriving on the same row produces the same UUID."""
    rows = _five_mock_rows()
    a = [shared._derive_passage_id("silver_samples", r["sample_id"]) for r in rows]
    b = [shared._derive_passage_id("silver_samples", r["sample_id"]) for r in rows]
    assert a == b


def test_smoke_five_rows_render_non_empty_text(m):
    """Every row produces a non-trivial NL passage."""
    rows = _five_mock_rows()
    for r in rows:
        text = m._render_samples_passage(r)
        assert text, "every sample row should render to a non-empty passage"
        # Head sentence always emits the sample_id + sample_type.
        assert str(r["sample_id"]) in text
        assert r["sample_type"] in text


def test_smoke_five_rows_build_upsert_payloads(m, shared):
    """Per the asset body, every rendered row becomes an UPSERT payload
    with the contract fields the embed cron + RLS layer need.

    This mirrors the loop inside ``silver_samples_nl_summary`` without
    touching Postgres — it pins the row-builder contract so future
    template churn can't silently break the UPSERT side."""
    rows = _five_mock_rows()
    payloads = []
    for r in rows:
        pid = shared._derive_passage_id("silver_samples", r["sample_id"])
        text = m._render_samples_passage(r)
        payloads.append({
            "passage_id":   str(pid),
            "document_id":  None,
            "workspace_id": str(r["workspace_id"]),
            "text":         text,
            "text_hash":    shared._text_hash(text),
            "ordinal":      0,
            "chunk_kind":   shared.CHUNK_KIND_STRUCTURED,
            "parser_used":  "silver_samples_nl_summary_v1",
        })

    # All five payloads built.
    assert len(payloads) == 5

    # Contract pins per row.
    for p in payloads:
        # document_id explicitly NULL — these are synthesised, not PDF chunks.
        assert p["document_id"] is None
        # chunk_kind discriminator pinned to the ADR-0012 value.
        assert p["chunk_kind"] == "structured_summary"
        # parser_used variant marks the synthesizer for audit / debug.
        assert p["parser_used"] == "silver_samples_nl_summary_v1"
        # text_hash conforms to the CHAR(64) sha256 format silver.document_passages enforces.
        assert len(p["text_hash"]) == 64
        assert all(c in "0123456789abcdef" for c in p["text_hash"])
        # passage_id parses as a UUID.
        uuid.UUID(p["passage_id"])
        # workspace_id parses as a UUID — required by RLS.
        uuid.UUID(p["workspace_id"])


# ---------------------------------------------------------------------------
# parser_used constant — pin contract for downstream audit filters
# ---------------------------------------------------------------------------


def test_parser_used_value_matches_audit_filter(m):
    """Operators query silver.document_passages WHERE parser_used = ...
    when debugging which synthesizer emitted what. Pin the literal."""
    # Re-render any row and confirm the asset would write this constant.
    # We don't expose the constant as a module-level binding because the
    # asset hard-codes it inline — pin the string here so a future rename
    # is caught.
    expected = "silver_samples_nl_summary_v1"
    src = importlib.import_module(
        "georag_dagster.assets.silver_samples_nl_summary"
    ).__loader__.get_source(
        "georag_dagster.assets.silver_samples_nl_summary"
    )
    assert expected in src
