"""Unit tests for the Azure Document Intelligence OCR adapter (#28).

Fully mocked — no real Azure resource, no network calls. This module is
not wired into the live ingest path, so these tests only need to prove
the adapter's own contract: config gating, credential handling, and the
(text, mean_confidence) shape it produces from a mocked SDK response.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ingest import document_intelligence_client as di


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(di.ENGINE_ENV, raising=False)
    monkeypatch.delenv(di.ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(di.KEY_ENV, raising=False)


class TestEngineSelection:
    def test_defaults_to_not_selected(self) -> None:
        assert di.is_engine_selected() is False

    def test_selected_when_env_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(di.ENGINE_ENV, "azure_document_intelligence")
        assert di.is_engine_selected() is True

    def test_not_selected_for_other_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(di.ENGINE_ENV, "tesseract")
        assert di.is_engine_selected() is False

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(di.ENGINE_ENV, "Azure_Document_Intelligence")
        assert di.is_engine_selected() is True


class TestIsConfigured:
    def test_false_when_absent(self) -> None:
        assert di.is_configured() is False

    def test_false_when_only_endpoint_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        assert di.is_configured() is False

    def test_true_when_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")
        assert di.is_configured() is True


class TestOcrPage:
    pytestmark = pytest.mark.asyncio

    async def test_raises_not_configured_without_credentials(self) -> None:
        with pytest.raises(di.DocumentIntelligenceNotConfigured):
            await di.ocr_page(b"%PDF-1.4 fake bytes", page_num=1)

    async def test_returns_empty_result_on_sdk_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.begin_analyze_document = AsyncMock(side_effect=RuntimeError("boom"))

        with patch.object(di, "_build_client", return_value=mock_client):
            result = await di.ocr_page(b"%PDF-1.4 fake bytes", page_num=1)

        assert result == di.PageOcrResult("", 0.0)

    async def test_extracts_text_and_mean_confidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")

        word_a = MagicMock(content="Patterson", confidence=0.98)
        word_b = MagicMock(content="Lake", confidence=0.92)
        page = MagicMock(words=[word_a, word_b])
        analyze_result = MagicMock(pages=[page])

        poller = AsyncMock()
        poller.result = AsyncMock(return_value=analyze_result)

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.begin_analyze_document = AsyncMock(return_value=poller)

        with patch.object(di, "_build_client", return_value=mock_client):
            result = await di.ocr_page(b"%PDF-1.4 fake bytes", page_num=3)

        assert result.text == "Patterson Lake"
        assert result.mean_confidence == pytest.approx((0.98 + 0.92) / 2)
        mock_client.begin_analyze_document.assert_awaited_once_with(
            "prebuilt-read", body=b"%PDF-1.4 fake bytes", pages="3",
        )

    async def test_empty_result_when_no_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")

        analyze_result = MagicMock(pages=[])
        poller = AsyncMock()
        poller.result = AsyncMock(return_value=analyze_result)

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.begin_analyze_document = AsyncMock(return_value=poller)

        with patch.object(di, "_build_client", return_value=mock_client):
            result = await di.ocr_page(b"%PDF-1.4 fake bytes", page_num=1)

        assert result == di.PageOcrResult("", 0.0)
