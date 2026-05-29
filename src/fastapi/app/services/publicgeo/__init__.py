"""PublicGeo adapter services — superseded.

All synthetic-stub adapters (sk_minoccur, sk_drillhole, bc_minfile,
bc_drillhole, nrcan_mines, assessment_survey, bedrock_geology, usgs_mrds)
were retired on 2026-05-25. Public Geoscience data now lands via the
Dagster Bronze→Silver pipeline (`bronze_pg_ca_*` → `silver_pg_ca_*`
assets in `src/dagster/georag_dagster/assets/`).

This package is intentionally empty; left in place so any stale imports
fail loudly rather than silently picking up a stub.
"""
