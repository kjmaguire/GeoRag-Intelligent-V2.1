"""GeoRAG ingestion parsers — public API surface.

Import from this module to get format parsers without coupling to internal
module structure. All public symbols are re-exported here.
"""

from georag_dagster.parsers.pdf_report import (
    ReportParseResult,
    ReportSection,
    parse_pdf_report,
)
from georag_dagster.parsers.docx_parser import (
    DocxParseResult,
    parse_doc_or_docx_report,
    parse_docx_report,
)
from georag_dagster.parsers.raster_parser import (
    RasterBandStats,
    RasterParseResult,
    parse_raster_file,
)
from georag_dagster.parsers.xlsx_parser import (
    ExcelParseResult,
    XlsxParseResult,   # backward-compatible alias
    parse_xlsx_sheet,
)

__all__ = [
    # PDF
    "ReportParseResult",
    "ReportSection",
    "parse_pdf_report",
    # DOCX / DOC
    "DocxParseResult",
    "parse_docx_report",
    "parse_doc_or_docx_report",
    # Raster (Sprint 4)
    "RasterBandStats",
    "RasterParseResult",
    "parse_raster_file",
    # Excel — .xlsx, .xlsm, .xls (Sprint 4)
    "ExcelParseResult",
    "XlsxParseResult",   # backward-compatible alias
    "parse_xlsx_sheet",
]
