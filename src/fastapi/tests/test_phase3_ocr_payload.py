"""Phase 3 (2026-05-22) — OCR provenance plumbing through ingest_pdf
and the qdrant payload.

These tests verify:
  - _run_parser_subprocess section dicts include ocr_confidence + ocr_method
  - INSERT_PASSAGE_SQL has both new columns + 9-parameter binding
  - ParseOut.sections shape carries the two fields
  - DocumentChunk on the agent side has ocr_confidence + ocr_method
  - passage_embedder builds qdrant payload with both fields

Run with:
    pytest src/fastapi/tests/test_phase3_ocr_payload.py -v
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# Same dagster-parsers stub injection as Phase 1 persist tests, so the
# fastapi ingest module can import _FIGURE_TEMPDIR_ROOT without the real
# dagster install. The parser-side tests own the real implementation.
def _ensure_parser_stub_module():
    if "georag_dagster.parsers.pdf_report" in sys.modules:
        return
    pkg_root = sys.modules.get("georag_dagster") or types.ModuleType("georag_dagster")
    pkg_parsers = types.ModuleType("georag_dagster.parsers")
    mod = types.ModuleType("georag_dagster.parsers.pdf_report")
    mod._FIGURE_TEMPDIR_ROOT = "/tmp/georag_figures"

    def _figure_tempdir(sha256: str) -> str:
        import os as _os
        d = f"{mod._FIGURE_TEMPDIR_ROOT}/{sha256}"
        _os.makedirs(d, exist_ok=True)
        return d

    mod._figure_tempdir = _figure_tempdir
    mod.parse_pdf_report = MagicMock()
    pkg_parsers.pdf_report = mod
    pkg_root.parsers = pkg_parsers
    sys.modules["georag_dagster"] = pkg_root
    sys.modules["georag_dagster.parsers"] = pkg_parsers
    sys.modules["georag_dagster.parsers.pdf_report"] = mod


_ensure_parser_stub_module()


# ---------------------------------------------------------------------------
# 1. _run_parser_subprocess section dicts include ocr_confidence + ocr_method
# ---------------------------------------------------------------------------

def test_run_parser_subprocess_includes_ocr_fields():
    from app.hatchet_workflows import ingest_pdf as mod

    stub = MagicMock()
    for attr in (
        "title", "company", "filing_date", "commodity", "project_name",
        "region",
    ):
        setattr(stub, attr, None)
    stub.authors = []
    stub.parse_quality_pct = 0.0
    stub.parser_used = "fitz+tesseract_fallback"
    stub.skipped_elements = 0
    stub.warnings = []
    stub.page_languages = ["en"]
    stub.resource_tables = []
    stub.is_scanned = False
    stub.figure_manifest = []

    fitz_section = MagicMock()
    fitz_section.section_number = "1"
    fitz_section.section_title = "Summary"
    fitz_section.text = "Section text on a text-layer page."
    fitz_section.page_first = 1
    fitz_section.page_last = 1
    fitz_section.ocr_confidence = None
    fitz_section.ocr_method = "fitz_native"

    ocr_section = MagicMock()
    ocr_section.section_number = "2"
    ocr_section.section_title = "Drill Log"
    ocr_section.text = "OCR'd image page text"
    ocr_section.page_first = 5
    ocr_section.page_last = 6
    ocr_section.ocr_confidence = 0.78
    ocr_section.ocr_method = "tesseract"

    stub.sections = [fitz_section, ocr_section]

    with patch.object(
        sys.modules["georag_dagster.parsers.pdf_report"],
        "parse_pdf_report",
        MagicMock(return_value=stub),
    ):
        out = mod._run_parser_subprocess(b"%PDF-1.4 fake", sha256="aa" * 32)

    assert len(out["sections"]) == 2
    s0, s1 = out["sections"]
    assert s0["ocr_confidence"] is None
    assert s0["ocr_method"] == "fitz_native"
    assert s1["ocr_confidence"] == 0.78
    assert s1["ocr_method"] == "tesseract"


# ---------------------------------------------------------------------------
# 2. INSERT_PASSAGE_SQL has the two new columns
# ---------------------------------------------------------------------------

def test_insert_passage_sql_includes_ocr_columns():
    from app.hatchet_workflows import ingest_pdf as mod
    sql = mod.INSERT_PASSAGE_SQL
    assert "ocr_confidence" in sql
    assert "ocr_method" in sql
    # 9 binds: document_id, workspace, text, hash, ordinal, page_first,
    # page_last, ocr_confidence, ocr_method
    assert "$9" in sql
    assert "$10" not in sql  # don't accidentally over-bind


# ---------------------------------------------------------------------------
# 3. INSERT_PASSAGE_SQL ON CONFLICT preserves existing OCR provenance
# ---------------------------------------------------------------------------

def test_insert_passage_sql_on_conflict_preserves_existing_ocr():
    from app.hatchet_workflows import ingest_pdf as mod
    sql = mod.INSERT_PASSAGE_SQL
    # Both fields should be wrapped in COALESCE(existing, new) on conflict
    assert "COALESCE(silver.document_passages.ocr_confidence, EXCLUDED.ocr_confidence)" in sql
    assert "COALESCE(silver.document_passages.ocr_method,     EXCLUDED.ocr_method)" in sql


# ---------------------------------------------------------------------------
# 4. ParseOut model — sections list is permissive (extra keys OK)
# ---------------------------------------------------------------------------

def test_parseout_accepts_ocr_section_fields():
    """ParseOut.sections is typed list[dict]; the new keys must survive
    Pydantic serialization unchanged."""
    from app.hatchet_workflows.ingest_pdf import ParseOut

    p = ParseOut(
        sha256="abc",
        sections=[
            {
                "section_number": "1",
                "section_title": "X",
                "text": "y",
                "page_first": 1,
                "page_last": 1,
                "ocr_confidence": 0.82,
                "ocr_method": "tesseract",
            }
        ],
    )
    assert p.sections[0]["ocr_confidence"] == 0.82
    assert p.sections[0]["ocr_method"] == "tesseract"


# ---------------------------------------------------------------------------
# 5. DocumentChunk has ocr_confidence + ocr_method with default None
# ---------------------------------------------------------------------------

def test_document_chunk_has_ocr_fields_default_none():
    from app.agent.tools import DocumentChunk

    c = DocumentChunk(
        chunk_id="cid",
        text="t",
        source_document_id="d",
        document_title="title",
        section_number=None,
        section_title=None,
        section=None,
        page=None,
        document_type="NI43",
        report_id="r",
        relevance_score=0.5,
    )
    assert hasattr(c, "ocr_confidence")
    assert hasattr(c, "ocr_method")
    assert c.ocr_confidence is None
    assert c.ocr_method is None


def test_document_chunk_accepts_ocr_fields():
    from app.agent.tools import DocumentChunk

    c = DocumentChunk(
        chunk_id="cid", text="t",
        source_document_id="d", document_title="title",
        section_number=None, section_title=None, section=None,
        page=None, document_type="NI43", report_id="r",
        relevance_score=0.5,
        ocr_confidence=0.65, ocr_method="docling_rapidocr",
    )
    assert c.ocr_confidence == 0.65
    assert c.ocr_method == "docling_rapidocr"


# ---------------------------------------------------------------------------
# 6. passage_embedder SELECT mentions both new columns
# ---------------------------------------------------------------------------

def test_passage_embedder_query_selects_ocr_columns():
    import inspect
    from app.services.ingest import passage_embedder

    src = inspect.getsource(passage_embedder)
    assert "dp.ocr_confidence" in src
    assert "dp.ocr_method" in src


# ---------------------------------------------------------------------------
# 7. passage_embedder payload dict includes the two new keys
# ---------------------------------------------------------------------------

def test_passage_embedder_payload_includes_ocr_fields():
    import inspect
    from app.services.ingest import passage_embedder

    src = inspect.getsource(passage_embedder)
    # Match both lines in the payload dict literal
    assert '"ocr_confidence":' in src
    assert '"ocr_method":' in src
