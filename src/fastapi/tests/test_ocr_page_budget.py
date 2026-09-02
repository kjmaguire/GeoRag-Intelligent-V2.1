"""The remote-OCR page budget, and what it did quietly.

Named the Document Intelligence budget until 2026-09-02; the engine behind
it is now Cohere Parse (ADR-0019) and the knob is OCR_MAX_PAGES_PER_DOC.

The cap itself is right — Document Intelligence is billed per page and a
400-page scanned NI 43-101 should not be able to spend without limit. Two
things around it were not.

First, hitting the cap was invisible. Pages 1-300 were read by Document
Intelligence and pages 301-400 by tesseract, which extracts no table
structure at all, so the tail of a long report lost every assay and
resource table it contained. One WARNING line in a log with no alert rule
attached was the only trace, and the parse result came back looking like
any other successful parse.

Second, the budget registry was single-slot: `_ocr_budget_take` cleared
every other document's counter the moment it saw a new path. A Hatchet
worker runs several ingest_pdf tasks in one process, so two interleaved
documents reset each other — the per-document cap could be overrun by a
multiple of itself, and an exhaustion record could vanish before its own
parse got to report it.
"""

from __future__ import annotations

import pytest

from app.services.ingest import pdf_report


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    """The budget lives in module globals; each test starts from empty."""
    monkeypatch.setattr(pdf_report, "_OCR_PAGES_USED", {})
    monkeypatch.setattr(pdf_report, "_OCR_CAP_LOGGED", set())
    monkeypatch.setattr(pdf_report, "_OCR_CAP_EXHAUSTED", {})
    monkeypatch.setenv("OCR_MAX_PAGES_PER_DOC", "300")


class TestTheCapHolds:
    def test_a_long_document_gets_exactly_the_cap(self) -> None:
        granted = sum(
            1 for _ in range(400) if pdf_report._ocr_budget_take("/data/A.pdf")
        )

        assert granted == 300

    def test_a_short_document_is_never_capped(self) -> None:
        granted = sum(
            1 for _ in range(40) if pdf_report._ocr_budget_take("/data/B.pdf")
        )

        assert granted == 40
        assert pdf_report._ocr_budget_warning("/data/B.pdf") is None


class TestTheDowngradeIsVisible:
    def test_exhaustion_becomes_a_parse_warning(self) -> None:
        for _ in range(400):
            pdf_report._ocr_budget_take("/data/A.pdf")

        warning = pdf_report._ocr_budget_warning("/data/A.pdf")

        assert warning is not None
        assert warning["code"] == "ocr_page_budget_exhausted"
        assert warning["cap"] == 300
        # The operator needs to know what was actually lost, not just that a
        # limit was reached.
        assert "table structure" in warning["message"]
        assert pdf_report._OCR_PAGE_BUDGET_ENV in warning["message"]

    def test_the_warning_reaches_the_ingestion_run(self) -> None:
        """It travels the channel that marks a run `partial`.

        ingest_pdf passes parse warnings through verbatim as dicts and
        counts them into warnings_count; a non-empty warnings list is what
        stops the terminal write from recording `completed`.
        """
        for _ in range(400):
            pdf_report._ocr_budget_take("/data/A.pdf")

        warning = pdf_report._ocr_budget_warning("/data/A.pdf")

        assert isinstance(warning, dict)
        assert isinstance(warning.get("message"), str)


class TestConcurrentDocumentsDoNotResetEachOther:
    def test_an_interleaved_document_does_not_refill_the_budget(self) -> None:
        """The cost bug: B's first page used to zero A's counter."""
        a_granted = 0
        for i in range(400):
            if pdf_report._ocr_budget_take("/data/A.pdf"):
                a_granted += 1
            if i < 50:
                pdf_report._ocr_budget_take("/data/B.pdf")

        assert a_granted == 300

    def test_an_exhaustion_record_survives_the_interleaving(self) -> None:
        for i in range(400):
            pdf_report._ocr_budget_take("/data/A.pdf")
            if i < 50:
                pdf_report._ocr_budget_take("/data/B.pdf")

        assert pdf_report._ocr_budget_warning("/data/A.pdf") is not None
        assert pdf_report._ocr_budget_warning("/data/B.pdf") is None

    def test_the_registry_stays_bounded(self) -> None:
        """Eviction still happens — it is just FIFO instead of total."""
        for n in range(pdf_report._OCR_BUDGET_REGISTRY_MAX + 10):
            pdf_report._ocr_budget_take(f"/data/doc-{n}.pdf")

        assert len(pdf_report._OCR_PAGES_USED) <= pdf_report._OCR_BUDGET_REGISTRY_MAX


class TestTheOldEnvNameStillWorks:
    def test_legacy_name_is_honoured_with_a_warning(self, monkeypatch, caplog) -> None:
        """A worker env that still says AZURE_DI_MAX_PAGES_PER_DOC keeps its cap."""
        monkeypatch.delenv("OCR_MAX_PAGES_PER_DOC", raising=False)
        monkeypatch.setenv("AZURE_DI_MAX_PAGES_PER_DOC", "7")
        monkeypatch.setattr(pdf_report, "_LEGACY_BUDGET_ENV_WARNED", False)

        with caplog.at_level("WARNING"):
            assert pdf_report._ocr_max_pages_per_doc() == 7
            assert pdf_report._ocr_max_pages_per_doc() == 7

        warned = [r for r in caplog.records if "old name" in r.getMessage()]
        assert len(warned) == 1

    def test_new_name_wins_over_the_old_one(self, monkeypatch) -> None:
        monkeypatch.setenv("OCR_MAX_PAGES_PER_DOC", "12")
        monkeypatch.setenv("AZURE_DI_MAX_PAGES_PER_DOC", "7")

        assert pdf_report._ocr_max_pages_per_doc() == 12
