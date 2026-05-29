# GeoRAG Test Fixtures — Generation Report

Date generated: 2026-04-10
Project: GeoRAG Intelligence V.1.0

## Summary

Three test fixture families have been created for the deferred format parsers:

1. **Excel fixture**: `tests/fixtures/excel/PLS_collars.xlsx`
2. **SEG-Y fixture**: `tests/fixtures/seismic/test_2D_line.sgy`
3. **XYZ fixture**: `tests/fixtures/xyz/PLS_magnetics.xyz`

All fixtures use realistic Athabasca Basin coordinates and are designed to test parser robustness without depending on proprietary field data.

## Fixture Details

### 1. Excel Fixture: PLS_collars.xlsx

**Location:** `tests/fixtures/excel/PLS_collars.xlsx`

**Generator:** `tests/fixtures/excel/generate_test_xlsx.py`

**Status:** Ready to generate ✓

**Schema:**

| Column | Type | Sample Range |
|--------|------|--------------|
| HoleID | String | XLS-24-01 to XLS-24-10 |
| Easting | Float | 497000–497900 m |
| Northing | Float | 6219000–6219900 m |
| Elevation | Float | 420–426 m |
| TotalDepth | Float | 280–480 m |
| Azimuth | Float | 75–95° |
| Dip | Float | -82 to -88° |
| HoleType | String | Diamond |
| DrillDate | Date | 2024-01-15 to 2024-08-15 |
| Status | String | Completed |

**Rows:** 10 drill holes + 1 header = 11 rows

**File Size (estimated):** 5–10 KB

**CRS:** UTM Zone 13N (EPSG:32613), Athabasca Basin

**Use case:** Testing Excel workbook parsing, collar coordinate validation, date parsing

**Dependencies:**
- `openpyxl` (pip install openpyxl)
- Python 3.8+

**Generate with:**
```bash
cd tests/fixtures/excel
python generate_test_xlsx.py
```

---

### 2. SEG-Y Fixture: test_2D_line.sgy

**Location:** `tests/fixtures/seismic/test_2D_line.sgy`

**Generator:** `tests/fixtures/seismic/generate_test_segy.py`

**Status:** Ready to generate ✓

**Specification:**

| Parameter | Value |
|-----------|-------|
| Seismic type | 2D reflection line |
| Number of traces | 100 |
| Samples per trace | 500 |
| Sample interval | 2000 µs (2 ms) |
| Record length | 1000 ms (1 second) |
| Trace data | Synthetic sine wave (50 Hz) + Gaussian noise |
| Amplitude variation | ±2% per trace for realism |

**Textual Header:** "GeoRAG test 2D seismic line -- synthetic data for pipeline validation"

**Trace coordinates:** Simulated line from X=494000 to X=494990 m, Y=6219000 m (UTM Zone 13N)

**File Size (estimated):** 400–600 KB

**Use case:** Testing SEG-Y header parsing, trace data validation, sample rate handling, endianness detection

**Dependencies:**
- `segyio` (pip install segyio)
- `numpy` (pip install numpy)
- Python 3.8+

**Generate with:**
```bash
cd tests/fixtures/seismic
python generate_test_segy.py
```

---

### 3. XYZ Fixture: PLS_magnetics.xyz

**Location:** `tests/fixtures/xyz/PLS_magnetics.xyz`

**Generator:** `tests/fixtures/xyz/generate_test_xyz.py`

**Status:** CREATED (text fixture, no generation required) ✓

**Format:** Geosoft XYZ (ASCII, comment-delimited)

**Structure:**

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

**Flight lines:** 4

| Line ID | Start Y (m) | Points | Description |
|---------|-------------|--------|-------------|
| 1010 | 6219000 | 50 | North-south line baseline |
| 1020 | 6219200 | 50 | 200 m north offset |
| 1030 | 6219400 | 50 | 400 m north offset |
| 1040 | 6219600 | 50 | 600 m north offset |

**Data parameters:**

| Column | Range | Notes |
|--------|-------|-------|
| X (Easting) | 494000–494490 m | 10 m spacing along line |
| Y (Northing) | 6219000–6219625 m | 0.5 m drift N-NE per point |
| LINE | 1010, 1020, 1030, 1040 | Flight line identifier |
| MAG_TMI | 55000–56000 nT | Total magnetic intensity |
| MAG_RESID | -50 to +50 nT | Residual anomaly |
| ALT_RADAR | 80–90 m | Radar altitude |

**Total data points:** 200

**File size:** 18 KB

**Lines:** 206 (6 header + 200 data)

**Use case:** Testing XYZ parsing, coordinate system detection, geophysical value ranges, multi-line flight block handling

**Dependencies:** None (text file, standard library only)

**Generate with:**
```bash
cd tests/fixtures/xyz
python generate_test_xyz.py
```

---

## Generation Scripts

### Master Generator

Run all three fixtures at once:

