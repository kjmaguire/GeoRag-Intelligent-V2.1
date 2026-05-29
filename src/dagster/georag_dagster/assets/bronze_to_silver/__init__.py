"""Bronze → silver transforms for the drillhole stack (2026-05-20).

Each asset in this package:
  1. Reads from a bronze.raw_* table (or a CSV/Excel upload via S3).
  2. Validates + standardises (unit conversion, hole-id resolution,
     element-symbol canonicalisation, etc.).
  3. UPSERTs into the corresponding silver table, keyed on
     bronze_source_id so re-runs are idempotent.
  4. Returns a MaterializeResult with per-row counts so Dagster's UI
     surfaces ingest health.

The 9 assets follow the silver-tier table list from
`georag-drillhole-schema.md`:

  assays.py         → silver.assays_v2
  lithology.py      → silver.lithology
  recovery.py       → silver.recovery
  specific_gravity.py → silver.specific_gravity
  structure.py      → silver.structure
  alteration.py     → silver.alteration
  mineralization.py → silver.mineralization
  geotechnical.py   → silver.geotechnical
  qaqc.py           → silver.qaqc_results
"""
