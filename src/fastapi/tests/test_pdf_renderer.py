"""Phase G.3 follow-up — tests for the markdown → HTML → PDF renderer."""
from __future__ import annotations

import pytest

from app.services.report_builder.renderers.pdf_renderer import (
    markdown_to_html,
    render_pdf_from_markdown,
)


# ─────────────────────── markdown_to_html ────────────────────────


def test_h1_h2_h3_render_to_matching_tags() -> None:
    html = markdown_to_html("# Title\n## Section\n### Subsection")
    assert "<h1>Title</h1>" in html
    assert "<h2>Section</h2>" in html
    assert "<h3>Subsection</h3>" in html


def test_bullet_list_wraps_in_ul() -> None:
    html = markdown_to_html("- one\n- two\n- three")
    assert html.startswith("<ul>")
    assert "<li>one</li>" in html
    assert "<li>two</li>" in html
    assert html.rstrip().endswith("</ul>")


def test_numbered_list_wraps_in_ol() -> None:
    html = markdown_to_html("1. first\n2. second")
    assert html.startswith("<ol>")
    assert "<li>first</li>" in html
    assert "<li>second</li>" in html


def test_fenced_code_block() -> None:
    md = "```\nlet x = 1;\n```"
    html = markdown_to_html(md)
    assert "<pre><code>" in html
    assert "let x = 1;" in html
    assert "</code></pre>" in html


def test_inline_code_and_bold_and_em() -> None:
    html = markdown_to_html(
        "the **fast** way uses `O(n)` time and is *correct*",
    )
    assert "<strong>fast</strong>" in html
    assert "<code>O(n)</code>" in html
    assert "<em>correct</em>" in html


def test_link_renders_with_href() -> None:
    html = markdown_to_html("see [docs](https://example.com)")
    assert '<a href="https://example.com">docs</a>' in html


def test_horizontal_rule() -> None:
    html = markdown_to_html("above\n\n---\n\nbelow")
    assert "<hr />" in html


def test_paragraphs_separated_by_blank_lines() -> None:
    html = markdown_to_html("first paragraph\n\nsecond paragraph")
    assert html.count("<p>") == 2


def test_html_special_chars_in_text_are_escaped() -> None:
    html = markdown_to_html("the <script> tag is dangerous")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ─────────────────────── render_pdf_from_markdown ──────────────────────


def test_render_pdf_returns_real_pdf_bytes() -> None:
    md = "# Test\n\nThis is a real PDF."
    pdf = render_pdf_from_markdown(md, title="Smoke Test")
    assert isinstance(pdf, (bytes, bytearray))
    assert pdf[:4] == b"%PDF"
    # A page with one heading + one paragraph should be at least 1KB.
    assert len(pdf) > 1024


def test_render_pdf_handles_empty_input_gracefully() -> None:
    pdf = render_pdf_from_markdown("", title="Empty")
    assert pdf[:4] == b"%PDF"  # WeasyPrint still produces a valid empty PDF


def test_render_pdf_with_complex_bundle_succeeds() -> None:
    md = (
        "# Weekly Project Digest\n\n"
        "**Report ID:** `abc-uuid`  \n"
        "**Risk tier:** R3\n\n"
        "---\n\n"
        "## Summary\n\n"
        "- 63 drill holes ingested\n"
        "- 16 distinct log curves\n"
        "- Coverage: Cameco Shirley Basin\n\n"
        "## Provenance Proof\n\n"
        "- evidence_sha256: `abc123def456`\n"
    )
    pdf = render_pdf_from_markdown(md)
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 2048
