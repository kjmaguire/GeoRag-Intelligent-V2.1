"""Run the PaddleOCR-VL vs Docling shadow eval — ADR-0016 Phase 2 step 4.

Operator step for the docparser shadow gate (app.services.eval.docparser_shadow).
Runs BOTH full-page parsers on each PDF, prints per-PDF metrics, and emits the
aggregate `assess_docparser_shadow` recommendation (promote / hold /
insufficient_data).

Usage (inside the fastapi container — it has paddleocr + docling + GPU access):
    python scripts/run_docparser_shadow.py <pdf1> [<pdf2> ...]
    python scripts/run_docparser_shadow.py --pages 0,1,2 <pdf>   # limit pages

PaddleOCR-VL loads in-process. With the CPU paddle wheel it runs on CPU (slow —
expect minutes per multi-page PDF); the first run downloads the model weights.
With paddlepaddle-gpu installed and VRAM free it moves to GPU automatically.
Output is written to stdout AND, if --json <path> is given, as a JSON report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from app.services.eval.docparser_shadow import (
    assess_docparser_shadow,
    run_docparser_shadow_pair,
)


async def _run(paths: list[str], pages: list[int] | None) -> dict:
    observations = []
    for p in paths:
        pdf = Path(p)
        if not pdf.exists():
            print(f"[skip] {p} — not found", flush=True)
            continue
        print(f"[shadow] {pdf.name} ...", flush=True)
        t0 = time.perf_counter()
        obs = await run_docparser_shadow_pair(pdf, pdf_id=pdf.name, pages=pages)
        dt = time.perf_counter() - t0
        observations.append(obs)
        print(
            f"  done in {dt:.1f}s | pages={obs.page_count}\n"
            f"    docling: tables={obs.docling_tables} rows={obs.docling_table_rows} "
            f"figures={obs.docling_figures} headings={obs.docling_headings} "
            f"text={obs.docling_text_regions} ({obs.docling_latency_ms:.0f}ms)\n"
            f"    vl     : tables={obs.vl_tables} rows={obs.vl_table_rows} "
            f"figures={obs.vl_figures} headings={obs.vl_headings} "
            f"text={obs.vl_text_regions} ({obs.vl_latency_ms:.0f}ms)",
            flush=True,
        )
    assessment = assess_docparser_shadow(observations)
    report = {
        "n_pdfs": len(observations),
        "assessment": assessment.to_dict(),
    }
    print("\n=== ASSESSMENT (ADR-0016 step 4) ===", flush=True)
    print(json.dumps(assessment.to_dict(), indent=2), flush=True)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--pages", default=None, help="comma-separated 0-based page indices")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()
    pages = [int(x) for x in args.pages.split(",")] if args.pages else None
    report = asyncio.run(_run(args.pdfs, pages))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
        print(f"\n[wrote] {args.json_out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
