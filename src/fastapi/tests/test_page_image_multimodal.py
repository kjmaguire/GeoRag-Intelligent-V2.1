"""Unit tests for multimodal page-image indexing (Cohere Embed v4, 2026-08-18).

Fully offline — no Azure, no object storage, no network. The three things
under test are the ones that fail expensively in production:

  1. The 2M-pixel cap. Every other render path in this repo targets OCR
     resolution (250 DPI), which is ~3x over the limit. If `dpi_for_page`
     regresses, every image embed 400s and the failure looks like a
     credentials problem.
  2. Scope selection. `IMAGE_EMBED_PAGE_SCOPE` decides whether ingestion
     issues one embed call per document or one per page — a ~50x cost and
     latency difference.
  3. The image wire-shape fallback, which exists because Cohere ships two
     accepted request shapes for v4 and we cannot tell from here which one
     this deployment takes.
"""

from __future__ import annotations

import pytest

from app.services.ingest import page_image


# ---------------------------------------------------------------------------
# 1. Pixel cap
# ---------------------------------------------------------------------------
# Page sizes in PDF points (72 pt/inch).
_US_LETTER = (612.0, 792.0)
_A4 = (595.3, 841.9)
_A0_PLAN = (2383.9, 3370.4)      # mine plan sheet
_TABLOID = (792.0, 1224.0)
_BUSINESS_CARD = (252.0, 144.0)


class TestPixelCap:
    @pytest.mark.parametrize(
        "size",
        [_US_LETTER, _A4, _A0_PLAN, _TABLOID, _BUSINESS_CARD],
        ids=["letter", "a4", "a0_plan", "tabloid", "business_card"],
    )
    def test_derived_dpi_never_exceeds_the_model_cap(self, size) -> None:
        """The whole point of deriving DPI per page: A0 and Letter both fit."""
        width_pt, height_pt = size
        dpi = page_image.dpi_for_page(width_pt, height_pt)

        px = (width_pt / 72.0 * dpi) * (height_pt / 72.0 * dpi)
        assert px <= page_image.EMBED_V4_MAX_PIXELS, (
            f"{size} at {dpi:.1f} DPI renders {px:.0f} px, over the "
            f"{page_image.EMBED_V4_MAX_PIXELS} cap"
        )

    def test_the_ocr_dpi_this_repo_uses_elsewhere_would_have_been_rejected(self) -> None:
        """Guards the reason this module exists.

        250 DPI is what _ocr_single_page and _ocr_tiled_pdf_page rasterise
        at. Reusing that here is the obvious "simplification" a future
        reader might make, and it silently breaks every image embed.
        """
        width_pt, height_pt = _US_LETTER
        px_at_250 = (width_pt / 72.0 * 250) * (height_pt / 72.0 * 250)
        assert px_at_250 > page_image.EMBED_V4_MAX_PIXELS

        assert page_image.dpi_for_page(width_pt, height_pt) < 250

    def test_small_pages_do_not_upscale_to_fill_the_budget(self) -> None:
        dpi = page_image.dpi_for_page(*_BUSINESS_CARD)
        assert dpi <= 200.0

    def test_degenerate_page_box_does_not_divide_by_zero(self) -> None:
        assert page_image.dpi_for_page(0.0, 0.0) > 0
        assert page_image.dpi_for_page(-1.0, 100.0) > 0


# ---------------------------------------------------------------------------
# 2. Scope selection
# ---------------------------------------------------------------------------
class TestScope:
    def test_defaults_to_all_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("IMAGE_EMBED_PAGE_SCOPE", raising=False)
        assert page_image.image_embed_scope() == "all"
        assert page_image.should_embed_page(7, text_pages={7}) is True

    def test_figures_scope_skips_pages_with_a_real_text_layer(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "figures")
        assert page_image.should_embed_page(7, text_pages={7}) is False
        assert page_image.should_embed_page(8, text_pages={7}) is True

    def test_off_disables_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "off")
        assert page_image.should_embed_page(1, text_pages=set()) is False

    def test_unknown_scope_falls_back_to_all_rather_than_crashing_ingest(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "sometimes")
        assert page_image.image_embed_scope() == "all"