```bash
python tests/fixtures/generate_all_fixtures_v2.py
```

This script:
- Detects missing dependencies and provides helpful error messages
- Creates fixture directories if needed
- Generates all fixtures with error handling
- Reports file sizes and validation

### Individual Generators

Each fixture has its own generator script that can be run independently:

```bash
# Excel
python tests/fixtures/excel/generate_test_xlsx.py

# SEG-Y
python tests/fixtures/seismic/generate_test_segy.py

# XYZ
python tests/fixtures/xyz/generate_test_xyz.py
```

### Docker-based Generation

For consistency across environments, generate inside Docker containers:

```bash
# Excel
docker run --rm -v "$(pwd):/work" -w /work/tests/fixtures/excel \
  python:3.13-slim sh -c "pip install -q openpyxl && python generate_test_xlsx.py"

# SEG-Y
docker run --rm -v "$(pwd):/work" -w /work/tests/fixtures/seismic \
  python:3.13-slim sh -c "pip install -q segyio numpy && python generate_test_segy.py"

# XYZ (no dependencies)
docker run --rm -v "$(pwd):/work" -w /work/tests/fixtures/xyz \
  python:3.13-slim python generate_test_xyz.py
```

---

## Dagster Integration

Fixtures must also be available to the Dagster ingestion pipeline container. Copy to:

```
src/dagster/tests/fixtures/
├── excel/
│   └── PLS_collars.xlsx
├── seismic/
│   └── test_2D_line.sgy
└── xyz/
    └── PLS_magnetics.xyz
```

**Copy script:**
```bash
bash tests/fixtures/copy_to_dagster.sh
```

Or manually:
```bash
mkdir -p src/dagster/tests/fixtures/{excel,seismic,xyz}
cp tests/fixtures/excel/PLS_collars.xlsx src/dagster/tests/fixtures/excel/
cp tests/fixtures/seismic/test_2D_line.sgy src/dagster/tests/fixtures/seismic/
cp tests/fixtures/xyz/PLS_magnetics.xyz src/dagster/tests/fixtures/xyz/
```

---

## Fixture Validation

After generation, verify integrity with:

```bash
# Check file existence and size
ls -lh tests/fixtures/excel/PLS_collars.xlsx
ls -lh tests/fixtures/seismic/test_2D_line.sgy
ls -lh tests/fixtures/xyz/PLS_magnetics.xyz

# Verify Excel structure (requires openpyxl)
python -c "from openpyxl import load_workbook; wb = load_workbook('tests/fixtures/excel/PLS_collars.xlsx'); print(f'Rows: {wb.active.max_row}, Cols: {wb.active.max_column}')"

# Verify SEG-Y header (requires segyio)
python -c "import segyio; f = segyio.open('tests/fixtures/seismic/test_2D_line.sgy'); print(f'Traces: {len(list(f.trace))}, Samples: {f.samples.size}')"

# Verify XYZ line count
wc -l tests/fixtures/xyz/PLS_magnetics.xyz
```

---

## File Structure

```
tests/fixtures/
├── FIXTURES_MANIFEST.md            # This file
├── FIXTURE_GENERATION.md           # Detailed generation guide
├── generate_all_fixtures_v2.py     # Master generator script
├── copy_to_dagster.sh              # Copy script for Dagster
│
├── excel/
│   ├── generate_test_xlsx.py       # Excel generator
│   └── PLS_collars.xlsx            # Generated fixture (10 holes)
│
├── seismic/
│   ├── generate_test_segy.py       # SEG-Y generator
│   └── test_2D_line.sgy            # Generated fixture (100 traces)
│
├── xyz/
│   ├── generate_test_xyz.py        # XYZ generator
│   └── PLS_magnetics.xyz           # Text fixture (200 points)
│
├── well_logs/
│   ├── generate_test_las.py        # Existing LAS generator
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

---

## Notes

### Idempotency

All generators are **idempotent** — running them multiple times produces identical output files. This is safe for CI/CD pipelines.

### Data Privacy

All fixture data is **synthetic** and derived from public Athabasca Basin coordinates. No proprietary drilling or geophysical data is used.

### Coordinate System

All fixtures use **UTM Zone 13N (EPSG:32613)**, which covers the Patterson Lake South project area in Saskatchewan, Canada.

### Hole ID Collision Avoidance

The Excel fixture uses **XLS-24-** prefix (versus existing PLS-18/19/20/21/22/23/24 data) to prevent collision with real project data in existing CSV fixtures.

### Dependencies

- **Excel only:** openpyxl
- **SEG-Y only:** segyio, numpy
- **XYZ:** standard library only

To install all:
```bash
pip install openpyxl segyio numpy
```

---

## References

- Section 04b: Data ingestion pipeline in architecture doc
- `src/dagster/georag_dagster/assets/bronze_*.py`: Consumer assets
- `src/dagster/georag_dagster/assets/silver_*.py`: Transformation assets
