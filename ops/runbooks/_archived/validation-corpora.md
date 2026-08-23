# Validation Corpora Runbook

Documents the parse-quality test corpus structure and procedures. Use this when adding a new test file, running the parser regression suite, or understanding what a quality baseline claim requires.

---

## Corpus invariant (Global Invariant 3)

No quality claim anywhere in the codebase, tests, or docs may assert "X% accurate" or "Y% parse quality" without referencing:

1. A named corpus version (date-stamped file list)
2. A parser-version stamp at the time of measurement

Baselines in `ops/baselines/` always carry both. Do not add quality percentages to parser docstrings without a baseline entry.

---

## Current corpus state

**Root fixture directory:** `tests/fixtures/`

Corpus files live under format-specific subdirectories. No `tests/corpora/` directory exists — the fixtures directory is the corpus location.

| Format | Fixture path | Input files | Expected-output fixture | Coverage verdict |
|---|---|---|---|---|
| CSV collar | `tests/fixtures/` | `sample_collars.csv` | Hardcoded in test constants (`test_csv_collar_parser.py`) | PARTIAL — 1 file, no malformed variant |
| CSV survey | `tests/fixtures/` | `sample_surveys.csv` | Hardcoded in test constants | PARTIAL |
| CSV lithology | `tests/fixtures/` | `sample_lithology.csv` | Hardcoded in test constants | PARTIAL |
| CSV sample | `tests/fixtures/` | `sample_samples.csv` | Hardcoded in test constants | PARTIAL |
| LAS 2.0 | `tests/fixtures/well_logs/` | `PLS-22-08_gamma_resistivity.las` | MISSING | MISSING expected-output fixture |
| Shapefile | `tests/fixtures/spatial/` | MISSING | MISSING | MISSING entirely — primary V1 vector format |
| GeoJSON | `tests/fixtures/spatial/` | `pls_alteration_anomalies.geojson`, `pls_property_boundary.geojson` | MISSING | MISSING expected-output fixture |
| GeoPackage | `tests/fixtures/spatial/` | `test_multilayer.gpkg` (not present in directory listing — Phase A noted it existed) | MISSING | MISSING expected-output fixture |
| GeoTIFF | `tests/fixtures/spatial/` | `test_small.tif`, `test_no_crs.asc` (Phase A confirmed) | MISSING | MISSING COG variant; no expected-output |
| PDF (NI 43-101) | `tests/fixtures/reports/` | `PLS-2024-Technical-Report.pdf` | MISSING | MISSING expected-output fixture |
| DOCX | `tests/fixtures/` | MISSING | MISSING | MISSING entirely |
| XLSX | `tests/fixtures/excel/` | `PLS_collars.xlsx`, `PLS_collars_legacy.xls` (Phase A audit) | MISSING | MISSING expected-output fixture |
| SEG-Y | `tests/fixtures/seismic/` | `test_2D_line.sgy` | MISSING | MISSING expected-output fixture |
| XYZ | `tests/fixtures/xyz/` | `PLS_magnetics.xyz` | MISSING | MISSING expected-output fixture |

**Phase C blocker:** Every V1 format is missing expected-output fixture JSON files. The fixture inputs exist for most formats but the golden outputs do not. Phase C corpus assembly is required before any quality percentage claim can be made.

---

## How to add a new test file

1. Drop the input file into the correct format directory:
   ```
   tests/fixtures/<format>/my-new-file.<ext>
   ```
   Examples: `tests/fixtures/well_logs/`, `tests/fixtures/spatial/`, `tests/fixtures/excel/`

2. Run the parser once against the new file to generate a baseline output. Example for CSV collar:
   ```bash
   docker exec georag-dagster-daemon python - <<'EOF'
   import json
   import hashlib
   from georag_dagster.parsers.csv_collar import parse_collars
   
   result = parse_collars("tests/fixtures/sample_collars_v2.csv")
   fixture = {
       "parse_total": result.total_rows,
       "parse_ok":    result.valid_rows,
       "parse_failed": result.skipped_rows,
       "detected_crs": "EPSG:32613",
       "parser_name":  result.provenance["parser_name"],
       "parser_version": result.provenance["parser_version"],
       "corpus_version": "2026-04-20"
   }
   print(json.dumps(fixture, indent=2))
   EOF
   ```

