# NI 43-101 Technical Report Fixtures

This directory contains synthetic NI 43-101 technical reports generated for testing the GeoRAG document ingestion and RAG pipeline.

## Files

- **`generate_test_report.py`** — Python script that generates a synthetic NI 43-101 technical report PDF for the Patterson Lake South uranium project. Uses `reportlab` to create a realistic multi-section document with proper formatting, page numbering, and geological content.
- **`PLS-2024-Technical-Report.pdf`** — Generated test fixture (created by running the generator script).

## Generating the Report

### Option 1: Direct Python Execution (Recommended)

Requires `reportlab` and optionally `PyPDF2` for page count verification:

```bash
pip install reportlab PyPDF2
cd tests/fixtures/reports
python generate_test_report.py
```

Output:
- Generates `PLS-2024-Technical-Report.pdf` in the current directory
- Prints file size and page count to stdout

### Option 2: Docker

```bash
docker run --rm -v "$(pwd):/work" -w /work/tests/fixtures/reports python:3.13-slim sh -c "pip install reportlab PyPDF2 && python generate_test_report.py"
```

## Report Content

The generated PDF contains a complete NI 43-101 technical report for the Patterson Lake South uranium project:

**Title Page:**
- Property: Patterson Lake South, Athabaska Basin, Saskatchewan
- Operator: Fission Uranium Corp.
- Effective Date: June 15, 2024
- Report Date: August 30, 2024
- Qualified Persons: Dr. Sarah Thompson, P.Geo. and Dr. James Chen, P.Eng.

**Sections (17 total, ~10-15 pages):**
1. Summary — Resource estimates: 5.2 Mt @ 1.52% U₃O₈ (Indicated), 3.8 Mt @ 0.85% U₃O₈ (Inferred)
2. Introduction — Compliance with NI 43-101, site visit data
3. Reliance on Other Experts — Standard disclaimer
4. Property Description and Location — 31,039 hectares, UTM 13N, 495000E / 6220000N
5. Accessibility, Climate, Local Resources, Infrastructure and Physiography — Subarctic setting, remote access
6. History — Exploration from 1970s to present, 47 drill holes
7. Geological Setting and Mineralization — Unconformity-type uranium, basement graphitic conductors
8. Deposit Types — Comparable to McArthur River, Cigar Lake, Arrow deposits
9. Exploration — VTEM, TDEM, geochemistry, 47 drill holes (15,000+ m total)
10. Drilling — Current program: 10 holes, 3,645 m; key intercepts >50,000 ppm U₃O₈
11. Sample Preparation, Analyses and Security — ICP-MS assay, SRC Geoanalytical Labs, QA/QC protocols
12. Data Verification — Qualified Person site visit, 50 interval cross-checks
13. Mineral Resource Estimates — Kriging methodology, CIM 2014 definitions
14. Adjacent Properties — Wheeler River, Arrow deposit, regional context
15. Other Relevant Data and Information — Environmental baseline, Indigenous consultation
16. Interpretation and Conclusions — 245 Mlb contained uranium, further exploration warranted
17. Recommendations — Two-phase program: Phase 1 (2025, 12,000 m, CAD $8.5M), Phase 2 (2026, 8,000 m, CAD $6.2M)

## Usage in Tests

The PDF fixture is intended for:

- **Document ingestion testing:** Verify that GeoRAG can parse and extract text from multi-section technical reports
- **Schema mapping:** Test extraction of key data (resource estimates, drill holes, assay results) into structured fields
- **RAG pipeline testing:** Validate that document chunks are properly indexed and retrievable
- **Hallucination prevention:** Test that answers to queries are grounded in document citations

### Example Test Usage

```python
# In your test file
import pytest
from pathlib import Path

FIXTURE_PATH = Path(__file__).parent / 'reports' / 'PLS-2024-Technical-Report.pdf'

def test_ingest_ni43101_report():
    """Test ingestion of NI 43-101 technical report."""
    assert FIXTURE_PATH.exists(), "Report fixture not found. Run: python tests/fixtures/reports/generate_test_report.py"
    
    # Ingest and test
    document = ingest_pdf(FIXTURE_PATH)
    assert len(document.chunks) > 0
    assert 'Patterson Lake South' in document.text
    assert 'Triple R' in document.text
```

## Technical Details

**Python Dependencies:**
- `reportlab>=4.0` — PDF generation library
- `PyPDF2>=4.0` (optional) — For page count verification

**PDF Format:**
- Page size: US Letter (8.5" x 11")
- Margins: 0.75" on all sides
- Fonts: Helvetica (headings), Times-Roman (body)
- Encoding: UTF-8 with proper Unicode support (subscripts like U₃O₈)

**Output Specifications:**
- File: `PLS-2024-Technical-Report.pdf`
- Expected size: ~150-200 KB (depends on reportlab version)
- Expected pages: 12-15 pages
- Page numbering: Centered footer on each page

## Notes

- This is a **synthetic document** created for testing purposes. While it follows the structure and content conventions of real NI 43-101 reports, it does not represent actual project data.
- All geological parameters (coordinates, assay results, resource estimates) are fictional and chosen to create a realistic test case.
- The report complies with Canadian NI 43-101 section structure and numbering conventions.

## Regenerating the Fixture

If you need to update the report content, edit `generate_test_report.py` and re-run the generator. The script is structured to make it easy to modify:

- Title page text: Edit the story builder section at the top of `generate_report()`
- Section headings and body: Edit each `story.append()` block
- Fonts and styling: Modify `create_title_page_style()` and `create_section_style()` functions

Example: To change the project name globally, search and replace `Patterson Lake South` throughout the script.
