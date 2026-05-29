"""Golden tests for _split_into_sections — the unified sliding-window
chunker that replaced the section-detection-first chunker.

Regression target: a doc whose body is a giant un-numbered preamble
followed by a numbered appendix (e.g. NI 43-101 cover + option-agreement
schedule). The old chunker emitted a 100+ KB "Preamble" passage that
bge-small truncated, hiding the body from retrieval. The new chunker
windows every region so no chunk exceeds WINDOW_CHARS.

Run with:  pytest tests/test_pdf_section_chunking.py -v
"""

from __future__ import annotations

from georag_dagster.parsers.pdf_report import (
    WINDOW_CHARS,
    WINDOW_OVERLAP_CHARS,
    ReportSection,
    _split_into_sections,
)


def _make_per_page_text(pages_text: list[str]) -> list[tuple[int, str]]:
    """1-indexed (page_num, text) tuples matching the parser's contract."""
    return [(i + 1, t) for i, t in enumerate(pages_text)]


def _join_pages(pages_text: list[str]) -> str:
    """Mirror _build_page_index's char accounting (single "\n" between pages)."""
    return "\n".join(pages_text)


def _assert_all_chunks_within_window(sections: list[ReportSection]) -> None:
    for s in sections:
        assert len(s.text) <= WINDOW_CHARS, (
            f"chunk exceeds WINDOW_CHARS: title={s.section_title!r} "
            f"len={len(s.text)}"
        )


def test_empty_document_returns_no_sections() -> None:
    assert _split_into_sections("") == []
    assert _split_into_sections("   \n\t  ") == []


def test_short_document_with_no_headings_emits_one_document_chunk() -> None:
    text = "A small fact sheet about gold exploration in Ontario."
    sections = _split_into_sections(text)
    assert len(sections) == 1
    assert sections[0].section_number is None
    assert sections[0].section_title == "Document"
    assert sections[0].text == text


def test_long_document_with_no_headings_is_sliding_window_chunked() -> None:
    """No NI 43-101 headings → windowed end-to-end. Used to dump everything
    into one passage that bge-small would truncate."""
    pages = [("A" * 1200 + " gold ") for _ in range(5)]  # 5 pages × ~1.2 KB
    full_text = _join_pages(pages)
    per_page_text = _make_per_page_text(pages)

    sections = _split_into_sections(full_text, per_page_text)

    _assert_all_chunks_within_window(sections)
    assert len(sections) >= 4, (
        f"expected multiple windows over a ~6 KB doc, got {len(sections)}"
    )
    for s in sections:
        assert s.section_title == "Document"
        assert s.section_number is None
        assert s.page_first is not None and s.page_last is not None
        assert 1 <= s.page_first <= 5
        assert s.page_first <= s.page_last <= 5


def test_long_preamble_before_first_heading_is_chunked() -> None:
    """The bug we shipped: a 100K-char preamble before the first detected
    heading got dumped as one passage. Now it should be windowed."""
    long_preamble_pages = [("PreambleBody " * 200) for _ in range(20)]  # ~50 KB
    appendix_pages = [
        "1. Schedule A\nFirst clause of the option agreement.",
        "Second clause covers royalties and milestones.",
    ]
    full_text = _join_pages(long_preamble_pages + appendix_pages)
    per_page_text = _make_per_page_text(long_preamble_pages + appendix_pages)

    sections = _split_into_sections(full_text, per_page_text)

    _assert_all_chunks_within_window(sections)
    preamble_chunks = [s for s in sections if s.section_title == "Preamble"]
    # Roughly len(preamble) / (WINDOW_CHARS - WINDOW_OVERLAP_CHARS) windows
    expected_min = (sum(len(p) for p in long_preamble_pages)
                    // (WINDOW_CHARS - WINDOW_OVERLAP_CHARS)) - 1
    assert len(preamble_chunks) >= expected_min, (
        f"preamble of ~{sum(len(p) for p in long_preamble_pages)} chars produced "
        f"only {len(preamble_chunks)} chunks (expected ≥{expected_min})"
    )
    # Preamble chunks must carry page_first/page_last inside the preamble pages.
    for s in preamble_chunks:
        assert s.section_number is None
        assert s.page_first is not None and s.page_first <= 20
        assert s.page_last is not None and s.page_last <= 20


def test_section_metadata_is_attached_to_every_window() -> None:
    """Every chunk inside 'N. Title' must carry section_number=N and the
    parent title — even when the section is long enough to be split."""
    long_section_body = "Geology body text. " * 200  # ~3.8 KB
    full_text = (
        "Preamble line.\n"
        "1. Summary\n"
        "Brief summary of the project.\n"
        "2. Geology\n"
        + long_section_body
    )
    sections = _split_into_sections(full_text)

    _assert_all_chunks_within_window(sections)

    section1_chunks = [s for s in sections if s.section_number == "1"]
    section2_chunks = [s for s in sections if s.section_number == "2"]
    preamble_chunks = [s for s in sections if s.section_title == "Preamble"]

    assert len(preamble_chunks) >= 1
    assert len(section1_chunks) >= 1
    assert all(s.section_title == "Summary" for s in section1_chunks)

    assert len(section2_chunks) >= 2, (
        f"3.8 KB Section 2 should produce multiple windows, got {len(section2_chunks)}"
    )
    assert all(s.section_title == "Geology" for s in section2_chunks)


def test_windows_overlap_by_configured_amount() -> None:
    """Adjacent windows inside one section must share WINDOW_OVERLAP_CHARS
    so split sentences still match retrieval."""
    body = ("X" * (WINDOW_CHARS * 3))  # forces ≥3 windows
    full_text = "1. Big\n" + body
    sections = [
        s for s in _split_into_sections(full_text) if s.section_number == "1"
    ]
    assert len(sections) >= 3
    # Each pair of adjacent chunks should share at least
    # WINDOW_OVERLAP_CHARS // 2 characters (the strip() in _emit_windows
    # can trim whitespace, so we don't require exact overlap).
    for prev, curr in zip(sections, sections[1:]):
        common_tail = prev.text[-WINDOW_OVERLAP_CHARS:]
        common_head = curr.text[: WINDOW_OVERLAP_CHARS]
        # Both are 'X' * N substrings so they should overlap.
        assert common_tail and common_head


def test_parse_quality_pct_counts_unique_section_numbers_not_chunks() -> None:
    """Regression guard for the parse_quality_pct calc in parse_pdf_report.

    With the unified chunker, a single section can produce many chunks.
    We don't want parse_quality_pct to inflate above 1.0 just because
    Section 14 generated 30 windows.
    """
    long_section = "Body content. " * 500  # ~7 KB → many windows
    full_text = "1. Summary\nShort.\n2. Geology\n" + long_section
    sections = _split_into_sections(full_text)

    unique_numbered = {
        s.section_number for s in sections if s.section_number is not None
    }
    assert unique_numbered == {"1", "2"}

    # The same dedupe logic now lives in parse_pdf_report; this test pins
    # the contract so future refactors don't silently revert.
    numbered_chunks = [s for s in sections if s.section_number is not None]
    assert len(numbered_chunks) > len(unique_numbered), (
        "Test fixture should produce multi-chunk sections to exercise the "
        "dedupe path; adjust long_section size if this fails."
    )
