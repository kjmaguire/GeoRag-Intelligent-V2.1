"""ADR-0010 Session B regression pin.

The reranker_labels asset graph MUST read its chunk population from
silver.document_passages — the canonical chunked-content corpus per
ADR-0010. If a future edit accidentally re-introduces a query against
silver.ingest_extractions or silver.ingest_ocr_results (the abandoned
raw-extraction layer) or against silver.reports.sections_text (the
legacy 7% of reports), the resulting label dataset would be 47× smaller
than the real corpus and the §5e reranker LoRA training would produce
an unusably narrow model.

These tests pin the SQL surface so the regression is caught at unit-
test time rather than after a multi-hour materialise.
"""
from __future__ import annotations


def _import_module():
    """Lazy import — keeps test collection cheap when dagster isn't on path."""
    from georag_dagster.assets import reranker_labels as m
    return m


# ---------------------------------------------------------------------------
# SQL targets
# ---------------------------------------------------------------------------


def test_chunk_population_sql_reads_silver_document_passages():
    """Positive pin — the population SQL MUST select FROM silver.document_passages."""
    m = _import_module()
    assert "FROM silver.document_passages" in m._FETCH_DOCUMENT_PASSAGES_SQL


def test_chunk_population_sql_does_not_read_legacy_ingest_tables():
    """Negative pin — the abandoned raw-extraction tables are explicitly NOT
    the source. If either appears in the SQL the cutover regressed."""
    m = _import_module()
    assert "silver.ingest_extractions" not in m._FETCH_DOCUMENT_PASSAGES_SQL
    assert "silver.ingest_ocr_results" not in m._FETCH_DOCUMENT_PASSAGES_SQL
    assert "silver.ingest_layouts" not in m._FETCH_DOCUMENT_PASSAGES_SQL


def test_chunk_population_sql_does_not_read_sections_text():
    """The legacy sections_text JSONB column is the OTHER corpus we
    deprecated. Reading it would pull only the 86 reports that have
    pre-§04p content — the opposite of the ADR-0010 outcome."""
    m = _import_module()
    assert "sections_text" not in m._FETCH_DOCUMENT_PASSAGES_SQL


# ---------------------------------------------------------------------------
# Field mapping (ADR-0010 §Session B)
# ---------------------------------------------------------------------------


def test_chunk_population_sql_maps_passage_id_to_chunk_id():
    """passage_id → chunk_id. Used directly as the Qdrant point_id so
    upserts are idempotent at the chunk level."""
    m = _import_module()
    assert "p.passage_id::text" in m._FETCH_DOCUMENT_PASSAGES_SQL
    assert "AS chunk_id" in m._FETCH_DOCUMENT_PASSAGES_SQL


def test_chunk_population_sql_maps_document_id_to_report_id():
    """document_id → report_id — same UUID, preserved for downstream
    code paths (training script, persist step) that key on report_id."""
    m = _import_module()
    assert "p.document_id::text" in m._FETCH_DOCUMENT_PASSAGES_SQL
    assert "AS report_id" in m._FETCH_DOCUMENT_PASSAGES_SQL


def test_chunk_population_sql_maps_page_first_to_page():
    """page_first → page. Passages that span multiple pages anchor to
    the first page; bbox stays anchored to that page's region."""
    m = _import_module()
    assert "p.page_first" in m._FETCH_DOCUMENT_PASSAGES_SQL
    assert "AS page" in m._FETCH_DOCUMENT_PASSAGES_SQL


def test_chunk_population_sql_maps_parser_confidence_to_extraction_confidence():
    """parser_confidence → extraction_confidence — column rename in flight
    so the persist step's record schema doesn't have to change."""
    m = _import_module()
    assert "p.parser_confidence" in m._FETCH_DOCUMENT_PASSAGES_SQL
    assert "AS extraction_confidence" in m._FETCH_DOCUMENT_PASSAGES_SQL


def test_chunk_population_sql_synthesises_bbox_array_from_components():
    """bbox_x0/y0/x1/y1 → [x0,y0,x1,y1]. The downstream persist step
    writes a single `bbox` array per row (training script expects [4] floats)."""
    m = _import_module()
    sql = m._FETCH_DOCUMENT_PASSAGES_SQL
    assert "ARRAY[" in sql and "bbox_x0" in sql and "bbox_y0" in sql
    assert "bbox_x1" in sql and "bbox_y1" in sql
    assert "AS bbox" in sql


