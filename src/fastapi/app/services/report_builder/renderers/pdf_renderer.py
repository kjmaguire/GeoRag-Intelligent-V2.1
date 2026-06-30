"""Phase G.3 follow-up — minimal-viable markdown → PDF renderer.

The Phase G.3 ``export_package`` node emits a markdown bundle as a
``data:text/markdown;base64`` URI. This module pairs with it: given
the markdown source, returns a printable PDF (bytes) via WeasyPrint.

Why a hand-rolled markdown-to-HTML converter:
  * The ``markdown`` package isn't in pyproject.toml and adding it
    requires senior-reviewer approval (per CLAUDE.md hard rules).
  * Our deterministic report templates use a small subset of markdown
    (h1-h3, bold/em, inline code, fenced code blocks, bullet lists,
    horizontal rules). A 100-line regex pipeline handles them all.

WeasyPrint loads HTML + CSS into a single PDF; no external deps
beyond the existing system libs (cairo, pango, gdk-pixbuf) the
Dockerfile already installs.
"""
from __future__ import annotations

import html
import logging
import re
import textwrap

logger = logging.getLogger(__name__)


# ─────────────────────── Minimal markdown → HTML ────────────────────────


_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_EM_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_NUMBER_BULLET_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_HORIZONTAL_RULE_RE = re.compile(r"^---+\s*$")


def _render_inline(text: str) -> str:
    """Apply inline-markdown substitutions to a single line of text.

    Order matters: escape HTML first, then apply markdown rules, so
    user-supplied angle brackets don't break the HTML.
    """
    out = html.escape(text)
    out = _INLINE_CODE_RE.sub(r"<code>\1</code>", out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _EM_RE.sub(r"<em>\1</em>", out)
    out = _LINK_RE.sub(r'<a href="\2">\1</a>', out)
    return out


def markdown_to_html(markdown_text: str) -> str:
    """Convert a markdown string to the HTML body the PDF renderer wraps.

    Handles: # / ## / ### headers, fenced ``` blocks, - / * bullet
    lists, 1. / 2. ordered lists, --- horizontal rules, inline `code`,
    **bold**, *em*, [link](url), and paragraphs separated by blank lines.
    """
    lines = markdown_text.splitlines()
    out: list[str] = []
    i = 0
    in_ul = False
    in_ol = False
    in_p_buf: list[str] = []

    def _flush_paragraph() -> None:
        if in_p_buf:
            out.append(f"<p>{' '.join(in_p_buf)}</p>")
            in_p_buf.clear()

    def _close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        fence_match = _FENCE_RE.match(stripped)
        if fence_match:
            _flush_paragraph()
            _close_lists()
            i += 1
            code_lines: list[str] = []
            while i < len(lines) and not _FENCE_RE.match(lines[i].strip()):
                code_lines.append(html.escape(lines[i]))
                i += 1
            i += 1  # skip closing fence
            out.append(
                f"<pre><code>{chr(10).join(code_lines)}</code></pre>"
            )
            continue

        # Horizontal rule
        if _HORIZONTAL_RULE_RE.match(stripped):
            _flush_paragraph()
            _close_lists()
            out.append("<hr />")
            i += 1
            continue

        # Header
        header_match = _HEADER_RE.match(stripped)
        if header_match:
            _flush_paragraph()
            _close_lists()
            level = len(header_match.group(1))
            content = _render_inline(header_match.group(2))
            out.append(f"<h{level}>{content}</h{level}>")
            i += 1
            continue

        # Bullet list item
        bullet_match = _BULLET_RE.match(stripped)
        if bullet_match:
            _flush_paragraph()
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"  <li>{_render_inline(bullet_match.group(1))}</li>")
            i += 1
            continue

        # Numbered list item
        num_match = _NUMBER_BULLET_RE.match(stripped)
        if num_match:
            _flush_paragraph()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"  <li>{_render_inline(num_match.group(2))}</li>")
            i += 1
            continue

        # Blank line — paragraph break
        if not stripped:
            _flush_paragraph()
            _close_lists()
            i += 1
            continue

        # Plain paragraph line — accumulate
        _close_lists()
        in_p_buf.append(_render_inline(stripped))
        i += 1

    _flush_paragraph()
    _close_lists()
    return "\n".join(out)


# ─────────────────────── PDF rendering ──────────────────────────────────


_BASE_CSS = textwrap.dedent("""
    @page {
        size: letter;
        margin: 0.75in;
        @bottom-right {
            content: counter(page) " / " counter(pages);
            font-size: 9pt;
            color: #666;
        }
    }
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        font-size: 11pt;
        line-height: 1.5;
        color: #1a1a1a;
    }
    h1 {
        font-size: 22pt;
        margin: 0 0 0.4em;
        border-bottom: 2px solid #444;
        padding-bottom: 0.2em;
    }
    h2 {
        font-size: 16pt;
        margin: 1.2em 0 0.4em;
        color: #2a2a2a;
    }
    h3 {
        font-size: 13pt;
        margin: 1em 0 0.3em;
    }
    p {
        margin: 0.5em 0;
    }
    code {
        font-family: 'SF Mono', Monaco, 'Cascadia Mono', monospace;
        background: #f3f3f3;
        padding: 1px 5px;
        border-radius: 3px;
        font-size: 0.92em;
    }
    pre {
        background: #f3f3f3;
        padding: 0.6em 0.8em;
        border-radius: 4px;
        overflow-x: auto;
        font-size: 9pt;
    }
    pre code {
        background: transparent;
        padding: 0;
    }
    ul, ol {
        padding-left: 1.5em;
        margin: 0.4em 0;
    }
    li {
        margin: 0.2em 0;
    }
    hr {
        border: none;
        border-top: 1px solid #ccc;
        margin: 1.5em 0;
    }
    a {
        color: #1a5fb4;
        text-decoration: none;
    }
""").strip()


def render_pdf_from_markdown(
    markdown_text: str,
    *,
    title: str | None = None,
    extra_css: str | None = None,
) -> bytes:
    """Render a markdown bundle to PDF bytes via WeasyPrint.

    Args:
        markdown_text: The bundle source (one big string).
        title: Optional <title> for the rendered HTML doc.
        extra_css: Optional CSS appended after the base stylesheet.

    Returns:
        PDF bytes ready to upload to SeaweedFS, return as a download,
        or wrap in a data URI.
    """
    try:
        # WeasyPrint import is lazy — the dep is heavy and not every
        # report path needs PDF rendering.
        from weasyprint import CSS, HTML
    except ImportError as exc:  # pragma: no cover — install-time only
        raise RuntimeError(
            "WeasyPrint not installed — add to pyproject.toml"
        ) from exc

    body = markdown_to_html(markdown_text)
    css_text = _BASE_CSS + ("\n" + extra_css if extra_css else "")
    html_doc = (
        "<!doctype html><html><head>"
        f"<meta charset='utf-8'><title>{html.escape(title or 'GeoRAG Report')}</title>"
        "</head><body>" + body + "</body></html>"
    )
    pdf_bytes = HTML(string=html_doc).write_pdf(stylesheets=[CSS(string=css_text)])
    logger.info(
        "render_pdf_from_markdown: rendered %d markdown bytes → %d PDF bytes",
        len(markdown_text), len(pdf_bytes),
    )
    return pdf_bytes


__all__ = [
    "markdown_to_html",
    "render_pdf_from_markdown",
]
