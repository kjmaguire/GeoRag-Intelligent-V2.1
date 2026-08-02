# Schema `gold` — Data Dictionary (skeleton)

See [Appendix A §4](../appendix/A-medallion-contract.md#4-gold--materialisation-map) for the
materialisation map. All tables are workspace-scoped (RLS enforced).

## Tables

| Table | Created by | Refresher | Status |
|---|---|---|---|
| `gold.h3_density_mineral` | [phase0/104-section6-h3-density-table.sql](../../../database/raw/phase0/104-section6-h3-density-table.sql) | Dagster `gold_h3_density` | Live |
| `gold.drillhole_intervals_visual` | [2026_05_13_080000](../../../database/migrations/2026_05_13_080000_create_gold_drillhole_intervals_visual.php) (sole DDL source — PK `visual_id`, `depth_from`/`depth_to`, `interval_kind`, JSONB `assay_payload`/`alteration_payload`/`structure_payload`). A conflicting Phase-H3 starter with a different shape (PK `interval_id`, `from_depth_m`, scalar assay columns) was archived 2026-07-03 to [\_archive/phase5-10-drillhole-intervals-visual.sql](../../../database/raw/_archive/phase5-10-drillhole-intervals-visual.sql); it never applied on any cluster. | Dagster `gold_drillhole_intervals_visual` | Live |
| `gold.cross_section_panels` | [2026_05_13_080001](../../../database/migrations/2026_05_13_080001_create_gold_cross_section_panels.php) (sole DDL source — one row per (project_id, section_name) with `collars_projected` JSONB). A conflicting per-interval rewrite was archived 2026-07-02 to [\_archive/phase5-20-cross-section-panels.sql](../../../database/raw/_archive/phase5-20-cross-section-panels.sql); it never applied on any cluster. | Dagster `gold_cross_section_panels` | Live |
| `gold.structure_measurements_visual` | [2026_05_13_080002](../../../database/migrations/2026_05_13_080002_create_gold_structure_measurements_visual.php) (sole DDL source — PK `visual_id`, `structure_type`, `stereonet_x`/`stereonet_y`, `projection`). A conflicting Phase-H4 file with a different gold shape (PK `measurement_id`, `pole_trend_deg`) — which also created a phantom `silver.structure_measurements` (never exists live; real source is `silver.structures`) — was archived 2026-07-03 to [\_archive/phase5-30-structure-measurements-visual.sql](../../../database/raw/_archive/phase5-30-structure-measurements-visual.sql); it never applied on any cluster. | Dagster `gold_structure_measurements_visual` | Live |
| `gold.assay_composites` | [2026_05_20_060700](../../../database/migrations/2026_05_20_060700_create_gold_drillhole_tables.php) §1 | Dagster `silver_to_gold/assay_composites` | Live |
| `gold.significant_intersections` | same migration §2 | Dagster `silver_to_gold/significant_intersections` | Live |
| `gold.drill_summaries` | same migration §3 | Dagster `silver_to_gold/drill_summaries` | Live |
| `gold.zone_statistics` | same migration §4 | Dagster `silver_to_gold/zone_statistics` | Live |
| `gold.qaqc_statistics` | same migration §5 | Dagster `silver_to_gold/qaqc_statistics` | Live |
| `gold.campaign_summaries` | same migration §6 | Dagster `silver_to_gold/campaign_summaries` | Live |
| `gold.element_correlations` | same migration §7 | Dagster `silver_to_gold/element_correlations` | Live |
| `gold.mv_refresh_log` | [2026_05_25_020546](../../../database/migrations/2026_05_25_020546_create_gold_mv_refresh_log.php) | every gold-asset run writes one row | Live |

## Reads

Most gold tables are read by:
- Frontend pages: DrillholeDetail, HoleCompare, Workspace 3D, Targets, ProjectAnalytics.
- Martin function `silver.significant_intersections_by_project` reads `gold.significant_intersections` ⋈ `silver.collars`.

## RLS

All tables `workspace_id NOT NULL` with the canonical strict policy ([Ch 11 §4](../manual/11-tenancy-and-rls.md)).