# ---------------------------------------------------------------------------
# chunk_kind → source_method_bucket derivation
# ---------------------------------------------------------------------------


def test_chunk_kind_table_maps_to_table_extract_bucket():
    m = _import_module()
    assert m._chunk_kind_to_source_bucket("table", None) == "table-extract"


def test_chunk_kind_narrative_maps_to_text_bucket():
    m = _import_module()
    assert m._chunk_kind_to_source_bucket("narrative", None) == "text"


def test_chunk_kind_section_maps_to_text_bucket():
    m = _import_module()
    assert m._chunk_kind_to_source_bucket("section", None) == "text"


def test_chunk_kind_paragraph_maps_to_text_bucket():
    m = _import_module()
    assert m._chunk_kind_to_source_bucket("paragraph", None) == "text"


def test_unknown_chunk_kind_falls_back_to_text_bucket():
    """Per ADR-0010: fallback to 'text'. Covers caption_figure /
    character_window / NULL — anything that isn't a table."""
    m = _import_module()
    for kind in ("caption_figure", "character_window", None, "novel_kind"):
        assert m._chunk_kind_to_source_bucket(kind, None) == "text"


def test_ocr_method_present_routes_to_ocr_bucket_regardless_of_chunk_kind():
    """OCR provenance overrides chunk_kind so the strata distribution
    keeps an 'ocr' bucket populated even though chunk_kind itself doesn't
    encode OCR-ness. Keeps the 9-stratum cross from collapsing."""
    m = _import_module()
    assert m._chunk_kind_to_source_bucket("narrative", "docling_rapidocr") == "ocr"
    assert m._chunk_kind_to_source_bucket("table", "paddleocr_pp_ocrv5") == "ocr"
    assert m._chunk_kind_to_source_bucket(None, "paddleocr_pp_structure_v3") == "ocr"


# ---------------------------------------------------------------------------
# Qdrant collection target
# ---------------------------------------------------------------------------


def test_qdrant_collection_is_georag_chunks_post_cutover():
    """ADR-0010 — hard-negative mining queries the new canonical
    collection. If a future edit reverts to 'georag_reports' the
    mining step would key chunk_ids against a stale point space."""
    m = _import_module()
    assert m.QDRANT_COLLECTION == "georag_chunks"


def test_dead_qdrant_to_page_sql_was_removed():
    """The legacy _QDRANT_TO_PAGE_SQL helper round-tripped report_id
    through silver.ingest_extractions because the old georag_reports
    payload didn't carry `page`. The new georag_chunks payload carries
    page directly (via page_first alias), so the helper is gone. If a
    future edit re-introduces it under that name, this test catches it."""
    m = _import_module()
    assert not hasattr(m, "_QDRANT_TO_PAGE_SQL")


# ---------------------------------------------------------------------------
# Asset graph wiring
# ---------------------------------------------------------------------------


def test_mined_negatives_depends_on_index_document_passages():
    """The mining step must depend on the NEW index asset so a fresh
    materialise produces points in georag_chunks before the mining
    step queries them. Dependency on the old index_reports would mean
    georag_chunks could be empty when mining runs."""
    m = _import_module()
    # Dagster AssetsDefinition exposes upstream deps via dependency_keys
    # (frozenset of AssetKey). Stringify and substring-check; the full
    # AssetKey string for a top-level asset key is 'AssetKey([\"name\"])'.
    dep_str = " ".join(str(k) for k in m.reranker_mined_negatives.dependency_keys)
    assert "index_document_passages" in dep_str, (
        f"reranker_mined_negatives must dep on index_document_passages; "
        f"got dependency_keys={m.reranker_mined_negatives.dependency_keys}"
    )
    # Negative pin: the legacy index_reports dep must be gone — otherwise
    # the asset graph still triggers index_reports materialisations and
    # the cutover doesn't actually retire the legacy index path.
    assert "index_reports" not in dep_str
