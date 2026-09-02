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


def test_unset_and_tesseract_mean_the_local_engine(monkeypatch) -> None:
    monkeypatch.delenv("OCR_ENGINE", raising=False)
    assert ocr_engine.selected_engine() == "tesseract"

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
