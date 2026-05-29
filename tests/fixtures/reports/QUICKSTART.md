# Quick Start: Generate NI 43-101 Test Fixture

## TL;DR

```bash
pip install reportlab PyPDF2
python tests/fixtures/reports/generate_test_report.py
```

Expected output:
```
PDF generated successfully: /path/to/tests/fixtures/reports/PLS-2024-Technical-Report.pdf
File size: 156,234 bytes (152.6 KB)
Page count: 13
```

## What You Get

A realistic 13-page NI 43-101 technical report PDF for testing GeoRAG's document ingestion pipeline.

**Report Details:**
- **Project:** Patterson Lake South (uranium, Athabasca Basin, Saskatchewan)
- **Operator:** Fission Uranium Corp.
- **Sections:** 17 (Summary through Recommendations, per NI 43-101 standard)
- **Key Data:**
  - Indicated resource: 5.2 Mt @ 1.52% U₃O₈ (174 Mlb)
  - Inferred resource: 3.8 Mt @ 0.85% U₃O₈ (71 Mlb)
  - 47 drill holes, 15,000+ meters total
  - Qualified Persons: Dr. Sarah Thompson, P.Geo. and Dr. James Chen, P.Eng.

## File Locations

```
tests/fixtures/reports/
├── generate_test_report.py           # Main generator script
├── PLS-2024-Technical-Report.pdf     # Generated fixture (after running script)
├── README.md                          # Detailed documentation
├── QUICKSTART.md                      # This file
├── run_generator.sh                   # Bash wrapper for convenience
├── verify_and_build.py               # Alternative build script
└── .env.example                       # Configuration template
```

## Prerequisites

**Minimal (for PDF generation):**
```bash
pip install reportlab
```

**Full (with page count verification):**
```bash
pip install reportlab PyPDF2
```

## Running the Generator

### From Repository Root

```bash
cd tests/fixtures/reports
python generate_test_report.py
```

### From Anywhere (Absolute Path)

```bash
python /path/to/tests/fixtures/reports/generate_test_report.py
```

### Using Bash Wrapper

```bash
bash tests/fixtures/reports/run_generator.sh
```

### Using Docker

```bash
docker run --rm \
  -v "$(pwd):/work" \
  -w /work/tests/fixtures/reports \
  python:3.13-slim \
  sh -c "pip install reportlab PyPDF2 && python generate_test_report.py"
```

## Verify the Generated PDF

Check that the file was created:

```bash
ls -lh tests/fixtures/reports/PLS-2024-Technical-Report.pdf
```

Expected output:
```
-rw-r--r-- 1 user user 156K Apr 10 15:23 PLS-2024-Technical-Report.pdf
```

Verify the PDF is valid:

```bash
# Check file signature
head -c 4 tests/fixtures/reports/PLS-2024-Technical-Report.pdf
# Should output: %PDF

# Get file info (if pdfinfo is installed)
pdfinfo tests/fixtures/reports/PLS-2024-Technical-Report.pdf
```

## Using in Tests

### Python (Pydantic/FastAPI)

```python
from pathlib import Path
from tests.conftest import FIXTURES_PATH

REPORT_PATH = FIXTURES_PATH / 'reports' / 'PLS-2024-Technical-Report.pdf'

def test_ingest_ni43101():
    assert REPORT_PATH.exists()
    # Test ingestion...
```

### PHP (Laravel)

```php
use Tests\TestCase;

class DocumentIngestionTest extends TestCase
{
    protected const FIXTURE_PATH = __DIR__ . '/../fixtures/reports/PLS-2024-Technical-Report.pdf';
    
    public function test_ingest_ni43101_report()
    {
        $this->assertFileExists(self::FIXTURE_PATH);
        // Test ingestion...
    }
}
```

## Troubleshooting

**Issue:** `ModuleNotFoundError: No module named 'reportlab'`

**Solution:** Install reportlab
```bash
pip install --upgrade reportlab
```

---

**Issue:** PDF file is not created

**Solution:** Check that you're running the script from the correct directory and that the directory is writable:
```bash
python -c "import os; print(os.getcwd())"
ls -l tests/fixtures/reports/
```

---

**Issue:** Page count shows as 0 or doesn't print

**Solution:** PyPDF2 is optional. Install it to get page counts:
```bash
pip install PyPDF2
```

---

**Issue:** Want to customize the report content

**Solution:** Edit `generate_test_report.py` and re-run. The script is well-commented with easy-to-find sections for each report section. See the [main README](./README.md) for details.

## File Cleanup

The generated PDF is a test fixture and should NOT be committed to git.

Verify your `.gitignore` includes:
```
tests/fixtures/reports/*.pdf
```

To remove a locally generated fixture:
```bash
rm tests/fixtures/reports/PLS-2024-Technical-Report.pdf
```

## Next Steps

1. **Generate the fixture:** Run the script (see above)
2. **Verify:** Check that the PDF was created
3. **Test:** Use the fixture in your document ingestion tests
4. **Iterate:** Modify `generate_test_report.py` if you need different test data

See [README.md](./README.md) for detailed documentation on report content, structure, and usage patterns.
