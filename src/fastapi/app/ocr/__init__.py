"""GeoRAG §04p PDF parser stack (in-process module).

Per ADR-0002, the §04p stack runs as an in-process module inside the
existing `georag/fastapi:latest` image. Hatchet ingest_pdf step functions
import these parsers directly — no HTTP IPC, no separate container.

**Import-boundary rule** (enforced by
`scripts/phase3_master_plan_step1_import_boundary.sh`): nothing under
`app/routers/` or `app/main.py` may import `app.ocr`. Only
`app/hatchet_workflows/ingest_pdf.py` and tests may import this package.
The rule keeps PaddleOCR + Docling out of the user-facing FastAPI
process's resident memory.

Module surface (master-plan §3, doc-phase 49+):
- preflight        — qpdf/pikepdf preflight (Step 1 skeleton, Step 3 impl)
- profile          — PDF profile classification (Step 1 skeleton, Step 3 impl)
- parse_native     — pdfminer.six + pdfplumber (Step 1 skeleton, Step 3 impl)
- parse_scanned    — PaddleOCR PP-OCRv5 CPU (Step 1 skeleton, Step 4 impl)
- parse_mixed      — Docling layout-first (Step 1 skeleton, Step 5 impl)
- parse_table_heavy — pdfplumber + Docling table focus (Step 1 skeleton, Step 5 impl)
- render           — pypdfium2 page-to-image (Step 1 skeleton, Step 4 impl)
- quality_graph    — LangGraph OCR Quality Graph (Step 1 skeleton, Step 6 impl)

Step 1 (this doc-phase): typed async signatures + NotImplementedError
bodies. The interface contract is locked here; behavioural
implementations land in subsequent doc-phase ticks.

CPU-OCR latency baselines measured 2026-05-12 (see
`ops/validation/reports/ocr_cpu_smoke_*.json`):
- Native: ~60 ms/page
- Scanned (PaddleOCR PP-OCRv5, image-input path): ~6 sec/page warm
- Mixed (Docling layout-first): ~12 sec/page
"""
from __future__ import annotations

from app.ocr.parse_docparser_vl import parse_docparser_vl
from app.ocr.parse_mixed import parse_mixed
from app.ocr.parse_native import parse_native
from app.ocr.parse_scanned import parse_scanned
from app.ocr.parse_table_heavy import parse_table_heavy

# Skeleton re-exports — concrete implementations land in later steps.
from app.ocr.preflight import preflight
from app.ocr.profile import profile
from app.ocr.quality_graph import route_page, summarize_document
from app.ocr.render import render_page

__all__ = [
    "preflight",
    "profile",
    "parse_native",
    "parse_scanned",
    "parse_mixed",
    "parse_table_heavy",
    "parse_docparser_vl",
    "render_page",
    "route_page",
    "summarize_document",
]
