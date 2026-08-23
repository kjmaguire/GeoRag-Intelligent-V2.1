"""What a retrieved chunk tells the model about itself.

Two kinds of content must not reach the LLM looking like a faithful extract:
a vision model's description of a page image, and a page the OCR quality
router tiered as unreadable.

The image case was handled. The OCR case was not, despite passage_embedder's
comment claiming "retrieval down-weights them via the ocr_status payload
field": the field was written to the Qdrant payload and read by nothing, since
DocumentChunk had no such attribute. A 1960s scanned assay sheet at 0.42 mean
confidence still contains recognisable hole IDs, so it scored well on BM25,
the reranker has no notion of quality, and the answer came back quoting
misread digits with a real page citation on it.
"""

from __future__ import annotations

from app.agent.tools import DocumentChunk


def _chunk(**overrides) -> DocumentChunk:
    defaults = dict(
        chunk_id="c1",
        text="Hole 36-1085 returned 1.85 g/t Au over 12.5 m.",
        source_document_id="doc-1",
        document_title="1962 Assay Programme",
        section_number=None,
        section_title=None,
        section=None,
        page=14,
        document_type="NI43",
        report_id="r1",
        relevance_score=0.8,
    )
    defaults.update(overrides)
    return DocumentChunk(**defaults)


class TestOcrQualityReachesTheContextWindow:
    def test_a_clean_page_is_passed_through_unchanged(self) -> None:
        chunk = _chunk(ocr_status="ok", ocr_confidence=0.98)

        assert chunk.annotated_text == chunk.text
        assert not chunk.is_low_confidence_ocr

    def test_a_flagged_page_announces_itself(self) -> None:
        chunk = _chunk(ocr_status="low_confidence", ocr_confidence=0.42)

        annotated = chunk.annotated_text

        assert chunk.is_low_confidence_ocr
        assert "OCR quality flagged" in annotated
        assert "page 14" in annotated
        assert "0.42" in annotated
        # The warning is a prefix, not a replacement — the text still has to
        # be usable when it is the only page that mentions the subject.
        assert chunk.text in annotated

    def test_the_warning_survives_a_missing_confidence(self) -> None:
        chunk = _chunk(ocr_status="low_confidence", ocr_confidence=None)

        assert "OCR quality flagged" in chunk.annotated_text

    def test_status_matching_is_case_insensitive(self) -> None:
        assert _chunk(ocr_status="LOW_CONFIDENCE").is_low_confidence_ocr

    def test_a_page_with_no_status_is_not_flagged(self) -> None:
        """NULL means the passage came from the PDF text layer, not OCR."""
        assert not _chunk(ocr_status=None).is_low_confidence_ocr

    def test_a_flagged_page_image_carries_both_warnings(self) -> None:
        chunk = _chunk(
            ocr_status="low_confidence",
            ocr_confidence=0.31,
            modality="image",
            page_number=14,
        )

        annotated = chunk.annotated_text

        assert "OCR quality flagged" in annotated
        assert "not quoted text from the document" in annotated
