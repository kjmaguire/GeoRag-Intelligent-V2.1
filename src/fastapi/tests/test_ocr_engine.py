"""OCR_ENGINE resolution — retired values are loud, never silent."""

from __future__ import annotations

import logging

import pytest

from app.services.ingest import ocr_engine


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(ocr_engine, "_WARNED", set())


@pytest.mark.parametrize("raw", ["cohere_parse", "Cohere_Parse", "  cohere_parse "])
def test_cohere_parse_is_selected_case_insensitively(monkeypatch, raw) -> None:
    monkeypatch.setenv("OCR_ENGINE", raw)

    assert ocr_engine.selected_engine() == "cohere_parse"


def test_unset_or_blank_means_cohere_parse(monkeypatch) -> None:
    """2026-10-04: the default is the hosted engine production runs (same rule
    as EMBEDDING_BACKEND). Fail-safe: a keyless worker falls back to Tesseract
    loudly in pdf_report, tested in test_pdf_parse_mode.py."""
    monkeypatch.delenv("OCR_ENGINE", raising=False)
    assert ocr_engine.selected_engine() == "cohere_parse"

    monkeypatch.setenv("OCR_ENGINE", "   ")
    assert ocr_engine.selected_engine() == "cohere_parse"


def test_tesseract_stays_selectable(monkeypatch) -> None:
    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    assert ocr_engine.selected_engine() == "tesseract"


@pytest.mark.parametrize("retired", sorted(ocr_engine.RETIRED_VALUES))
def test_a_retired_value_logs_critical_once_and_runs_tesseract(
    monkeypatch, caplog, retired
) -> None:
    monkeypatch.setenv("OCR_ENGINE", retired)

    with caplog.at_level(logging.CRITICAL, logger="georag.ingest.ocr_engine"):
        assert ocr_engine.selected_engine() == "tesseract"
        assert ocr_engine.selected_engine() == "tesseract"

    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1
    assert "ADR-0019" in critical[0].getMessage()
    assert "OCR_ENGINE=cohere_parse" in critical[0].getMessage()


def test_an_unknown_value_warns_once_and_runs_tesseract(monkeypatch, caplog) -> None:
    monkeypatch.setenv("OCR_ENGINE", "paddle")

    with caplog.at_level(logging.WARNING, logger="georag.ingest.ocr_engine"):
        assert ocr_engine.selected_engine() == "tesseract"
        assert ocr_engine.selected_engine() == "tesseract"

    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1


def test_unset_engine_without_a_key_runs_tesseract_with_one_critical_per_process(
    monkeypatch, caplog
) -> None:
    """The default flip is fail-safe: unset OCR_ENGINE selects Cohere Parse,
    but a worker with no COHERE_API_KEY must still OCR (Tesseract) and say so
    exactly once per process — not once per page."""
    import sys
    import types
    from unittest.mock import MagicMock, patch

    from app.services.ingest import pdf_report

    monkeypatch.delenv("OCR_ENGINE", raising=False)
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    monkeypatch.setattr(pdf_report, "_ENGINE_NOT_CONFIGURED_WARNED", False)

    fake_tess = types.ModuleType("pytesseract")
    fake_tess.Output = types.SimpleNamespace(DICT="dict")
    fake_tess.image_to_data = MagicMock(side_effect=RuntimeError("force text path"))
    fake_tess.image_to_string = MagicMock(return_value="Tesseract read this page")
    fake_pdf2image = types.ModuleType("pdf2image")
    fake_pdf2image.convert_from_path = MagicMock(return_value=[object()])
    monkeypatch.setitem(sys.modules, "pytesseract", fake_tess)
    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)

    with (
        patch.object(pdf_report, "_engine_single_page_request") as engine_call,
        patch.object(pdf_report, "_ocr_budget_take") as budget,
        patch.object(pdf_report, "_preprocess_image_for_ocr", side_effect=lambda im: im),
        patch.object(pdf_report, "_meter_ocr_page"),
        caplog.at_level(logging.CRITICAL),
    ):
        texts = [pdf_report._ocr_single_page("/nonexistent.pdf", n) for n in (1, 2, 3)]

    engine_call.assert_not_called()
    budget.assert_not_called()
    assert all("Tesseract read this page" in t for t in texts)
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1
    assert "COHERE_API_KEY" in critical[0].getMessage()
