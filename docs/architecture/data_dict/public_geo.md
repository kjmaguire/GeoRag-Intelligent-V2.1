# Schema `public_geo` — Data Dictionary (skeleton)

See [Ch 03 §9](../manual/03-schemas.md), [Ch 09](../manual/09-martin-and-maplibre.md).

> **Naming note.** Phase 0 locked the canonical name as
> `public_geoscience`, but the rename migration has not been applied —
> the live schema is still `public_geo`. See
> [docker/martin/martin.yaml:5](../../../docker/martin/martin.yaml).

## Canonical Tier-1 tables (created by 2026_04_14_* batch)

| Table | MVT view | Status |
|---|---|---|
| `public_geo.pg_mines` | `v_pg_mines_mvt` | Live |
| `public_geo.pg_mineral_occurrences` | `v_pg_mineral_occurrences_mvt` | Live |
| `public_geo.pg_drillhole_collars` | `v_pg_drillhole_collars_mvt` | Live |
| `public_geo.pg_rock_samples` | `v_pg_rock_samples_mvt` | Live |
| `public_geo.pg_assessment_surveys` | `v_pg_assessment_surveys_mvt` | Live |
| `public_geo.pg_resource_potential` | `v_pg_resource_potential_mvt` | Live |
| `public_geo.pg_mineral_dispositions` | `v_pg_mineral_dispositions_mvt` | Live |
| `public_geo.pg_bedrock_geology` | `v_pg_bedrock_geology_mvt` | Live |
| `public_geo.jurisdictions` | (lookup) — drives etag_hash freshness contract | Live |

## Tier 2/3 (Planned)

Pre-written but commented out in [martin.yaml:391-744](../../../docker/martin/martin.yaml):
`pg_surficial_geology`, `pg_geological_faults`, `pg_geological_dykes`,
`pg_geological_feature_points`, `pg_geological_feature_lines`,
`pg_petroleum_wells`, `pg_petroleum_well_trajectories`,
`pg_petroleum_pools`, `pg_geophysics_control_points`,
`pg_geophysics_survey_coverage`, `pg_geological_domains`,
`pg_regional_compilation_*`, `pg_geoscience_publications`,
`pg_geochronology_samples`, `pg_geochemistry_samples`. Status: Planned.

## Loader

Kestra flow [public_geoscience_pull](../../../kestra/flows/georag/public_geoscience_pull.yaml)
→ FastAPI → Hatchet `public_geoscience_pull` → Dagster
`bronze_public_geoscience` → `silver_public_geoscience`.
