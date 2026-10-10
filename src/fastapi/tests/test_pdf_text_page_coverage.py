"""text_page_coverage_pct sees the blank pages (audit finding 13).

``parse_pdf_report`` took the coverage ratio over ``len(per_page_text)``, and
the extractors only append a page that produced text - so the ratio was 100%
for any document that produced text on at least one page, and the
``low_text_page_coverage`` warning in ingest_pdf could never fire for the case
it exists for (a scan whose pages mostly came back blank).

This builds a real PDF with blank pages and parses it with every OCR route off.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.ingest.pdf_report import _pdf_page_count, parse_pdf_report

_PARAGRAPH = [
    "The Eagle Point deposit is hosted by Proterozoic granodiorite intruded by",
    "narrow quartz veins carrying pyrite and chalcopyrite with visible gold.",
    "Diamond drilling in 2019 intersected 14.2 metres grading 3.4 grams per",
    "tonne gold from 212.0 metres depth in hole EP-19-004 beneath the east zone.",
    "Core recovery averaged ninety six percent and rock quality exceeded eighty.",
]


def _pdf(page_texts: list[list[str]]) -> bytes:
    """A valid multi-page PDF; an empty list is a blank page."""
    n = len(page_texts)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, lines in enumerate(page_texts):
        stream = "".join(
            f"BT /F1 11 Tf 60 {740 - 16 * row} Td ({line}) Tj ET\n"
            for row, line in enumerate(lines)
        )
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {5 + 2 * i} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}endstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return out


@pytest.fixture(autouse=True)
def _no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing may read the blank pages: no Tesseract floor, no remote engine."""
    monkeypatch.setenv("PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "false")
    monkeypatch.delenv("COHERE_API_KEY", raising=False)


def _parse(tmp_path: Path, page_texts: list[list[str]]):
    path = tmp_path / "report.pdf"
    path.write_bytes(_pdf(page_texts))
    return parse_pdf_report(str(path))


def test_the_page_count_is_the_documents_not_the_pages_with_text(tmp_path: Path) -> None:
    path = tmp_path / "five.pdf"
    path.write_bytes(_pdf([_PARAGRAPH, [], [], [], []]))

    assert _pdf_page_count(str(path)) == 5
    assert _pdf_page_count(str(tmp_path / "missing.pdf")) is None


def test_four_blank_pages_in_five_read_twenty_percent_not_a_hundred(tmp_path: Path) -> None:
    result = _parse(tmp_path, [_PARAGRAPH, [], [], [], []])

    assert result.text_page_coverage_pct == pytest.approx(0.2)


def test_a_fully_texted_document_still_reads_a_hundred_percent(tmp_path: Path) -> None:
    result = _parse(tmp_path, [_PARAGRAPH, _PARAGRAPH, _PARAGRAPH])

    assert result.text_page_coverage_pct == pytest.approx(1.0)


def test_text_pages_are_the_exact_pages_that_read(tmp_path: Path) -> None:
    """Audit finding 14: the figures scope needs the pages WITHOUT text, and a
    chunk spanning pages 1-3 must not hide the blank page 2 (see
    test_page_image_exact_text_pages.py for what consumes this)."""
    result = _parse(tmp_path, [_PARAGRAPH, [], _PARAGRAPH, [], []])

    assert result.text_pages == [1, 3]


def test_a_document_that_read_nothing_says_so_with_an_empty_set(tmp_path: Path) -> None:
    result = _parse(tmp_path, [[], [], []])

    assert result.text_pages == []          # not None: "computed, and there are none"
    assert result.text_page_coverage_pct == 0.0


def test_the_run_warning_can_now_fire_for_a_mostly_blank_scan(tmp_path: Path) -> None:
    """The consumer: ingest_pdf.build_run_warnings compares this number with
    MIN_TEXT_PAGE_COVERAGE, and with a 100% input it never could."""
    from app.hatchet_workflows.ingest_pdf import MIN_TEXT_PAGE_COVERAGE, build_run_warnings

    result = _parse(tmp_path, [_PARAGRAPH, [], [], [], [], [], [], [], [], []])
    assert result.text_page_coverage_pct == pytest.approx(0.1)

    warnings = build_run_warnings(
        {"warnings": [], "text_page_coverage_pct": result.text_page_coverage_pct},
        passages_written=3, page_count=10,
    )

    assert result.text_page_coverage_pct < MIN_TEXT_PAGE_COVERAGE
    assert [w["code"] for w in warnings] == ["low_text_page_coverage"]
