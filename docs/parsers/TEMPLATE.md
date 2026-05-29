# Parser Addition Template

Copy this file and fill in every section before merging a new format parser. Delete instructional comments (lines starting with `>`) in your copy.

---

## Scope gate

Before implementing:

- V1 supported formats: CSV/XLSX, LAS, Shapefile, GeoPackage, GeoTIFF, PDF (via RAGFlow), DOCX
- V1-roadmap deferred (do NOT add without Kyle's approval): KML/KMZ, Geosoft GDB, SEG-Y, acQuire/Maxwell/Leapfrog
- XYZ and SEG-Y have Silver-trapped implementations — retrieval wiring tracked in `ops/backlog/module-4-intake.md`
- If your format is not in the V1 list above, stop and get Kyle's approval before proceeding

---

## Format metadata

| Field | Value |
|---|---|
| Format name | `<e.g. LAS 2.0>` |
| File extensions | `<e.g. .las>` |
| Library | `<e.g. lasio>=0.31>` |
| Parser file | `src/dagster/georag_dagster/parsers/<name>_parser.py` |
| Bronze asset | `src/dagster/georag_dagster/assets/bronze_<name>.py` |
| Silver asset | `src/dagster/georag_dagster/assets/silver_<name>.py` |
| Check file | `src/dagster/georag_dagster/checks/<name>_checks.py` |
| Kyle approval date | `<YYYY-MM-DD>` |

---

## Pydantic IR shape

Every parser must produce a typed intermediate representation before any Silver write. The IR is validated before the Silver INSERT — schema violations are logged and counted, never silently dropped.

> Use a dataclass or Pydantic model. Existing parsers use `@dataclass` (e.g. `CollarParseResult`). Pydantic is preferred for new parsers since it provides built-in validator support.

Example skeleton (Pydantic):

```python
# src/dagster/georag_dagster/parsers/<name>_parser.py

from dataclasses import dataclass, field
from typing import Any

PARSER_NAME = "<format-name>"
PARSER_VERSION = "1.0.0"


@dataclass
class <Format>ParseResult:
    """Intermediate representation for <format> parser output.

    All fields are populated before any Silver write. parse_ratio = parse_ok / parse_total.
    """
    # Required quality counts — every parser must emit these four
    parse_total: int               # rows/features/curves attempted
    parse_ok: int                  # rows that passed validation
    parse_failed: int              # rows rejected (sum of skipped_details)
    parse_ratio: float             # parse_ok / parse_total (0.0–1.0)
    parser_name: str = PARSER_NAME
    parser_version: str = PARSER_VERSION

    # Format-specific payload
    records: list[dict[str, Any]] = field(default_factory=list)

    # Rejection detail — one dict per failed row
    skipped_details: list[dict[str, Any]] = field(default_factory=list)

    # Warnings (non-fatal)
    warnings: list[str] = field(default_factory=list)

    # Provenance — must include at minimum source_file_sha256
    provenance: dict[str, Any] = field(default_factory=dict)
```

For spatial parsers, add `detected_crs: str`, `crs_confidence: float`, `source_crs: str`.

For raster parsers, add `is_cog: bool`, `band_count: int`, `bounds_4326: tuple`.

---

## Failure handling

No silent drops. The rule: if a row cannot be parsed, log it and move on. Count every rejection.

Required pattern:

```python
for i, row in enumerate(raw_rows):
    try:
        record = _parse_row(row)
        parse_ok += 1
        records.append(record)
    except (ValueError, KeyError, TypeError) as exc:
        parse_failed += 1
        skipped_details.append({
            "row": i,
            "code": type(exc).__name__,
            "reason": str(exc),
            "raw": row,
        })
```

At the end of the parser, compute:

```python
parse_ratio = parse_ok / parse_total if parse_total > 0 else 0.0
```

If `parse_total == 0` (empty file): `parse_ratio = 0.0`. The `parse_total_positive` asset check catches this.

The `MaterializeResult` metadata emitted by the Silver asset must include:

```python
metadata={
    "parse_total":    MetadataValue.int(result.parse_total),
    "parse_ok":       MetadataValue.int(result.parse_ok),
    "parse_failed":   MetadataValue.int(result.parse_failed),
    "parse_ratio":    MetadataValue.float(result.parse_ratio),
    "parser_name":    MetadataValue.text(result.parser_name),
    "parser_version": MetadataValue.text(result.parser_version),
}
```

These metadata fields feed Phase C quality baseline measurements and the `schema_conformance_pass_rate` asset check.

---

## Quality metrics

Standard metrics (required for all parsers):

| Metric | Description |
|---|---|
| `parse_total` | Total input rows/features/objects attempted |
| `parse_ok` | Successfully parsed and schema-validated |
| `parse_failed` | Rejected — see `skipped_details` for reasons |
| `parse_ratio` | `parse_ok / parse_total` |

Parser-specific metrics (add to your IR and emit in metadata):

| Format | Additional metrics |
|---|---|
| Spatial (Shapefile, GeoPackage) | `detected_crs`, `crs_confidence`, `empty_geom_skipped`, `feature_count` |
| LAS | `curve_count`, `skipped_curves`, `parse_quality_pct` |
| GeoTIFF | `is_cog`, `band_count`, `crs_confidence`, `pixel_resolution_m` |
| CSV (any) | `unmapped_columns`, `detected_encoding`, `bbox_rejected_rows` |
| Document (PDF/DOCX) | `sections_detected`, `sections_expected`, `parse_quality_pct` |

---

## Corpus addition (required)

Before this parser merges, contribute at least 5 real-file test cases:

```
tests/fixtures/<format>/
    real-file-01.<ext>
    real-file-01.expected.json
    real-file-02.<ext>
    real-file-02.expected.json
    ...
    real-file-05.<ext>
    real-file-05.expected.json
```

See `ops/runbooks/validation-corpora.md` for the expected-output JSON shape and the baseline entry procedure.

The corpus must include at minimum:
- 1 happy-path file (well-formed, expected output)
- 1 file with missing optional fields (graceful degradation)
- 1 file with a malformed row or geometry (rejection counted, not crash)

Add a baseline entry to `ops/baselines/<date>-ingestion-baselines.md` documenting parse quality on your corpus.

---

## Asset-check requirements

Every new Silver-writing asset needs at minimum one blocking `AssetCheckSpec`. Implement in `src/dagster/georag_dagster/checks/<name>_checks.py`.

Required check (always applicable):

```python
@asset_check(
    asset="silver_<name>",
    name="parse_total_positive",
    description="Blocks commit if the Silver table contains zero rows.",
    blocking=True,
)
def silver_<name>_check_parse_total_positive(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM silver.<table_name>;")
            count = cur.fetchone()[0]
    passed = count > 0
    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": MetadataValue.int(count)},
    )
```

Format-specific checks to add when applicable:

| Check name | When to add |
|---|---|
| `crs_round_trip_sane` | Any spatial format — zero NULL geom + zero SRID=0 |
| `crs_srid_populated` | Any spatial format — no SRID=0 rows in geometry column |
| `schema_conformance_pass_rate` | All document and structured formats — `parse_ok > 0` |
| `text_hash_sha256_valid` | Document passage formats — regex on text_hash |

Export all check functions from `src/dagster/georag_dagster/checks/__init__.py`.

---

## Wiring into the commit gate

New Silver assets that emit data for the private-project pipeline must be upstream dependencies of `commit_ingestion_run`.

In `src/dagster/georag_dagster/assets/commit_ingestion_run.py`, add to the `deps=[]` list:

```python
@asset(
    group_name="commit",
    deps=[
        silver_collars,
        silver_reports,
        silver_spatial,
        index_reports,
        index_neo4j,
        silver_drill_traces,
        silver_cog_rasters,
        silver_<new_format>,   # add here
    ],
    ...
)
def commit_ingestion_run(...):
    ...
```

Also import the new asset at the top of `commit_ingestion_run.py`:

```python
from georag_dagster.assets.silver_<name> import silver_<name>
```

If the new format also needs post-ingest spatial tuning (GIST index + CLUSTER), add a `_TUNE_TARGETS` entry:

```python
_TUNE_TARGETS = [
    ...
    {
        "table": "silver.<table_name>",
        "index": "idx_<table_name>_geom",
        "matview": None,
    },
]
```

Finally, import and register the new asset and its checks in `src/dagster/georag_dagster/definitions.py`.

---

## Checklist before merging

- [ ] Parser file created at `src/dagster/georag_dagster/parsers/<name>_parser.py`
- [ ] Bronze asset created at `src/dagster/georag_dagster/assets/bronze_<name>.py`
- [ ] Silver asset created at `src/dagster/georag_dagster/assets/silver_<name>.py`
- [ ] IR emits `parse_total`, `parse_ok`, `parse_failed`, `parse_ratio`, `parser_name`, `parser_version`
- [ ] No silent drops — all rejected rows in `skipped_details` with reason
- [ ] At least 1 blocking `AssetCheckSpec` in `checks/<name>_checks.py`
- [ ] Checks exported from `checks/__init__.py`
- [ ] Asset and checks imported and registered in `definitions.py`
- [ ] `commit_ingestion_run` deps updated (if private-project pipeline)
- [ ] `_TUNE_TARGETS` updated (if spatial)
- [ ] 5+ corpus files in `tests/fixtures/<format>/` with expected-output fixtures
- [ ] Baseline entry in `ops/baselines/`
- [ ] Kyle's scope approval documented in the format metadata table above

---

_Template written 2026-04-20 during Module 3 Phase D. Update this file whenever parser patterns or scope rules change._
