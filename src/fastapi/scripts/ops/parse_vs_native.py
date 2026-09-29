"""Cohere Parse vs the native PDF stack, on real reports — a READ-ONLY harness.

Why this exists
---------------
Kyle is "okay with using Parse for everything for now, if it will do a good
job" (PDF_PARSE_MODE=all, see app/services/ingest/pdf_report.py). "A good job"
on an NI 43-101 means NUMBERS: a grade, a tonnage or a cut-off that is
misread is a wrong answer with a citation on it. This script measures that on
reports already in the corpus, before anyone flips the switch.

It runs INSIDE AWS (the operator's sandbox has no AWS credentials): a one-off
ECS task on the Hatchet worker task definition, started by
.github/workflows/parse-comparison.yml, printing to stdout so the report
lands in CloudWatch and in the GitHub job log. It is in scripts/ops/ because
docker/fastapi.Dockerfile copies the whole fastapi tree, so the script ships in
the image with no Dockerfile change.

What it does
------------
1. Picks a project (``--project-slug``, default: the project with the most
   reports that have a ``source_object_key``) and up to ``--max-reports``
   born-digital reports from ``silver.reports``.
2. Downloads each PDF from the bronze bucket with the project's storage client
   (the same call the ingest_pdf workflow makes).
3. Samples up to ``--max-pages-per-report`` pages: table pages (pdfplumber /
   drawing-based detection, up to half the budget), resource/reserve/"Table N"
   keyword pages, and evenly spaced prose pages for the rest.
4. For each sampled page: native text (pypdfium2 ``get_text_bounded``),
   pdfplumber tables, and Cohere Parse through the SAME client code the pipeline
   uses (``cohere_parse_client.ocr_page_sync``: same render, request body, retry
   and response adapter) — so this also exercises the live wire shape. Parse
   errors are recorded verbatim (secrets redacted).
5. Metrics per page: normalised character similarity (difflib) native vs
   Parse; NUMBER recall in both directions; table count / rows x cols / numeric
   tokens captured by pdfplumber vs Parse; latency; engine error.
6. Prints a Markdown summary, up to 4 side-by-side samples (worst table pages
   and worst prose pages) and the full JSON between BEGIN/END markers.

Safety
------
* READ-ONLY. The Postgres session is ``default_transaction_read_only = on``;
  nothing is written to S3 or Qdrant (there is no Qdrant client in here).
* Parse pages are capped at 120 per run, and the estimated cost is printed
  ($1.50 per 1,000 pages — an ASSUMED list price, not read from an invoice).
* The API key is never printed. ``--dry-run`` needs no key and sends nothing.
* Text leaves the corpus only as excerpts: at most ~1,200 characters per
  representation for at most 4 sample pages, into CloudWatch and the job log.
  The JSON carries metrics, not page text.

Row-level security
------------------
silver.reports / silver.projects are RLS-protected by ``app.workspace_id``. The
script first reads unscoped (the policies admit an unset GUC on some
deployments); if that shows nothing it walks ``silver.workspaces`` and scopes to
each workspace in turn, and ``--workspace-id`` pins one explicitly. If the
rows are hidden from every scope it says so and exits 2 — it does not guess.

Exit codes: 0 report produced; 2 nothing could be compared (no report visible,
downloaded or samplable); 3 every Parse request failed (the report still
prints — a report full of auth failures must not read as a pass);
1 unexpected error.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import html
import json
import logging
import os
import re
import statistics
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# /app/scripts/ops/x.py is run as `python3 /app/scripts/ops/x.py`, which puts
# the SCRIPT'S directory (not /app) on sys.path — so `import app` needs this.
_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

logger = logging.getLogger("parse_vs_native")

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Assumed Parse list price. Not read from a bill; the report says so.
PARSE_USD_PER_1000_PAGES = 1.50
#: Hard ceiling on Parse pages per run, whatever the flags say.
MAX_PARSE_PAGES_PER_RUN = 120
MAX_REPORTS_LIMIT = 10
SAMPLE_CHARS = 1200
#: Pages under this many native characters are image-only for our purposes
#: (mirrors pdf_report.PER_PAGE_MIN_CHARS) and are not sampled.
MIN_NATIVE_CHARS = 80
#: difflib is O(n*m) on characters; beyond this the tail is not compared.
MAX_SIMILARITY_CHARS = 12_000
PARSE_CONCURRENCY = 4

BEGIN_SUMMARY = "=====BEGIN PARSE_VS_NATIVE_SUMMARY_MD====="
END_SUMMARY = "=====END PARSE_VS_NATIVE_SUMMARY_MD====="
BEGIN_JSON = "=====BEGIN PARSE_VS_NATIVE_JSON====="
END_JSON = "=====END PARSE_VS_NATIVE_JSON====="

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")


# --------------------------------------------------------------------------
# Pure helpers: text normalisation, similarity, numbers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_MD_SEPARATOR_ROW_RE = re.compile(r"^[ \t]*\|?(?:[ \t]*:?-{2,}:?[ \t]*\|)+[ \t]*:?-*:?[ \t]*$", re.MULTILINE)
_MD_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WS_RE = re.compile(r"\s+")


def strip_markup(text: str) -> str:
    """Remove the markdown / HTML furniture Parse adds, keep the words.

    HTML tags become spaces (a ``<td>`` boundary is a word boundary), markdown
    table pipes and separator rows go, headings/links/emphasis lose their
    syntax. This is for COMPARISON only; nothing stored is ever passed through
    it.
    """
    if not text:
        return ""
    out = _TAG_RE.sub(" ", text)
    out = html.unescape(out)
    out = _MD_SEPARATOR_ROW_RE.sub(" ", out)
    out = _MD_IMAGE_RE.sub(" ", out)
    out = _MD_LINK_RE.sub(r"\1", out)
    out = _MD_HEADING_RE.sub("", out)
    out = out.replace("|", " ").replace("**", "").replace("__", "").replace("`", "")
    return out


def normalize_text(text: str) -> str:
    """NFKC (ligatures, full-width forms), markup stripped, case-folded, one
    space between tokens."""
    if not text:
        return ""
    return _WS_RE.sub(" ", strip_markup(unicodedata.normalize("NFKC", text)).casefold()).strip()


def char_similarity(a: str, b: str) -> tuple[float, bool]:
    """difflib ratio of the two normalised texts, and whether it was truncated.

    Two empty texts are identical (1.0); one empty and one not are 0.0. The
    second value is True when either text exceeded MAX_SIMILARITY_CHARS and
    was cut, so a long page never silently compares only its head.
    """
    na, nb = normalize_text(a), normalize_text(b)
    if not na and not nb:
        return 1.0, False
    if not na or not nb:
        return 0.0, False
    truncated = len(na) > MAX_SIMILARITY_CHARS or len(nb) > MAX_SIMILARITY_CHARS
    na, nb = na[:MAX_SIMILARITY_CHARS], nb[:MAX_SIMILARITY_CHARS]
    # autojunk=False: with it on, every common letter in a 200+ char string is
    # "junk" and the ratio is meaningless for prose.
    return difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio(), truncated


# 1,234.5 | 1,234 | 0.45 | 12 | -3.2 | 12% | 12 %   (not the digits inside a word: "DH-001" gives "001")
_NUMBER_RE = re.compile(r"(?<![\w.,])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[ \t]?%)?")


def canonical_number(token: str) -> str:
    """``"1,234.50 %"`` -> ``"1234.50%"``. Trailing zeros are KEPT: 0.450 and
    0.45 are different reported precisions in a resource table."""
    out = token.replace(",", "").replace("−", "-").replace(" ", "").replace("\t", "")
    return out[1:] if out.startswith("+") else out


def is_significant_number(token: str) -> bool:
    """A number that is data, not furniture: has a decimal point, a percent
    sign, or at least three digits. Single/double-digit integers are page
    numbers, list markers and years-of-nothing; they match in both directions
    whatever the engine did and flatter the recall."""
    if "." in token or "%" in token:
        return True
    return sum(ch.isdigit() for ch in token) >= 3


def extract_numbers(text: str, *, significant_only: bool = False) -> list[str]:
    """Canonical numeric tokens in reading order (markup stripped first)."""
    tokens = [canonical_number(m.group(0)) for m in _NUMBER_RE.finditer(strip_markup(text or ""))]
    if significant_only:
        tokens = [t for t in tokens if is_significant_number(t)]
    return tokens


def number_recall(reference: Iterable[str], candidate: Iterable[str]) -> float | None:
    """Fraction of ``reference`` numbers found in ``candidate`` (multiset:
    a value that appears 5 times must be found 5 times). None when the
    reference has no numbers — 'nothing to recall' is not 100%."""
    ref, cand = Counter(reference), Counter(candidate)
    total = sum(ref.values())
    if total == 0:
        return None
    return sum(min(n, cand[tok]) for tok, n in ref.items()) / total


def unique_number_recall(reference: Iterable[str], candidate: Iterable[str]) -> float | None:
    """Set-based recall: which distinct values were found at all."""
    ref, cand = set(reference), set(candidate)
    if not ref:
        return None
    return len(ref & cand) / len(ref)


def table_shape(grid: Sequence[Sequence[Any]]) -> tuple[int, int]:
    """(rows, widest row) of a table grid."""
    rows = [r for r in grid if r is not None]
    return len(rows), max((len(r) for r in rows), default=0)


def grid_text(grid: Sequence[Sequence[Any]]) -> str:
    return " ".join(str(c) for row in grid for c in (row or []) if c is not None)


def tables_numbers(grids: Iterable[Sequence[Sequence[Any]]], *, significant_only: bool = False) -> list[str]:
    return extract_numbers("\n".join(grid_text(g) for g in grids), significant_only=significant_only)


# --------------------------------------------------------------------------
# Pure helpers: sampling
# --------------------------------------------------------------------------


def evenly_spaced(items: Sequence[int], n: int) -> list[int]:
    """``n`` items spread across ``items`` (first and last included when n >= 2)."""
    if n <= 0 or not items:
        return []
    if n >= len(items):
        return list(items)
    if n == 1:
        return [items[len(items) // 2]]
    picked = {round(i * (len(items) - 1) / (n - 1)) for i in range(n)}
    return [items[i] for i in sorted(picked)]


def select_pages(
    all_pages: Sequence[int],
    table_pages: Iterable[int],
    keyword_pages: Iterable[int],
    budget: int,
) -> list[tuple[int, str]]:
    """Choose ``(page, reason)`` pairs, at most ``budget`` of them, sorted by page.

    ``all_pages`` are the samplable pages (native text present). Table pages
    take up to half the budget; keyword-only pages up to half of what is left;
    evenly spaced prose pages the rest. Whatever a category cannot fill is
    topped up from the others (tables first), so a report with no tables still
    uses its whole budget on prose.
    """
    if budget <= 0:
        return []
    universe = sorted(set(all_pages))
    tables = [p for p in sorted(set(table_pages)) if p in set(universe)]
    keywords = [p for p in sorted(set(keyword_pages)) if p in set(universe) and p not in set(tables)]
    prose = [p for p in universe if p not in set(tables) and p not in set(keywords)]

    table_n = min(len(tables), budget // 2)
    kw_n = min(len(keywords), (budget - table_n) // 2)
    prose_n = min(len(prose), budget - table_n - kw_n)

    chosen: dict[int, str] = {}
    for p in evenly_spaced(tables, table_n):
        chosen[p] = "table"
    for p in evenly_spaced(keywords, kw_n):
        chosen[p] = "keyword"
    for p in evenly_spaced(prose, prose_n):
        chosen[p] = "prose"

    # Top up: unused table pages, then keyword pages, then prose.
    for pool, reason in ((tables, "table"), (keywords, "keyword"), (prose, "prose")):
        for p in pool:
            if len(chosen) >= budget:
                break
            chosen.setdefault(p, reason)
    return sorted(chosen.items())


# --------------------------------------------------------------------------
# Pure helpers: redaction, aggregation, rendering
# --------------------------------------------------------------------------

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
# `Authorization: Bearer <tok>` is _BEARER_RE's job; the negative lookahead keeps this from eating the word "Bearer".
_KEYISH_RE = re.compile(r"(?i)((?:api[_-]?key|authorization|token|secret)[\"']?\s*[:=]\s*[\"']?)(?!bearer\b)[^\s\"',}]{6,}")


def redact(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Remove credentials from an error string before it is printed.

    Knows the literal values it is given (the API key), bearer tokens and
    ``api_key=...``-shaped pairs. A provider that echoes request material back
    in an error must not be able to put a key into CloudWatch.
    """
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "[REDACTED]")
    out = _BEARER_RE.sub("Bearer [REDACTED]", out)
    return _KEYISH_RE.sub(r"\1[REDACTED]", out)


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _mean(values: Iterable[float | None]) -> float | None:
    real = [v for v in values if v is not None]
    return statistics.fmean(real) if real else None


