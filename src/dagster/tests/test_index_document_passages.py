"""Unit tests for index_document_passages — ADR-0010 Session A.

Pure-function tests around the payload builder + SQL shape. The
Qdrant + sentence-transformers + SPLADE++ paths are integration-
level and exercised by the live materialise in Session A close-out.
"""
from __future__ import annotations


def _import_module():
    """Lazy import so pytest collection doesn't fail when the dagster
    package isn't on sys.path (e.g. an accidental run from the FastAPI
    container where it isn't mounted)."""
    from georag_dagster.assets import index_document_passages as m
    return m


def _row(**overrides) -> dict:
    """Minimal valid passage row matching SELECT_PASSAGES_SQL output."""
    defaults = dict(
        passage_id="11111111-1111-1111-1111-111111111111",
        document_id="22222222-2222-2222-2222-222222222222",
        workspace_id="33333333-3333-3333-3333-333333333333",
        revision_number=1,
        text="The Patterson Lake South property hosts uranium mineralisation.",
        text_hash="a" * 64,
        ordinal=0,
        chunk_kind="narrative",
        page_first=3,
        page_last=3,
        bbox_x0=0.05,
        bbox_y0=0.10,
        bbox_x1=0.95,
        bbox_y1=0.30,
        parser_confidence=0.92,
        ocr_confidence=None,
        ocr_method=None,
        ocr_status="accepted",
        parent_chunk_id=None,
        commodity="uranium",
        project_name="Patterson Lake South",
        document_title="NI 43-101 Technical Report on PLS",
        # Plan §1c/§6c columns — None on default for the dominant
        # NI 43-101 path (workspace owns the content).
        document_type=None,
        authors=None,
        license=None,
        license_url=None,
        attribution_text=None,
        source_url=None,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def test_payload_carries_required_tenancy_keys():
    """workspace_id + document_id must be present — they're the GI-9
    isolation + per-document scope keys the search_documents tool
    filters on."""
    m = _import_module()
    payload = m._build_payload(_row(), payload_text="text content")
    for key in ("workspace_id", "document_id", "passage_id"):
        assert key in payload
        assert payload[key], f"{key} must be non-empty"


def test_payload_carries_citation_precision_fields():
    """§04i hallucination-prevention requires the chunk provenance
    (page, bbox, confidence) be available at retrieval time."""
    m = _import_module()
    payload = m._build_payload(_row(), payload_text="text")
    assert payload["page_first"] == 3
    assert payload["page_last"] == 3
    assert payload["bbox_x0"] == 0.05
    assert payload["bbox_y0"] == 0.10
    assert payload["bbox_x1"] == 0.95
    assert payload["bbox_y1"] == 0.30
    assert payload["parser_confidence"] == 0.92
    assert payload["text_hash"] == "a" * 64


def test_payload_carries_chunk_kind_and_ordinal():
    m = _import_module()
    payload = m._build_payload(_row(chunk_kind="paragraph", ordinal=7),
                                payload_text="text")
    assert payload["chunk_kind"] == "paragraph"
    assert payload["ordinal"] == 7


def test_payload_preserves_null_parent_chunk_id_for_root_chunks():
    """parent_chunk_id IS NULL is semantically 'root chunk', not
    'missing'. The search_documents tool checks for IS NULL on it for
    §3d parent-expansion. Must NOT be filtered to absence."""
    m = _import_module()
    payload = m._build_payload(_row(parent_chunk_id=None), payload_text="text")
    assert "parent_chunk_id" in payload
    assert payload["parent_chunk_id"] is None


def test_payload_handles_ocr_fields_when_set():
    """OCR-derived chunks carry ocr_method + ocr_confidence + ocr_status
    which downstream filters use to drop low-confidence rows."""
    m = _import_module()
    payload = m._build_payload(_row(
        ocr_confidence=0.65,
        ocr_method="docling_rapidocr",
        ocr_status="low_confidence",
    ), payload_text="text")
    assert payload["ocr_confidence"] == 0.65
    assert payload["ocr_method"] == "docling_rapidocr"
    assert payload["ocr_status"] == "low_confidence"


def test_payload_full_text_not_truncated_by_default():
    """ADR-0010 stipulates full text in the payload (vs index_reports
    which uses a 500-char snippet). The whole point of the canonical
    chunked-content corpus is downstream tools see the real content."""
    m = _import_module()
    long_text = "The Patterson Lake South property " * 200  # ~7000 chars
    payload = m._build_payload(_row(), payload_text=long_text)
    assert payload["text"] == long_text
    assert len(payload["text"]) > 5000


def test_payload_carries_legacy_index_reports_compat_keys():
    """Downstream readers that currently consume georag_reports payloads
    expect these keys (commodity, project_name, document_title,
    document_type). Carry them so the cutover doesn't require a
    parallel payload-shape branch in search_documents."""
    m = _import_module()
    payload = m._build_payload(_row(), payload_text="text")
    for key in ("commodity", "project_name", "document_title", "document_type"):
        assert key in payload
    assert payload["document_type"] == "NI43"


def test_payload_aliases_report_id_and_page_for_collection_swap():
    """ADR-0010 hard-flag-flip compat — the FastAPI search_documents tool +
    response_assembler + nightly integrity check all read `report_id` and
    `page` from the legacy georag_reports payload. The new payload must
    mirror document_id → report_id and page_first → page so the collection
    swap is a single env-flag flip with zero downstream payload mapping
    changes."""
    m = _import_module()
    payload = m._build_payload(_row(), payload_text="text")
    assert payload["report_id"] == payload["document_id"]
    assert payload["page"] == payload["page_first"]


def test_payload_indexed_at_is_iso_utc():
    m = _import_module()
    payload = m._build_payload(_row(), payload_text="text")
    assert "indexed_at" in payload
    # Trivially ensures we wrote an ISO string (not a datetime, which
    # Qdrant would reject).
    assert isinstance(payload["indexed_at"], str)
    assert "T" in payload["indexed_at"]


def test_payload_handles_null_bbox_gracefully():
    """A passage with NULL bbox (e.g. from raw OCR rows that didn't
    carry layout info) shouldn't crash payload construction; the keys
    should still be present so the search_documents schema stays
    uniform."""
    m = _import_module()
    payload = m._build_payload(
        _row(bbox_x0=None, bbox_y0=None, bbox_x1=None, bbox_y1=None),
        payload_text="text",
    )
    for key in ("bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1"):
        assert key in payload
        assert payload[key] is None


# ---------------------------------------------------------------------------
# SQL shape
# ---------------------------------------------------------------------------


def test_select_sql_reads_silver_document_passages():
    """ADR-0010 contract: the asset MUST read silver.document_passages
    as the canonical source. If a future edit accidentally swaps in
    silver.ingest_extractions or silver.reports.sections_text this
    test catches the regression."""
    m = _import_module()
    assert "FROM silver.document_passages p" in m.SELECT_PASSAGES_SQL
    # Negative assertions — these tables are explicitly NOT the source.
    assert "silver.ingest_extractions" not in m.SELECT_PASSAGES_SQL
    assert "sections_text" not in m.SELECT_PASSAGES_SQL


def test_select_sql_left_joins_silver_reports():
    """The carryover provenance fields (commodity, project_name,
    document_title) come from the FK join. Must be LEFT JOIN so a
    passage with a missing report row still indexes (don't drop
    rows just because their report row was deleted)."""
    m = _import_module()
    assert "LEFT JOIN silver.reports r" in m.SELECT_PASSAGES_SQL


def test_select_sql_projects_all_required_payload_columns():
    """Spot-check that every column the payload builder reads is in
    the SELECT list. Drift detection — a future edit that drops a
    column from SQL would crash the payload builder at runtime,
    this test catches it at unit-test time."""
    m = _import_module()
    required = [
        "passage_id", "document_id", "workspace_id", "revision_number",
        "text", "text_hash", "ordinal", "chunk_kind",
        "page_first", "page_last",
        "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1",
        "parser_confidence", "ocr_confidence", "ocr_method", "ocr_status",
        "parent_chunk_id",
    ]
    for col in required:
        assert col in m.SELECT_PASSAGES_SQL, (
            f"SQL is missing {col} — payload builder will crash at runtime"
        )


# ---------------------------------------------------------------------------
# Config + constants
# ---------------------------------------------------------------------------


def test_target_collection_is_georag_chunks():
    """ADR-0010 target collection name. Locked because downstream
    search_documents has a feature-flag fallback path that flips
    between collections — name must match exactly."""
    m = _import_module()
    assert m.QDRANT_COLLECTION == "georag_chunks"


def test_payload_keyword_indices_include_document_id_and_chunk_kind():
    """document_id index lets search filter to one report quickly;
    chunk_kind index lets downstream tools narrow to narrative-only
    or paragraph-children for §3d parent expansion."""
    m = _import_module()
    assert "workspace_id" in m._PAYLOAD_KEYWORD_INDICES
    assert "document_id" in m._PAYLOAD_KEYWORD_INDICES
    assert "chunk_kind" in m._PAYLOAD_KEYWORD_INDICES
    assert "parent_chunk_id" in m._PAYLOAD_KEYWORD_INDICES


def test_payload_integer_indices_include_page_first():
    """Page-range filtering (e.g. 'show me passages between p10 and
    p20') needs an integer index on page_first."""
    m = _import_module()
    assert "page_first" in m._PAYLOAD_INTEGER_INDICES


def test_payload_document_type_defaults_to_NI43_when_row_missing():
    """Plan §1c — legacy rows without a classifier-set report_type
    keep rendering as NI43 (the dominant document_type before the
    classifier shipped). Don't break the existing citation surface."""
    m = _import_module()
    payload = m._build_payload(_row(document_type=None), payload_text="text")
    assert payload["document_type"] == "NI43"


def test_payload_document_type_carries_classifier_value():
    """Once §1c classifier sets a value, it flows through to the
    payload. Used by §3b authority ranking + citation rendering."""
    m = _import_module()
    payload = m._build_payload(
        _row(document_type="TEXTBOOK_OER"), payload_text="text"
    )
    assert payload["document_type"] == "TEXTBOOK_OER"


def test_payload_oer_attribution_fields_carry_through():
    """Plan §6c — when silver.reports carries license / attribution
    columns (textbook OER content), the payload surfaces them so the
    chat citation renderer can embed the attribution line."""
    m = _import_module()
    payload = m._build_payload(_row(
        document_type="TEXTBOOK_OER",
        authors=["Steven Earle"],
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution_text="Physical Geology – 2nd Edition by Steven Earle, licensed under CC-BY 4.0",
        source_url="https://opentextbc.ca/physicalgeology2ed/",
    ), payload_text="text")
    assert payload["authors"] == ["Steven Earle"]
    assert payload["license"] == "CC-BY-4.0"
    assert payload["license_url"].startswith("https://creativecommons.org/")
    assert "Steven Earle" in payload["attribution_text"]
    assert payload["source_url"].startswith("https://")


def test_payload_oer_attribution_fields_are_null_on_legacy_rows():
    """The dominant NI 43-101 path has no licence to surface — the
    workspace owns the content. None on these keys means the chat
    citation renderer should NOT show an attribution line."""
    m = _import_module()
    payload = m._build_payload(_row(), payload_text="text")
    for key in ("authors", "license", "license_url",
                "attribution_text", "source_url"):
        assert payload.get(key) is None, (
            f"{key} should be None on a legacy NI 43-101 row"
        )


def test_select_sql_projects_license_and_attribution_columns():
    """ADR-0010 §6c — the SQL must project the new silver.reports
    columns so they flow through to the payload builder."""
    m = _import_module()
    for col in ("license", "license_url", "attribution_text",
                "source_url", "authors"):
        assert col in m.SELECT_PASSAGES_SQL, (
            f"SELECT_PASSAGES_SQL is missing column {col}"
        )


def test_embed_dimensions_matches_bge_small():
    """ADR-0008 picked bge-small-en-v1.5 which is 384-dim. If the
    EMBED_DIMENSIONS constant drifts away from 384 the Qdrant collection
    creation will produce an unusable index."""
    m = _import_module()
    assert m.EMBED_DIMENSIONS == 384
    assert "bge-small" in m.EMBED_MODEL_NAME.lower()
