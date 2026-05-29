# GeoRAG Test Fixtures

Test data files for format parser validation across deferred data ingestion pipelines.

## Quick Start

To generate binary fixtures (Excel and SEG-Y):

```bash
# Install dependencies
pip install openpyxl segyio numpy

# Generate all fixtures
python generate_all_fixtures_v2.py
```

This creates three files:
1. `excel/PLS_collars.xlsx` — 10 drill collar records
2. `seismic/test_2D_line.sgy` — 2D seismic survey
3. `xyz/PLS_magnetics.xyz` — Airborne magnetic survey (pre-created)

## Fixtures Overview

### Excel: PLS_collars.xlsx

Drill collar collar coordinates for test project XLS-24- (10 holes).

| HoleID | Easting | Northing | Elevation | TotalDepth | Azimuth | Dip | HoleType | DrillDate | Status |
|--------|---------|----------|-----------|------------|---------|-----|----------|-----------|--------|
| XLS-24-01 | 497000 | 6219000 | 420 | 280 | 75 | -82 | Diamond | 2024-01-15 | Completed |
| ... | ... | ... | ... | ... | ... | ... | ... | ... | ... |
| XLS-24-10 | 497900 | 6219900 | 426 | 480 | 85 | -88 | Diamond | 2024-08-15 | Completed |

**Use case:** Testing Excel workbook parsing, collar validation, coordinate CRS detection

**Generated size:** ~6 KB

---

### SEG-Y: test_2D_line.sgy

Synthetic 2D seismic reflection line.

| Parameter | Value |
|-----------|-------|
| Traces | 100 |
| Samples/trace | 500 |
| Sample interval | 2 ms |
| Record length | 1 second |
| Data | Synthetic sine (50 Hz) + noise |

**Use case:** Testing SEG-Y header parsing, trace validation, sample rate handling

**Generated size:** ~500 KB

---

### XYZ: PLS_magnetics.xyz

Geosoft XYZ format airborne magnetic survey (4 flight lines, 200 points).

```
/ GeoRAG test XYZ -- synthetic airborne magnetic survey
/ Source: Patterson Lake South Project
/ CRS: UTM Zone 13N (EPSG:32613)
/ Date: 2024-03-15
/
/ X            Y            LINE    MAG_TMI     MAG_RESID     ALT_RADAR
  494000.0     6219000.0    1010     55330.0      14.3         84.8
  ...
```

**Flight lines:**
- 1010: Y=6219000 m, 50 points
- 1020: Y=6219200 m, 50 points
- 1030: Y=6219400 m, 50 points
- 1040: Y=6219600 m, 50 points

**Use case:** Testing XYZ parsing, multi-line handling, geophysical value validation

**File size:** ~18 KB (text, pre-created)

---

## Directory Structure

```
tests/fixtures/
├── README.md                      # This file
├── FIXTURES_MANIFEST.md           # Detailed manifest
├── FIXTURE_GENERATION.md          # Generation guide
│
├── generate_all_fixtures_v2.py    # Master generator (recommended)
├── copy_to_dagster.sh             # Copy to Dagster fixtures
│
├── inline_generate_*.py           # Individual generators
│
├── excel/
│   ├── generate_test_xlsx.py      # Excel generator
│   └── PLS_collars.xlsx           # Generated (run generator)
│
├── seismic/
│   ├── generate_test_segy.py      # SEG-Y generator
│   └── test_2D_line.sgy           # Generated (run generator)
│
├── xyz/
│   ├── generate_test_xyz.py       # XYZ generator
│   └── PLS_magnetics.xyz          # Text fixture (pre-created)
│
├── well_logs/
│   ├── generate_test_las.py
│   └── PLS-22-08_gamma_resistivity.las
│
├── spatial/
│   ├── pls_property_boundary.geojson
│   └── pls_alteration_anomalies.geojson
│
├── reports/
│   └── PLS-2024-Technical-Report.pdf
│
└── (CSV fixtures)
    ├── sample_collars.csv
    ├── sample_surveys.csv
    ├── sample_lithology.csv
    └── sample_samples.csv
```

## Generation Methods

### Method 1: Master Generator (Recommended)

Generates all three fixtures with dependency checking:

```bash
python generate_all_fixtures_v2.py
```

