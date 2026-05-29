# GeoRAG Test Fixture Generation

This directory contains test fixtures for the GeoRAG format parser pipelines. Binary fixtures (Excel, SEG-Y) need to be generated using Python script runners.

## Quick Start

Run all generators with:

```bash
# Navigate to project root
cd /path/to/GeoRAG\ Intelligence\ V.1.0

# Generate all fixtures (requires openpyxl, segyio, numpy)
python tests/fixtures/generate_all_fixtures.py
```

## Individual Fixture Generation

### Excel Fixture: `excel/PLS_collars.xlsx`

10 drill collar records for test project XLS-24-01 through XLS-24-10.

**Generator:** `tests/fixtures/excel/generate_test_xlsx.py`

**Required packages:**
```bash
pip install openpyxl
```

**Run:**
```bash
cd tests/fixtures/excel
python generate_test_xlsx.py
```

**Output:** `PLS_collars.xlsx` (~5-8 KB)

**Schema:**
- HoleID: XLS-24-01 to XLS-24-10
- Easting: 497000-497900 (UTM Zone 13N)
- Northing: 6219000-6219900 (UTM Zone 13N)
- Elevation: 420-426 m
- TotalDepth: 280-480 m
- Azimuth: 75-95°
- Dip: -82 to -88°
- HoleType: Diamond
- DrillDate: January-August 2024
- Status: Completed

### SEG-Y Fixture: `seismic/test_2D_line.sgy`

2D synthetic seismic line with 100 traces and 500 samples per trace.

**Generator:** `tests/fixtures/seismic/generate_test_segy.py`

**Required packages:**
```bash
pip install segyio numpy
```

**Run:**
```bash
cd tests/fixtures/seismic
python generate_test_segy.py
```

**Output:** `test_2D_line.sgy` (~400-600 KB)

**Specification:**
- Type: 2D seismic line
- Traces: 100
- Samples per trace: 500
- Sample interval: 2000 µs (2 ms)
- Record length: 1000 ms (1 second)
- Trace data: synthetic sine wave (50 Hz) + Gaussian noise
- Amplitude variation: ±2% across traces for realism

### XYZ Fixture: `xyz/PLS_magnetics.xyz`

Geosoft XYZ format airborne magnetic survey across 4 flight lines.

**Generator:** `tests/fixtures/xyz/generate_test_xyz.py`

**Required packages:** None (standard library only)

**Run:**
```bash
cd tests/fixtures/xyz
python generate_test_xyz.py
```

**Output:** `PLS_magnetics.xyz` (~15-20 KB)

**Specification:**
- Flight lines: 4 (1010, 1020, 1030, 1040)
- Points per line: 50 (200 total)
- X spacing: 10 m along line
- Y spacing: 0.5 m north-northeast drift
- CRS: UTM Zone 13N (EPSG:32613)
- MAG_TMI: 55000-56000 nT (total magnetic intensity)
- MAG_RESID: -50 to +50 nT (residual anomaly)
- ALT_RADAR: 80-90 m (radar altitude)

## Docker-Based Generation

For consistent environment, generate using Docker:

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

## Fixture Locations

For integration with Dagster ingestion pipeline, fixtures are also copied to:

```
src/dagster/tests/fixtures/
├── excel/
│   └── PLS_collars.xlsx
├── seismic/
│   └── test_2D_line.sgy
└── xyz/
    └── PLS_magnetics.xyz
```

Copy with:

```bash
mkdir -p src/dagster/tests/fixtures/excel \
         src/dagster/tests/fixtures/seismic \
         src/dagster/tests/fixtures/xyz

cp tests/fixtures/excel/PLS_collars.xlsx \
   src/dagster/tests/fixtures/excel/

cp tests/fixtures/seismic/test_2D_line.sgy \
   src/dagster/tests/fixtures/seismic/

cp tests/fixtures/xyz/PLS_magnetics.xyz \
   src/dagster/tests/fixtures/xyz/
```

## Validation

After generation, verify fixture integrity:

```bash
ls -lh tests/fixtures/excel/PLS_collars.xlsx
ls -lh tests/fixtures/seismic/test_2D_line.sgy
ls -lh tests/fixtures/xyz/PLS_magnetics.xyz
```

Expected file sizes (approximate):
- `PLS_collars.xlsx`: 5-10 KB
- `test_2D_line.sgy`: 400-600 KB (SEG-Y includes full precision floats)
- `PLS_magnetics.xyz`: 15-25 KB

## Notes

- All fixtures use realistic Athabasca Basin coordinates (UTM Zone 13N)
- Drill hole IDs use `XLS-24-` prefix to avoid collision with existing PLS-18/19/20/21/22/23/24 data
- XYZ file is generated as a text fixture (see `PLS_magnetics.xyz`)
- Generators are idempotent — running multiple times will recreate identical files