class TestTextPageDetection:
    def test_ocr_pages_are_not_counted_as_text_pages(self) -> None:
        """A page that needed OCR is exactly the page worth embedding.

        document_intelligence / tesseract pages are maps, plates and scanned
        inserts — the picture carries what the text does not.
        """
        sections = [
            {"page_first": 1, "page_last": 2, "ocr_method": "fitz_native"},
            {"page_first": 3, "page_last": 3, "ocr_method": "document_intelligence"},
            {"page_first": 4, "page_last": 4, "ocr_method": "tesseract"},
            {"page_first": 5, "page_last": 5, "ocr_method": "pdfplumber_native"},
        ]
        assert page_image.text_pages_from_sections(sections) == {1, 2, 5}

    def test_missing_ocr_method_is_treated_as_native_text(self) -> None:
        # Sections predating OCR provenance carry no ocr_method; they came
        # from the text layer.
        assert page_image.text_pages_from_sections(
            [{"page_first": 9, "page_last": 9}]
        ) == {9}

    def test_handles_empty_and_malformed_sections(self) -> None:
        assert page_image.text_pages_from_sections([]) == set()
        assert page_image.text_pages_from_sections(
            [{"page_first": None, "ocr_method": "fitz_native"}]
        ) == set()


class TestPlaceholderText:
    def test_placeholder_is_unique_per_page(self) -> None:
        """UNIQUE (document_id, revision_number, text_hash) depends on this.

        If two image pages produced identical text, the second row would be
        swallowed by ON CONFLICT and that page would never be indexed.
        """
        a = page_image.placeholder_text(3, "Madsen Technical Report")
        b = page_image.placeholder_text(4, "Madsen Technical Report")
        assert a != b

    def test_placeholder_reads_honestly_in_a_citation(self) -> None:
        text = page_image.placeholder_text(12, "PureGold NI 43-101")
        assert "12" in text
        assert "page image" in text.lower()

    def test_missing_title_does_not_produce_dangling_of(self) -> None:
        assert page_image.placeholder_text(1, None).count(" of ") == 0
        assert page_image.placeholder_text(1, "  ").count(" of ") == 0


# ---------------------------------------------------------------------------
# 3. Image wire-shape negotiation
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class TestImageWireShapeFallback:
    """Cohere accepts either `images: [...]` or `inputs: [...]` for v4.

    Which one a given Foundry build takes is undocumented, so embed_image
    tries one and falls back on a schema rejection. These tests pin that
    behaviour without a live endpoint.
    """

    @pytest.fixture(autouse=True)
    def _reset_shape_cache(self):
        from app.services.embedding import _FoundryEmbedding

        original = _FoundryEmbedding._IMAGE_WIRE_SHAPE
        _FoundryEmbedding._IMAGE_WIRE_SHAPE = None
        yield
        _FoundryEmbedding._IMAGE_WIRE_SHAPE = original

    def _client(self):
        from app.services.embedding import _FoundryEmbedding

        return _FoundryEmbedding("https://example.invalid", "key", "embed-v-4-0")

    def test_falls_back_to_the_alternate_shape_on_a_schema_rejection(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import httpx

        from app.services import embedding as emb

        seen: list[str] = []
        vector = [0.5] * 1024

        def fake_retry(do, label: str = ""):
            body = do.__defaults__[0]()  # the bound _b=build_body default
            shape = "images" if "images" in body else "inputs"
            seen.append(shape)
            if shape == "images":
                response = httpx.Response(400, json={"message": "unsupported field"})
                raise httpx.HTTPStatusError("400", request=None, response=response)
            return _Resp({"embeddings": {"float": [vector]}})

        monkeypatch.setattr(
            "app.services._foundry_retry.with_foundry_retry", fake_retry,
        )

        got = self._client().embed_image(b"\x89PNG fake")

        assert seen == ["images", "inputs"], "should try both shapes, in order"
        assert len(got) == 1024
        assert emb._FoundryEmbedding._IMAGE_WIRE_SHAPE == "inputs"

    def test_a_non_schema_error_is_not_retried_with_different_json(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """429/5xx mean the request was understood — reshaping just burns a call."""
        import httpx

        calls: list[int] = []

        def fake_retry(do, label: str = ""):
            calls.append(1)
            response = httpx.Response(429, json={"message": "throttled"})
            raise httpx.HTTPStatusError("429", request=None, response=response)

        monkeypatch.setattr(
            "app.services._foundry_retry.with_foundry_retry", fake_retry,
        )

        with pytest.raises(httpx.HTTPStatusError):
            self._client().embed_image(b"\x89PNG fake")

        assert len(calls) == 1

    def test_both_shapes_rejected_raises_rather_than_returning_a_bad_vector(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import httpx

        def fake_retry(do, label: str = ""):
            response = httpx.Response(400, json={"message": "nope"})
            raise httpx.HTTPStatusError("400", request=None, response=response)

        monkeypatch.setattr(
            "app.services._foundry_retry.with_foundry_retry", fake_retry,
        )

        with pytest.raises(RuntimeError, match="both documented image wire shapes"):
            self._client().embed_image(b"\x89PNG fake")
