"""Unit tests for the Azure Document Intelligence OCR adapter (#28).

Fully mocked — no real Azure resource, no network calls. These tests prove
config gating, credential handling, and the word-level
text/confidence/polygon shape used by tiled reconstruction.
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

    async def test_returns_empty_result_on_sdk_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.begin_analyze_document = AsyncMock(side_effect=RuntimeError("boom"))

        with patch.object(di, "_build_client", return_value=mock_client):
            result = await di.ocr_page(b"%PDF-1.4 fake bytes", page_num=1)

        assert result.text == ""
        assert result.request_succeeded is False
        assert result.error == "boom"

    async def test_extracts_text_and_mean_confidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")

        point_a = MagicMock(x=10, y=20)
        point_b = MagicMock(x=30, y=20)
        point_c = MagicMock(x=30, y=40)
        point_d = MagicMock(x=10, y=40)
        word_a = MagicMock(
            content="Patterson",
            confidence=0.98,
            polygon=[point_a, point_b, point_c, point_d],
        )
        word_b = MagicMock(content="Lake", confidence=0.92, polygon=None)
        page = MagicMock(words=[word_a, word_b], lines=[MagicMock()])
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
        assert result.request_succeeded is True
        assert result.mean_confidence == pytest.approx((0.98 + 0.92) / 2)
        assert result.words[0].polygon == (
            10.0,
            20.0,
            30.0,
            20.0,
            30.0,
            40.0,
            10.0,
            40.0,
        )
        assert result.detected_region_count == 2
        # prebuilt-layout is the default model since scanned-table support
        # (2026-08-11); AZURE_DI_MODEL_ID is the prebuilt-read escape hatch.
        mock_client.begin_analyze_document.assert_awaited_once_with(
            "prebuilt-layout",
            body=b"%PDF-1.4 fake bytes",
            pages="3",
        )

    async def test_reconstructs_text_from_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """2026-08-14: page text is rebuilt from DI's `lines` collection
        (joined with newlines) so pdf_report's MULTILINE section-heading
        regexes can match on scanned docs; the word stream still drives
        confidence, and the word join stays as the no-lines fallback."""
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")

        word_a = MagicMock(content="1.", confidence=0.9, polygon=None)
        word_b = MagicMock(content="Summary", confidence=0.8, polygon=None)
        line_1 = MagicMock(content="1. Summary")
        line_2 = MagicMock(content="The Patterson Lake property")
        page = MagicMock(words=[word_a, word_b], lines=[line_1, line_2])
        analyze_result = MagicMock(pages=[page])

        poller = AsyncMock()
        poller.result = AsyncMock(return_value=analyze_result)
        mock_client = MagicMock()
        mock_client.begin_analyze_document = AsyncMock(return_value=poller)

        with patch.object(di, "_build_client", return_value=mock_client):
            result = await di.ocr_page(b"%PDF-1.4 fake bytes", page_num=1)

        assert result.text == "1. Summary\nThe Patterson Lake property"
        assert result.mean_confidence == pytest.approx((0.9 + 0.8) / 2)
        assert result.detected_region_count == 2

    async def test_empty_result_when_no_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

    async def test_ocr_image_omits_pdf_page_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")

        point = MagicMock(x=1, y=2)
        word = MagicMock(
            content="Tile",
            confidence=0.99,
            polygon=[point, point],
        )
        analyze_result = MagicMock(pages=[MagicMock(words=[word], lines=[])])
        poller = AsyncMock()
        poller.result = AsyncMock(return_value=analyze_result)
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.begin_analyze_document = AsyncMock(return_value=poller)

        with patch.object(di, "_build_client", return_value=mock_client):
            result = await di.ocr_image(b"\x89PNG fake")

        assert result.text == "Tile"
        mock_client.begin_analyze_document.assert_awaited_once_with(
            "prebuilt-layout",
            body=b"\x89PNG fake",
        )

    async def test_extracts_layout_tables_as_grids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scanned-table support (2026-08-11): prebuilt-layout `tables` become
        row-major grids; span content lands in the anchor cell only."""
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")

        def _cell(row, col, content):
            return MagicMock(row_index=row, column_index=col, content=content)

        table = MagicMock(
            row_count=2,
            column_count=3,
            cells=[
                # Header cell spanning cols 0-1: SDK emits ONE cell at the
                # anchor (0, 0) with column_span=2 — no cell exists at (0, 1).
                MagicMock(row_index=0, column_index=0, content="Hole ID ", column_span=2),
                _cell(0, 2, "Au g/t"),
                _cell(1, 0, "MAD-22-001"),
                _cell(1, 1, "120.5"),
                _cell(1, 2, " 7.2"),
            ],
        )
        analyze_result = MagicMock(pages=[], tables=[table])
        poller = AsyncMock()
        poller.result = AsyncMock(return_value=analyze_result)
        mock_client = MagicMock()
        mock_client.begin_analyze_document = AsyncMock(return_value=poller)

        with patch.object(di, "_build_client", return_value=mock_client):
            result = await di.ocr_page(b"%PDF-1.4 fake bytes", page_num=1)

        assert result.tables == [
            [
                ["Hole ID", "", "Au g/t"],
                ["MAD-22-001", "120.5", "7.2"],
            ]
        ]