Output:
```
======================================================================
GeoRAG Test Fixture Generator
======================================================================

✓ Excel fixture: tests/fixtures/excel/PLS_collars.xlsx
  Size: 6,144 bytes (6.00 KB)
✓ SEG-Y fixture: tests/fixtures/seismic/test_2D_line.sgy
  Size: 524,288 bytes (512.00 KB)
  Traces: 100, Samples: 500
✓ XYZ fixture: tests/fixtures/xyz/PLS_magnetics.xyz
  Size: 18,432 bytes (18.00 KB)
  Flight lines: 4, Points: 200

======================================================================
Summary
======================================================================
✓ PASS: Excel fixture
✓ PASS: SEG-Y fixture
✓ PASS: XYZ fixture

All fixtures generated successfully!
```

### Method 2: Individual Generators

Run each fixture independently:

```bash
# Excel
cd tests/fixtures/excel
python generate_test_xlsx.py

# SEG-Y
cd tests/fixtures/seismic
python generate_test_segy.py

# XYZ
cd tests/fixtures/xyz
python generate_test_xyz.py
```

### Method 3: Docker

For reproducible environments:

```bash
# All at once
docker run --rm -v "$(pwd):/work" -w /work/tests/fixtures \
  python:3.13-slim sh -c \
  "pip install -q openpyxl segyio numpy && python generate_all_fixtures_v2.py"

# Or individually:
docker run --rm -v "$(pwd):/work" -w /work/tests/fixtures/excel \
  python:3.13-slim sh -c "pip install -q openpyxl && python generate_test_xlsx.py"

docker run --rm -v "$(pwd):/work" -w /work/tests/fixtures/seismic \
  python:3.13-slim sh -c "pip install -q segyio numpy && python generate_test_segy.py"

docker run --rm -v "$(pwd):/work" -w /work/tests/fixtures/xyz \
  python:3.13-slim python generate_test_xyz.py
```

## Dagster Integration

Copy fixtures to the Dagster test fixtures directory for ingestion pipeline testing:

```bash
# Automatic copy
bash copy_to_dagster.sh

# Manual copy
mkdir -p src/dagster/tests/fixtures/{excel,seismic,xyz}
cp tests/fixtures/excel/PLS_collars.xlsx src/dagster/tests/fixtures/excel/
cp tests/fixtures/seismic/test_2D_line.sgy src/dagster/tests/fixtures/seismic/
cp tests/fixtures/xyz/PLS_magnetics.xyz src/dagster/tests/fixtures/xyz/
```

## Validation

Verify fixtures after generation:

```bash
# File existence and size
ls -lh tests/fixtures/excel/PLS_collars.xlsx
ls -lh tests/fixtures/seismic/test_2D_line.sgy
ls -lh tests/fixtures/xyz/PLS_magnetics.xyz

# Excel structure
python -c "from openpyxl import load_workbook; wb = load_workbook('tests/fixtures/excel/PLS_collars.xlsx'); print(f'Rows: {wb.active.max_row}, Cols: {wb.active.max_column}')"

# SEG-Y structure
python -c "import segyio; f = segyio.open('tests/fixtures/seismic/test_2D_line.sgy'); print(f'Traces: {len(list(f.trace))}, Samples: {f.samples.size}')"

# XYZ line count
wc -l tests/fixtures/xyz/PLS_magnetics.xyz
```

## Data Characteristics

All fixtures use **realistic Athabasca Basin coordinates** (UTM Zone 13N, EPSG:32613):

- **Easting range:** 494000–497900 m
- **Northing range:** 6219000–6219900 m
- **Elevation range:** 80–450 m
- **Geophysical values:** Realistic ranges for magnetic intensity, density, gamma ray

**No proprietary data is used** — all values are synthetic and derived from public coordinates.

## Dependencies

Install all requirements:

```bash
pip install openpyxl segyio numpy
```

Or per fixture:

| Fixture | Package | Install |
|---------|---------|---------|
| Excel | openpyxl | `pip install openpyxl` |
| SEG-Y | segyio | `pip install segyio` |
| SEG-Y | numpy | `pip install numpy` |
| XYZ | — | None (text file) |

## Integration with Dagster

Fixtures are consumed by:

```
src/dagster/georag_dagster/assets/
├── bronze_xlsx.py      # Reads excel/PLS_collars.xlsx
├── bronze_seismic.py   # Reads seismic/test_2D_line.sgy
└── bronze_xyz.py       # Reads xyz/PLS_magnetics.xyz
```

And transformed by:

```
src/dagster/georag_dagster/assets/
├── silver_xlsx.py
└── silver_seismic.py
```

## Further Reading

- **FIXTURES_MANIFEST.md** — Detailed specification of each fixture
- **FIXTURE_GENERATION.md** — Step-by-step generation guide
- **Architecture doc, Section 04b** — Data ingestion pipeline
- **Architecture doc, Section 04e** — Schema definitions
