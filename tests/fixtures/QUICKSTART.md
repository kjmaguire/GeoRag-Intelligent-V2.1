# GeoRAG Test Fixtures — Quick Start

Generate test fixtures for format parser validation.

## One-Liner (Recommended)

```bash
cd /path/to/GeoRAG\ Intelligence\ V.1.0
python tests/fixtures/generate_all_fixtures_v2.py
```

Generates all three fixtures with dependency checking.

## Manual Generation

```bash
# Install dependencies first
pip install openpyxl segyio numpy

# Generate all at once
cd tests/fixtures
python excel/generate_test_xlsx.py
python seismic/generate_test_segy.py
python xyz/generate_test_xyz.py
```

## Docker (Recommended for CI/CD)

```bash
docker run --rm -v "$(pwd):/work" -w /work/tests/fixtures \
  python:3.13-slim sh -c \
  "pip install -q openpyxl segyio numpy && python generate_all_fixtures_v2.py"
```

## Files Created

After generation, you'll have:

```
tests/fixtures/
├── excel/
│   └── PLS_collars.xlsx          (6 KB) — 10 drill holes
├── seismic/
│   └── test_2D_line.sgy          (500 KB) — 100 traces, 500 samples
└── xyz/
    └── PLS_magnetics.xyz         (18 KB) — 200 data points, 4 lines
```

Pre-created:
- `xyz/PLS_magnetics.xyz` — Already exists (text fixture)

To generate:
- `excel/PLS_collars.xlsx` — Run generator
- `seismic/test_2D_line.sgy` — Run generator

## Verify Generation

```bash
# Check files exist and sizes are reasonable
ls -lh tests/fixtures/excel/PLS_collars.xlsx
ls -lh tests/fixtures/seismic/test_2D_line.sgy
ls -lh tests/fixtures/xyz/PLS_magnetics.xyz
```

Expected sizes:
- Excel: 5–10 KB
- SEG-Y: 400–600 KB
- XYZ: 15–25 KB

## Copy to Dagster

```bash
# Automatic
bash tests/fixtures/copy_to_dagster.sh

# Manual
mkdir -p src/dagster/tests/fixtures/{excel,seismic,xyz}
cp tests/fixtures/excel/PLS_collars.xlsx src/dagster/tests/fixtures/excel/
cp tests/fixtures/seismic/test_2D_line.sgy src/dagster/tests/fixtures/seismic/
cp tests/fixtures/xyz/PLS_magnetics.xyz src/dagster/tests/fixtures/xyz/
```

## What Each Fixture Tests

| Fixture | Format | Purpose |
|---------|--------|---------|
| **PLS_collars.xlsx** | Excel | Workbook parsing, collar coordinate validation, date handling |
| **test_2D_line.sgy** | SEG-Y | Header parsing, trace data validation, endianness detection |
| **PLS_magnetics.xyz** | XYZ | Multi-line handling, geophysical value ranges, CRS detection |

## Fixture Specs

**Excel (PLS_collars.xlsx):**
- 10 drill holes (XLS-24-01 to XLS-24-10)
- UTM Zone 13N coordinates
- Depths: 280–480 m
- Dates: Jan–Aug 2024

**SEG-Y (test_2D_line.sgy):**
- 100 traces, 500 samples each
- 2 ms sample interval
- 50 Hz synthetic sine + noise
- Line from X=494000–494990 m

**XYZ (PLS_magnetics.xyz):**
- 4 flight lines (1010, 1020, 1030, 1040)
- 50 points per line
- MAG_TMI: 55000–56000 nT
- MAG_RESID: -50 to +50 nT
- ALT: 80–90 m

## Dependencies

```bash
# Install all at once
pip install openpyxl segyio numpy

# Or per fixture:
pip install openpyxl        # Excel only
pip install segyio numpy    # SEG-Y only
                            # XYZ needs nothing
```

## Troubleshooting

**Missing openpyxl?**
```bash
pip install openpyxl
```

**Missing segyio?**
```bash
pip install segyio numpy
```

**Permission denied on copy_to_dagster.sh?**
```bash
chmod +x tests/fixtures/copy_to_dagster.sh
```

**Generator says "ERROR"?**
- Check pip install completed successfully
- Verify Python 3.8+ is in use
- Try Docker version above

## More Information

- `README.md` — Full guide with all methods
- `FIXTURES_MANIFEST.md` — Detailed specifications
- `FIXTURE_GENERATION.md` — Step-by-step instructions
- `CREATED_FILES.txt` — Complete file inventory

## Locations

All generator scripts and documentation are in:
```
/home/Development/GeoRAG Intelligence V.1.0/tests/fixtures/
```

Fixtures are created in subdirectories:
```
tests/fixtures/{excel,seismic,xyz}/
```

Dagster copies to:
```
src/dagster/tests/fixtures/{excel,seismic,xyz}/
```
