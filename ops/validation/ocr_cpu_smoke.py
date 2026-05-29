"""CPU-OCR smoke-bench for the §04p stack on this Threadripper.

Runs inside `georag-hatchet-worker-ingestion` (which already has the
§04p libs installed per ADR-0002 amendment 2026-05-12). Measures
per-page wall-clock latency for the four parser paths the master plan
§3 will exercise, on a known input PDF.

Inputs:
- One real native PDF (defaults to the PLS-2024-Technical-Report
  fixture)
- Synthetic scanned + mixed variants are generated in-memory by
  rasterizing the native PDF's first 5 pages via pypdfium2 and
  reassembling them into a new PDF with pikepdf.

Outputs:
- JSON report to /tmp/ocr_cpu_smoke_<timestamp>.json inside the
  container; the bash wrapper copies it out to
  ops/validation/reports/ on the host.

Acceptance (per kickoff Step 1):
- Native latency within 1-25 sec/page
- Scanned latency within 5-150 sec/page
- Library import + first-call cold-start measured separately

This script is intentionally minimal — it does NOT replicate the full
Step 4-5 parser logic (deskew, layout dispatch, table extraction
heuristics). It measures the raw library invocation cost so we know
the CPU floor.
"""
from __future__ import annotations

import io
import json
import os
import statistics
import sys
import time
import traceback
from pathlib import Path

REPORT_PATH = Path(f"/tmp/ocr_cpu_smoke_{int(time.time())}.json")
NATIVE_PDF_DEFAULT = "/app/src/dagster/tests/fixtures/reports/PLS-2024-Technical-Report.pdf"
SCANNED_PAGES = 5
MIXED_PAGES = 5


def time_block(label: str, fn):
    """Run fn(), return (wall_ms, result_or_exception)."""
    t0 = time.perf_counter()
    try:
        out = fn()
        wall_ms = (time.perf_counter() - t0) * 1000
        return {"label": label, "wall_ms": round(wall_ms, 2), "ok": True, "value": out}
    except Exception as exc:
        wall_ms = (time.perf_counter() - t0) * 1000
        return {
            "label": label,
            "wall_ms": round(wall_ms, 2),
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=4),
        }


def bench_imports() -> dict:
    """Cold-start cost of importing each parser library.

    Discards the module object itself from the report (modules aren't
    JSON-serializable) — we only want the timing.
    """
    results = []
    for module_name in (
        "pikepdf",
        "pypdfium2",
        "pdfminer.high_level",
        "pdfplumber",
        "docling",
        "paddleocr",
    ):
        block = time_block(f"import:{module_name}", lambda m=module_name: __import__(m))
        # Replace the module object with a string marker so the report is JSON-safe.
        if block.get("ok"):
            block["value"] = "imported"
        results.append(block)
    return {"imports": results}


def make_synthetic_scanned_pdf(source_path: Path, page_count: int) -> bytes:
    """Rasterize the first N pages of source_path and return a single PDF as bytes.

    pypdfium2.PdfDocument -> render each page to PIL Image -> stitch into a new
    image-only PDF via pikepdf.

    The result is a PDF with no extractable text layer; pdfminer.six will return
    empty strings; PaddleOCR has to do the work.
    """
    import pypdfium2 as pdfium
    from PIL import Image

    src = pdfium.PdfDocument(str(source_path))
    images: list[Image.Image] = []
    for page_idx in range(min(page_count, len(src))):
        page = src[page_idx]
        bitmap = page.render(scale=2.0)
        pil = bitmap.to_pil().convert("RGB")
        images.append(pil)

    buf = io.BytesIO()
    images[0].save(
        buf,
        format="PDF",
        save_all=True,
        append_images=images[1:],
    )
    return buf.getvalue()