def _micro(pages: Sequence[dict[str, Any]], hit_key: str, total_key: str) -> float | None:
    hits = sum(p["metrics"].get(hit_key) or 0 for p in pages)
    total = sum(p["metrics"].get(total_key) or 0 for p in pages)
    return hits / total if total else None


def summarize_pages(pages: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-page result dicts (see ``compare_page``) for one group.

    Only pages Parse ANSWERED contribute to the quality numbers — a failed
    request is counted as an error, not as a similarity of zero, so an outage
    cannot masquerade as "Parse is bad" (or, worse, be averaged away).
    """
    answered = [p for p in pages if p["parse"]["ok"]]
    skipped = [p for p in pages if p["parse"].get("skipped")]
    latencies = [p["parse"]["latency_s"] for p in answered if p["parse"].get("latency_s") is not None]
    return {
        "pages": len(pages),
        "parse_answered": len(answered),
        "parse_skipped": len(skipped),
        # Skipped (dry run) is not an error; a failed request is.
        "parse_errors": len(pages) - len(answered) - len(skipped),
        "similarity_mean": _mean(p["metrics"].get("similarity") for p in answered),
        "similarity_min": min(
            (p["metrics"]["similarity"] for p in answered if p["metrics"].get("similarity") is not None), default=None
        ),
        # Micro-averaged: total numbers found / total numbers present, so one
        # number-dense page weighs what its numbers weigh.
        "native_numbers_in_parse_micro": _micro(answered, "native_in_parse_hits", "native_numbers"),
        "parse_numbers_in_native_micro": _micro(answered, "parse_in_native_hits", "parse_numbers"),
        "native_sig_numbers_in_parse_micro": _micro(answered, "native_sig_in_parse_hits", "native_sig_numbers"),
        "parse_sig_numbers_in_native_micro": _micro(answered, "parse_sig_in_native_hits", "parse_sig_numbers"),
        "pdfplumber_tables": sum(p["pdfplumber"]["table_count"] for p in pages),
        "parse_tables": sum(p["parse"].get("table_count") or 0 for p in answered),
        "table_capture_pdfplumber_micro": _micro(
            [p for p in answered if p["reason"] == "table"], "sig_captured_by_pdfplumber_hits", "native_sig_numbers"
        ),
        "table_capture_parse_micro": _micro(
            [p for p in answered if p["reason"] == "table"], "sig_captured_by_parse_hits", "native_sig_numbers"
        ),
        "latency_p50_s": percentile(latencies, 0.5),
        "latency_p95_s": percentile(latencies, 0.95),
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _secs(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}s"


def estimated_cost_usd(pages: int) -> float:
    return round(pages * PARSE_USD_PER_1000_PAGES / 1000.0, 4)


def _trim(text: str, n: int = SAMPLE_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " ...[truncated]"


def render_markdown(result: dict[str, Any]) -> str:
    """The human-readable report. Tables are GitHub-flavoured Markdown."""
    meta = result["meta"]
    lines: list[str] = ["# Cohere Parse vs native PDF extraction", ""]
    lines.append(
        f"- project: `{meta.get('project_slug') or 'n/a'}`  |  reports: {len(result['reports'])}  |  "
        f"mode: {'DRY RUN (no Parse calls)' if meta['dry_run'] else 'live'}"
    )
    lines.append(
        f"- Parse pages sent: **{meta['parse_pages_sent']}** (cap {MAX_PARSE_PAGES_PER_RUN})  |  "
        f"estimated cost this run: **${meta['estimated_cost_usd']:.2f}** at ${PARSE_USD_PER_1000_PAGES:.2f}/1000 pages "
        f"(assumed list price)"
        + (
            f"  |  a live run of this selection would send {meta['parse_pages_planned']} pages "
            f"(~${estimated_cost_usd(meta['parse_pages_planned']):.2f})"
            if meta["dry_run"]
            else ""
        )
    )
    if meta.get("project_total_pages"):
        lines.append(
            f"- for scale: PDF_PARSE_MODE=all over this project's {meta['project_total_pages']} recorded pages "
            f"would be about **${estimated_cost_usd(meta['project_total_pages']):.2f}** at the same price"
        )
    lines.append(f"- Parse model `{meta.get('parse_model')}`, host `{meta.get('parse_host')}`")
    if meta["dry_run"]:
        lines += ["", "> Dry run: pages were selected and the native side was measured; Parse was NOT called."]
    lines.append("")

    lines += [
        "## Overall",
        "",
        "| group | pages | Parse answered | errors | text similarity (mean / min) | native numbers found in Parse | Parse numbers found in native | table numbers captured: pdfplumber | table numbers captured: Parse | latency p50 / p95 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, s in [("ALL", result["overall"]), *result["by_reason"].items()]:
        lines.append(_summary_row(name, s))
    lines += [
        "",
        "Numbers are 'significant' values only (decimals, percentages, 3+ digit integers), micro-averaged over pages Parse answered; "
        "the JSON also carries every-number recall. A failed request counts as an error, never as a low score.",
        "",
    ]

    lines += [
        "## Per report",
        "",
        "| report | pages sampled (table / keyword / prose) | Parse errors | text similarity | native numbers found in Parse | Parse numbers found in native |",
        "|---|---|---|---|---|---|",
    ]
    for rep in result["reports"]:
        if rep.get("skipped"):
            lines.append(f"| {rep['title'][:60]} (`{rep['report_id'][:8]}`) | skipped: {rep['skipped']} | | | | |")
            continue
        s = rep["summary"]
        counts = Counter(p["reason"] for p in rep["pages"])
        lines.append(
            f"| {rep['title'][:60]} (`{rep['report_id'][:8]}`, {rep['page_count']} pp) | {s['pages']} "
            f"({counts['table']} / {counts['keyword']} / {counts['prose']}) | {s['parse_errors']} | "
            f"{_pct(s['similarity_mean'])} | {_pct(s['native_sig_numbers_in_parse_micro'])} | {_pct(s['parse_sig_numbers_in_native_micro'])} |"
        )
    lines.append("")

    errors = result.get("parse_errors") or []
    if errors:
        lines += ["## Parse errors (verbatim, secrets redacted)", ""]
        for err, count in Counter(e["error"] for e in errors).most_common(10):
            lines.append(f"- x{count}: `{err[:400]}`")
        lines.append("")

    samples = result.get("samples") or []
    if samples:
        lines += ["## Side-by-side samples (worst pages)", ""]
        for s in samples:
            lines += [
                f"### {s['kind']} page {s['page']} of {s['report_title'][:60]} (`{s['report_id'][:8]}`)",
                "",
                f"similarity {_pct(s['similarity'])}; native numbers found in Parse {_pct(s['native_in_parse'])}; "
                f"Parse numbers found in native {_pct(s['parse_in_native'])}",
                "",
            ]
            for label, body in s["representations"].items():
                lines += [f"**{label}**", "", "```text", body, "```", ""]
    return "\n".join(lines)


def _summary_row(name: str, s: dict[str, Any]) -> str:
    return (
        f"| {name} | {s['pages']} | {s['parse_answered']} | {s['parse_errors']} | "
        f"{_pct(s['similarity_mean'])} / {_pct(s['similarity_min'])} | "
        f"{_pct(s['native_sig_numbers_in_parse_micro'])} | {_pct(s['parse_sig_numbers_in_native_micro'])} | "
        f"{_pct(s['table_capture_pdfplumber_micro'])} | {_pct(s['table_capture_parse_micro'])} | "
        f"{_secs(s['latency_p50_s'])} / {_secs(s['latency_p95_s'])} |"
    )


# --------------------------------------------------------------------------
# Native side: text + pdfplumber tables (synchronous; run in a thread)
# --------------------------------------------------------------------------


@dataclass
class NativeScan:
    total_pages: int = 0
    text: dict[int, str] = field(default_factory=dict)
    tables: dict[int, list[list[list[str]]]] = field(default_factory=dict)
    keyword_pages: set[int] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


def scan_native(pdf_path: str) -> NativeScan:
    """Native text for every page and pdfplumber tables, detected exactly the
    way the pipeline detects them.

    Table detection reuses ``pdf_report``'s own pieces — the drawing-based
    bordered/borderless classification, the lines / text strategies per class,
    ``_table_has_data`` and the signature dedupe — so "a table page" here means
    what it means to PDF_PARSE_MODE=tables. The scan is over every page; the
    sample is chosen from it afterwards.
    """
    import pdfplumber  # noqa: PLC0415
    import pypdfium2 as pdfium  # noqa: PLC0415

    from app.services.ingest import pdf_report as pr  # noqa: PLC0415

    scan = NativeScan()
    doc = pdfium.PdfDocument(pdf_path)
    try:
        scan.total_pages = len(doc)
        for n in range(1, scan.total_pages + 1):
            try:
                scan.text[n] = (doc[n - 1].get_textpage().get_text_bounded() or "").strip()
            except Exception as exc:  # noqa: BLE001 — recorded, page just has no native text
                scan.text[n] = ""
                scan.errors.append(f"native text page {n}: {type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            doc.close()

    for n, txt in scan.text.items():
        if pr._page_has_table_keywords(txt):
            scan.keyword_pages.add(n)

    page_class = pr._classify_pages_from_pdf(pdf_path)
    classification_failed = not page_class
    with pdfplumber.open(pdf_path) as pdf:
        for n, page in enumerate(pdf.pages, start=1):
            kind = page_class.get(n)
            run_lines = classification_failed or kind == "bordered"
            run_text = True  # bordered pages still get the text pass, as in the pipeline
            found: list[list[list[Any]]] = []
            for enabled, strategy in ((run_lines, "lines"), (run_text, "text")):
                if not enabled:
                    continue
                try:
                    found.extend(
                        page.extract_tables(
                            table_settings={"vertical_strategy": strategy, "horizontal_strategy": strategy}
                        )
                        or []
                    )
                except Exception as exc:  # noqa: BLE001 — recorded; the page just has no tables from this pass
                    scan.errors.append(f"pdfplumber {strategy} page {n}: {type(exc).__name__}: {exc}")
            kept: list[list[list[str]]] = []
            seen: set[str] = set()
            for tbl in found:
                if not pr._table_has_data(tbl):
                    continue
                sig = pr._table_signature(tbl)
                if sig in seen:
                    continue
                seen.add(sig)
                kept.append([[("" if c is None else str(c)) for c in (row or [])] for row in tbl])
            if kept:
                scan.tables[n] = kept
    return scan


# --------------------------------------------------------------------------
# Parse side (same client code path as the pipeline)
# --------------------------------------------------------------------------


@dataclass
class ParseOutcome:
    ok: bool
    text: str = ""
    tables: list[list[list[str]]] = field(default_factory=list)
    latency_s: float | None = None
    error: str | None = None


def parse_one_page(pdf_path: str, page: int, *, secrets: Sequence[str]) -> ParseOutcome:
    """One page through ``cohere_parse_client.ocr_page_sync`` — the function
    the pipeline's per-page path calls. Fails soft: any error is captured
    (redacted), never raised."""
    from app.services.ingest import cohere_parse_client as cpc  # noqa: PLC0415

    started = time.monotonic()
    try:
        result = cpc.ocr_page_sync(pdf_path, page)
    except Exception as exc:  # noqa: BLE001 — recorded verbatim (redacted); the run continues
        return ParseOutcome(
            ok=False,
            latency_s=time.monotonic() - started,
            error=redact(f"{type(exc).__name__}: {exc}", secrets=secrets),
        )
    latency = time.monotonic() - started
    if not result.request_succeeded:
        return ParseOutcome(
            ok=False, latency_s=latency, error=redact(result.error or "request failed (no detail)", secrets=secrets)
        )
    return ParseOutcome(
        ok=True, text=result.text or "", tables=[list(t) for t in (result.tables or [])], latency_s=latency
    )


async def parse_pages(
    pdf_path: str, pages: Sequence[int], *, secrets: Sequence[str], concurrency: int = PARSE_CONCURRENCY
) -> dict[int, ParseOutcome]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(page: int) -> tuple[int, ParseOutcome]:
        async with sem:
            return page, await asyncio.to_thread(parse_one_page, pdf_path, page, secrets=secrets)

    return dict(await asyncio.gather(*(_one(p) for p in pages)))


# --------------------------------------------------------------------------
# Per-page comparison
# --------------------------------------------------------------------------


def compare_page(
    page: int,
    reason: str,
    native_text: str,
    native_tables: Sequence[Sequence[Sequence[str]]],
    parse: ParseOutcome | None,
) -> dict[str, Any]:
    """Everything measured for one page, as a JSON-able dict (no page text)."""
    native_nums = extract_numbers(native_text)
    native_sig = extract_numbers(native_text, significant_only=True)
    pl_nums = tables_numbers(native_tables)
    pl_sig = tables_numbers(native_tables, significant_only=True)
    out: dict[str, Any] = {
        "page": page,
        "reason": reason,
        "native_chars": len(native_text),
        "pdfplumber": {
            "table_count": len(native_tables),
            "shapes": [f"{r}x{c}" for r, c in (table_shape(t) for t in native_tables)],
            "numbers": len(pl_nums),
            "sig_numbers": len(pl_sig),
        },
        "parse": {"ok": False, "error": None, "latency_s": None},
        "metrics": {},
    }
    if parse is None:  # dry run
        out["parse"] = {"ok": False, "error": "not called (dry run)", "latency_s": None, "skipped": True}
        return out

    out["parse"] = {"ok": parse.ok, "error": parse.error, "latency_s": parse.latency_s}
    if not parse.ok:
        return out

    parse_nums = extract_numbers(parse.text)
    parse_sig = extract_numbers(parse.text, significant_only=True)
    pa_nums = tables_numbers(parse.tables)
    pa_sig = tables_numbers(parse.tables, significant_only=True)
    similarity, truncated = char_similarity(native_text, parse.text)
    out["parse"].update(
        {
            "chars": len(parse.text),
            "table_count": len(parse.tables),
            "shapes": [f"{r}x{c}" for r, c in (table_shape(t) for t in parse.tables)],
            "numbers": len(pa_nums),
            "sig_numbers": len(pa_sig),
        }
    )

    def _hits(ref: list[str], cand: list[str]) -> int:
        rc, cc = Counter(ref), Counter(cand)
        return sum(min(n, cc[t]) for t, n in rc.items())

    out["metrics"] = {
        "similarity": similarity,
        "similarity_truncated": truncated,
        "native_numbers": len(native_nums),
        "parse_numbers": len(parse_nums),
        "native_sig_numbers": len(native_sig),
        "parse_sig_numbers": len(parse_sig),
        "native_in_parse": number_recall(native_nums, parse_nums),
        "parse_in_native": number_recall(parse_nums, native_nums),
        "native_sig_in_parse": number_recall(native_sig, parse_sig),
        "parse_sig_in_native": number_recall(parse_sig, native_sig),
        "unique_native_sig_in_parse": unique_number_recall(native_sig, parse_sig),
        "native_in_parse_hits": _hits(native_nums, parse_nums),
        "parse_in_native_hits": _hits(parse_nums, native_nums),
        "native_sig_in_parse_hits": _hits(native_sig, parse_sig),
        "parse_sig_in_native_hits": _hits(parse_sig, native_sig),
        # Of the significant numbers on the page (text layer), how many did
        # each TABLE extractor capture in its tables?
        "sig_captured_by_pdfplumber_hits": _hits(native_sig, pl_sig),
        "sig_captured_by_parse_hits": _hits(native_sig, pa_sig),
        "sig_captured_by_pdfplumber": number_recall(native_sig, pl_sig),
        "sig_captured_by_parse": number_recall(native_sig, pa_sig),
    }
    return out


def _samples(
    reports: Sequence[dict[str, Any]], keep: dict[tuple[str, int], dict[str, Any]], per_kind: int = 2
) -> list[dict[str, Any]]:
    """Worst ``per_kind`` table pages and worst ``per_kind`` prose pages,
    with the first ~1,200 characters of each representation."""
    candidates: dict[str, list[tuple[float, dict[str, Any], dict[str, Any]]]] = {"table": [], "prose": []}
    for rep in reports:
        for pg in rep.get("pages") or []:
            if not pg["parse"]["ok"]:
                continue
            kind = "table" if pg["reason"] == "table" else "prose" if pg["reason"] == "prose" else None
            if kind is None:
                continue
            m = pg["metrics"]
            # Worst = lowest recall of the numbers the text layer says are
            # there (tables), lowest text similarity (prose).
            score = m.get("native_sig_in_parse") if kind == "table" else m.get("similarity")
            if score is None:
                score = m.get("similarity", 1.0)
            candidates[kind].append((score, rep, pg))

    out: list[dict[str, Any]] = []
    for kind in ("table", "prose"):
        for _score, rep, pg in sorted(candidates[kind], key=lambda t: t[0])[:per_kind]:
            excerpt = keep.get((rep["report_id"], pg["page"]))
            if excerpt is None:
                continue
            out.append(
                {
                    "kind": kind,
                    "page": pg["page"],
                    "report_id": rep["report_id"],
                    "report_title": rep["title"],
                    "similarity": pg["metrics"].get("similarity"),
                    "native_in_parse": pg["metrics"].get("native_sig_in_parse"),
                    "parse_in_native": pg["metrics"].get("parse_sig_in_native"),
                    "representations": excerpt,
                }
            )
    return out


# --------------------------------------------------------------------------
# Selection (Postgres, read-only) and download (storage client)
# --------------------------------------------------------------------------


class SelectionError(RuntimeError):
    """Nothing could be selected; the message says what to do about it."""


@dataclass
class ReportRef:
    report_id: str
    title: str
    source_object_key: str
    page_count: int | None
    is_scanned: bool | None
    workspace_id: str | None


@dataclass
class Selection:
    project_id: str
    project_slug: str | None
    workspace_id: str | None
    reports: list[ReportRef]
    project_total_pages: int | None


_PROJECT_SQL = """
SELECT p.project_id::text AS project_id, p.slug, p.workspace_id::text AS workspace_id,
       count(r.report_id) AS report_count,
       COALESCE(sum(r.page_count), 0)::bigint AS total_pages
  FROM silver.projects p
  JOIN silver.reports r ON r.project_id = p.project_id
 WHERE r.source_object_key IS NOT NULL AND r.source_object_key <> ''
   AND ($1::text IS NULL OR p.slug = $1)
 GROUP BY p.project_id, p.slug, p.workspace_id
 ORDER BY report_count DESC, p.slug
 LIMIT 1
"""

# Born-digital reports first (a scan has no native text to compare against),
# then the longest (more tables), one row per bronze object.
_REPORTS_SQL = """
SELECT DISTINCT ON (r.source_object_key)
       r.report_id::text AS report_id, r.title, r.source_object_key, r.page_count,
       r.is_scanned, r.workspace_id::text AS workspace_id
  FROM silver.reports r
 WHERE r.project_id = $1::uuid
   AND r.source_object_key IS NOT NULL AND r.source_object_key <> ''
 ORDER BY r.source_object_key
"""


async def select_reports(conn: Any, *, slug: str | None, workspace_id: str | None, max_reports: int) -> Selection:
    """Pick the project and reports. See the module docstring on RLS."""
    from app.db import bind_workspace_scope  # noqa: PLC0415

    async def _find(scope: str | None) -> Any:
        if scope is None:
            await conn.execute("SELECT set_config('app.workspace_id', '', false)")
        else:
            await bind_workspace_scope(conn, workspace_id=scope, site="parse_vs_native", is_local=False)
        return await conn.fetchrow(_PROJECT_SQL, slug)

    row = None
    tried: list[str] = []
    if workspace_id:
        row = await _find(workspace_id)
        tried.append(f"workspace {workspace_id}")
    else:
        row = await _find(None)
        tried.append("unscoped")
        if row is None:
            workspaces = [
                r["workspace_id"]
                for r in await conn.fetch(
                    "SELECT workspace_id::text AS workspace_id FROM silver.workspaces ORDER BY workspace_id"
                )
            ]
            best = None
            for ws in workspaces:
                cand = await _find(ws)
                tried.append(f"workspace {ws}")
                if cand is not None and (best is None or cand["report_count"] > best["report_count"]):
                    best = cand
            row = best
            if row is not None:
                await bind_workspace_scope(
                    conn, workspace_id=row["workspace_id"], site="parse_vs_native", is_local=False
                )

    if row is None:
        wanted = f"project slug {slug!r}" if slug else "any project"
        raise SelectionError(
            f"no reports with a source_object_key were visible for {wanted} (tried: {', '.join(tried)}). "
            "On a fresh deploy the corpus is empty. Otherwise RLS may be hiding the rows: pass --workspace-id."
        )

    rows = await conn.fetch(_REPORTS_SQL, row["project_id"])
    refs = [
        ReportRef(
            report_id=r["report_id"],
            title=r["title"] or "(untitled)",
            source_object_key=r["source_object_key"],
            page_count=r["page_count"],
            is_scanned=r["is_scanned"],
            workspace_id=r["workspace_id"],
        )
        for r in rows
    ]
    refs.sort(key=lambda r: (bool(r.is_scanned), -(r.page_count or 0), r.source_object_key))
    return Selection(
        project_id=row["project_id"],
        project_slug=row["slug"],
        workspace_id=row["workspace_id"],
        reports=refs[:max_reports],
        project_total_pages=int(row["total_pages"] or 0) or None,
    )


async def open_readonly_connection() -> Any:
    """A direct, session-read-only asyncpg connection built like every other
    Hatchet workflow's (app.db.dsn.build_dsn)."""
    import asyncpg  # noqa: PLC0415

    from app.db.dsn import build_dsn  # noqa: PLC0415

    conn = await asyncpg.connect(
        build_dsn(scheme="postgresql", include_sslmode=True), timeout=30, statement_cache_size=0
    )
    # Belt and braces for "read-only": the database refuses any write this
    # session attempts, whatever the code does.
    await conn.execute("SET default_transaction_read_only = on")
    await conn.execute("SET statement_timeout = '120s'")
    return conn


async def download_pdf(source_object_key: str, dest: str) -> None:
    """The ingest_pdf workflow's own download call (bronze bucket)."""
    from georag_object_storage import Bucket, get_async_storage_client  # noqa: PLC0415

    await get_async_storage_client().get_file(Bucket.BRONZE, source_object_key, dest)


# --------------------------------------------------------------------------
# One report
# --------------------------------------------------------------------------


def _is_pdf(path: str) -> bool:
    with open(path, "rb") as fh:
        return fh.read(5) == b"%PDF-"


async def analyse_report(
    ref: ReportRef,
    pdf_path: str,
    *,
    page_budget: int,
    dry_run: bool,
    secrets: Sequence[str],
    keep: dict[tuple[str, int], dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Scan, sample, (optionally) call Parse, compare. Returns
    ``(report_dict, parse_error_records)``."""
    base: dict[str, Any] = {
        "report_id": ref.report_id,
        "title": ref.title,
        "source_object_key": ref.source_object_key,
        "page_count": ref.page_count,
        "pages": [],
    }
    scan = await asyncio.to_thread(scan_native, pdf_path)
    base["page_count"] = scan.total_pages
    base["native_scan_errors"] = scan.errors[:20]
    samplable = [n for n, t in scan.text.items() if len(t) >= MIN_NATIVE_CHARS]
    base["image_only_pages"] = scan.total_pages - len(samplable)
    base["table_pages_detected"] = len(scan.tables)
    base["keyword_pages_detected"] = len(scan.keyword_pages)
    sample = select_pages(samplable, scan.tables.keys(), scan.keyword_pages, page_budget)
    if not sample:
        base["skipped"] = "no page has a native text layer to compare against"
        base["summary"] = summarize_pages([])
        return base, []

    outcomes: dict[int, ParseOutcome] = {}
    if not dry_run:
        outcomes = await parse_pages(pdf_path, [p for p, _ in sample], secrets=secrets)

    errors: list[dict[str, Any]] = []
    for page, reason in sample:
        outcome = None if dry_run else outcomes[page]
        result = compare_page(page, reason, scan.text[page], scan.tables.get(page, []), outcome)
        base["pages"].append(result)
        if outcome is not None and not outcome.ok:
            errors.append({"report_id": ref.report_id, "page": page, "error": outcome.error or "unknown"})
        if outcome is not None and outcome.ok:
            from app.services.ingest import pdf_report as pr  # noqa: PLC0415

            pl_md = (
                "\n\n".join(pr._table_to_markdown(t) for t in scan.tables.get(page, []))
                or "(pdfplumber found no table)"
            )
            pa_md = "\n\n".join(pr._table_to_markdown(t) for t in outcome.tables) or "(Parse returned no table)"
            keep[(ref.report_id, page)] = {
                "native text (pypdfium2)": _trim(scan.text[page]),
                "Parse text": _trim(outcome.text),
                "pdfplumber tables": _trim(pl_md),
                "Parse tables": _trim(pa_md),
            }
    base["summary"] = summarize_pages(base["pages"])
    return base, errors


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def build_result(
    args: argparse.Namespace,
    selection: Selection | None,
    reports: list[dict[str, Any]],
    parse_errors: list[dict[str, Any]],
    keep: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    all_pages = [p for r in reports for p in r.get("pages", [])]
    parse_pages_sent = 0 if args.dry_run else len(all_pages)
    by_reason = {
        reason: summarize_pages([p for p in all_pages if p["reason"] == reason])
        for reason in ("table", "keyword", "prose")
    }
    cohere_base = os.environ.get("COHERE_BASE_URL", "").strip() or "https://api.cohere.com"
    return {
        "meta": {
            "project_slug": selection.project_slug if selection else None,
            "project_id": selection.project_id if selection else None,
            "workspace_id": selection.workspace_id if selection else None,
            "dry_run": bool(args.dry_run),
            "max_reports": args.max_reports,
            "max_pages_per_report": args.max_pages_per_report,
            "parse_pages_sent": parse_pages_sent,
            "parse_pages_planned": len(all_pages),
            "parse_page_cap": MAX_PARSE_PAGES_PER_RUN,
            "usd_per_1000_pages_assumed": PARSE_USD_PER_1000_PAGES,
            "estimated_cost_usd": estimated_cost_usd(parse_pages_sent),
            "project_total_pages": selection.project_total_pages if selection else None,
            "parse_model": os.environ.get("COHERE_PARSE_MODEL", "").strip() or "parse-v5.0",
            "parse_host": re.sub(r"^https?://", "", cohere_base).split("/")[0],
            "api_key_present": bool((os.environ.get("COHERE_API_KEY") or "").strip()),
        },
        "overall": summarize_pages(all_pages),
        "by_reason": by_reason,
        "reports": reports,
        "parse_errors": parse_errors,
        "samples": _samples(reports, keep),
    }


def emit(result: dict[str, Any]) -> None:
    """Markdown summary, then the full JSON, each between markers."""
    print(BEGIN_SUMMARY)
    print(render_markdown(result))
    print(END_SUMMARY)
    print(BEGIN_JSON)
    # indent: keeps every log line short (CloudWatch splits very long events).
    print(json.dumps(result, indent=1, default=str))
    print(END_JSON)
    sys.stdout.flush()


async def run(args: argparse.Namespace) -> int:
    secrets = [s for s in (os.environ.get("COHERE_API_KEY", ""),) if s]
    if not args.dry_run and not secrets:
        print(
            "COHERE_API_KEY is not set on this task; nothing to compare against. Use --dry-run to measure the native side only.",
            file=sys.stderr,
        )
        return 2

    reports: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    keep: dict[tuple[str, int], dict[str, Any]] = {}
    selection: Selection | None = None
    parse_budget = MAX_PARSE_PAGES_PER_RUN

    with tempfile.TemporaryDirectory(prefix="parse_vs_native_") as tmp:
        work: list[tuple[ReportRef, str]] = []
        if args.local_pdf:
            for i, p in enumerate(args.local_pdf):
                work.append((ReportRef(f"local-{i}", Path(p).name, p, None, None, None), p))
        else:
            conn = await open_readonly_connection()
            try:
                selection = await select_reports(
                    conn, slug=args.project_slug, workspace_id=args.workspace_id, max_reports=args.max_reports
                )
            except SelectionError as exc:
                print(f"SELECTION FAILED: {exc}", file=sys.stderr)
                return 2
            finally:
                await conn.close()
            for i, ref in enumerate(selection.reports):
                dest = os.path.join(tmp, f"{i}.pdf")
                try:
                    await download_pdf(ref.source_object_key, dest)
                except Exception as exc:  # noqa: BLE001 — recorded on the report; the run continues
                    reports.append(
                        {
                            "report_id": ref.report_id,
                            "title": ref.title,
                            "source_object_key": ref.source_object_key,
                            "page_count": ref.page_count,
                            "pages": [],
                            "skipped": f"download failed: {type(exc).__name__}: {redact(str(exc), secrets=secrets)}",
                            "summary": summarize_pages([]),
                        }
                    )
                    continue
                if not _is_pdf(dest):
                    reports.append(
                        {
                            "report_id": ref.report_id,
                            "title": ref.title,
                            "source_object_key": ref.source_object_key,
                            "page_count": ref.page_count,
                            "pages": [],
                            "skipped": "object is not a PDF (no %PDF- header)",
                            "summary": summarize_pages([]),
                        }
                    )
                    continue
                work.append((ref, dest))

        for ref, path in work:
            budget = min(args.max_pages_per_report, parse_budget)
            if budget <= 0:
                reports.append(
                    {
                        "report_id": ref.report_id,
                        "title": ref.title,
                        "source_object_key": ref.source_object_key,
                        "page_count": ref.page_count,
                        "pages": [],
                        "skipped": f"run-wide Parse cap of {MAX_PARSE_PAGES_PER_RUN} pages reached",
                        "summary": summarize_pages([]),
                    }
                )
                continue
            try:
                rep, errs = await analyse_report(
                    ref, path, page_budget=budget, dry_run=args.dry_run, secrets=secrets, keep=keep
                )
            except Exception as exc:  # noqa: BLE001 — one unreadable PDF must not sink the others
                logger.error(
                    "analysis failed for %s: %s: %s",
                    ref.report_id,
                    type(exc).__name__,
                    redact(str(exc), secrets=secrets),
                )
                reports.append(
                    {
                        "report_id": ref.report_id,
                        "title": ref.title,
                        "source_object_key": ref.source_object_key,
                        "page_count": ref.page_count,
                        "pages": [],
                        "skipped": f"analysis failed: {type(exc).__name__}: {redact(str(exc), secrets=secrets)}",
                        "summary": summarize_pages([]),
                    }
                )
                continue
            reports.append(rep)
            parse_errors.extend(errs)
            parse_budget -= len(rep["pages"])

    result = build_result(args, selection, reports, parse_errors, keep)
    emit(result)

    compared = sum(len(r.get("pages", [])) for r in reports)
    if compared == 0:
        print("NOTHING COMPARED: no report produced a samplable page.", file=sys.stderr)
        return 2
    if not args.dry_run and result["overall"]["parse_answered"] == 0:
        print(
            "EVERY PARSE REQUEST FAILED: see the errors above. The report is printed, but this is not a pass.",
            file=sys.stderr,
        )
        return 3
    return 0


def _bounded_int(low: int, high: int):
    def _parse(raw: str) -> int:
        value = int(raw)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value

    return _parse


def _slug(raw: str) -> str:
    if not _SLUG_RE.fullmatch(raw):
        raise argparse.ArgumentTypeError("must match ^[a-z0-9-]{1,64}$")
    return raw


def _uuid(raw: str) -> str:
    if not _UUID_RE.fullmatch(raw):
        raise argparse.ArgumentTypeError("must be a UUID")
    return raw


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compare Cohere Parse with the native PDF stack on real reports (read-only)."
    )
    p.add_argument("--project-slug", type=_slug, default=None, help="default: the project with the most reports")
    p.add_argument("--max-reports", type=_bounded_int(1, MAX_REPORTS_LIMIT), default=3)
    p.add_argument("--max-pages-per-report", type=_bounded_int(1, MAX_PARSE_PAGES_PER_RUN), default=30)
    p.add_argument(
        "--dry-run", action="store_true", help="select and sample, measure the native side, call nothing paid"
    )
    p.add_argument(
        "--workspace-id",
        type=_uuid,
        default=None,
        help="scope to one workspace when RLS hides rows from an unscoped read",
    )
    p.add_argument(
        "--local-pdf",
        action="append",
        default=[],
        help="testing aid: compare a local PDF instead of reading Postgres/S3 (repeatable)",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 — last-resort report, exit 1
        secrets = [s for s in (os.environ.get("COHERE_API_KEY", ""),) if s]
        print(f"PARSE_VS_NATIVE FAILED: {type(exc).__name__}: {redact(str(exc), secrets=secrets)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
