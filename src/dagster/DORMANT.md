# `src/dagster/` is dormant as of 2026-07-28 (B2)

No container mounts or runs this tree, and nothing in `src/fastapi` imports it.
It is kept on disk because the CSV / XLSX / LAS / SEGY / raster parsers and
their tests represent real work worth recovering when structured ingestion is
rebuilt — but **none of it executes today.**

## What was removed

`dagster-daemon` and `dagster-webserver` were deleted from `docker-compose.yml`
along with the `dagster_home` volume and the `dev-ingest` profile.

The decisive fact: **`minio_upload_sensor` was STOPPED.** It is defined at
`georag_dagster/definitions.py:531` with `minimum_interval_seconds=300` and
declares no `default_status`, while every schedule in the same file declares one
explicitly. Dagster defaults sensors to STOPPED, and a live
`dagster sensor list` on the running stack confirmed it. So the sensor that was
supposed to pick up generic CSV/XLSX uploads had never fired: those uploads
landed an object and a `bronze` manifest row and then stopped dead.

PDF ingestion never came through here. Laravel dispatches it straight to
Hatchet (`UploadController`, category `reports` → `ingest_pdf`).

## What this tree can no longer do

`A1` moved the PDF parser to `src/fastapi/app/services/ingest/pdf_report.py`
and its OTel bootstrap to `src/fastapi/app/observability/`. Three things here
therefore have imports that no longer resolve:

- `georag_dagster/parsers/pdf_report.py` — gone (moved)
- `georag_dagster/parsers/__init__.py` — re-exports the moved PDF symbols
- `georag_dagster/parsers/docx_parser.py` — imports helpers from it
- `georag_dagster/assets/silver_reports.py` — calls `parse_pdf_report`

These were deliberately **not** deleted. `silver_reports` is referenced by
`commit_ingestion_run`, `index_reports`, `reranker_labels`, `sensor_helpers`,
seven asset checks and `definitions.py`, so removing it cascades through most
of the asset graph — and with the Dagster container gone there is no way to
import-check the result. An unverifiable cascade edit across ~7 files is worse
than a clearly-documented dormant tree.

`src/dagster/tests/test_provenance_and_source_row.py` stays here too: it
exercises the CSV and spatial parsers alongside the PDF one, so it did not
travel with the parser's own 11 test files.

## Reviving it

1. Decide where the PDF parser lives. If Dagster needs it again, import it from
   the FastAPI package or vendor a copy — do not re-fork the file.
2. Restore the compose services (see git history for the removed block).
3. **Give `minio_upload_sensor` an explicit `default_status`.** Leaving it
   implicit is what made this path silently dead for months.
4. Beware double-parsing: if the sensor is enabled while Laravel still
   dispatches `reports` to Hatchet, every PDF upload is parsed twice under two
   different `report_id`s.
5. Re-enable the retired upload categories in
   `app/Http/Controllers/Api/V1/UploadController.php` (`RETIRED_CATEGORIES`)
   only once a live consumer exists for them.

## Demo-scope decision (2026-07-29)

Confirmed: the Azure/Cloudflare demo is scoped to the PDF reader core only
(upload → OCR → cited answer). No CSV/assay upload requirement. This tree
stays dormant as-is — no revival work is in scope for the demo.