def make_synthetic_mixed_pdf(source_path: Path, page_count: int) -> bytes:
    """Interleave native pages with rasterized pages.

    For each page idx in range(page_count):
      - even idx → keep the original PDF page (native text layer)
      - odd idx  → rasterize and stitch in (no text layer)

    Produces a PDF that should classify as "mixed" by the profiler.
    """
    import pikepdf
    import pypdfium2 as pdfium
    from PIL import Image

    src_pikepdf = pikepdf.open(str(source_path))
    src_pdfium = pdfium.PdfDocument(str(source_path))

    out = pikepdf.Pdf.new()
    for idx in range(min(page_count, len(src_pikepdf.pages))):
        if idx % 2 == 0:
            out.pages.append(src_pikepdf.pages[idx])
        else:
            page = src_pdfium[idx]
            bitmap = page.render(scale=2.0)
            pil = bitmap.to_pil().convert("RGB")
            page_buf = io.BytesIO()
            pil.save(page_buf, format="PDF")
            page_buf.seek(0)
            stitched = pikepdf.open(page_buf)
            out.pages.append(stitched.pages[0])

    out_buf = io.BytesIO()
    out.save(out_buf)
    return out_buf.getvalue()


def bench_native_parse(pdf_path: Path) -> dict:
    """pdfminer.six text + pdfplumber tables for the first N pages."""
    import pdfminer.high_level
    import pdfplumber

    def parse():
        text = pdfminer.high_level.extract_text(str(pdfm_path), maxpages=SCANNED_PAGES)
        with pdfplumber.open(str(pdf_path)) as pdf:
            tables = [
                p.extract_tables() or []
                for p in pdf.pages[:SCANNED_PAGES]
            ]
        return {
            "chars_extracted": len(text),
            "tables_per_page": [len(ts) for ts in tables],
        }

    pdfm_path = pdf_path
    return time_block("parse_native:first_5_pages", parse)


def bench_scanned_parse(pdf_bytes: bytes) -> dict:
    """PaddleOCR PP-OCRv5 (CPU) on a synthetic image-only PDF.

    PaddleOCR's PDF input path requires PyMuPDF (`fitz`) which isn't
    installed in the FastAPI image. The real Step 4 parser will pre-render
    pages to images via pypdfium2 and then call PaddleOCR with image
    input — bypassing fitz entirely. This bench mirrors that pattern.

    Writes the bytes to /tmp/_smoke_scanned.pdf, renders each page to a
    numpy array via pypdfium2, then invokes paddleocr.ocr() with the
    array (PaddleOCR's image-input path is fitz-free).
    """
    out_path = Path("/tmp/_smoke_scanned.pdf")
    out_path.write_bytes(pdf_bytes)

    def parse():
        import numpy as np
        import pypdfium2 as pdfium
        from paddleocr import PaddleOCR

        # use_angle_cls=False to keep the bench minimal; real Step 4 will
        # add deskew + angle classification.
        ocr = PaddleOCR(use_angle_cls=False, lang="en", show_log=False)

        # Render each page to a numpy array. PaddleOCR.ocr() accepts a
        # numpy ndarray for image input — no fitz needed.
        pdf = pdfium.PdfDocument(str(out_path))
        page_results = []
        pages_ocrd = 0
        text_lines = 0
        for page_idx in range(len(pdf)):
            page = pdf[page_idx]
            bitmap = page.render(scale=2.0)
            arr = np.asarray(bitmap.to_pil().convert("RGB"))
            r = ocr.ocr(arr)
            pages_ocrd += 1
            text_lines += sum(len(p) for p in (r or []) if p)
            page_results.append(len(r[0]) if r and r[0] else 0)

        return {
            "pages_ocrd": pages_ocrd,
            "text_lines": text_lines,
            "per_page_text_line_counts": page_results,
        }

    return time_block("parse_scanned:first_5_pages_synthetic", parse)


def bench_mixed_parse(pdf_bytes: bytes) -> dict:
    """Docling layout-first on a synthetic mixed PDF."""
    out_path = Path("/tmp/_smoke_mixed.pdf")
    out_path.write_bytes(pdf_bytes)

    def parse():
        from docling.document_converter import DocumentConverter

        conv = DocumentConverter()
        result = conv.convert(str(out_path))
        doc = result.document
        return {
            "page_count": len(doc.pages) if hasattr(doc, "pages") else None,
            "text_chars": len(doc.export_to_text()) if hasattr(doc, "export_to_text") else None,
        }

    return time_block("parse_mixed:first_5_pages_synthetic", parse)