3. Save the JSON output as the expected-output fixture alongside the input:
   ```
   tests/fixtures/<format>/my-new-file.expected.json
   ```

4. Check in both the input file and the fixture JSON together in the same commit.

5. Add a baseline entry to `ops/baselines/` (see below).

---

## Expected-output JSON shape

Each fixture JSON must contain at minimum:

```json
{
  "parse_total":     10,
  "parse_ok":        10,
  "parse_failed":    0,
  "parse_ratio":     1.0,
  "parser_name":     "csv-collar",
  "parser_version":  "1.0.0",
  "corpus_version":  "2026-04-20"
}
```

For spatial parsers, add:

```json
{
  "detected_crs":    "EPSG:32613",
  "crs_confidence":  0.95,
  "feature_count":   42,
  "empty_geom_skipped": 0
}
```

For LAS parsers, add:

```json
{
  "curve_count":     4,
  "skipped_curves":  0,
  "parse_quality_pct": 100.0
}
```

For raster parsers, add:

```json
{
  "is_cog":          true,
  "band_count":      1,
  "crs_detected":    "EPSG:32613"
}
```

The `row_hash` field (SHA-256 of all output rows concatenated) is optional but recommended for CSV formats — it detects coordinate drift from CRS transform bugs:

```json
{
  "row_hash": "a3f5c1e2d4b6a8c0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8"
}
```

---

## Where measured baselines live

Directory: `ops/baselines/`

Naming convention: `YYYY-MM-DD-ingestion-baselines.md`

Existing baseline file: `ops/baselines/2026-04-20-datastores-baselines.md` (infrastructure, not parser quality — parser quality baselines do not exist yet).

When Phase C populates parser quality baselines, create: `ops/baselines/YYYY-MM-DD-ingestion-baselines.md` with a table of format → corpus file list → parser version → measured quality.

Example baseline entry structure:

```markdown
## CSV Collar — 2026-04-20

| Field | Value |
|---|---|
| Corpus files | tests/fixtures/sample_collars.csv (1 file) |
| Parser name | csv-collar |
| Parser version | 1.0.0 |
| parse_total | 10 |
| parse_ok | 10 |
| parse_ratio | 100% |
| Measured | 2026-04-20 |
```

---

## Running the suite

No dedicated corpus-driven pytest runner exists yet. This is a Phase C deliverable. The expected runner when Phase C lands:

```bash
# From the project root (in the Dagster container):
docker exec georag-dagster-daemon \
  python -m pytest src/dagster/tests/ -v --tb=short
```

For corpus-driven fixture tests specifically (once Phase C scaffolds them):

```bash
docker exec georag-dagster-daemon \
  python -m pytest src/dagster/tests/ -v -k "corpus" --tb=short
```

Current parser unit tests exist but do not load expected-output fixture JSON files — they use hardcoded constants. The corpus runner should:

1. Walk `tests/fixtures/`
2. For each `*.expected.json`, find the paired input file
3. Run the matching parser against the input
4. Assert the output matches the fixture JSON fields within tolerance (±0 for counts, ±0.001 for float ratios)

Until Phase C delivers this runner, parser regressions are caught by the existing unit tests only.

---

## Corpus file requirements for new format addition

Per the parser addition template (`docs/parsers/TEMPLATE.md`), every new parser must ship with:

- At minimum 5 real-file test cases in `tests/fixtures/<new-format>/`
- One expected-output fixture JSON per input file
- A baseline entry in `ops/baselines/` dated the day the corpus was assembled

Do not add a new parser without meeting this requirement. A parser with no corpus cannot make quality claims and cannot be regression-tested.

---

_Written 2026-04-20 during Module 3 Phase D. Update this file whenever the underlying procedure changes._
