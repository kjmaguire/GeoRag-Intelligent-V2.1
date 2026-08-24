"""prebuilt-layout analyze options: markdown and high-resolution OCR (2026-08-20).

We have been paying for `prebuilt-layout` and requesting none of the
options that distinguish it from the cheaper `prebuilt-read`. Two are
turned on here:

  - `output_content_format=markdown` (default ON). Without it, page text is
    rebuilt from the `lines` collection — a flat list of visual lines with
    no notion of a heading, a paragraph boundary, or a table. With it, the
    document's semantic structure comes back: `#` headings, blank-line
    paragraph separation, `<table>` markup that survives merged cells,
    `<figure>` blocks that keep a chart's axis labels with its caption.

  - `features=[ocrHighResolution]` (default OFF). Billed per page as an
    add-on. It matters for small text on geological charts and
    hand-annotated drill logs, but it should be turned on deliberately
    after someone has looked at what it costs.

The interesting part is not the flags — it's that markdown output is
DOCUMENT-level (`result.content`) while this pipeline is PAGE-level.
Pages carry `spans` into that string; getting those wrong would silently
file one page's text under another's citation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ingest import document_intelligence_client as di


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
    monkeypatch.setenv(di.KEY_ENV, "fake-key")
    monkeypatch.delenv(di._MARKDOWN_ENV, raising=False)
    monkeypatch.delenv(di._HIGH_RESOLUTION_ENV, raising=False)


def _span(offset: int, length: int):
    return MagicMock(offset=offset, length=length)


def _page(number: int, spans, *, lines: list[str] | None = None):
    return MagicMock(
        page_number=number,
        spans=spans,
        words=[MagicMock(content="w", confidence=0.9, polygon=None)],
        lines=[MagicMock(content=text) for text in (lines or ["fallback line"])],
    )


def _client_returning(result):
    poller = AsyncMock()
    poller.result = AsyncMock(return_value=result)
    client = MagicMock()
    client.begin_analyze_document = AsyncMock(return_value=poller)
    return client


class TestOptionFlags:
    def test_markdown_defaults_on(self) -> None:
        assert di.markdown_enabled() is True

    def test_high_resolution_defaults_off(self) -> None:
        """It is a billed add-on; opting in must be a deliberate act."""
        assert di.high_resolution_enabled() is False

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
    def test_markdown_can_be_turned_off(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(di._MARKDOWN_ENV, value)
        assert di.markdown_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
    def test_high_resolution_can_be_turned_on(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(di._HIGH_RESOLUTION_ENV, value)
        assert di.high_resolution_enabled() is True

    def test_options_reach_the_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from azure.ai.documentintelligence.models import (
            DocumentAnalysisFeature,
            DocumentContentFormat,
        )

        monkeypatch.setenv(di._HIGH_RESOLUTION_ENV, "1")
        kwargs: dict = {"body": b"x"}
        di._apply_analyze_options(kwargs)

        assert kwargs["output_content_format"] == DocumentContentFormat.MARKDOWN
        assert kwargs["features"] == [DocumentAnalysisFeature.OCR_HIGH_RESOLUTION]

    def test_no_features_key_when_high_resolution_is_off(self) -> None:
        """Sending an empty features list is not the same as sending none."""
        kwargs: dict = {"body": b"x"}
        di._apply_analyze_options(kwargs)
        assert "features" not in kwargs


class TestMarkdownPerPageSlicing:
    pytestmark = pytest.mark.asyncio

    async def test_each_page_gets_its_own_fragment(self) -> None:
        content = "# Geology\n\nPage one prose.\n<!-- PageBreak -->\n## Assays\n\nPage two prose."
        one = content.index("# Geology")
        two = content.index("## Assays")
        result = MagicMock(
            content=content,
            pages=[
                _page(1, [_span(one, two - one)]),
                _page(2, [_span(two, len(content) - two)]),
            ],
            tables=[],
        )

        with patch.object(di, "_build_client", return_value=_client_returning(result)):
            per_page = await di.analyze_page_block(b"%PDF-1.4", 2)

        assert per_page[1].text.startswith("# Geology")
        assert "Page one prose." in per_page[1].text
        assert "Page two prose." not in per_page[1].text, (
            "page 1 must not carry page 2's text — that is a citation "
            "pointing at the wrong page"
        )
        assert per_page[2].text.startswith("## Assays")

    async def test_html_comment_metadata_is_stripped(self) -> None:
        """PageHeader/PageFooter/PageBreak comments are markup noise that
        drag down the alphabetic ratio ocr_quality scores."""
        content = '<!-- PageHeader="Cameco 2019" -->\n# Summary\n\nReal prose.\n<!-- PageBreak -->'
        result = MagicMock(
            content=content,
            pages=[_page(1, [_span(0, len(content))])],
            tables=[],
        )

        with patch.object(di, "_build_client", return_value=_client_returning(result)):
            page = await di.ocr_page(b"%PDF-1.4", page_num=1)

        assert "PageHeader" not in page.text
        assert "PageBreak" not in page.text
        assert "# Summary" in page.text
        assert "Real prose." in page.text

    async def test_headings_survive_into_the_page_text(self) -> None:
        """The whole point: `lines` had no way to say 'this is a heading'."""
        content = "# 1. Summary\n\nThe Patterson Lake property.\n\n## 1.1 Location"
        result = MagicMock(
            content=content,
            pages=[_page(1, [_span(0, len(content))], lines=["1. Summary"])],
            tables=[],
        )

        with patch.object(di, "_build_client", return_value=_client_returning(result)):
            page = await di.ocr_page(b"%PDF-1.4", page_num=1)

        assert "# 1. Summary" in page.text
        assert "## 1.1 Location" in page.text

    async def test_falls_back_to_lines_when_spans_are_unusable(self) -> None:
        """A malformed or mocked result must degrade, not blank the page."""
        result = MagicMock(
            content="some markdown",
            pages=[_page(1, [_span(-5, 0)], lines=["Real line one", "Real line two"])],
            tables=[],
        )

        with patch.object(di, "_build_client", return_value=_client_returning(result)):
            page = await di.ocr_page(b"%PDF-1.4", page_num=1)

        assert page.text == "Real line one\nReal line two"

    async def test_span_past_the_end_of_content_is_ignored(self) -> None:
        result = MagicMock(
            content="short",
            pages=[_page(1, [_span(9_999, 100)], lines=["Real line"])],
            tables=[],
        )

        with patch.object(di, "_build_client", return_value=_client_returning(result)):
            page = await di.ocr_page(b"%PDF-1.4", page_num=1)

        assert page.text == "Real line"

    async def test_markdown_off_uses_the_line_reconstruction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`result.content` is populated in TEXT mode too.

        Slicing it by span there would silently swap the deliberate
        newline-per-line shape (which pdf_report's MULTILINE section
        regexes depend on) for a run-together fragment — so the guard is
        on the FLAG, not on the attribute being present.
        """
        monkeypatch.setenv(di._MARKDOWN_ENV, "0")
        content = "1. Summary The Patterson Lake property."
        result = MagicMock(
            content=content,
            pages=[_page(1, [_span(0, len(content))], lines=["1. Summary", "The Patterson Lake property."])],
            tables=[],
        )
        client = _client_returning(result)

        with patch.object(di, "_build_client", return_value=client):
            page = await di.ocr_page(b"%PDF-1.4", page_num=1)

        assert page.text == "1. Summary\nThe Patterson Lake property."
        assert "output_content_format" not in client.begin_analyze_document.await_args.kwargs

    async def test_word_confidence_is_unaffected_by_markdown(self) -> None:
        """Confidence and polygons drive tiling and review routing; they
        come from `words`, which markdown mode does not touch."""
        content = "# Heading\n\nprose"
        page = _page(1, [_span(0, len(content))])
        page.words = [
            MagicMock(content="Patterson", confidence=0.98, polygon=None),
            MagicMock(content="Lake", confidence=0.92, polygon=None),
        ]
        result = MagicMock(content=content, pages=[page], tables=[])

        with patch.object(di, "_build_client", return_value=_client_returning(result)):
            got = await di.ocr_page(b"%PDF-1.4", page_num=1)

        assert got.mean_confidence == pytest.approx((0.98 + 0.92) / 2)
        assert got.detected_region_count == 2

    async def test_structured_tables_still_come_back_separately(self) -> None:
        """Markdown renders tables as HTML inline; the `tables` collection
        is what feeds the dedicated GFM table chunks. Both must survive."""
        content = "# Assays\n\n<table><tr><td>DDH-1</td><td>1.2</td></tr></table>"
        cells = [
            MagicMock(row_index=0, column_index=0, content="DDH-1"),
            MagicMock(row_index=0, column_index=1, content="1.2"),
            MagicMock(row_index=1, column_index=0, content="DDH-2"),
            MagicMock(row_index=1, column_index=1, content="0.4"),
        ]
        table = MagicMock(
            row_count=2, column_count=2, cells=cells,
            bounding_regions=[MagicMock(page_number=1)],
        )
        result = MagicMock(
            content=content,
            pages=[_page(1, [_span(0, len(content))])],
            tables=[table],
        )

        with patch.object(di, "_build_client", return_value=_client_returning(result)):
            page = await di.ocr_page(b"%PDF-1.4", page_num=1)

        assert page.tables == [[["DDH-1", "1.2"], ["DDH-2", "0.4"]]]
        assert "<table>" in page.text