def main() -> int:
    native_path = Path(os.environ.get("OCR_SMOKE_NATIVE_PDF", NATIVE_PDF_DEFAULT))
    if not native_path.exists():
        print(f"FATAL: native input PDF not found at {native_path}", file=sys.stderr)
        return 2

    report: dict = {
        "schema_version": 1,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "native_input": str(native_path),
        "pages_sampled_per_path": SCANNED_PAGES,
        "host": os.environ.get("HOSTNAME", "unknown"),
        "cpu_count": os.cpu_count(),
    }

    print("=== imports ===", flush=True)
    report.update(bench_imports())

    print("=== synthesizing scanned + mixed fixtures ===", flush=True)
    scanned_bytes_block = time_block(
        "synth:scanned_pdf",
        lambda: make_synthetic_scanned_pdf(native_path, SCANNED_PAGES),
    )
    mixed_bytes_block = time_block(
        "synth:mixed_pdf",
        lambda: make_synthetic_mixed_pdf(native_path, MIXED_PAGES),
    )
    report["synth"] = [
        {**scanned_bytes_block, "value": f"{len(scanned_bytes_block.get('value') or b'')} bytes"},
        {**mixed_bytes_block, "value": f"{len(mixed_bytes_block.get('value') or b'')} bytes"},
    ]

    if not (scanned_bytes_block["ok"] and mixed_bytes_block["ok"]):
        print("FATAL: could not synthesize fixtures; skipping parser benches.", file=sys.stderr)
        report["aborted"] = "synth_failure"
        REPORT_PATH.write_text(json.dumps(report, indent=2))
        print(f"report: {REPORT_PATH}")
        return 3

    print("=== parse_native ===", flush=True)
    report["parse_native"] = bench_native_parse(native_path)

    print("=== parse_scanned (cold + warm) ===", flush=True)
    cold = bench_scanned_parse(scanned_bytes_block["value"])
    warm = bench_scanned_parse(scanned_bytes_block["value"])
    report["parse_scanned"] = {
        "cold": cold,
        "warm": warm,
        "per_page_cold_ms": round(cold["wall_ms"] / SCANNED_PAGES, 2) if cold["ok"] else None,
        "per_page_warm_ms": round(warm["wall_ms"] / SCANNED_PAGES, 2) if warm["ok"] else None,
    }

    print("=== parse_mixed ===", flush=True)
    report["parse_mixed"] = bench_mixed_parse(mixed_bytes_block["value"])

    # Acceptance gate: report whether measured latencies fall inside the
    # ADR-0002 estimate ranges, scaled by 5x.
    def gate(label: str, per_page_ms: float | None, lo_sec: float, hi_sec: float) -> dict:
        """ADR-0002 acceptance: measured per-page latency must NOT exceed
        upper bound × 5. Below lower bound is a pass (faster than estimate).
        Only above upper bound × 5 is a fail (catastrophically slow).
        """
        if per_page_ms is None:
            return {"label": label, "verdict": "no_measurement"}
        hi_ms = hi_sec * 1000 * 5
        if per_page_ms <= hi_ms:
            note = "faster_than_estimate" if per_page_ms < lo_sec * 1000 else "within_envelope"
            return {
                "label": label,
                "per_page_ms": per_page_ms,
                "envelope_lo_ms": lo_sec * 1000,
                "envelope_hi_ms": hi_sec * 1000,
                "fail_threshold_ms": hi_ms,
                "verdict": "pass",
                "note": note,
            }
        return {
            "label": label,
            "per_page_ms": per_page_ms,
            "envelope_lo_ms": lo_sec * 1000,
            "envelope_hi_ms": hi_sec * 1000,
            "fail_threshold_ms": hi_ms,
            "verdict": "fail",
            "note": "exceeds_5x_envelope",
        }

    report["gates"] = [
        gate(
            "native",
            (report["parse_native"]["wall_ms"] / SCANNED_PAGES)
            if report["parse_native"]["ok"]
            else None,
            1.0,
            5.0,
        ),
        gate("scanned_warm", report["parse_scanned"]["per_page_warm_ms"], 5.0, 30.0),
        gate(
            "mixed",
            (report["parse_mixed"]["wall_ms"] / MIXED_PAGES)
            if report["parse_mixed"]["ok"]
            else None,
            5.0,
            30.0,
        ),
    ]

    overall = "pass" if all(g.get("verdict") == "pass" for g in report["gates"]) else "investigate"
    report["overall"] = overall
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # default=str handles any future stray non-serializable value defensively;
    # don't lose a 5-minute bench run to a serialization edge case.
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nreport: {REPORT_PATH}")
    print(f"overall: {overall}")
    return 0 if overall == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
